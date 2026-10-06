# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einladungslogik je Organisation (Issue #62).

Jede Organisation konfiguriert in den Fraktionseinstellungen:

- **Modus**: Opt-in (Teilnehmende melden sich AN — bisheriges Verhalten)
  oder Opt-out (Teilnehmende melden sich AB — beim Erstversand gelten alle
  eingeladenen Mitglieder als angemeldet/"Zugesagt" und können absagen).
- **Einladungsvorlauf**: frei wählbare Stundenzahl vor Sitzungsbeginn
  (z. B. 48 oder 72 Stunden).
- **Versandart**: automatisch (der periodische Lauf verschickt zum
  Vorlaufzeitpunkt) ODER nach Freigabe: Vorstand/Vorsitz werden rechtzeitig
  benachrichtigt (in-App UND E-Mail; Standard 24 h und nochmals 3 h vor dem
  geplanten Versandzeitpunkt; E-Mails je Typ in den
  Benachrichtigungseinstellungen abschaltbar). Der Versand erfolgt erst
  nach Klick "Freigeben".

Vertretung: Stellv. Vorsitz darf ohne formale Delegation direkt freigeben —
es wird schlicht auditiert, WER es war (Issue #66).

Baut auf dem vorhandenen Versand (Issue #59: ICS/PDF/Nachladung) und der
Sitzungserzeugung (Issue #61) auf; der periodische Lauf hängt wie
Erinnerungen/Erzeugung an einem Zeitplan im Worker (apps/work/schedules.py).

Automatik der Reihe (Issue #871):

- **Automatische Einladung zum festen Zeitpunkt** je Reihe (Standard aus), z. B. am letzten
  Freitag vor der Sitzung um 18 Uhr – ohne Freigabe und mit der Tagesordnung von diesem Zeitpunkt.
  Gerechnet wird in der Ortszeit der Installation (``TIME_ZONE``) samt Sommerzeit. Für Sitzungen
  ohne solche Reihe gelten Vorlauf und Versandart der Organisation unverändert.
- **Erinnerung zum Eintragen von TOPs** (Organisationseinstellung, Standard aus): eine Mail an alle,
  die TOPs eintragen oder vorschlagen dürfen, eine einstellbare Zahl Stunden vor dem geplanten Versand.
- **Genau einmal:** Erinnerung und Erstversand werden vor dem Senden in der Datenbank beansprucht
  (bedingtes UPDATE); ein zweiter Lauf oder Worker findet nichts mehr. Scheitert der Versand als
  Ganzes, wird der Anspruch zurückgegeben und der nächste Lauf versucht es erneut. Wird der Prozess
  mitten im Erstversand beendet, gibt der Einladungslauf den hängenden Anspruch nach
  ``INVITATION_CLAIM_STALE_MINUTES`` frei (mit Warnung im Log) und versendet erneut.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings as django_settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_INVITATION_LOCK_KEY = "faction:invitation:lock"
_INVITATION_LOCK_TIMEOUT = 10 * 60

# Rollennamen, die als "Vorstand/Vorsitz" gelten (Freigabe + Bestätigungen)
BOARD_ROLE_NAMES = ("Fraktionsvorsitz", "Stellv. Vorsitz")

INVITATION_MODES = ("opt_in", "opt_out")
INVITATION_DISPATCH_MODES = ("automatic", "approval")

INVITATION_DEFAULTS = {
    "invitation_mode": "opt_in",
    "invitation_lead_hours": 72,
    "invitation_dispatch": "automatic",
    # Erinnerung zum Eintragen von TOPs (Issue #871): Stunden vor dem geplanten Einladungsversand
    "agenda_reminder_enabled": False,
    "agenda_reminder_hours": 24,
}

# Wer TOPs eintragen oder vorschlagen darf, bekommt die TOP-Erinnerung
AGENDA_REMINDER_PERMISSIONS = ("agenda.create", "agenda.propose", "agenda.manage", "faction.manage")

# Freigabe-Hinweise: Standardvorlauf vor dem geplanten Versandzeitpunkt
RELEASE_NOTICE_FIRST_HOURS = 24
RELEASE_NOTICE_FINAL_HOURS = 3

# TOP-Erinnerung höchstens zwei Wochen vor dem geplanten Versand
AGENDA_REMINDER_MAX_HOURS = 24 * 14

# Ein Anspruch auf den Erstversand, der so lange nicht abgeschlossen ist, gilt als hängend (Prozess beendet)
INVITATION_CLAIM_STALE_MINUTES = 30


# =============================================================================
# Einstellungen + Vorstand/Vorsitz
# =============================================================================


def get_invitation_settings(organization) -> dict:
    """Einladungs-Einstellungen der Organisation (mit Defaults)."""
    faction_settings = (organization.settings or {}).get("faction", {})
    result = dict(INVITATION_DEFAULTS)

    mode = faction_settings.get("invitation_mode")
    if mode in INVITATION_MODES:
        result["invitation_mode"] = mode

    dispatch = faction_settings.get("invitation_dispatch")
    if dispatch in INVITATION_DISPATCH_MODES:
        result["invitation_dispatch"] = dispatch

    try:
        lead = int(faction_settings.get("invitation_lead_hours", result["invitation_lead_hours"]))
        result["invitation_lead_hours"] = max(1, min(lead, 24 * 60))
    except (TypeError, ValueError):
        pass

    result["agenda_reminder_enabled"] = faction_settings.get("agenda_reminder_enabled") is True
    try:
        hours = int(faction_settings.get("agenda_reminder_hours", result["agenda_reminder_hours"]))
        result["agenda_reminder_hours"] = max(1, min(hours, AGENDA_REMINDER_MAX_HOURS))
    except (TypeError, ValueError):
        pass

    return result


def fixed_dispatch_at(start: datetime, weekday: int, at_time) -> datetime:
    """
    Letzter Zeitpunkt „Wochentag, Uhrzeit“ vor Sitzungsbeginn (Issue #871).

    Gerechnet in der Ortszeit der Installation (``TIME_ZONE``), damit „freitags 18 Uhr“ auch über
    die Zeitumstellung hinweg 18 Uhr Ortszeit bleibt. Fällt der Zeitpunkt auf den Sitzungstag und
    liegt nicht vor dem Beginn, gilt derselbe Wochentag eine Woche früher.
    """
    tz = timezone.get_default_timezone()
    local_start = timezone.localtime(start, tz)
    day = local_start.date() - timedelta(days=(local_start.weekday() - weekday) % 7)
    candidate = timezone.make_aware(datetime.combine(day, at_time), tz)
    if candidate >= start:
        candidate = timezone.make_aware(datetime.combine(day - timedelta(days=7), at_time), tz)
    return candidate


def auto_invite_schedule(meeting):
    """Reihe der Sitzung, wenn sie automatisch zum festen Zeitpunkt einlädt (Issue #871), sonst ``None``."""
    if meeting.schedule_id is None:
        return None
    schedule = meeting.schedule
    return schedule if schedule.auto_invite_ready else None


def invitation_dispatch_at(meeting, settings: dict | None = None):
    """Geplanter Versandzeitpunkt: fester Zeitpunkt der Reihe, sonst Sitzungsbeginn minus Vorlauf."""
    schedule = auto_invite_schedule(meeting)
    if schedule is not None:
        return fixed_dispatch_at(meeting.start, schedule.auto_invite_weekday, schedule.auto_invite_time)
    settings = settings or get_invitation_settings(meeting.organization)
    return meeting.start - timedelta(hours=settings["invitation_lead_hours"])


def dispatches_automatically(meeting, settings: dict | None = None) -> bool:
    """
    Geht die Einladung zum Versandzeitpunkt ohne weiteren Klick raus?

    Ja bei einer Reihe mit automatischer Einladung (auch im Freigabe-Modus der Organisation), bei
    Versandart „automatisch“ und nach erteilter Freigabe.
    """
    if auto_invite_schedule(meeting) is not None:
        return True
    settings = settings or get_invitation_settings(meeting.organization)
    return settings["invitation_dispatch"] == "automatic" or meeting.invitation_released_at is not None


def get_board_members(organization):
    """
    Aktive Mitglieder der Rollen Vorstand/Vorsitz (Freigabe-Empfänger).

    Fallback: Gibt es keine Mitglieder mit Vorstands-Rolle, gelten die
    Mitglieder mit faction.invite als Freigabe-Berechtigte.
    """
    memberships = list(
        organization.memberships.filter(is_active=True, roles__name__in=BOARD_ROLE_NAMES)
        .select_related("user")
        .distinct()
    )
    if memberships:
        return memberships

    from apps.common.permissions import PermissionChecker

    fallback = []
    for membership in organization.memberships.filter(is_active=True).select_related("user"):
        if PermissionChecker(membership).has_permission("faction.invite"):
            fallback.append(membership)
    return fallback


def is_board_member(membership) -> bool:
    """Gehört das Mitglied zu Vorstand/Vorsitz (inkl. stellv. Vorsitz)?"""
    if membership is None or not membership.is_active:
        return False
    return membership.roles.filter(name__in=BOARD_ROLE_NAMES).exists()


def can_release_invitations(membership) -> bool:
    """
    Darf das Mitglied den Einladungsversand freigeben?

    Vorstand/Vorsitz (inkl. stellv. Vorsitz — ohne formale Delegation)
    oder Mitglieder mit faction.invite.
    """
    if membership is None:
        return False
    return is_board_member(membership) or membership.has_permission("faction.invite")


def can_confirm_attendance(membership) -> bool:
    """
    Darf das Mitglied Teilnahmen final bestätigen (Issue #67)?

    Die Rolle Vorstand/Vorsitz — der stellv. Vorsitz darf ohne formale
    Delegation direkt bestätigen (dokumentiert wird nur, wer es war).
    Fallback: Sind keine Vorstands-Rollen besetzt, genügt faction.manage.
    """
    if membership is None or not membership.is_active:
        return False
    if is_board_member(membership):
        return True
    has_board = membership.organization.memberships.filter(is_active=True, roles__name__in=BOARD_ROLE_NAMES).exists()
    return not has_board and membership.has_permission("faction.manage")


# =============================================================================
# Versand (zentral — wendet den Opt-in/Opt-out-Modus an)
# =============================================================================


def dispatch_invitations(meeting, *, update: bool = False) -> int:
    """
    Einladungen zentral versenden (Issue #62).

    Erstversand: verschickt an alle Eingeladenen; im Opt-out-Modus gelten
    danach alle angeschriebenen Mitglieder als angemeldet ("Zugesagt") und
    können weiterhin absagen. Setzt die Versand-Metadaten der Sitzung.

    Aktualisierung (update=True): wie bisher — der Aufrufer erhöht die
    ICS-SEQUENCE selbst (siehe FactionActionView._invite).

    Returns:
        Anzahl versendeter E-Mails.
    """
    from .services import FactionMeetingEmailService, invitation_attendances

    settings = get_invitation_settings(meeting.organization)
    service = FactionMeetingEmailService()

    sent_count = service.send_invitations(meeting, update=update, invitation_mode=settings["invitation_mode"])

    # In-App-Benachrichtigung an die Angeschriebenen (Issue #70) — die
    # E-Mail mit ICS/PDF ging bereits separat raus, daher send_email=False
    _notify_invitations(meeting, update=update)

    if not update:
        # Opt-out: alle eingeladenen Mitglieder gelten als angemeldet – nur mit Zu- und Absagen
        # (Issue #871); ohne sie bleibt die Teilnahme offen, bis die Anwesenheit erfasst wird
        if settings["invitation_mode"] == "opt_out" and meeting.rsvp_enabled:
            for attendance in invitation_attendances(meeting):
                attendance.status = "confirmed"
                attendance.save(update_fields=["status", "updated_at"])

        meeting.invitation_sent = True
        meeting.invitation_sent_at = timezone.now()
        if meeting.status in ("draft", "planned"):
            meeting.status = "invited"
        meeting.save(update_fields=["invitation_sent", "invitation_sent_at", "status", "updated_at"])

    return sent_count


def dispatch_invitations_once(meeting, now=None) -> int | None:
    """
    Erstversand genau einmal (Issue #871): beansprucht den Versand vor dem Senden in der Datenbank.

    Der Anspruch trägt seinen Zeitpunkt (``invitation_claimed_at``). Endet der Prozess zwischen Anspruch
    und Abschluss, gibt der Einladungslauf ihn nach ``INVITATION_CLAIM_STALE_MINUTES`` frei.

    Returns:
        Anzahl versendeter E-Mails, ``None`` wenn schon ein anderer Lauf oder Klick verschickt hat.
    """
    from .models import FactionMeeting

    claimed = FactionMeeting.objects.filter(pk=meeting.pk, invitation_sent=False).update(
        invitation_sent=True, invitation_claimed_at=now or timezone.now()
    )
    if not claimed:
        return None
    try:
        return dispatch_invitations(meeting)
    except Exception:
        # Versand als Ganzes gescheitert (z. B. PDF): Anspruch zurückgeben, der nächste Lauf versucht es erneut
        FactionMeeting.objects.filter(pk=meeting.pk, invitation_sent_at__isnull=True).update(
            invitation_sent=False, invitation_claimed_at=None
        )
        raise


def release_stale_invitation_claims(now) -> int:
    """
    Hängende Ansprüche auf den Erstversand freigeben (Issue #871).

    Hängend heißt: beansprucht vor mehr als ``INVITATION_CLAIM_STALE_MINUTES`` Minuten, aber nie
    abgeschlossen (``invitation_sent_at`` leer, Status noch „Entwurf“ oder „Geplant“) – der Prozess
    wurde etwa beim Deploy oder wegen Speichermangels beendet. Ohne Freigabe bliebe die Sitzung ohne
    Einladung stehen, weil der Lauf nur unversandte Sitzungen betrachtet. Nach der Freigabe versendet
    der Lauf erneut an alle Eingeladenen; wer vor dem Abbruch schon eine Mail bekam, erhält sie doppelt.
    Ansprüche aus Versionen vor diesem Feld (``invitation_claimed_at`` leer) bleiben unberührt.

    Returns:
        Anzahl freigegebener Ansprüche.
    """
    from .models import FactionMeeting

    stale = FactionMeeting.objects.filter(
        invitation_sent=True,
        invitation_sent_at__isnull=True,
        invitation_claimed_at__lt=now - timedelta(minutes=INVITATION_CLAIM_STALE_MINUTES),
        status__in=["draft", "planned"],
        start__gt=now,
    )
    released = 0
    for meeting_id, claimed_at in stale.values_list("pk", "invitation_claimed_at"):
        # Bedingt: nur genau diesen Anspruch zurückgeben, falls der Versand doch noch abschließt
        if FactionMeeting.objects.filter(
            pk=meeting_id, invitation_sent_at__isnull=True, invitation_claimed_at=claimed_at
        ).update(invitation_sent=False, invitation_claimed_at=None):
            logger.warning(
                "Einladungsversand nicht abgeschlossen (meeting=%s, beansprucht %s): Anspruch freigegeben, "
                "der Lauf versendet erneut",
                meeting_id,
                claimed_at.isoformat(),
            )
            released += 1
    return released


def _claim_timestamp(meeting, field: str, now) -> bool:
    """Zeitstempel ``field`` setzen, falls noch leer (bedingtes UPDATE); True, wenn dieser Aufruf ihn bekam."""
    from .models import FactionMeeting

    claimed = FactionMeeting.objects.filter(pk=meeting.pk, **{f"{field}__isnull": True}).update(**{field: now})
    if claimed:
        setattr(meeting, field, now)
    return bool(claimed)


def _release_timestamp(meeting, field: str, now) -> None:
    """Anspruch zurückgeben (nur den eigenen Zeitstempel)."""
    from .models import FactionMeeting

    FactionMeeting.objects.filter(pk=meeting.pk, **{field: now}).update(**{field: None})
    setattr(meeting, field, None)


# =============================================================================
# Erinnerung zum Eintragen von TOPs (Issue #871)
# =============================================================================


def agenda_reminder_recipients(meeting) -> list:
    """Eingeladene Mitglieder (ohne Gäste und Absagen), die TOPs eintragen oder vorschlagen dürfen."""
    attendances = (
        meeting.attendances.filter(membership__isnull=False, membership__is_active=True, membership__is_guest=False)
        .exclude(status="declined")
        .select_related("membership__user", "membership__organization")
    )
    recipients = []
    for attendance in attendances:
        membership = attendance.membership
        if any(membership.has_permission(code) for code in AGENDA_REMINDER_PERMISSIONS):
            recipients.append(membership)
    return recipients


def send_agenda_reminder(meeting, dispatch_at) -> int:
    """
    Erinnerung zum Eintragen von TOPs verschicken (über den Versandweg der Organisation).

    Die Mail nennt die bisherige Tagesordnung – den nichtöffentlichen Teil nur für Vereidigte – und den
    geplanten Versand der Einladung.

    Returns:
        Anzahl versendeter E-Mails.
    """
    from apps.common import mail
    from apps.common.email import render_email

    from .services import FactionMeetingEmailService
    from .visibility import can_view_internal

    meeting_url = FactionMeetingEmailService().get_meeting_url(meeting)
    active = meeting.agenda_items.filter(proposal_status="active", parent__isnull=True).order_by("order", "number")
    public_items = list(active.filter(visibility="public"))
    internal_items = list(active.filter(visibility="internal"))
    local_dispatch = timezone.localtime(dispatch_at, timezone.get_default_timezone())

    sent = 0
    for membership in agenda_reminder_recipients(meeting):
        user = membership.user
        if not user.email:
            continue
        context = {
            "meeting": meeting,
            "organization": meeting.organization,
            "user": user,
            "dispatch_at": local_dispatch,
            "public_agenda_items": public_items,
            "internal_agenda_items": internal_items if can_view_internal(membership) else [],
            "meeting_url": meeting_url,
        }
        try:
            html_body, text_body = render_email("work/faction/email/agenda_reminder.html", context)
            if mail.send(
                kind="work.fraktion.top_erinnerung",
                organization=meeting.organization,
                subject=f"TOPs eintragen: {meeting.title}",
                body=text_body,
                html_body=html_body,
                to=[user.email],
                fail_silently=True,
            ):
                sent += 1
        except Exception:
            # Eine Adresse darf die übrigen nicht aufhalten; das Log nennt keine Empfänger
            logger.exception("TOP-Erinnerung nicht versendet (meeting=%s)", meeting.id)
    return sent


def _notify_invitations(meeting, *, update: bool) -> None:
    """
    In-App-Benachrichtigung für den Einladungsversand (Issue #70).

    Gleicher Empfängerkreis wie der E-Mail-Versand: beim Erstversand alle
    Eingeladenen, bei Aktualisierungen alle, die nicht abgesagt haben.
    """
    try:
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        from .services import invitation_attendances

        attendances = invitation_attendances(meeting, update=update)
        recipients = [a.membership for a in attendances.select_related("membership__user")]
        if not recipients:
            return

        local_start = timezone.localtime(meeting.start)
        when = local_start.strftime("%d.%m.%Y %H:%M")
        title = "Aktualisierte Einladung" if update else "Einladung zur Fraktionssitzung"
        message = f'"{meeting.title}" am {when} Uhr.'
        if meeting.location:
            message += f" Ort: {meeting.location}."

        NotificationHub.send_bulk(
            recipients=recipients,
            notification_type=NotificationType.FACTION_INVITATION,
            title=title,
            message=message,
            link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
            metadata={"meeting_id": str(meeting.id), "update": update},
            send_email=False,
        )
    except Exception:
        # Benachrichtigungen dürfen den Versand niemals zum Scheitern bringen
        logger.exception("In-App-Benachrichtigung zum Einladungsversand fehlgeschlagen (meeting=%s)", meeting.id)


def release_invitations(meeting, membership) -> bool:
    """
    Einladungsversand freigeben (Freigabe-Modus, Issue #62).

    Auditiert über den Feldwechsel, WER freigegeben hat. Liegt der geplante
    Versandzeitpunkt bereits in der Vergangenheit, wird sofort versendet —
    sonst verschickt der periodische Lauf zum Vorlaufzeitpunkt.

    Returns:
        True, wenn die Freigabe gesetzt wurde.
    """
    if meeting.invitation_sent or meeting.invitation_released_at is not None:
        return False
    if meeting.status not in ("draft", "planned"):
        return False

    meeting.invitation_released_at = timezone.now()
    meeting.invitation_released_by = membership
    meeting.save(update_fields=["invitation_released_at", "invitation_released_by", "updated_at"])

    settings = get_invitation_settings(meeting.organization)
    if timezone.now() >= invitation_dispatch_at(meeting, settings):
        dispatch_invitations_once(meeting)
    return True


# =============================================================================
# Freigabe-Hinweise an Vorstand/Vorsitz
# =============================================================================


def _send_release_notice(meeting, dispatch_at, *, final: bool) -> int:
    """
    Freigabe-Hinweis an Vorstand/Vorsitz (in-App UND E-Mail).

    E-Mails sind je Typ individuell in den Benachrichtigungseinstellungen
    abschaltbar (NotificationPreference). Versand über den konfigurierten
    Weg der Organisation (Issue #65).
    """
    from apps.work.notifications.models import NotificationPreference, NotificationType
    from apps.work.notifications.services import NotificationHub

    organization = meeting.organization
    board = get_board_members(organization)
    if not board:
        logger.warning("Keine Freigabe-Berechtigten für Organisation %s gefunden", organization.slug)
        return 0

    local_dispatch = timezone.localtime(dispatch_at)
    when = local_dispatch.strftime("%d.%m.%Y %H:%M")
    stage = "in Kürze" if final else "bald"
    title = "Einladungsversand wartet auf Freigabe"
    message = (
        f'Die Einladungen zur Sitzung "{meeting.title}" sollen {stage} versendet werden '
        f"(geplant: {when} Uhr). Bitte den Versand freigeben."
    )
    link = f"/work/{organization.slug}/faction/{meeting.id}/"

    sent = 0
    for membership in board:
        # In-App immer; E-Mail separat über den Organisations-Versandweg,
        # damit die Einstellung je Organisation greift (Issue #65)
        NotificationHub.send(
            recipient=membership,
            notification_type=NotificationType.FACTION_INVITATION_RELEASE,
            title=title,
            message=message,
            link=link,
            metadata={"meeting_id": str(meeting.id), "dispatch_at": dispatch_at.isoformat(), "final": final},
            send_email=False,
        )

        prefs, _created = NotificationPreference.objects.get_or_create(membership=membership)
        if not prefs.is_type_enabled(NotificationType.FACTION_INVITATION_RELEASE, "email"):
            continue
        user = membership.user
        if not user.email:
            continue

        from apps.common import mail

        body = "\n".join(
            [
                f"Hallo {user.first_name or user.email},",
                "",
                message,
                "",
                f"Zur Sitzung: {getattr(django_settings, 'SITE_URL', '').rstrip('/')}{link}",
                "",
                f"Viele Grüße,\n{organization.name}",
            ]
        )
        try:
            # Versand über den konfigurierten Weg der Organisation (Issue #65)
            if mail.send(
                kind="work.fraktion.freigabe",
                organization=organization,
                subject=f"Freigabe erforderlich: {meeting.title}",
                body=body,
                to=[user.email],
                fail_silently=True,
            ):
                sent += 1
        except Exception:
            logger.exception("Freigabe-Hinweis-E-Mail fehlgeschlagen (meeting=%s)", meeting.id)

    return sent


# =============================================================================
# Periodischer Einladungslauf (Zeitplan im Worker)
# =============================================================================


def run_faction_invitation_pass(now=None) -> dict:
    """
    Periodischer Einladungslauf (Issues #62, #871).

    - Versandart "automatic": Einladungen werden zum konfigurierten
      Vorlaufzeitpunkt automatisch versendet (einmalig je Sitzung).
    - Reihe mit automatischer Einladung: Versand zum festen Zeitpunkt der
      Reihe, ohne Freigabe (auch im Freigabe-Modus der Organisation).
    - Versandart "approval": Vorstand/Vorsitz erhalten 24 h und 3 h vor dem
      geplanten Versandzeitpunkt einen Freigabe-Hinweis (je einmal);
      versendet wird erst nach Freigabe.
    - TOP-Erinnerung (falls eingeschaltet): einmal je Sitzung im Fenster vor
      dem geplanten Versand.

    Ein Cache-Lock verhindert parallele Läufe (mehrere Worker/Prozesse); den
    Erstversand und die TOP-Erinnerung sichert zusätzlich ein Anspruch in der
    Datenbank ab (genau einmal).

    Returns:
        Statistik-Dict (dispatched, notices, agenda_reminders bzw. skipped-Grund).
    """
    from django.core.cache import cache

    from .models import FactionMeeting

    now = now or timezone.now()

    if not cache.add(_INVITATION_LOCK_KEY, "1", timeout=_INVITATION_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        stats = {
            "meetings": 0,
            "dispatched": 0,
            "notices": 0,
            "agenda_reminders": 0,
            "released_claims": release_stale_invitation_claims(now),
        }
        meetings = FactionMeeting.objects.filter(
            status__in=["draft", "planned"],
            invitation_sent=False,
            start__gt=now,
            organization__is_active=True,
        ).select_related("organization", "schedule")

        for meeting in meetings:
            settings = get_invitation_settings(meeting.organization)
            dispatch_at = invitation_dispatch_at(meeting, settings)

            if _agenda_reminder_due(meeting, settings, dispatch_at, now) and _claim_timestamp(
                meeting, "agenda_reminder_sent_at", now
            ):
                try:
                    sent = send_agenda_reminder(meeting, dispatch_at)
                except Exception:
                    # Erneut versuchen im nächsten Lauf; die Einladung selbst soll davon nicht abhängen
                    logger.exception("TOP-Erinnerung fehlgeschlagen (meeting=%s)", meeting.id)
                    _release_timestamp(meeting, "agenda_reminder_sent_at", now)
                else:
                    from .audit import log_event

                    log_event("agenda_reminder_sent", meeting, is_internal=False, changes={"empfaenger": sent})
                    stats["agenda_reminders"] += 1

            if dispatches_automatically(meeting, settings):
                if now >= dispatch_at:
                    try:
                        sent_count = dispatch_invitations_once(meeting, now=now)
                    except Exception:
                        logger.exception("Automatischer Einladungsversand fehlgeschlagen (meeting=%s)", meeting.id)
                        continue
                    if sent_count is None:
                        continue
                    stats["meetings"] += 1
                    stats["dispatched"] += 1
                continue

            # Freigabe-Modus ohne Freigabe: Hinweise an Vorstand/Vorsitz
            # (höchstens ein Hinweis je Lauf — der 3-h-Hinweis hat Vorrang)
            if meeting.release_notice_final_sent_at is None and now >= dispatch_at - timedelta(
                hours=RELEASE_NOTICE_FINAL_HOURS
            ):
                _send_release_notice(meeting, dispatch_at, final=True)
                meeting.release_notice_final_sent_at = now
                if meeting.release_notice_first_sent_at is None:
                    # 24-h-Hinweis entfällt, wenn der 3-h-Hinweis bereits fällig ist
                    meeting.release_notice_first_sent_at = now
                    meeting.save(
                        update_fields=["release_notice_final_sent_at", "release_notice_first_sent_at", "updated_at"]
                    )
                else:
                    meeting.save(update_fields=["release_notice_final_sent_at", "updated_at"])
                stats["meetings"] += 1
                stats["notices"] += 1
            elif meeting.release_notice_first_sent_at is None and now >= dispatch_at - timedelta(
                hours=RELEASE_NOTICE_FIRST_HOURS
            ):
                _send_release_notice(meeting, dispatch_at, final=False)
                meeting.release_notice_first_sent_at = now
                meeting.save(update_fields=["release_notice_first_sent_at", "updated_at"])
                stats["meetings"] += 1
                stats["notices"] += 1

        if stats["dispatched"] or stats["notices"] or stats["agenda_reminders"]:
            logger.info(
                "Fraktions-Einladungslauf: %d versendet, %d Freigabe-Hinweis(e), %d TOP-Erinnerung(en)",
                stats["dispatched"],
                stats["notices"],
                stats["agenda_reminders"],
            )
        return stats
    finally:
        cache.delete(_INVITATION_LOCK_KEY)


def _agenda_reminder_due(meeting, settings: dict, dispatch_at, now) -> bool:
    """TOP-Erinnerung fällig: eingeschaltet, noch nicht versandt und im Fenster vor dem geplanten Versand."""
    if not settings["agenda_reminder_enabled"] or meeting.agenda_reminder_sent_at is not None:
        return False
    return dispatch_at - timedelta(hours=settings["agenda_reminder_hours"]) <= now < dispatch_at
