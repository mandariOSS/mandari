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
Erinnerungen/Erzeugung am Sync-Watchdog.
"""

import logging
from datetime import timedelta

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
}

# Freigabe-Hinweise: Standardvorlauf vor dem geplanten Versandzeitpunkt
RELEASE_NOTICE_FIRST_HOURS = 24
RELEASE_NOTICE_FINAL_HOURS = 3


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

    return result


def invitation_dispatch_at(meeting, settings: dict | None = None):
    """Geplanter Versandzeitpunkt (Sitzungsbeginn minus Vorlauf)."""
    settings = settings or get_invitation_settings(meeting.organization)
    return meeting.start - timedelta(hours=settings["invitation_lead_hours"])


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
    from .services import FactionMeetingEmailService

    settings = get_invitation_settings(meeting.organization)
    service = FactionMeetingEmailService()

    sent_count = service.send_invitations(meeting, update=update, invitation_mode=settings["invitation_mode"])

    if not update:
        # Opt-out: alle eingeladenen Mitglieder gelten als angemeldet
        if settings["invitation_mode"] == "opt_out":
            for attendance in meeting.attendances.filter(status="invited", membership__isnull=False):
                attendance.status = "confirmed"
                attendance.save(update_fields=["status", "updated_at"])

        meeting.invitation_sent = True
        meeting.invitation_sent_at = timezone.now()
        if meeting.status in ("draft", "planned"):
            meeting.status = "invited"
        meeting.save(update_fields=["invitation_sent", "invitation_sent_at", "status", "updated_at"])

    return sent_count


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
        dispatch_invitations(meeting)
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

        from apps.common.email import send_email

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
            if send_email(
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
# Periodischer Einladungslauf (Sync-Watchdog)
# =============================================================================


def run_faction_invitation_pass(now=None) -> dict:
    """
    Periodischer Einladungslauf (Issue #62).

    - Versandart "automatic": Einladungen werden zum konfigurierten
      Vorlaufzeitpunkt automatisch versendet (einmalig je Sitzung).
    - Versandart "approval": Vorstand/Vorsitz erhalten 24 h und 3 h vor dem
      geplanten Versandzeitpunkt einen Freigabe-Hinweis (je einmal);
      versendet wird erst nach Freigabe.

    Ein Cache-Lock verhindert parallele Läufe (mehrere Worker/Prozesse).

    Returns:
        Statistik-Dict (dispatched, notices bzw. skipped-Grund).
    """
    from django.core.cache import cache

    from .models import FactionMeeting

    now = now or timezone.now()

    if not cache.add(_INVITATION_LOCK_KEY, "1", timeout=_INVITATION_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        stats = {"meetings": 0, "dispatched": 0, "notices": 0}
        meetings = FactionMeeting.objects.filter(
            status__in=["draft", "planned"],
            invitation_sent=False,
            start__gt=now,
            organization__is_active=True,
        ).select_related("organization")

        for meeting in meetings:
            settings = get_invitation_settings(meeting.organization)
            dispatch_at = invitation_dispatch_at(meeting, settings)

            if settings["invitation_dispatch"] == "automatic" or meeting.invitation_released_at is not None:
                if now >= dispatch_at:
                    try:
                        dispatch_invitations(meeting)
                    except Exception:
                        logger.exception("Automatischer Einladungsversand fehlgeschlagen (meeting=%s)", meeting.id)
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

        if stats["dispatched"] or stats["notices"]:
            logger.info(
                "Fraktions-Einladungslauf: %d versendet, %d Freigabe-Hinweis(e)",
                stats["dispatched"],
                stats["notices"],
            )
        return stats
    finally:
        cache.delete(_INVITATION_LOCK_KEY)
