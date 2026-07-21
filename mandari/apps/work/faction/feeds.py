# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Persönlicher iCal-Feed (Issue #70).

Je Benutzer:in ein abonnierbarer Kalender-Feed mit:
- den Fraktionssitzungen ALLER Organisationen der Person (nur Titel,
  Zeit, Ort, Status — NIEMALS NÖ-TOP-Inhalte oder Protokolle)
- den RIS-Terminen der OParl-Gremien, denen die Person über ihre
  Mitgliedschaften zugeordnet ist (``Membership.oparl_committees``,
  Grundlage von "Meine Sitzungen")

Zugriff OHNE Login (Kalender-Clients können sich nicht anmelden) —
Sicherheit ausschließlich über das opake Token
(:class:`apps.work.faction.models.CalendarFeedToken`), das in den
Profileinstellungen jederzeit erneuert werden kann.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.common.ical import build_ics_feed

logger = logging.getLogger(__name__)

# Zeitfenster des Feeds: 90 Tage zurück, ~13 Monate voraus
FEED_PAST_DAYS = 90
FEED_FUTURE_DAYS = 400


def _site_url() -> str:
    return getattr(settings, "SITE_URL", "").rstrip("/")


def build_personal_feed(user) -> bytes:
    """
    ICS-Feed für eine Benutzer:in erzeugen.

    NÖ-Schutz: Aus Fraktionssitzungen fließen ausschließlich Titel, Zeit,
    Ort/Online-Kennzeichnung und ein Deep-Link in den Feed — keinerlei
    Tagesordnungs- oder Protokollinhalte.
    """
    from insight_core.models import OParlMeeting

    from .models import FactionMeeting

    now = timezone.now()
    window_start = now - timedelta(days=FEED_PAST_DAYS)
    window_end = now + timedelta(days=FEED_FUTURE_DAYS)
    base = _site_url()

    memberships = list(
        user.memberships.filter(is_active=True, organization__is_active=True)
        .select_related("organization")
        .prefetch_related("oparl_committees")
    )

    events = []

    # -- Fraktionssitzungen aller Organisationen -------------------------
    org_ids = [m.organization_id for m in memberships]
    org_by_id = {m.organization_id: m.organization for m in memberships}
    faction_meetings = (
        FactionMeeting.objects.filter(
            organization_id__in=org_ids,
            start__gte=window_start,
            start__lte=window_end,
        )
        .exclude(status="draft")
        .order_by("start")
    )
    for meeting in faction_meetings:
        organization = org_by_id.get(meeting.organization_id)
        location = meeting.location or ("Online" if meeting.is_virtual else "")
        events.append(
            {
                "uid": f"faction-meeting-{meeting.pk}@mandari",
                "summary": meeting.title,
                "start": meeting.start,
                "end": meeting.end,
                "location": location,
                "description": (
                    f"Fraktionssitzung von {organization.name if organization else ''}\n"
                    f"{base}/work/{organization.slug}/faction/{meeting.pk}/"
                    if organization
                    else ""
                ),
                "status": "CANCELLED" if meeting.status == "cancelled" else "CONFIRMED",
                "sequence": meeting.invitation_sequence,
            }
        )

    # -- RIS-Termine der zugeordneten Gremien ----------------------------
    committee_ids = set()
    for membership in memberships:
        committee_ids.update(committee.pk for committee in membership.oparl_committees.all())

    if committee_ids:
        ris_meetings = (
            OParlMeeting.objects.filter(
                organizations__in=committee_ids,
                start__isnull=False,
                start__gte=window_start,
                start__lte=window_end,
            )
            .distinct()
            .prefetch_related("organizations")
            .order_by("start")
        )
        for meeting in ris_meetings:
            events.append(
                {
                    "uid": f"oparl-meeting-{meeting.pk}@mandari",
                    "summary": meeting.get_display_name() or "Sitzung",
                    "start": meeting.start,
                    "end": meeting.end,
                    "location": meeting.location_name or "",
                    "description": f"RIS-Termin\n{base}/insight/termine/{meeting.pk}/",
                    "status": "CANCELLED" if meeting.cancelled else "CONFIRMED",
                }
            )

    events.sort(key=lambda event: event["start"])
    return build_ics_feed(events, name="mandari Sitzungen")
