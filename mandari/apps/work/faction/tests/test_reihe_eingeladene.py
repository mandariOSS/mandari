# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe ändern: auch eingeladene Sitzungen anpassen und die Eingeladenen informieren (Issue #1227).

- Häkchen „Auch Sitzungen ändern, zu denen schon eingeladen wurde, und die Eingeladenen informieren“ (standardmäßig an):
  Eingeladene, noch nicht begonnene Sitzungen ändern sich nach denselben Regeln mit; ohne Häkchen wie bisher.
- Einzeln geänderte Angaben bleiben; laufende, vergangene und von Hand abgesagte Sitzungen bleiben unverändert.
- Die Eingeladenen erfahren die Änderung (In-App und E-Mail nach ihren Einstellungen), ohne nichtöffentliche Inhalte;
  Zu- und Absagen bleiben stehen. Bei neuem Termin auch, wer abgesagt hat.
- Eingeladene Sitzungen landen nach einem Rhythmuswechsel nie auf einer Pause und werden nie abgesagt.
- Die Vorschau nennt die eingeladenen Sitzungen einzeln. Läuft gerade ein Einladungsversand, übernimmt Work nichts.
- Der Knopf „Bearbeiten“ an der Reihe ist ohne Aufklappen sichtbar, das Häkchen ist gesetzt.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import generation
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
)
from apps.work.notifications.models import Notification, NotificationPreference, NotificationType

pytestmark = pytest.mark.django_db

CaptureCallbacks = Callable[..., Any]
HTMX = {"HTTP_HX_REQUEST": "true"}
GEAENDERT = NotificationType.FACTION_MEETING_UPDATED
NOE_TITEL = "Personalie Vertraulich-Beispiel"


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = 21


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.manage", "faction.view_public", "faction.view"], email="vorsitz@example.org")


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "faction.view_public", "faction.view"], email="mitglied@example.org")


def _url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


def _morgen() -> date:
    return timezone.localdate() + timedelta(days=1)


def _reihe(org: Any, capture: CaptureCallbacks, **felder: Any) -> FactionMeetingSchedule:
    """Wöchentliche Reihe am Wochentag von morgen, 18:00, im Büro, mit sofort angelegten Terminen (3 im Horizont)."""
    werte = {
        "name": "Fraktionssitzung",
        "weekday": _morgen().weekday(),
        "time": time(18, 0),
        "default_location": "Büro",
    }
    werte.update(felder)
    schedule = FactionMeetingSchedule.objects.create(organization=org, **werte)
    with capture(execute=True):
        generation.generate_meetings_now(schedule)
    schedule.refresh_from_db()
    return schedule


def _termine(schedule: FactionMeetingSchedule) -> list[FactionMeeting]:
    return list(FactionMeeting.objects.filter(schedule=schedule).order_by("start"))


def _einladen(meeting: FactionMeeting) -> FactionMeeting:
    """Wie nach dem Erstversand: beansprucht, abgeschlossen, Status „Eingeladen“."""
    jetzt = timezone.now()
    FactionMeeting.objects.filter(pk=meeting.pk).update(
        status="invited", invitation_sent=True, invitation_sent_at=jetzt, invitation_claimed_at=jetzt
    )
    meeting.refresh_from_db()
    return meeting


def _formular(schedule: FactionMeetingSchedule, eingeladene: bool = True, **felder: Any) -> dict[str, str]:
    daten = {
        "section": "update_schedule",
        "schedule_id": str(schedule.id),
        "name": schedule.name,
        "recurrence": schedule.recurrence,
        "weekday": str(schedule.weekday),
        "time": schedule.time.strftime("%H:%M"),
        "duration_minutes": str(schedule.duration_minutes),
        "default_location": schedule.default_location,
        "default_video_link": schedule.default_video_link,
    }
    if eingeladene:
        daten["eingeladene"] = "1"
    daten.update({k: str(v) for k, v in felder.items()})
    return daten


def _ortszeit(meeting: FactionMeeting) -> time:
    return timezone.localtime(meeting.start).time()


def _meldungen(response: Any) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


def _nachrichten(membership: Any) -> list[Notification]:
    return list(Notification.objects.filter(recipient=membership, notification_type=GEAENDERT))


def _teilnahmen(meeting: FactionMeeting) -> list[tuple[Any, ...]]:
    return list(meeting.attendances.order_by("pk").values_list("membership_id", "status", "responded_at"))


# =============================================================================
# Häkchen an und aus
# =============================================================================


def test_mit_haekchen_aendert_sich_die_eingeladene_sitzung_und_die_eingeladenen_erfahren_es(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks, rsvp_enabled=True)
    eingeladen = _einladen(_termine(schedule)[0])
    FactionAttendance.objects.filter(meeting=eingeladen, membership=mitglied).update(
        status="confirmed", responded_at=timezone.now()
    )
    teilnahmen = _teilnahmen(eingeladen)
    sequenz = eingeladen.invitation_sequence
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(vorsitz.user).post(
            _url(org), _formular(schedule, time="19:30", default_location="Rathaus")
        )

    assert response.status_code == 302
    eingeladen.refresh_from_db()
    assert _ortszeit(eingeladen) == time(19, 30) and eingeladen.location == "Rathaus"
    assert eingeladen.status == "invited" and eingeladen.invitation_sequence == sequenz + 1
    # Zu- und Absagen bleiben stehen
    assert _teilnahmen(eingeladen) == teilnahmen
    (nachricht,) = _nachrichten(mitglied)
    assert "neuer Beginn 19:30 Uhr" in nachricht.message and "neuer Ort: Rathaus" in nachricht.message
    assert "Zu- oder Absage" in nachricht.message
    # Das Ende wandert mit dem Beginn (Dauer gleich) und steht daher nicht eigens in der Nachricht
    assert "neues Ende" not in nachricht.message
    assert nachricht.link.endswith(f"/faction/{eingeladen.id}/")
    # Wer die Reihe ändert, bekommt keine Nachricht; die E-Mail geht an das Mitglied
    assert not _nachrichten(vorsitz)
    assert [m.to for m in mail.outbox] == [[mitglied.user.email]]
    assert "Rathaus" in mail.outbox[0].body
    assert any("1 eingeladene Sitzung mit Nachricht an die Eingeladenen" in m for m in _meldungen(response))


def test_ohne_haekchen_bleibt_die_eingeladene_sitzung_wie_bisher(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    eingeladen = _einladen(_termine(schedule)[0])
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(vorsitz.user).post(
            _url(org), _formular(schedule, eingeladene=False, time="19:30", default_location="Rathaus")
        )

    eingeladen.refresh_from_db()
    assert _ortszeit(eingeladen) == time(18, 0) and eingeladen.location == "Büro"
    assert not Notification.objects.filter(notification_type=GEAENDERT).exists()
    assert mail.outbox == []
    assert any("1 eingeladene Sitzung bleibt unverändert" in m for m in _meldungen(response))
    assert all(m.location == "Rathaus" for m in _termine(schedule) if m.pk != eingeladen.pk)


# =============================================================================
# Was bleibt, wie es ist
# =============================================================================


def test_einzeln_geaenderte_angaben_der_eingeladenen_sitzung_bleiben(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    eingeladen = _einladen(_termine(schedule)[0])
    FactionMeeting.objects.filter(pk=eingeladen.pk).update(location="Ratssaal")
    client = client_for(vorsitz.user)

    # Nur der Ort der Reihe ändert sich: Die Sitzung hat ihren eigenen Ort, niemand erhält eine Nachricht
    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(org), _formular(schedule, default_location="Rathaus"))
    eingeladen.refresh_from_db()
    assert eingeladen.location == "Ratssaal" and eingeladen.invitation_sequence == 0
    assert not _nachrichten(mitglied)

    # Der Beginn folgt der Reihe, der eigene Ort bleibt; die Nachricht nennt nur den Beginn
    schedule.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(org), _formular(schedule, time="19:30"))
    eingeladen.refresh_from_db()
    assert eingeladen.location == "Ratssaal" and _ortszeit(eingeladen) == time(19, 30)
    (nachricht,) = _nachrichten(mitglied)
    assert "neuer Beginn 19:30 Uhr" in nachricht.message and "neuer Ort" not in nachricht.message


def test_laufende_vergangene_und_von_hand_abgesagte_sitzungen_bleiben(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    laufend, abgesagt, geplant = _termine(schedule)
    jetzt = timezone.now()
    FactionMeeting.objects.filter(pk=laufend.pk).update(
        status="ongoing", started_at=jetzt, invitation_sent=True, invitation_sent_at=jetzt
    )
    FactionMeeting.objects.filter(pk=abgesagt.pk).update(
        status="cancelled", cancellation_reason="Krankheit", invitation_sent=True, invitation_sent_at=jetzt
    )
    gestern = timezone.localdate() - timedelta(days=1)
    beginn = timezone.make_aware(datetime.combine(gestern, time(18, 0)))
    vergangen = FactionMeeting.objects.create(
        organization=org,
        schedule=schedule,
        scheduled_date=gestern,
        title="Fraktionssitzung",
        start=beginn,
        end=beginn + timedelta(hours=2),
        location="Büro",
        status="invited",
        invitation_sent=True,
        invitation_sent_at=jetzt - timedelta(days=3),
    )

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, time="19:30", default_location="Rathaus"))

    for meeting, status in ((laufend, "ongoing"), (abgesagt, "cancelled"), (vergangen, "invited")):
        meeting.refresh_from_db()
        assert meeting.status == status
        assert meeting.location == "Büro" and _ortszeit(meeting) == time(18, 0)
    geplant.refresh_from_db()
    assert geplant.location == "Rathaus" and _ortszeit(geplant) == time(19, 30)
    assert not Notification.objects.filter(notification_type=GEAENDERT).exists()


def test_rhythmuswechsel_verschiebt_eingeladene_sitzung_nie_auf_eine_pause_und_sagt_sie_nie_ab(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks, rsvp_enabled=True)
    eingeladen = _einladen(_termine(schedule)[0])
    FactionAttendance.objects.filter(meeting=eingeladen, membership=mitglied).update(
        status="declined", responded_at=timezone.now()
    )
    tag = eingeladen.scheduled_date
    assert tag is not None
    # Der nächstgelegene Tag im neuen Rhythmus liegt in einer Pause
    FactionMeetingException.objects.create(
        schedule=schedule, original_date=tag + timedelta(days=1), exception_type="cancelled", reason="Klausur"
    )
    neuer_tag = (schedule.weekday + 1) % 7

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, weekday=neuer_tag))

    eingeladen.refresh_from_db()
    assert eingeladen.status == "invited" and eingeladen.cancellation_reason == ""
    assert eingeladen.scheduled_date == tag + timedelta(days=8)
    assert timezone.localtime(eingeladen.start).date() == tag + timedelta(days=8)
    # Auch wer abgesagt hat, erfährt den neuen Termin; die Absage bleibt stehen
    (nachricht,) = _nachrichten(mitglied)
    assert "neuer Termin" in nachricht.message and "Zu- oder Absage" in nachricht.message
    assert FactionAttendance.objects.get(meeting=eingeladen, membership=mitglied).status == "declined"


# =============================================================================
# Nachricht: Inhalte und Einstellungen
# =============================================================================


def test_nachricht_ohne_nichtoeffentliche_inhalte(
    org: Any,
    vorsitz: Any,
    make_member: Any,
    client_for: Any,
    django_capture_on_commit_callbacks: CaptureCallbacks,
) -> None:
    vereidigt = make_member(
        org, ["faction.view", "faction.view_public", "faction.view_non_public"], email="vereidigt@example.org"
    )
    vereidigt.is_sworn_in = True
    vereidigt.save(update_fields=["is_sworn_in"])
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    eingeladen = _einladen(_termine(schedule)[0])
    FactionAgendaItem.objects.create(meeting=eingeladen, number="3", title=NOE_TITEL, visibility="internal", order=9)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, default_location="Rathaus"))

    nachrichten = list(Notification.objects.filter(notification_type=GEAENDERT))
    assert [n.recipient_id for n in nachrichten] == [vereidigt.pk]
    assert all(NOE_TITEL not in n.title and NOE_TITEL not in n.message for n in nachrichten)
    assert mail.outbox
    for nachricht in mail.outbox:
        inhalte = [nachricht.subject, str(nachricht.body), *(str(a[0]) for a in getattr(nachricht, "alternatives", []))]
        assert all(NOE_TITEL not in inhalt for inhalt in inhalte)


def test_nachricht_folgt_den_benachrichtigungseinstellungen(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    NotificationPreference.objects.create(
        membership=mitglied, type_settings={GEAENDERT.value: {"in_app": True, "email": False}}
    )
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    _einladen(_termine(schedule)[0])
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, default_location="Rathaus"))

    assert len(_nachrichten(mitglied)) == 1
    assert mail.outbox == []


# =============================================================================
# Vorschau, laufender Versand, Seite
# =============================================================================


def test_vorschau_nennt_eingeladene_sitzungen_einzeln_und_speichert_nichts(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    eingeladen = _einladen(_termine(schedule)[0])
    client = client_for(vorsitz.user)

    mit = client.post(_url(org), {**_formular(schedule, time="20:00"), "vorschau": "1"}, **HTMX).content.decode()
    ohne = client.post(
        _url(org), {**_formular(schedule, eingeladene=False, time="20:00"), "vorschau": "1"}, **HTMX
    ).content.decode()

    tag = timezone.localtime(eingeladen.start).strftime("%d.%m.%Y")
    assert "Ändert 3 kommende Termine: 1 eingeladene Sitzung mit Nachricht an die Eingeladenen, 2 angepasst." in mit
    liste = mit.split('data-testid="fs-reihe-eingeladene"', 1)[1]
    assert f"{tag}, 18:00 Uhr" in liste and "neuer Beginn 20:00 Uhr" in liste
    assert 'data-testid="fs-reihe-eingeladene"' not in ohne
    assert "1 eingeladene Sitzung bleibt unverändert" in ohne
    eingeladen.refresh_from_db()
    schedule.refresh_from_db()
    assert _ortszeit(eingeladen) == time(18, 0) and schedule.time == time(18, 0)
    assert not Notification.objects.filter(notification_type=GEAENDERT).exists()


def test_laufender_einladungsversand_haelt_die_aenderung_auf(
    org: Any, vorsitz: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    im_versand = _termine(schedule)[0]
    # Der Einladungslauf hat den Versand beansprucht, aber noch nicht abgeschlossen
    FactionMeeting.objects.filter(pk=im_versand.pk).update(invitation_sent=True, invitation_claimed_at=timezone.now())
    client = client_for(vorsitz.user)

    vorschau = client.post(_url(org), {**_formular(schedule, time="19:30"), "vorschau": "1"}, **HTMX)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(org), _formular(schedule, time="19:30"))

    assert "wird gerade eingeladen" in vorschau.content.decode()
    assert any("wird gerade eingeladen" in m for m in _meldungen(response))
    schedule.refresh_from_db()
    assert schedule.time == time(18, 0)
    assert {_ortszeit(m) for m in _termine(schedule)} == {time(18, 0)}

    # Ohne Häkchen wie bisher: Die Sitzung im Versand bleibt außen vor, die übrigen folgen der Reihe
    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(org), _formular(schedule, eingeladene=False, time="19:30"))
    schedule.refresh_from_db()
    im_versand.refresh_from_db()
    assert schedule.time == time(19, 30) and _ortszeit(im_versand) == time(18, 0)
    assert not Notification.objects.filter(notification_type=GEAENDERT).exists()


def test_seite_zeigt_knopf_bearbeiten_und_haekchen_an(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)

    seite = client_for(vorsitz.user).get(_url(org)).content.decode()

    bearbeiten = seite.split('data-testid="fs-reihe-bearbeiten"', 1)[1]
    assert bearbeiten.lstrip(">").lstrip().startswith('<summary class="wf-knopf wf-klein">Bearbeiten')
    haken = seite.split('name="eingeladene"', 1)[1].split(">", 1)[0]
    assert f'id="reihe-eingeladene-{schedule.id}"' in haken and 'value="1"' in haken and "checked" in haken
    assert "Auch Sitzungen ändern, zu denen schon eingeladen wurde, und die Eingeladenen informieren" in seite
