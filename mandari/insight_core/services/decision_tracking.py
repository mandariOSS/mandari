# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Issue #48).

Die Verwaltung pflegt im Session-RIS den Umsetzungsstand ihrer Beschlüsse
(Issue #37). Schaltet sie „Umsetzungsstand im Bürgerportal veröffentlichen“
ein, erscheinen angenommene, öffentliche Beschlüsse mit Status-Zeitleiste und
öffentlicher Statusmeldung in Insight. Bürger:innen können einzelne Beschlüsse
abonnieren und erhalten eine E-Mail, wenn sich der Stand ändert.

Es werden ausschließlich Daten gezeigt, die die Verwaltung dafür freigegeben
hat: öffentliche Sitzung, öffentlicher TOP, Beschluss angenommen, Schalter am
Mandanten und am Beschluss aktiv. Interne Erledigungsvermerke bleiben intern —
nach außen geht nur die separate öffentliche Statusmeldung.

Mit ``RIS_DECISION_TRACKING=bestand`` (Issue #538, ``aus_bestand``) lesen Liste, Zahlen und Detailseite die
Beschlüsse über die Lese-Fassade aus dem RIS-Bestand (``hub.ris.selectors``): Dort steht der Umsetzungsstand nur,
wenn ihn der Herausgeber nach dieser Regel veröffentlicht hat (Erweiterung ``mandari:implementation``). Den
Veröffentlichungsstand der Kommune (Archiv, Pause, Rücknahme) bestimmt dann ``publication``. Abos und ihre Mails
bleiben an den Session-Beschluss gebunden (``session_beschluss``), bis die Benachrichtigung aus Ereignissen kommt.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.common import mail
from apps.common.email import render_email
from hub.ris import selectors as ris

from ..models import OParlAgendaItem

logger = logging.getLogger(__name__)

PUBLIC_STATUS_LABELS = {
    "open": "Beschlossen, Umsetzung steht an",
    "in_progress": "In Umsetzung",
    "done": "Umgesetzt",
    "deferred": "Zurückgestellt",
}

#: Beschlussliste: neueste Sitzung zuerst, TOPs einer Sitzung in Tagesordnungsreihenfolge. Sitzung,
#: TOP-Nummer und Primärschlüssel als eindeutiger Nachrang: Gleichzeitige Sitzungen und TOPs ohne
#: gepflegte Reihenfolge lägen sonst in beliebiger Folge vor, auch über Seitengrenzen hinweg (Issue #653).
PUBLIC_DECISIONS_ORDERING = ("-meeting__start", "meeting_id", "order", "number", "id")


#: Sichtbarkeit eines Beschlusses bzw. der Beschlussseiten einer Kommune (Issue #618)
VISIBLE = "visible"  # veröffentlicht: lesbar, Abos und Benachrichtigungen
ARCHIVED = "archived"  # Archiv: lesbar mit Hinweis, keine Abos und Benachrichtigungen mehr
PAUSED = "paused"  # vorübergehend abgeschaltet: Hinweis statt Inhalt (503)
WITHDRAWN = "withdrawn"  # dauerhaft zurückgenommen: nicht mehr verfügbar (410)
HIDDEN = "hidden"  # nie oder nicht (mehr) freigegeben: 404


def _tenant_visibility(tenant: Any) -> str:
    """Beschlussseiten eines Mandanten folgen der Veröffentlichung im Bürgerportal (Issue #618)."""
    if not (tenant.is_active and tenant.implementation_publish):
        return HIDDEN
    if tenant.insight_publish:
        return VISIBLE
    return {
        tenant.PORTAL_END_ARCHIVED: ARCHIVED,
        tenant.PORTAL_END_PAUSED: PAUSED,
        tenant.PORTAL_END_WITHDRAWN: WITHDRAWN,
    }.get(tenant.insight_end_mode or "", HIDDEN)


def tenant_publishes_implementation(tenant: Any) -> bool:
    """
    Anteil des Mandanten an ``is_publicly_visible``: Kommune verknüpft, Mandant aktiv, Umsetzungsstand von der
    Verwaltung freigegeben und im Bürgerportal aktiv veröffentlicht. Ändert sich das, ändert sich die Ausgabe des
    Umsetzungsstands aller freigegebenen Beschlüsse des Mandanten (Issue #525).
    """
    return bool(tenant.oparl_body_id) and _tenant_visibility(tenant) == VISIBLE


def publishing_tenants(body: Any) -> Any:
    """Mandanten, deren Beschlüsse lesbar sind: veröffentlichend oder als Archiv."""
    from django.db.models import Q

    from apps.session.models import SessionTenant

    return SessionTenant.objects.filter(oparl_body=body, is_active=True, implementation_publish=True).filter(
        Q(insight_publish=True) | Q(insight_end_mode=SessionTenant.PORTAL_END_ARCHIVED)
    )


def body_visibility(body: Any) -> str:
    """Beschlussseiten einer Kommune: lesbar, Archiv, abgeschaltet, zurückgenommen oder nichts."""
    from apps.session.models import SessionTenant

    found = {
        _tenant_visibility(tenant)
        for tenant in SessionTenant.objects.filter(oparl_body=body, is_active=True, implementation_publish=True)
    }
    for visibility in (VISIBLE, ARCHIVED, PAUSED, WITHDRAWN):
        if visibility in found:
            return visibility
    return HIDDEN


def public_decisions(body: Any) -> Any:
    """Öffentlich sichtbare Beschlüsse einer Kommune (nur mit Freigabe der Verwaltung)."""
    from apps.session.models import SessionAgendaItem

    return (
        SessionAgendaItem.objects.filter(
            meeting__tenant__in=publishing_tenants(body),
            vote_result="approved",
            is_public=True,
            meeting__is_public=True,
            implementation_public=True,
        )
        .exclude(is_withdrawn=True)
        .select_related("meeting__organization", "meeting__tenant", "paper")
        .order_by(*PUBLIC_DECISIONS_ORDERING)
    )


def aus_bestand() -> bool:
    """Lesen die Beschlussseiten den RIS-Bestand (``RIS_DECISION_TRACKING=bestand``, Issue #538)?"""
    return getattr(settings, "RIS_DECISION_TRACKING", "session") == "bestand"


def has_public_decisions(body: Any) -> bool:
    """Gibt es in der Kommune lesbare Beschlüsse? Nur dann steht „Beschlüsse“ in der Navigation (Issue #1261)."""
    if aus_bestand():
        return ris.decided_agenda_items([body], published_only=True).exists()
    return public_decisions(body).exists()


#: Adresse eines Tagesordnungspunkts in der OParl-Schnittstelle von mandari Session (``hub.ris.mapping.session``)
_SESSION_PUNKT = re.compile(r"/session/(?P<slug>[^/]+)/api/oparl/agendaitem/(?P<id>[0-9a-fA-F-]{36})/?$")


def session_beschluss(item: OParlAgendaItem) -> Any:
    """
    Session-Beschluss zu einem Tagesordnungspunkt des RIS-Bestands (Abos, Issue #538): Der Spiegel eines Session-
    Mandanten nennt Mandant und Kennung in der Adresse. ``None`` bei fremden Quellen und unbekannten Kennungen.
    """
    from apps.session.models import SessionAgendaItem

    treffer = _SESSION_PUNKT.search(item.external_id or "")
    if treffer is None:
        return None
    return (
        SessionAgendaItem.objects.select_related("meeting__organization", "meeting__tenant__oparl_body", "paper")
        .filter(
            pk=treffer["id"],
            meeting__tenant__slug=treffer["slug"],
            # Nur der Mandant, der diese Kommune veröffentlicht (nie ein gleichnamiger einer anderen Installation)
            meeting__tenant__oparl_body_id=item.meeting.body_id,
        )
        .first()
    )


def visibility(item: Any) -> str:
    """Sichtbarkeit eines Beschlusses (``VISIBLE``, ``ARCHIVED``, ``PAUSED``, ``WITHDRAWN``, ``HIDDEN``)."""
    tenant = item.meeting.tenant
    if not (
        tenant.oparl_body_id
        and item.vote_result == "approved"
        and item.is_public
        and item.meeting.is_public
        and item.implementation_public
        and not item.is_withdrawn
    ):
        return HIDDEN
    return _tenant_visibility(tenant)


def is_readable(item: Any) -> bool:
    """Seite lesbar (veröffentlicht oder Archiv)."""
    return visibility(item) in (VISIBLE, ARCHIVED)


def is_publicly_visible(item: Any) -> bool:
    """Aktiv veröffentlicht: Abos und Benachrichtigungen (im Archiv nicht mehr)."""
    return visibility(item) == VISIBLE


def stats(body) -> dict:
    if aus_bestand():
        return ris.implementation_stats(ris.decided_agenda_items([body], published_only=True), timezone.localdate())
    qs = public_decisions(body)
    counts = {status: qs.filter(implementation_status=status).count() for status, _ in PUBLIC_STATUS_LABELS.items()}
    counts["total"] = qs.count()
    counts["overdue"] = (
        qs.filter(implementation_deadline__lt=timezone.localdate()).exclude(implementation_status="done").count()
    )
    return counts


def timeline(item) -> list[dict]:
    """Status-Zeitleiste: beschlossen → in Umsetzung → umgesetzt (bzw. zurückgestellt)."""
    status = item.implementation_status
    decided_at = item.meeting.start
    # Aus dem RIS-Bestand (Issue #538) heißt der Zeitpunkt wie in der Erweiterung ``mandari:implementation``
    updated = item.implementation_modified if isinstance(item, OParlAgendaItem) else item.implementation_updated_at
    steps = [
        {"key": "decided", "label": "Beschlossen", "date": decided_at, "reached": True, "current": status == "open"},
        {
            "key": "in_progress",
            "label": "In Umsetzung",
            "date": updated if status in ("in_progress", "done") else None,
            "reached": status in ("in_progress", "done"),
            "current": status == "in_progress",
        },
        {
            "key": "done",
            "label": "Umgesetzt",
            "date": updated if status == "done" else None,
            "reached": status == "done",
            "current": status == "done",
        },
    ]
    if status == "deferred":
        steps.append({"key": "deferred", "label": "Zurückgestellt", "date": updated, "reached": True, "current": True})
    return steps


def public_url(item) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    return f"{site_url}/insight/beschluesse/{item.id}/"


def oeffentlicher_name(item) -> str:
    """
    Name eines Beschlusses für Bürgerportal und Abo-Mails: ohne Hausnummer, wenn der Punkt oder seine Vorlage eine
    Eingabe aus der Einwohnerschaft oder einen Bauantrag nennt (``ortsschutz``, Issue #1269). Work zeigt den vollen Namen.
    """
    from .ortsschutz import grund_nach_art, oeffentlicher_titel

    if isinstance(item, OParlAgendaItem):
        # Aus dem RIS-Bestand: Vorlagen des Punkts hat der Aufrufer als ``_ortsschutz`` ausgewertet (Issue #538)
        return item.titel_oeffentlich
    paper = item.paper if getattr(item, "paper_id", None) else None
    schutz = grund_nach_art(None, item.name) or (
        grund_nach_art(paper.get_paper_type_display(), paper.name) if paper is not None else ""
    )
    return oeffentlicher_titel(item.name, None, schutz)


# =============================================================================
# Abos
# =============================================================================


def _from_email() -> str | None:
    """Eigener Absender der Abo-Mails; ohne Angabe der Absender der Plattform."""
    return getattr(settings, "INSIGHT_DIGEST_FROM_EMAIL", "") or None


def _send(subject: str, template: str, context: dict, recipient: str) -> bool:
    try:
        html_message, text_message = render_email(template, context)
        return mail.send(
            kind="insight.beschluss",
            subject=subject,
            body=text_message,
            html_body=html_message,
            from_email=_from_email(),
            to=[recipient],
        )
    except Exception as exc:  # Mailfehler dürfen die Seite nicht brechen
        logger.warning("Beschluss-Abo-Mail an %s fehlgeschlagen: %s", recipient, exc)
        return False


def send_confirmation(subscription) -> bool:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    confirm_url = f"{site_url}/insight/beschluesse/abo/bestaetigen/{subscription.token}/"
    item = subscription.agenda_item
    return _send(
        subject="Bitte bestätigen: Benachrichtigung zum Beschluss",
        template="emails/decisions/confirm.html",
        context={
            "item": item,
            "titel": oeffentlicher_name(item),
            "confirm_url": confirm_url,
            "site_url": site_url,
            "decision_url": public_url(item),
        },
        recipient=subscription.email,
    )


def notify_status_change(item, old_status: str | None, old_public_note: str | None) -> int:
    """Alle bestätigten Abonnent:innen über einen neuen Umsetzungsstand informieren."""
    from ..models import DecisionSubscription

    if not is_publicly_visible(item):
        return 0
    status_changed = old_status is not None and old_status != item.implementation_status
    note_changed = (
        old_public_note is not None
        and (old_public_note or "").strip() != (item.implementation_public_note or "").strip()
    )
    if not (status_changed or note_changed):
        return 0

    subscriptions = DecisionSubscription.objects.filter(agenda_item=item, confirmed=True, unsubscribed_at__isnull=True)
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    label = PUBLIC_STATUS_LABELS.get(item.implementation_status, item.get_implementation_status_display())
    titel = oeffentlicher_name(item)
    sent = 0
    for subscription in subscriptions:
        ok = _send(
            subject=f"Neuer Stand: {label} — {titel}"[:150],
            template="emails/decisions/status.html",
            context={
                "item": item,
                "titel": titel,
                "status_label": label,
                "status_changed": status_changed,
                "decision_url": public_url(item),
                "unsubscribe_url": f"{site_url}/insight/beschluesse/abo/abmelden/{subscription.token}/",
                "site_url": site_url,
            },
            recipient=subscription.email,
        )
        sent += 1 if ok else 0
    return sent
