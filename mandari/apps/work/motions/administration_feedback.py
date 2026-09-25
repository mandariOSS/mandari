# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldung der Verwaltung an die Fraktion (Issue #316, Fortsetzung von #40).

Passiert in Session etwas am eingereichten Antrag (Eingang, Umwandlung in eine Vorlage, Beratung
terminiert, Ergebnis, Beschluss), meldet ein Signal die Antrags-ID. Nach dem Commit der
Session-Transaktion baut :func:`sync_application` den öffentlich zulässigen Rückmeldestand
(:mod:`apps.session.services.application_feedback`) und

1. setzt den Work-Status über die definierten Übergänge (``Motion.advance_to``) – aber nur, wenn sich
   der von der Verwaltung abgeleitete Stand geändert hat,
2. meldet jedes Ereignis genau einmal (Schlüssel in :class:`MotionAdministrationEvent`),
3. benachrichtigt Autor:in und Federführung – nur mit Angaben aus dem öffentlichen Stand.

Sicherheit: Die Zuordnung läuft ausschließlich über die gespeicherte Verknüpfung
``Motion.session_application``; zusätzlich muss die einreichende Organisation des Antrags die
Organisation des Dokuments sein. Fehler werden protokolliert und nie an Session weitergereicht – die
Verwaltung speichert ungestört.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Case, CharField, F, OuterRef, Q, QuerySet, Subquery, Value, When
from django.urls import reverse
from django.utils import timezone

from apps.session.services import application_feedback
from apps.session.services.application_feedback import Feedback, Station

from .models import Motion, MotionAdministrationEvent, StatusTransitionError

if TYPE_CHECKING:
    from apps.session.models import SessionApplication
    from apps.tenants.models import Membership

logger = logging.getLogger(__name__)

#: Beschlussergebnis → Work-Status
OUTCOME_STATUS = {"approved": "adopted", "rejected": "rejected", "noted": "completed", "withdrawn": "withdrawn"}
#: Status, die die Rückmeldung der Verwaltung setzen darf (auch als Zwischenschritte)
FEEDBACK_STATUSES = frozenset({"submitted", "at_admin", "on_agenda", "adopted", "completed", "rejected", "withdrawn"})
#: Zwischenschritte beim Einreichen: Wer mit Freigaberecht einreicht, gibt damit frei
SUBMISSION_VIA = frozenset({"internal_review", "approved", "at_admin"})
#: Antragsstatus, deren Wechsel die Fraktion erfährt (eingereicht meldet die Einreichung selbst)
NOTIFIED_APPLICATION_STATUSES = frozenset({"received", "in_review", "accepted", "converted", "rejected", "withdrawn"})


@dataclass(frozen=True)
class Event:
    key: str
    kind: str
    title: str
    message: str


# =============================================================================
# Rückmeldestand für die Anzeige
# =============================================================================


def feedback_for(motion: Motion) -> Feedback | None:
    """Aktueller, öffentlich zulässiger Rückmeldestand eines eingereichten Dokuments (oder None)."""
    application = motion.session_application
    if application is None or application.submitting_organization_id != motion.organization_id:
        return None
    return application_feedback.build(application)


def with_administration_reference(queryset: QuerySet[Motion]) -> QuerySet[Motion]:
    """
    Drucksachennummer für Listen als ``administration_reference`` annotieren (eine Unterabfrage).

    Dieselbe Regel wie :func:`feedback_for`: erste Vorlage des Antrags, Nummer nur wenn veröffentlicht,
    und nur für Anträge der eigenen Organisation.
    """
    from apps.session.models import SessionPaper
    from apps.session.oparl_publication import UNVEROEFFENTLICHT

    published_reference = Case(
        When(Q(is_public=True) & ~Q(status__in=UNVEROEFFENTLICHT), then=F("reference")),
        default=Value(""),
        output_field=CharField(),
    )
    first_paper = (
        SessionPaper.objects.filter(
            source_application=OuterRef("session_application"),
            source_application__submitting_organization=OuterRef("organization"),
            tenant=OuterRef("session_application__tenant"),
        )
        .order_by("created_at")
        .annotate(published_reference=published_reference)
        .values("published_reference")[:1]
    )
    return queryset.annotate(administration_reference=Subquery(first_paper, output_field=CharField()))


def target_status(feedback: Feedback) -> str:
    """Work-Status, der dem Stand der Verwaltung entspricht."""
    if feedback.decision is not None:
        return OUTCOME_STATUS[feedback.decision.result]
    if feedback.application_status == "withdrawn":
        return "withdrawn"
    if feedback.application_status == "rejected":
        return "rejected"
    if any(
        station.public and station.scheduled and not station.cancelled and not station.removed_from_agenda
        for station in feedback.stations
    ):
        return "on_agenda"
    if feedback.application_status == "submitted":
        return "submitted"
    return "at_admin"


# =============================================================================
# Anstoß aus den Session-Signalen
# =============================================================================


def schedule_sync(application_id: Any) -> None:
    """Rückmeldung nach dem Commit der Session-Transaktion anstoßen (bricht nie ab)."""
    if application_id is None:
        return
    transaction.on_commit(lambda: _safe_sync(application_id), robust=True)


def _safe_sync(application_id: Any) -> None:
    try:
        sync_application(application_id)
    except Exception:  # noqa: BLE001 – Rückmeldungen dürfen die Verwaltung nie stören
        logger.exception("Rückmeldung an Work für Antrag %s fehlgeschlagen", application_id)


def sync_application(application_id: Any) -> list[Event]:
    """
    Rückmeldestand eines Antrags ins verknüpfte Work-Dokument übernehmen.

    Returns:
        Die neu gemeldeten Ereignisse (für Tests und Protokoll).
    """
    from apps.session.models import SessionApplication

    application = SessionApplication.objects.select_related("tenant").filter(pk=application_id).first()
    if application is None:
        return []
    motion = (
        Motion.objects.select_related("organization", "author__user", "responsible__user")
        .filter(session_application=application)
        .first()
    )
    if motion is None:
        return []
    if application.submitting_organization_id != motion.organization_id:
        # Verknüpfung passt nicht mehr zur einreichenden Organisation: nichts preisgeben
        logger.warning("Rückmeldung verworfen: Antrag %s gehört nicht zur Organisation des Dokuments", application.pk)
        return []

    feedback = application_feedback.build(application)
    with transaction.atomic():
        locked = Motion.objects.select_for_update().get(pk=motion.pk)
        _apply_status(locked, feedback)
        # Erste Rückmeldung für ein Dokument, das vor Issue #316 eingereicht wurde: Stand übernehmen,
        # ohne alles Bisherige noch einmal zu melden.
        silent = not MotionAdministrationEvent.objects.filter(motion=locked).exists()
        new_events = _record_events(locked, feedback)
    if silent:
        return new_events
    for event in _collapse(new_events):
        _notify(motion, event.title, event.message)
    return new_events


def _apply_status(motion: Motion, feedback: Feedback) -> None:
    """Work-Status nachziehen, wenn sich der Stand der Verwaltung geändert hat."""
    target = target_status(feedback)
    if target == motion.administration_status:
        return
    motion.administration_status = target
    motion.save(update_fields=["administration_status"])
    if motion.status == target or motion.status not in FEEDBACK_STATUSES:
        # Außerhalb der Rückmelde-Status (z. B. archiviert, zurück in Entwurf) entscheidet die Fraktion
        return
    try:
        motion.advance_to(target, via=FEEDBACK_STATUSES)
    except StatusTransitionError:
        logger.info("Kein Übergang von %s zu %s für Dokument %s", motion.status, target, motion.pk)


# =============================================================================
# Ereignisse (idempotent) und Benachrichtigungstexte
# =============================================================================


def _record_events(motion: Motion, feedback: Feedback) -> list[Event]:
    # Doppelte Schlüssel (z. B. zwei Stationen derselben Sitzung) nur einmal melden
    candidates = list({event.key: event for event in _events(motion, feedback)}.values())
    if not candidates:
        return []
    known = set(
        MotionAdministrationEvent.objects.filter(motion=motion, key__in=[e.key for e in candidates]).values_list(
            "key", flat=True
        )
    )
    new = [event for event in candidates if event.key not in known]
    MotionAdministrationEvent.objects.bulk_create(
        [MotionAdministrationEvent(motion=motion, key=event.key, kind=event.kind) for event in new],
        ignore_conflicts=True,
    )
    return new


def _events(motion: Motion, feedback: Feedback) -> list[Event]:
    """
    Alle Ereignisse, die der aktuelle Stand belegt – Texte nur aus öffentlich zulässigen Angaben.

    Termine und Ergebnisse sind an die Sitzung gebunden: Eine Station berät die Vorlage je Sitzung
    einmal. Speichern, Umsortieren, TOP-Anlage oder Neu-Nummerieren erzeugen deshalb nie eine zweite
    Meldung; eine Verschiebung auf eine andere Sitzung ist ein neuer Termin.
    """
    prefix = feedback.application_id
    title = motion.title
    ref = feedback.display_reference or "ohne Nummer"
    events: list[Event] = []

    status = feedback.application_status
    if status in NOTIFIED_APPLICATION_STATUSES:
        label = feedback.application_status_label
        if status == "converted" and feedback.paper_reference:
            message = (
                f"Ihr Antrag „{title}“ ({feedback.application_reference}) wurde bei {feedback.tenant_name} in eine "
                f"Vorlage umgewandelt: {feedback.reference_label} {feedback.paper_reference}."
            )
        else:
            message = (
                f"Ihr Antrag „{title}“ ({feedback.application_reference}) hat bei {feedback.tenant_name} "
                f"jetzt den Status „{label}“."
            )
        events.append(Event(f"{prefix}:antrag:{status}", "status", f"Verwaltung: {label}", message))

    if feedback.paper_reference:
        events.append(
            Event(
                f"{prefix}:vorlage:{feedback.paper_reference}",
                "paper",
                f"{feedback.reference_label} {feedback.paper_reference}",
                f"Ihr Antrag „{title}“ wird bei {feedback.tenant_name} als {feedback.reference_label} "
                f"{feedback.paper_reference} geführt.",
            )
        )

    for station in feedback.public_stations:
        if station.scheduled and not station.cancelled:
            events.append(
                Event(
                    f"{prefix}:termin:{station.meeting_key}",
                    "scheduled",
                    "Beratung terminiert",
                    f"Ihr Antrag „{title}“ ({ref}) wird am {_when(station)} im Gremium „{station.organization}“ "
                    f"beraten ({station.role_label}).",
                )
            )
        if not station.result or not station.scheduled:
            continue
        decision = feedback.decision
        if decision is not None and decision.station_key == station.key:
            number = f" Beschlussnummer: {decision.resolution_number}." if decision.resolution_number else ""
            events.append(
                Event(
                    f"{prefix}:beschluss:{station.meeting_key}:{station.result}",
                    "decision",
                    f"Beschluss: {station.result_label}",
                    f"Ihr Antrag „{title}“ ({ref}) wurde am {_day(station)} im Gremium „{station.organization}“ "
                    f"entschieden: {station.result_label}.{number}",
                )
            )
        else:
            events.append(
                Event(
                    f"{prefix}:ergebnis:{station.meeting_key}:{station.result}",
                    "result",
                    f"Beratungsergebnis: {station.result_label}",
                    f"Ihr Antrag „{title}“ ({ref}) wurde am {_day(station)} im Gremium „{station.organization}“ "
                    f"beraten ({station.role_label}): {station.result_label}.",
                )
            )
    return events


def _collapse(events: list[Event]) -> list[Event]:
    """Umwandlung mit Nummer meldet die Nummer bereits – keine zweite Benachrichtigung dafür."""
    kinds = {event.kind for event in events}
    converted_with_number = any(e.kind == "status" and e.key.endswith(":antrag:converted") for e in events)
    if converted_with_number and "paper" in kinds:
        return [event for event in events if event.kind != "paper"]
    return events


def _when(station: Station) -> str:
    if station.start is None:
        return "einem noch offenen Termin"
    return timezone.localtime(station.start).strftime("%d.%m.%Y um %H:%M Uhr")


def _day(station: Station) -> str:
    if station.start is None:
        return "einem nicht genannten Tag"
    return timezone.localtime(station.start).strftime("%d.%m.%Y")


def _recipients(motion: Motion) -> list[Membership]:
    """Autor:in und Federführung – nur aktive Mitglieder der Organisation des Dokuments."""
    recipients: list[Membership] = []
    for member in (motion.author, motion.responsible):
        if (
            member is not None
            and member.is_active
            and member.organization_id == motion.organization_id
            and all(member.pk != known.pk for known in recipients)
        ):
            recipients.append(member)
    return recipients


def _notify(motion: Motion, title: str, message: str) -> None:
    from apps.work.notifications.services import NotificationHub

    for recipient in _recipients(motion):
        try:
            NotificationHub.notify_motion_ris_status(motion, recipient, title, message)
        except Exception:  # noqa: BLE001 – eine gescheiterte Benachrichtigung stoppt die übrigen nicht
            logger.exception("Benachrichtigung zur Rückmeldung für Dokument %s fehlgeschlagen", motion.pk)


# =============================================================================
# Einreichung: Ereignis festhalten und Eingangsbestätigung
# =============================================================================


def record_submission(motion: Motion, application: SessionApplication) -> None:
    """Einreichung als erstes Ereignis festhalten (danach meldet die Rückmeldung jede Änderung)."""
    MotionAdministrationEvent.objects.get_or_create(
        motion=motion, key=f"{application.pk}:antrag:submitted", defaults={"kind": "submitted"}
    )


def send_receipt(motion_id: Any, membership_id: Any) -> bool:
    """
    Eingangsbestätigung an die einreichende Person – über den Mailweg der Organisation (eigenes SMTP
    oder mandari-Versand). Ein Versandfehler wird protokolliert und nie weitergereicht.
    """
    from apps.tenants.models import Membership
    from apps.work.organization.emails import absolute_url, send_organization_mail

    try:
        motion = Motion.objects.select_related("organization", "session_application__tenant").get(pk=motion_id)
        membership = Membership.objects.select_related("user").get(
            pk=membership_id, organization_id=motion.organization_id
        )
        application = motion.session_application
        if application is None or not membership.user.email:
            return False
        status_url = absolute_url(
            reverse("work:document_submit_ris", kwargs={"org_slug": motion.organization.slug, "motion_id": motion.pk})
        )
        return send_organization_mail(
            motion.organization,
            template="submission_receipt.html",
            subject=f"Eingangsbestätigung: {application.reference} bei {application.tenant.name}",
            to=membership.user.email,
            context={
                "motion": motion,
                "application": application,
                "recipient": membership.user,
                "status_url": status_url,
            },
        )
    except Exception:  # noqa: BLE001 – die Einreichung ist bereits erfolgt
        logger.exception("Eingangsbestätigung für Dokument %s konnte nicht versendet werden", motion_id)
        return False
