# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokollkette der Fraktionssitzungen (Issue #1295): Die vorherige Sitzung ist der Termin direkt davor.

Bis #1295 verkettete Work eine neue Sitzung mit der zuletzt begonnenen oder abgeschlossenen. Reihentermine entstehen
aber Wochen im Voraus: Die auf einmal angelegten blieben unverkettet („Tagesordnung festlegen“ ohne Protokoll), der
später am Rand des Horizonts erzeugte Termin genehmigte das Protokoll der Sitzung, die in dieser Woche stattfand,
also rund drei Monate zu spät („Die Fraktion genehmigt das Protokoll in Nr. 16 am Mo 08.02.“ für die Sitzung vom
09.11.).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.faction import kette, protokoll
from apps.work.faction.generation import generate_meetings_for_schedule, run_faction_schedule_pass
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionMeetingSchedule, FactionMeetingType
from apps.work.faction.services import ProtocolApprovalService

# Dienstag, 05.03.2030, 12:00 Uhr Ortszeit, und der Montag davor um 18:00 Uhr
JETZT = datetime(2030, 3, 5, 11, 0, tzinfo=UTC)
MONTAG = datetime(2030, 3, 4, 17, 0, tzinfo=UTC)

MIT_PROTOKOLL = "Tagesordnung festlegen und letztes Protokoll genehmigen"
OHNE_PROTOKOLL = "Tagesordnung festlegen"


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = 21


@pytest.fixture
def standardart(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.get(organization=org, is_default=True)


def _sitzung(org: Any, start: datetime, *, top: bool = True, **felder: Any) -> FactionMeeting:
    """Sitzung wie von Hand angelegt: gespeichert, danach der automatische erste TOP."""
    sitzung = FactionMeeting.objects.create(
        organization=org, start=start, **{"title": "Fraktionssitzung", "status": "planned", **felder}
    )
    if top:
        cast(Any, ProtocolApprovalService).auto_create_approval_item(sitzung)
    return sitzung


def _wochen(org: Any, zahl: int, **felder: Any) -> list[FactionMeeting]:
    return [_sitzung(org, MONTAG + timedelta(weeks=woche), **felder) for woche in range(zahl)]


def _vorherige(*sitzungen: FactionMeeting) -> list[Any]:
    return [FactionMeeting.objects.get(pk=s.pk).previous_meeting_id for s in sitzungen]


def _top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.get(meeting=sitzung, is_approval_item=True)


# =============================================================================
# Anlegen
# =============================================================================


@pytest.mark.django_db
def test_reihe_verkettet_jeden_termin_mit_dem_davor(org: Any) -> None:
    reihe = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0)
    )

    generate_meetings_for_schedule(reihe, now=JETZT)

    termine = list(FactionMeeting.objects.filter(schedule=reihe).order_by("start"))
    assert len(termine) == 3
    assert [t.previous_meeting_id for t in termine] == [None, termine[0].pk, termine[1].pk]
    assert [_top(t).title for t in termine] == [OHNE_PROTOKOLL, MIT_PROTOKOLL, MIT_PROTOKOLL]
    assert [_top(t).approves_meeting_id for t in termine] == [None, termine[0].pk, termine[1].pk]


@pytest.mark.django_db
def test_termin_am_rand_des_horizonts_genehmigt_nicht_die_sitzung_dieser_woche(org: Any) -> None:
    """Der Fall aus dem Issue: Die erste Sitzung ist vorbei, eine Woche später entsteht der nächste Randtermin."""
    reihe = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0)
    )
    generate_meetings_for_schedule(reihe, now=MONTAG - timedelta(days=1))
    erste = FactionMeeting.objects.filter(schedule=reihe).order_by("start").first()
    assert erste is not None
    erste.status = "completed"
    erste.save()

    generate_meetings_for_schedule(reihe, now=JETZT + timedelta(days=7))

    termine = list(FactionMeeting.objects.filter(schedule=reihe).order_by("start"))
    assert len(termine) == 5
    assert [t.previous_meeting_id for t in termine[1:]] == [t.pk for t in termine[:-1]]
    g = protokoll.genehmigung(erste)
    assert g.sitzung == termine[1], "die nächste Sitzung der Reihe, nicht der Termin am Rand des Horizonts"
    assert FactionMeeting.objects.get(pk=erste.pk).protocol_status == "pending"


@pytest.mark.django_db
def test_von_hand_eingeschobene_sitzung_reiht_sich_ein(org: Any, make_member: Any, client_for: Any) -> None:
    vorsitz = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "faction.create", "faction.manage"],
        email="v@example.org",
    )
    erste, zweite = _wochen(org, 2)

    daten = {"title": "Sondersitzung", "start_date": "2030-03-07", "start_time": "18:00"}
    antwort = client_for(vorsitz.user).post(reverse("work:faction", kwargs={"org_slug": org.slug}), daten)

    assert antwort.status_code == 302
    sonder = FactionMeeting.objects.get(organization=org, title="Sondersitzung")
    assert _vorherige(erste, sonder, zweite) == [None, erste.pk, sonder.pk]
    assert _top(sonder).approves_meeting_id == erste.pk
    assert _top(sonder).title == MIT_PROTOKOLL
    assert _top(zweite).approves_meeting_id == sonder.pk


# =============================================================================
# Absagen, wieder ansetzen, verschieben, löschen
# =============================================================================


@pytest.mark.django_db
def test_abgesagter_termin_wird_uebersprungen_und_kehrt_zurueck(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)
    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]

    zweite.status = "cancelled"
    zweite.save()

    assert _vorherige(erste, zweite, dritte) == [None, None, erste.pk]
    assert _top(dritte).approves_meeting_id == erste.pk
    assert _top(zweite).approves_meeting_id is None
    assert _top(zweite).title == OHNE_PROTOKOLL
    # Die gespeicherte Sitzung trägt den neuen Stand: Erneutes Speichern schreibt die alte Zuordnung nicht zurück
    assert zweite.previous_meeting_id is None
    zweite.save()
    assert _vorherige(erste, zweite, dritte) == [None, None, erste.pk]

    zweite.status = "planned"
    zweite.save()

    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]
    assert _top(zweite).title == MIT_PROTOKOLL
    assert _top(dritte).approves_meeting_id == zweite.pk


@pytest.mark.django_db
def test_absage_des_ersten_termins_nimmt_dem_zweiten_das_protokoll(org: Any) -> None:
    erste, zweite = _wochen(org, 2)

    erste.status = "cancelled"
    erste.save(update_fields=["status", "updated_at"])

    assert _vorherige(erste, zweite) == [None, None]
    assert _top(zweite).title == OHNE_PROTOKOLL
    assert _top(zweite).approves_meeting_id is None


@pytest.mark.django_db
def test_geloeschter_termin_schliesst_die_kette(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)

    zweite.delete()

    assert _vorherige(erste, dritte) == [None, erste.pk]
    assert _top(dritte).approves_meeting_id == erste.pk


@pytest.mark.django_db
def test_verschobener_termin_wechselt_die_stelle(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)

    erste.start = dritte.start + timedelta(days=2)
    erste.save(update_fields=["start", "updated_at"])

    assert _vorherige(zweite, dritte, erste) == [None, zweite.pk, dritte.pk]
    assert [_top(s).approves_meeting_id for s in (zweite, dritte, erste)] == [None, zweite.pk, dritte.pk]
    assert _top(zweite).title == OHNE_PROTOKOLL


@pytest.mark.django_db
def test_ohne_aenderung_an_status_beginn_oder_art_wird_nicht_geordnet(org: Any) -> None:
    erste, zweite = _wochen(org, 2)
    # Am Speichern vorbei abgesagt: Die Kette ist veraltet, bis jemand sie ordnet
    FactionMeeting.objects.filter(pk=erste.pk).update(status="cancelled")
    erste.refresh_from_db()

    erste.title = "Umbenannt"
    erste.save(update_fields=["title", "updated_at"])
    erste.description = "Nur Text"
    erste.save()

    assert _vorherige(zweite) == [erste.pk], "Titel und Beschreibung: keine Neuordnung durch das Speichern"
    assert kette.alle_ordnen() == 1
    assert _vorherige(zweite) == [None]


# =============================================================================
# Was feststeht, bleibt
# =============================================================================


@pytest.mark.django_db
def test_begonnene_und_abgeschlossene_sitzungen_behalten_ihre_zuordnung(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)
    for sitzung, status in ((erste, "completed"), (zweite, "ongoing")):
        sitzung.status = status
        sitzung.save()

    # Eine nachträglich angelegte Sitzung zwischen zwei feststehenden ändert deren Zuordnung nicht
    dazwischen = _sitzung(org, MONTAG + timedelta(days=3))

    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]
    assert _vorherige(dazwischen) == [None], "die Sitzung davor ist schon der laufenden zugeordnet"
    assert _top(zweite).approves_meeting_id == erste.pk


@pytest.mark.django_db
def test_bearbeiteter_genehmigungs_top_behaelt_titel_und_folgt_der_kette(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)
    top = _top(dritte)
    top.title = "Formalia"
    top.save()

    zweite.status = "cancelled"
    zweite.save()

    top.refresh_from_db()
    assert top.title == "Formalia"
    assert top.approves_meeting_id == erste.pk


@pytest.mark.django_db
def test_ohne_genehmigungs_top_bleibt_es_bei_der_zuordnung(org: Any) -> None:
    erste, zweite = (_sitzung(org, MONTAG + timedelta(weeks=woche), top=False) for woche in range(2))

    assert _vorherige(erste, zweite) == [None, erste.pk]
    assert not FactionAgendaItem.objects.filter(meeting__organization=org).exists()

    erste.status = "completed"
    erste.save()

    assert FactionMeeting.objects.get(pk=erste.pk).protocol_status == "draft", "niemand genehmigt es per TOP"


# =============================================================================
# Protokollstand und Genehmigung
# =============================================================================


@pytest.mark.django_db
def test_abgeschlossene_sitzung_steht_zur_genehmigung_in_der_folgenden(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    erste, zweite, dritte = _wochen(org, 3)
    assert erste.protocol_status == "draft"

    erste.status = "ongoing"
    erste.save()
    erste.status = "completed"
    erste.save()

    # Auch die gespeicherte Sitzung trägt den neuen Protokollstand (erneutes Speichern setzt ihn nicht zurück)
    assert erste.protocol_status == "pending"
    erste.save()
    erste.refresh_from_db()
    assert erste.protocol_status == "pending"
    g = protokoll.genehmigung(erste)
    assert (g.genehmigt, g.sitzung, g.top_nr) == (False, zweite, "1")

    # Die angenommene Abstimmung am TOP 1 der folgenden Sitzung genehmigt genau dieses Protokoll
    from apps.work.faction.services import apply_approval_item_decision

    class Angenommen:
        passed = True

    assert cast(Any, apply_approval_item_decision)(_top(zweite), Angenommen(), zweite, vorsitz) is True
    erste.refresh_from_db()
    assert erste.protocol_approved and erste.protocol_approved_in_id == zweite.pk
    assert FactionMeeting.objects.get(pk=dritte.pk).previous_meeting_id == zweite.pk


# =============================================================================
# Bestand von vor #1295
# =============================================================================


@pytest.mark.django_db
def test_erzeugungslauf_ordnet_den_bestand(org: Any, standardart: FactionMeetingType) -> None:
    """Stand wie in einer Organisation mit Reihe: abgeschlossene unverkettet, Randtermine falsch verkettet."""
    sitzungen = _wochen(org, 6, top=False)
    alt_1, alt_2, naechste, mitte, rand_1, rand_2 = sitzungen
    FactionMeeting.objects.filter(pk__in=[s.pk for s in sitzungen]).update(previous_meeting=None)
    FactionMeeting.objects.filter(pk__in=[alt_1.pk, alt_2.pk]).update(status="completed")
    FactionMeeting.objects.filter(pk=alt_1.pk).update(protocol_status="approved", protocol_approved=True)
    FactionMeeting.objects.filter(pk=rand_1.pk).update(previous_meeting=alt_1)
    FactionMeeting.objects.filter(pk=rand_2.pk).update(previous_meeting=alt_2)
    for sitzung in sitzungen:
        FactionMeeting.objects.get(pk=sitzung.pk).create_approval_agenda_item()
    assert _top(naechste).title == OHNE_PROTOKOLL and _top(rand_2).approves_meeting_id == alt_2.pk

    ergebnis = run_faction_schedule_pass(now=JETZT)

    assert ergebnis["relinked"] == 4
    assert _vorherige(*sitzungen) == [None, None, alt_2.pk, naechste.pk, mitte.pk, rand_1.pk]
    assert [_top(s).approves_meeting_id for s in sitzungen] == [None, None, alt_2.pk, naechste.pk, mitte.pk, rand_1.pk]
    assert _top(naechste).title == MIT_PROTOKOLL
    assert protokoll.genehmigung(FactionMeeting.objects.get(pk=alt_2.pk)).sitzung == naechste
    assert FactionMeeting.objects.get(pk=alt_2.pk).protocol_status == "pending"
    # Ein zweiter Lauf ändert nichts mehr
    assert kette.alle_ordnen() == 0
    assert kette.ordnen(org.pk, standardart.pk) == {}
