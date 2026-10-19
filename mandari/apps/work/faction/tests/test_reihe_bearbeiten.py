# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe bearbeiten, Erinnerung an die Sitzung und gelöschte Termine (Issue #1071, Muster einstellungen.html).

- Bearbeiten wirkt nur auf künftige, nicht eingeladene Termine; eingeladene bleiben. Einzeln geänderte Angaben am
  Termin bleiben. Bei einem Rhythmuswechsel wandern die Termine, Termine mit Inhalten werden nie gelöscht. Vorschau
  zählt, ohne zu speichern. Videolink nur als http(s)-Adresse. Audit-Eintrag „Sitzungsreihe geändert“.
- Rechte und Mandantentrennung: nur mit faction.manage, nur Reihen und Ausnahmen der eigenen Organisation.
- Gelöschte Termine merkt die Reihe als Ausnahme „Gelöscht“; die Erzeugung legt sie nicht wieder an, auch nicht nach
  einem Neubeginn. „Wiederherstellen“ entfernt die Ausnahme und legt den Termin wieder an.
- Erinnerung an die Sitzung: einstellbar (an/aus, Vorlauf), Ergebnis je Person, keine Doppelversände.
- Kleinfix: Textfelder mit eigenem Sofort-Speichern tragen data-sofort-tippen (ihr change löst den Abschnitt nicht aus).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, time, timedelta
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction import deletion, generation, reihe
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionAuditLog,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
)
from apps.work.faction.services import run_faction_reminder_pass

pytestmark = pytest.mark.django_db

CaptureCallbacks = Callable[..., Any]
HORIZONT = 21
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = HORIZONT


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["faction.manage", "faction.view_public", "faction.delete", "faction.view"], email="vorsitz@example.org"
    )


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "faction.view_public", "faction.view"], email="mitglied@example.org")


def _url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


def _morgen() -> date:
    return timezone.localdate() + timedelta(days=1)


def _reihe(org: Any, capture: CaptureCallbacks, **felder: Any) -> FactionMeetingSchedule:
    """Wöchentliche Reihe am Wochentag von morgen, 18:00, mit sofort angelegten Terminen (3 im Horizont)."""
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


def _formular(schedule: FactionMeetingSchedule, **felder: Any) -> dict[str, str]:
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
    daten.update({k: str(v) for k, v in felder.items()})
    return daten


def _ortszeit(meeting: FactionMeeting) -> time:
    return timezone.localtime(meeting.start).time()


def _meldungen(response: Any) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


# =============================================================================
# Sitzungsreihe bearbeiten
# =============================================================================


def test_uhrzeit_und_ort_wirken_nur_auf_nicht_eingeladene_termine(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    eingeladen, einzeln, normal = _termine(schedule)
    FactionMeeting.objects.filter(pk=eingeladen.pk).update(status="invited", invitation_sent=True)
    FactionMeeting.objects.filter(pk=einzeln.pk).update(location="Rathaus, Raum 3")

    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(vorsitz.user).post(
            _url(org), _formular(schedule, time="19:30", default_location="Neues Büro")
        )

    assert response.status_code == 302
    eingeladen.refresh_from_db()
    einzeln.refresh_from_db()
    normal.refresh_from_db()
    assert _ortszeit(eingeladen) == time(18, 0) and eingeladen.location == "Büro"
    assert _ortszeit(einzeln) == time(19, 30) and einzeln.location == "Rathaus, Raum 3"
    assert _ortszeit(normal) == time(19, 30) and normal.location == "Neues Büro"
    assert normal.end is not None and normal.end - normal.start == timedelta(minutes=120)
    schedule.refresh_from_db()
    assert schedule.time == time(19, 30) and schedule.default_location == "Neues Büro"
    assert any("Ändert 2 kommende Termine" in m and "1 eingeladene Sitzung bleibt" in m for m in _meldungen(response))
    assert FactionAuditLog.objects.filter(action="schedule_changed", object_id=schedule.pk).exists()


def test_rhythmuswechsel_verschiebt_termine_ohne_datenverlust(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    vorher = _termine(schedule)
    nummern = sorted(m.meeting_number for m in vorher)
    mit_inhalt = vorher[1]
    eigener = FactionAgendaItem.objects.create(
        meeting=mit_inhalt, number="2", title="Haushalt 2027", visibility="public", order=5
    )
    neuer_tag = (schedule.weekday + 1) % 7

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, weekday=neuer_tag))

    nachher = _termine(schedule)
    assert {m.scheduled_date.weekday() for m in nachher if m.scheduled_date} == {neuer_tag}
    assert {m.start.astimezone(timezone.get_default_timezone()).weekday() for m in nachher} == {neuer_tag}
    # Dieselben Sitzungen, nur verschoben: Nummern bleiben, nichts gelöscht, der eigene TOP hängt noch am Termin
    assert {m.pk for m in vorher} <= {m.pk for m in nachher}
    assert sorted(m.meeting_number for m in nachher if m.meeting_number)[: len(nummern)] == nummern
    eigener.refresh_from_db()
    assert eigener.meeting_id == mit_inhalt.pk


def test_seltenerer_rhythmus_entfernt_nur_leere_termine(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    termine = _termine(schedule)
    mit_inhalt = termine[2]
    FactionAgendaItem.objects.create(
        meeting=mit_inhalt, number="2", title="Antrag Radweg", visibility="public", order=5
    )
    eingeladen = termine[0]
    FactionMeeting.objects.filter(pk=eingeladen.pk).update(status="invited", invitation_sent=True)

    with django_capture_on_commit_callbacks(execute=True):
        client_for(vorsitz.user).post(_url(org), _formular(schedule, recurrence="monthly_last"))

    uebrig = {m.pk for m in _termine(schedule)}
    # Eingeladene Sitzung und Termin mit Inhalten bleiben in jedem Fall; nur der leere mittlere darf wegfallen
    assert eingeladen.pk in uebrig and mit_inhalt.pk in uebrig
    assert FactionAgendaItem.objects.filter(meeting=mit_inhalt, title="Antrag Radweg").exists()


def test_vorschau_zaehlt_und_speichert_nichts(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)

    response = client_for(vorsitz.user).post(_url(org), {**_formular(schedule, time="20:00"), "vorschau": "1"}, **HTMX)

    assert response.status_code == 200
    assert "Ändert 3 kommende Termine: 3 angepasst." in response.content.decode()
    schedule.refresh_from_db()
    assert schedule.time == time(18, 0)
    assert {_ortszeit(m) for m in _termine(schedule)} == {time(18, 0)}


def test_vorschau_meldet_ungueltigen_videolink(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    formular = _formular(schedule, default_video_link="javascript:alert(1)")

    vorschau = client_for(vorsitz.user).post(_url(org), {**formular, "vorschau": "1"}, **HTMX)
    response = client_for(vorsitz.user).post(_url(org), formular)

    assert "https://" in vorschau.content.decode()
    assert any("https://" in m for m in _meldungen(response))
    schedule.refresh_from_db()
    assert schedule.default_video_link == ""


def test_plan_ohne_aenderung_aendert_nichts(org: Any, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    plan = reihe.plan(schedule, reihe.werte_aus(_formular(schedule)))
    assert plan.zahl == 0 and not plan.reihe_felder
    assert plan.satz() == "Keine Änderung."


def test_mitglied_darf_reihe_nicht_bearbeiten_und_nichts_wiederherstellen(
    org: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    ausnahme = FactionMeetingException.objects.create(
        schedule=schedule, original_date=_morgen() + timedelta(days=7), exception_type="deleted"
    )
    client = client_for(mitglied.user)

    assert client.post(_url(org), _formular(schedule, time="07:00")).status_code == 403
    assert client.post(_url(org), {**_formular(schedule, time="07:00"), "vorschau": "1"}, **HTMX).status_code == 403
    assert (
        client.post(_url(org), {"section": "restore_deleted_date", "exception_id": str(ausnahme.id)}).status_code == 403
    )
    schedule.refresh_from_db()
    assert schedule.time == time(18, 0)
    assert FactionMeetingException.objects.filter(pk=ausnahme.pk).exists()


def test_reihe_und_ausnahme_anderer_organisation_bleiben_unberuehrt(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    fremd = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    fremde_reihe = _reihe(fremd, django_capture_on_commit_callbacks)
    fremde_ausnahme = FactionMeetingException.objects.create(
        schedule=fremde_reihe, original_date=_morgen() + timedelta(days=7), exception_type="deleted"
    )
    client = client_for(vorsitz.user)

    client.post(_url(org), _formular(fremde_reihe, time="07:00"))
    vorschau = client.post(_url(org), {**_formular(fremde_reihe, time="07:00"), "vorschau": "1"}, **HTMX)
    client.post(_url(org), {"section": "restore_deleted_date", "exception_id": str(fremde_ausnahme.id)})

    assert "nicht gefunden" in vorschau.content.decode()
    fremde_reihe.refresh_from_db()
    assert fremde_reihe.time == time(18, 0)
    assert FactionMeetingException.objects.filter(pk=fremde_ausnahme.pk).exists()


# =============================================================================
# Gelöschte Termine merken
# =============================================================================


def test_geloeschter_termin_kommt_nicht_wieder_und_laesst_sich_wiederherstellen(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    termin = _termine(schedule)[1]
    tag = termin.scheduled_date
    assert tag is not None

    response = client_for(vorsitz.user).post(
        reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": termin.id}),
        {"action": "delete", "confirmation": termin.start.strftime("%d.%m.%Y"), "reason": "Herbstferien"},
    )

    assert response.status_code == 302
    ausnahme = FactionMeetingException.objects.get(schedule=schedule, original_date=tag)
    assert ausnahme.exception_type == "deleted" and ausnahme.reason == "Herbstferien"
    assert ausnahme.created_by_name
    assert any("nicht wieder an" in m for m in _meldungen(response))

    # Auch wenn die Reihe von vorn beginnt (nach einem Rhythmuswechsel), bleibt der Tag frei
    FactionMeetingSchedule.objects.filter(pk=schedule.pk).update(generated_until=None)
    schedule.refresh_from_db()
    generation.generate_meetings_for_schedule(schedule)
    assert not FactionMeeting.objects.filter(schedule=schedule, scheduled_date=tag).exists()

    # Einstellungen und Liste zeigen ihn mit „Wiederherstellen“
    einstellungen = client_for(vorsitz.user).get(_url(org)).content.decode()
    liste = client_for(vorsitz.user).get(reverse("work:faction", kwargs={"org_slug": org.slug})).content.decode()
    for seite in (einstellungen, liste):
        assert 'data-testid="fs-geloescht"' in seite and "Herbstferien" in seite and "Wiederherstellen" in seite

    response = client_for(vorsitz.user).post(
        _url(org), {"section": "restore_deleted_date", "exception_id": str(ausnahme.id), "zurueck": "liste"}
    )
    assert response.status_code == 302 and response.url == reverse("work:faction", kwargs={"org_slug": org.slug})
    assert not FactionMeetingException.objects.filter(pk=ausnahme.pk).exists()
    assert FactionMeeting.objects.get(schedule=schedule, scheduled_date=tag).status == "planned"


def test_mitglied_sieht_geloeschte_termine_ohne_wiederherstellen(
    org: Any, mitglied: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    schedule = _reihe(org, django_capture_on_commit_callbacks)
    FactionMeetingException.objects.create(
        schedule=schedule, original_date=_morgen() + timedelta(days=7), exception_type="deleted", reason="Ratssitzung"
    )

    liste = client_for(mitglied.user).get(reverse("work:faction", kwargs={"org_slug": org.slug})).content.decode()

    assert "Ratssitzung" in liste
    assert "restore_deleted_date" not in liste


def test_vergangene_und_reihenlose_termine_werden_nicht_gemerkt(org: Any, vorsitz: Any) -> None:
    einzeln = FactionMeeting.objects.create(
        organization=org, title="Sondersitzung", start=timezone.now() + timedelta(days=3), status="planned"
    )
    deletion.delete_meeting(einzeln, vorsitz, "Sondersitzung")
    assert not FactionMeetingException.objects.exists()


# =============================================================================
# Erinnerung an die Sitzung
# =============================================================================


def _eingeladen(org: Any, stunden: int) -> FactionMeeting:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(hours=stunden),
        status="invited",
        invitation_sent=True,
    )
    for membership in org.memberships.filter(is_active=True, is_guest=False):
        FactionAttendance.objects.create(meeting=meeting, membership=membership, status="invited")
    return meeting


def _einstellen(org: Any, **faction: Any) -> None:
    org.settings = {**(org.settings or {}), "faction": {**(org.settings or {}).get("faction", {}), **faction}}
    org.save(update_fields=["settings"])


def test_erinnerung_einmal_je_person_mit_ergebnis(org: Any, vorsitz: Any, mitglied: Any) -> None:
    meeting = _eingeladen(org, 30)
    mail.outbox = []

    run_faction_reminder_pass()
    erste = sorted(m.to[0] for m in mail.outbox)
    # Auch ein zweiter Lauf (etwa nach einem Abbruch, Sitzung wieder offen) schickt niemandem eine zweite Erinnerung
    FactionMeeting.objects.filter(pk=meeting.pk).update(reminder_sent_at=None)
    run_faction_reminder_pass()

    assert erste == sorted([vorsitz.user.email, mitglied.user.email])
    assert len(mail.outbox) == 2
    assert all(a.reminder_mailed_at and not a.reminder_error for a in meeting.attendances.all())


def test_erinnerung_ausgeschaltet_oder_spaeter(org: Any, vorsitz: Any) -> None:
    meeting = _eingeladen(org, 30)
    mail.outbox = []

    _einstellen(org, meeting_reminder_enabled=False)
    run_faction_reminder_pass()
    assert mail.outbox == []

    _einstellen(org, meeting_reminder_enabled=True, meeting_reminder_hours=24)
    run_faction_reminder_pass()
    assert mail.outbox == []
    meeting.refresh_from_db()
    assert meeting.reminder_sent_at is None

    run_faction_reminder_pass(now=meeting.start - timedelta(hours=23))
    assert [m.to for m in mail.outbox] == [[vorsitz.user.email]]


def test_erinnerung_ohne_adresse_mit_grund(org: Any, vorsitz: Any) -> None:
    vorsitz.user.email = ""
    vorsitz.user.save(update_fields=["email"])
    meeting = _eingeladen(org, 10)

    run_faction_reminder_pass()

    attendance = meeting.attendances.get(membership=vorsitz)
    assert attendance.reminder_mailed_at is None
    assert attendance.reminder_error == "Keine E-Mail-Adresse hinterlegt."


def test_erinnerung_sofort_speichern_und_formular_ohne_feld(org: Any, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    felder = "meeting_reminder_enabled,meeting_reminder_hours"

    antwort = client.post(
        _url(org), {"section": "save_settings", "felder": felder, "meeting_reminder_hours": "24"}, **HTMX
    )
    falsch = client.post(
        _url(org), {"section": "save_settings", "felder": felder, "meeting_reminder_hours": "0"}, **HTMX
    )
    # Das Formular als Ganzes ohne die neuen Felder (bisherige Vorlage) lässt die Erinnerung, wie sie ist
    client.post(_url(org), {"invitation_lead_hours": "72"})

    assert antwort.status_code == 204 and falsch.status_code == 400
    org.refresh_from_db()
    faction = org.settings["faction"]
    assert faction["meeting_reminder_enabled"] is False and faction["meeting_reminder_hours"] == 24


# =============================================================================
# Einstellungsseite: Abschnitte und Kleinfix
# =============================================================================


def test_seite_mit_erinnerung_noe_und_bearbeiten(
    org: Any, vorsitz: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    _reihe(org, django_capture_on_commit_callbacks)
    seite = client_for(vorsitz.user).get(_url(org)).content.decode()

    assert "Erinnerung an die Sitzung" in seite and 'name="meeting_reminder_hours"' in seite
    assert 'data-testid="fs-reihe-bearbeiten"' in seite and 'value="update_schedule"' in seite
    assert 'data-testid="fs-einst-noe"' in seite and "Nichtöffentliche Vorgänge" in seite
    # Nicht vereidigt: keine Dateizahl, kein Link in den Ordner
    assert "Ordner öffnen" not in seite
    # Kleinfix: Textfelder mit eigenem hx-post sind gekennzeichnet (ihr change löst den Abschnitt nicht aus)
    for name in ("invitation_lead_hours", "meeting_reminder_hours", "agenda_reminder_hours"):
        feld = seite.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        assert "data-sofort-tippen" in feld, name


def test_noe_ordner_fuer_vereidigte(org: Any, make_member: Any, client_for: Any) -> None:
    from apps.work.motions.models import DocumentFolder

    vereidigt = make_member(org, ["faction.manage", "faction.view_public", "faction.view_non_public"])
    vereidigt.is_sworn_in = True
    vereidigt.save(update_fields=["is_sworn_in"])
    ordner = DocumentFolder.non_public_folder(org, created_by=vereidigt)

    seite = client_for(vereidigt.user).get(_url(org)).content.decode()

    assert "zurzeit 1 von 1" in seite
    assert f"?ordner={ordner.id}" in seite and "0 Dateien" in seite
