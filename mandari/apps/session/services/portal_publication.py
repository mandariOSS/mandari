# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Veröffentlichung im Bürgerportal beenden (Issue #618).

Bisher schaltete „Veröffentlichung beenden“ nur den Abgleich ab: Der gespiegelte Bestand blieb
öffentlich und veraltete ohne Hinweis. Jetzt wählt die Verwaltung, was mit ihm geschieht:

- **Vorübergehend abschalten** (z. B. Wartung): Seiten zeigen einen Hinweis statt Inhalt (503),
  der Bestand bleibt erhalten, Wiedereinschalten stellt alles her.
- **Als Archiv behalten** (z. B. Wechsel des Systems): Der Bestand bleibt lesbar, jede Seite trägt
  „nicht mehr aktuell“; aktualisiert wird nichts mehr.
- **Dauerhaft zurücknehmen**: Seiten antworten mit „nicht mehr verfügbar“ (410), Einträge verschwinden
  aus Suche und Sitemaps, OParl meldet sie als gelöscht.

Alle drei lassen sich wechseln und mit „Wieder veröffentlichen“ aufheben. Die Wirkung setzt der
Signal-Hook über ``insight_service.sync_publication_state`` um; hier liegen Hilfetexte, die
Zusammenfassung der Folgen für die Bestätigung und das Audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.session.models import SessionTenant


@dataclass(frozen=True)
class EndOption:
    """Eine Möglichkeit, die Veröffentlichung zu beenden (Hilfetext und Folgen)."""

    key: str
    title: str
    when: str
    effects: tuple[str, ...]
    undo: str
    icon: str


OPTIONS: tuple[EndOption, ...] = (
    EndOption(
        key=SessionTenant.PORTAL_END_PAUSED,
        title="Vorübergehend abschalten",
        when="Etwa während einer Wartung oder einer Prüfung der veröffentlichten Daten.",
        effects=(
            "Alle Seiten Ihrer Kommune im Bürgerportal zeigen statt der Inhalte den Hinweis "
            "„vorübergehend nicht verfügbar“.",
            "Suche und Merkliste blenden Ihre Einträge aus; Sitemap und OParl-Schnittstelle melden "
            "„vorübergehend nicht verfügbar“ – Suchmaschinen behalten die Seiten.",
            "Der Abgleich ruht, gelöscht wird nichts.",
        ),
        undo="„Wieder veröffentlichen“ stellt alles her; Änderungen aus der Zwischenzeit folgen mit dem nächsten Abgleich.",
        icon="circle-pause",
    ),
    EndOption(
        key=SessionTenant.PORTAL_END_ARCHIVED,
        title="Als Archiv behalten",
        when="Etwa beim Wechsel zu einem anderen System, wenn die bisherigen Informationen auffindbar bleiben sollen.",
        effects=(
            "Alle bisher veröffentlichten Inhalte bleiben lesbar – in Listen, Suche, Sitemap und OParl.",
            "Jede Seite Ihrer Kommune trägt deutlich den Hinweis „Archiv – nicht mehr aktuell“.",
            "Es gibt keine Aktualisierung mehr; Abonnements zum Umsetzungsstand erhalten keine E-Mails mehr.",
        ),
        undo="„Wieder veröffentlichen“ nimmt den Abgleich wieder auf und entfernt den Hinweis.",
        icon="archive",
    ),
    EndOption(
        key=SessionTenant.PORTAL_END_WITHDRAWN,
        title="Dauerhaft zurücknehmen",
        when="Wenn Ihre Ratsinformationen nicht mehr über das Bürgerportal abrufbar sein sollen.",
        effects=(
            "Alle Seiten Ihrer Kommune antworten mit „nicht mehr verfügbar“ (HTTP 410).",
            "Die Einträge verschwinden aus Kommunenauswahl, Suche und Sitemaps; die OParl-Schnittstelle "
            "des Bürgerportals meldet sie als gelöscht.",
            "Die Daten in mandari Session bleiben unberührt, ebenso Ihre eigene OParl-Schnittstelle.",
        ),
        undo="„Wieder veröffentlichen“ holt den Bestand zurück, soweit er in Session weiterhin öffentlich ist. "
        "Wer die Einträge bereits als gelöscht übernommen hat, erhält sie mit dem nächsten Abgleich neu.",
        icon="eye-off",
    ),
)
OPTION_BY_KEY = {option.key: option for option in OPTIONS}


def option(key: str | None) -> EndOption | None:
    return OPTION_BY_KEY.get(key or "")


def state_label(tenant: Any) -> str:
    """Stand in Worten (Audit, Oberfläche)."""
    if tenant.insight_publish:
        return "veröffentlicht"
    chosen = option(tenant.insight_end_mode)
    return chosen.title if chosen else "nicht veröffentlicht"


def inventory(tenant: Any) -> dict[str, int]:
    """Was derzeit aus diesem Mandanten im Bürgerportal steht (für die Zusammenfassung der Folgen)."""
    from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson

    from .insight_service import session_sources

    body_ids = list(OParlBody.objects.filter(source__in=session_sources(tenant)).values_list("id", flat=True))
    counts = {
        "sitzungen": OParlMeeting.objects.filter(body__in=body_ids, deleted=False).count(),
        "vorlagen": OParlPaper.objects.filter(body__in=body_ids, deleted=False).count(),
        "dokumente": OParlFile.objects.filter(deleted=False, paper__body__in=body_ids).count()
        + OParlFile.objects.filter(deleted=False, paper__isnull=True, meeting__body__in=body_ids).count(),
        "gremien": OParlOrganization.objects.filter(body__in=body_ids, deleted=False).count(),
        "personen": OParlPerson.objects.filter(body__in=body_ids, deleted=False).count(),
    }
    counts["gesamt"] = sum(counts.values())
    return counts


def end_publication(tenant: Any, mode: str, *, user: Any = None, request: Any = None) -> Any:
    """
    Veröffentlichung mit der gewählten Möglichkeit beenden bzw. zwischen den Möglichkeiten wechseln.

    Gibt die Wirkung im Bürgerportal zurück (``PortalChange``) oder ``None``, wenn schon so eingestellt.
    """
    from apps.session import audit

    from .insight_service import PortalChange

    if option(mode) is None:
        raise ValueError(f"Unbekannte Möglichkeit: {mode}")
    if not tenant.insight_publish and tenant.insight_end_mode == mode:
        return None
    vorher, war = state_label(tenant), tenant.insight_publish
    tenant._portal_change = None
    tenant.insight_publish = False
    tenant.insight_end_mode = mode
    tenant.save(update_fields=["insight_publish", "insight_end_mode", "updated_at"])
    change = getattr(tenant, "_portal_change", None) or PortalChange()
    audit.log_event(
        "unpublish",
        tenant,
        tenant=tenant,
        user=user,
        request=request,
        changes={
            "buergerportal": {"alt": vorher, "neu": state_label(tenant)},
            "insight_publish": {"alt": war, "neu": False},
            "wirkung": change.as_dict(),
        },
        object_repr="Veröffentlichung im Bürgerportal",
    )
    return change


def resume_publication(tenant: Any, *, user: Any = None, request: Any = None) -> Any:
    """Wieder (bzw. erstmals) veröffentlichen; hebt eine gewählte Möglichkeit auf. ``None``: lief schon."""
    from apps.session import audit

    from .insight_service import PortalChange

    if tenant.insight_publish:
        return None
    vorher = state_label(tenant)
    tenant._portal_change = None
    tenant.insight_publish = True
    tenant.insight_end_mode = ""
    tenant.save(update_fields=["insight_publish", "insight_end_mode", "updated_at"])
    change = getattr(tenant, "_portal_change", None) or PortalChange()
    audit.log_event(
        "publish",
        tenant,
        tenant=tenant,
        user=user,
        request=request,
        changes={
            "buergerportal": {"alt": vorher, "neu": state_label(tenant)},
            "insight_publish": {"alt": False, "neu": True},
            "wirkung": change.as_dict(),
        },
        object_repr="Veröffentlichung im Bürgerportal",
    )
    return change
