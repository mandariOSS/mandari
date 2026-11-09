# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnahme neuer Mitglieder an kommenden Fraktionssitzungen (Issue #1288).

Die Teilnahmen einer Sitzung entstehen beim Anlegen, von Hand oder aus der Sitzungsreihe, aus den Teilnehmenden ihrer
Art (:func:`apps.work.faction.sitzungsarten.teilnehmende`). Wer danach Mitglied wird, wieder aktiv wird oder eine
Rolle, ein Gremium oder die Auswahl einer Art erhält, fehlte bisher in den schon angelegten Sitzungen: keine Einladung,
keine Zu- oder Absage, nicht in der Anwesenheit. :func:`nachtragen` trägt das Mitglied in die kommenden Sitzungen der
Arten ein, zu deren Teilnehmenden es gehört:

- nur aktive Mitglieder ohne Gastzugang (wie ``teilnehmende``);
- nur Sitzungen, die noch nicht begonnen haben (Entwurf, geplant, eingeladen, Beginn in der Zukunft);
- wer schon eine Teilnahme hat, auch abgesagt oder entschuldigt, bleibt, wie er ist. Entfernt wird nichts: Verliert
  jemand eine Rolle, bleibt die Einladung stehen (Entfernen aus der Organisation räumt sie wie bisher ab).

Ist zu einer Sitzung schon eingeladen, schickt der Einladungslauf der nachgetragenen Person ihre Einladung
(:func:`nachzuegler_einladen`), so wie allen anderen beim Erstversand; die übrigen erhalten nichts erneut.

Ausgelöst wird das Nachtragen durch die gespeicherte Mitgliedschaft (``post_save``: Beitritt, Reaktivierung), geänderte
Rollen und Gremien und die Auswahl der Teilnehmenden einer Art (``m2m_changed``), jeweils nach dem Abschluss der
Transaktion: Beim Beitritt kommen die Rollen erst nach dem Speichern der Mitgliedschaft dazu.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

if TYPE_CHECKING:
    from datetime import datetime

    from apps.tenants.models import Membership

    from .models import FactionMeeting

logger = logging.getLogger(__name__)

#: Sitzungen, in die nachgetragen wird: vor Sitzungsbeginn
OFFEN = ("draft", "planned", "invited")


def kommende_sitzungen_ohne(membership: Membership) -> QuerySet[FactionMeeting]:
    """Kommende Sitzungen der Arten, zu deren Teilnehmenden das Mitglied gehört, in denen es noch nicht steht."""
    from .models import FactionMeeting
    from .sitzungsarten import arten_mit_teilnahme

    return FactionMeeting.objects.filter(
        organization_id=membership.organization_id,
        meeting_type__in=arten_mit_teilnahme(membership).values("pk"),
        status__in=OFFEN,
        start__gt=timezone.now(),
    ).exclude(attendances__membership=membership)


def nachtragen(membership: Membership) -> int:
    """Mitglied in die kommenden Sitzungen seiner Arten eintragen (eingeladen); liefert die Zahl neuer Teilnahmen."""
    from .agenda import lock_meeting
    from .models import FactionAttendance

    if not membership.is_active or membership.is_guest:
        return 0
    zahl = 0
    for meeting in kommende_sitzungen_ohne(membership):
        with transaction.atomic():
            # Keine Eindeutigkeit in der Datenbank: Gleichzeitige Signale dürfen keine zweite Teilnahme anlegen
            lock_meeting(meeting)
            if meeting.attendances.filter(membership=membership).exists():
                continue
            FactionAttendance.objects.create(meeting=meeting, membership=membership, status="invited")
            zahl += 1
    if zahl:
        logger.info("Teilnahmen nachgetragen (membership=%s, sitzungen=%d)", membership.pk, zahl)
    return zahl


def _nachtragen_spaeter(membership_ids: list[Any]) -> None:
    """Nach dem Abschluss der Transaktion nachtragen; Fehler dürfen das Speichern der Mitgliedschaft nie stören."""

    def ausfuehren() -> None:
        from apps.tenants.models import Membership

        for membership in Membership.objects.filter(pk__in=membership_ids, is_active=True, is_guest=False):
            try:
                nachtragen(membership)
            except Exception:
                logger.exception("Teilnahmen nicht nachgetragen (membership=%s)", membership.pk)

    if membership_ids:
        transaction.on_commit(ausfuehren)


def _mitgliedschaft_gespeichert(sender: Any, instance: Any, raw: bool = False, **kwargs: Any) -> None:
    if raw or not instance.is_active or instance.is_guest:
        return
    _nachtragen_spaeter([instance.pk])


def _zuordnung_geaendert(sender: Any, instance: Any, action: str, model: Any, pk_set: Any, **kwargs: Any) -> None:
    """Rollen, Gremien oder Auswahl einer Art hinzugefügt: betroffene Mitglieder nachtragen (beide Richtungen)."""
    from apps.tenants.models import Membership

    if action != "post_add":
        return
    if isinstance(instance, Membership):
        _nachtragen_spaeter([instance.pk])
    elif model is Membership and pk_set:
        _nachtragen_spaeter(list(pk_set))


def register() -> None:
    from django.db.models.signals import m2m_changed, post_save

    from apps.tenants.models import Membership

    from .models import FactionMeetingType

    post_save.connect(_mitgliedschaft_gespeichert, sender=Membership, dispatch_uid="faction_teilnahme_mitgliedschaft")
    for name, through in (
        ("roles", Membership.roles.through),
        ("oparl_committees", Membership.oparl_committees.through),
        ("participants", FactionMeetingType.participants.through),
    ):
        m2m_changed.connect(_zuordnung_geaendert, sender=through, dispatch_uid=f"faction_teilnahme_{name}")


# =============================================================================
# Einladung für Nachgetragene (Einladungslauf)
# =============================================================================


def nachzuegler_einladen(now: datetime | None = None) -> int:
    """
    Einladung an Teilnahmen, die nach dem Erstversand ihrer Sitzung entstanden sind und noch keine bekamen: neue
    Mitglieder (:func:`nachtragen`) und vor der Sitzung von Hand hinzugefügte. Je Person die Einladung des Erstversands
    (Tagesordnung als PDF, Kalendereintrag mit der aktuellen SEQUENCE) samt Hinweis in Work; im Opt-out-Modus mit Zu-
    und Absagen gilt sie danach als angemeldet, wie alle beim Erstversand. Die übrigen Mitglieder erhalten nichts.

    Wer einmal ein Ergebnis hat (verschickt oder Grund), wird nicht erneut angeschrieben. Aufgerufen im Einladungslauf
    unter dessen Sperre (:func:`apps.work.faction.invitations.run_faction_invitation_pass`). Liefert die Zahl der Mails.
    """
    from .invitations import _notify_attendances, get_invitation_settings
    from .models import FactionAttendance
    from .services import FactionMeetingEmailService

    now = now or timezone.now()
    offen = (
        FactionAttendance.objects.filter(
            status="invited",
            membership__isnull=False,
            membership__is_active=True,
            membership__is_guest=False,
            invitation_mailed_at__isnull=True,
            invitation_error="",
            meeting__invitation_sent_at__isnull=False,
            meeting__status__in=("planned", "invited"),
            meeting__start__gt=now,
            meeting__organization__is_active=True,
            created_at__gt=F("meeting__invitation_sent_at"),
        )
        .select_related("meeting__organization", "membership__user")
        .order_by("meeting_id", "created_at")
    )
    je_sitzung: dict[Any, list[Any]] = defaultdict(list)
    for attendance in offen:
        je_sitzung[attendance.meeting_id].append(attendance)

    service = FactionMeetingEmailService()
    gesendet = 0
    for attendances in je_sitzung.values():
        meeting = attendances[0].meeting
        try:
            einstellungen = get_invitation_settings(meeting)
            pdf_public = service.build_agenda_pdf(meeting, include_internal=False)
            pdf_full = service.build_agenda_pdf(meeting, include_internal=True)
            ics_bytes = service.build_meeting_ics(meeting)
            erreicht = []
            for attendance in attendances:
                if service._send_invitation_email(
                    meeting,
                    attendance,
                    pdf_public=pdf_public,
                    pdf_full=pdf_full,
                    ics_bytes=ics_bytes,
                    invitation_mode=einstellungen["invitation_mode"],
                ):
                    gesendet += 1
                erreicht.append(attendance)
            _notify_attendances(meeting, erreicht, update=False)
            if einstellungen["invitation_mode"] == "opt_out" and meeting.rsvp_enabled:
                # Wie der Erstversand (dispatch_invitations): einzeln speichern, damit der Verlauf es festhält
                for attendance in FactionAttendance.objects.filter(pk__in=[a.pk for a in erreicht], status="invited"):
                    attendance.status = "confirmed"
                    attendance.save(update_fields=["status", "updated_at"])
        except Exception:
            logger.exception("Einladung für Nachgetragene gescheitert (meeting=%s)", meeting.pk)
    if gesendet:
        logger.info("Einladungen an Nachgetragene verschickt: %d", gesendet)
    return gesendet
