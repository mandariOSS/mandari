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
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import agenda, kette, protokoll, sitzung, vorbereiten
from apps.work.faction.generation import generate_meetings_for_schedule, run_faction_schedule_pass
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAuditLog,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionMeetingType,
)
from apps.work.faction.services import ProtocolApprovalService
from apps.work.organization import selectors, services

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
    neu = FactionMeeting.objects.create(
        organization=org, start=start, **{"title": "Fraktionssitzung", "status": "planned", **felder}
    )
    if top:
        cast(Any, ProtocolApprovalService).auto_create_approval_item(neu)
    return neu


def _wochen(org: Any, zahl: int, **felder: Any) -> list[FactionMeeting]:
    return [_sitzung(org, MONTAG + timedelta(weeks=woche), **felder) for woche in range(zahl)]


def _vorherige(*sitzungen: FactionMeeting) -> list[Any]:
    return [FactionMeeting.objects.get(pk=s.pk).previous_meeting_id for s in sitzungen]


def _top(von: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.get(meeting=von, is_approval_item=True)


def _status(von: FactionMeeting, *folge: str) -> None:
    for status in folge:
        von.status = status
        von.save()


class Angenommen:
    passed = True


def _schalter(org: Any, mitglied: Any, *, an: bool) -> None:
    """„Sitzungen automatisch verketten“ wie auf der Einstellungsseite: Das Feld fehlt im Formular, wenn es aus ist."""
    formular = {"link_previous_meeting": "on"} if an else {}
    services.save_faction_settings(org, mitglied, formular, fields=["link_previous_meeting"])


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
    assert _top(dritte).title == MIT_PROTOKOLL

    erste.delete()

    assert _vorherige(dritte) == [None]
    assert _top(dritte).approves_meeting_id is None
    assert _top(dritte).title == OHNE_PROTOKOLL, "ohne Sitzung davor entfällt der Zusatz, wie beim Absagen"


@pytest.mark.django_db
def test_geloeschter_termin_titel_nennt_die_neue_vorherige_sitzung(org: Any) -> None:
    org.settings = {"faction": {"first_agenda_title_with_previous": "Protokoll vom {datum_letzte_sitzung}"}}
    org.save(update_fields=["settings"])
    erste, zweite, dritte = _wochen(org, 3)
    assert _top(dritte).title == f"Protokoll vom {zweite.start:%d.%m.%Y}"

    zweite.delete()

    assert _top(dritte).title == f"Protokoll vom {erste.start:%d.%m.%Y}"
    assert _top(dritte).approves_meeting_id == erste.pk


@pytest.mark.django_db
def test_geloeschter_termin_laesst_bearbeiteten_top_der_folgenden_stehen(org: Any) -> None:
    erste, zweite = _wochen(org, 2)
    top = _top(zweite)
    top.title = "Formalia"
    top.save()

    erste.delete()

    top.refresh_from_db()
    assert (top.title, top.approves_meeting_id) == ("Formalia", None)


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
    _status(erste, "ongoing", "completed")
    _status(zweite, "ongoing", "completed")

    # Eine nachträglich angelegte Sitzung zwischen zwei abgeschlossenen ändert deren Zuordnung nicht
    dazwischen = _sitzung(org, MONTAG + timedelta(days=3))

    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]
    assert _vorherige(dazwischen) == [None]
    assert _top(zweite).approves_meeting_id == erste.pk
    assert _top(dazwischen).title == OHNE_PROTOKOLL


@pytest.mark.django_db
def test_nie_begonnene_sitzung_wird_uebersprungen_sobald_die_naechste_beginnt(org: Any, make_member: Any) -> None:
    """Die mittlere Sitzung fand nicht statt und wurde nicht abgesagt: Ihr Protokoll gibt es nicht."""
    from apps.work.faction.services import apply_approval_item_decision

    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    erste, zweite, dritte = _wochen(org, 3)
    _status(erste, "ongoing", "completed")
    assert _vorherige(zweite, dritte) == [erste.pk, zweite.pk]
    # Solange nichts begonnen hat, genehmigt die Abstimmung kein Protokoll einer Sitzung, die nicht stattfand
    assert cast(Any, apply_approval_item_decision)(_top(dritte), Angenommen(), dritte, vorsitz) is False
    assert FactionMeeting.objects.get(pk=zweite.pk).protocol_approved is False

    _status(dritte, "ongoing")

    assert _vorherige(erste, zweite, dritte) == [None, None, erste.pk]
    assert _top(dritte).approves_meeting_id == erste.pk
    assert _top(zweite).title == OHNE_PROTOKOLL
    assert cast(Any, apply_approval_item_decision)(_top(dritte), Angenommen(), dritte, vorsitz) is True
    erste.refresh_from_db()
    assert erste.protocol_approved and erste.protocol_approved_in_id == dritte.pk


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

    _status(zweite, "ongoing")
    assert cast(Any, apply_approval_item_decision)(_top(zweite), Angenommen(), zweite, vorsitz) is True
    erste.refresh_from_db()
    assert erste.protocol_approved and erste.protocol_approved_in_id == zweite.pk
    assert FactionMeeting.objects.get(pk=dritte.pk).previous_meeting_id == zweite.pk


@pytest.mark.django_db
def test_hinweise_und_unterlagen_erst_wenn_die_vorherige_sitzung_stattgefunden_hat(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)
    hinweis = "work/faction/neu/_vb_hinweise.html"

    # Die Kette reicht in die Zukunft: Vor der Sitzung davor gibt es kein Protokoll, das fehlen oder vorliegen könnte
    for kommende in (zweite, dritte):
        frisch = FactionMeeting.objects.get(pk=kommende.pk)
        assert frisch.has_pending_protocol_approval is False
        assert "fs-hinweis-protokoll" not in render_to_string(hinweis, {"meeting": frisch})
        assert sitzung.vorige_mit_protokoll(_top(kommende)) is None
        assert "Protokoll von" not in sitzung.herkunft(_top(kommende))

    _status(erste, "ongoing", "completed")

    frisch = FactionMeeting.objects.get(pk=zweite.pk)
    assert frisch.has_pending_protocol_approval is True
    assert "fs-hinweis-protokoll" in render_to_string(hinweis, {"meeting": frisch})
    assert sitzung.vorige_mit_protokoll(_top(zweite)) == erste
    assert "liegt zur Genehmigung vor" in sitzung.herkunft(_top(zweite))
    assert FactionMeeting.objects.get(pk=dritte.pk).has_pending_protocol_approval is False


# =============================================================================
# Änderungshistorie und Einladung
# =============================================================================


@pytest.mark.django_db
def test_umhaengen_vor_der_einladung_hinterlaesst_keine_bearbeitung(org: Any) -> None:
    """Die Reihe hält einen Termin nur für leer, solange seine Historie nichts als das Anlegen kennt."""
    erste, zweite, dritte = _wochen(org, 3)
    stand = {top.pk: top.updated_at for top in FactionAgendaItem.objects.filter(meeting__in=[zweite, dritte])}

    _status(erste, "cancelled")
    assert _top(zweite).title == OHNE_PROTOKOLL
    _status(erste, "planned")

    assert _top(zweite).title == MIT_PROTOKOLL and _top(zweite).approves_meeting_id == erste.pk
    historie = FactionAuditLog.objects.filter(meeting_id_ref__in=[zweite.pk, dritte.pk])
    assert not historie.exclude(action="create").exists()
    assert {t.pk: t.updated_at for t in FactionAgendaItem.objects.filter(meeting__in=[zweite, dritte])} == stand


@pytest.mark.django_db
def test_eingeladene_erfahren_nur_sichtbare_aenderungen(org: Any) -> None:
    erste, zweite, dritte = _wochen(org, 3)
    FactionMeeting.objects.filter(pk__in=[zweite.pk, dritte.pk]).update(
        invitation_sent=True, invitation_sent_at=timezone.now()
    )

    # Die mittlere Sitzung entfällt: TOP 1 der dritten heißt wie zuvor, nur das genehmigte Protokoll wechselt
    _status(zweite, "cancelled")

    dritte.refresh_from_db()
    assert _top(dritte).approves_meeting_id == erste.pk
    assert vorbereiten.tagesordnung_geaendert(dritte) is False

    # Auch die erste entfällt: TOP 1 der dritten verliert das Protokoll, die Infoleiste bietet die Aktualisierung an
    _status(erste, "cancelled")

    assert _top(dritte).title == OHNE_PROTOKOLL
    assert vorbereiten.tagesordnung_geaendert(dritte) is True


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
    for eine in sitzungen:
        FactionMeeting.objects.get(pk=eine.pk).create_approval_agenda_item()
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


# =============================================================================
# Schalter „Sitzungen automatisch verketten“ (Issue #1297)
# =============================================================================


@pytest.mark.django_db
def test_ohne_kette_bleiben_neue_sitzungen_unverkettet(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    _schalter(org, vorsitz, an=False)
    reihe = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0)
    )

    generate_meetings_for_schedule(reihe, now=JETZT)
    von_hand = _sitzung(org, MONTAG + timedelta(weeks=10))

    termine = [*FactionMeeting.objects.filter(schedule=reihe).order_by("start"), von_hand]
    assert len(termine) == 4
    assert [t.previous_meeting_id for t in termine] == [None] * 4
    assert [_top(t).title for t in termine] == [OHNE_PROTOKOLL] * 4
    assert [_top(t).approves_meeting_id for t in termine] == [None] * 4
    # Die Vorschauen im Dialog „Neue Sitzung“ und in den Einstellungen zeigen denselben Titel
    assert agenda.new_meeting_has_previous(org) is False
    assert agenda.standard_agenda_preview(org, may_view_internal=True)["approval_title"] == OHNE_PROTOKOLL
    vorschau = selectors.standard_agenda_context(org, may_view_internal=True, approval_item=True)
    assert vorschau["standard_agenda_approval_title"] == OHNE_PROTOKOLL
    assert kette.alle_ordnen() == 0


@pytest.mark.django_db
def test_abschalten_loest_kommende_sitzungen_und_laesst_begonnene(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    erste, zweite, dritte, vierte, fuenfte = _wochen(org, 5)
    _status(erste, "ongoing", "completed")
    _status(zweite, "ongoing")
    FactionMeeting.objects.filter(pk=vierte.pk).update(invitation_sent=True, invitation_sent_at=timezone.now())
    bearbeitet = _top(fuenfte)
    bearbeitet.title = "Formalia"
    bearbeitet.save()
    assert agenda.new_meeting_has_previous(org) is True

    _schalter(org, vorsitz, an=False)

    # Die laufende Sitzung berät ihren ersten TOP so, wie er dasteht; die abgeschlossene ohnehin
    assert _vorherige(erste, zweite, dritte, vierte, fuenfte) == [None, erste.pk, None, None, None]
    assert (_top(zweite).title, _top(zweite).approves_meeting_id) == (MIT_PROTOKOLL, erste.pk)
    assert (_top(dritte).title, _top(dritte).approves_meeting_id) == (OHNE_PROTOKOLL, None)
    # Die Eingeladenen der vierten kennen den alten Titel: Die Infoleiste bietet die Aktualisierung an
    vierte.refresh_from_db()
    assert _top(vierte).title == OHNE_PROTOKOLL
    assert vorbereiten.tagesordnung_geaendert(vierte) is True
    # Ein von Hand bearbeiteter TOP behält seinen Titel und genehmigt nichts mehr
    assert (_top(fuenfte).title, _top(fuenfte).approves_meeting_id) == ("Formalia", None)
    assert FactionMeeting.objects.get(pk=dritte.pk).has_pending_protocol_approval is False
    assert kette.alle_ordnen() == 0


@pytest.mark.django_db
def test_einschalten_ordnet_die_kommenden_sitzungen_wieder(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    _schalter(org, vorsitz, an=False)
    erste, zweite, dritte = _wochen(org, 3)
    _status(erste, "ongoing", "completed")
    assert _vorherige(erste, zweite, dritte) == [None, None, None]
    assert FactionMeeting.objects.get(pk=erste.pk).protocol_status == "draft"

    _schalter(org, vorsitz, an=True)

    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]
    assert (_top(zweite).title, _top(zweite).approves_meeting_id) == (MIT_PROTOKOLL, erste.pk)
    abgeschlossen = FactionMeeting.objects.get(pk=erste.pk)
    assert abgeschlossen.protocol_status == "pending"
    assert protokoll.genehmigung(abgeschlossen).sitzung == zweite


@pytest.mark.django_db
def test_sitzungsart_uebersteuert_den_schalter(org: Any) -> None:
    gruppe = FactionMeetingType.objects.create(organization=org, name="Arbeitsgruppe Umwelt", position=1)
    f1, f2 = _wochen(org, 2)
    g1, g2 = (_sitzung(org, MONTAG + timedelta(days=1, weeks=woche), meeting_type=gruppe) for woche in range(2))
    assert _vorherige(f1, f2, g1, g2) == [None, f1.pk, None, g1.pk]

    # Nur die Arbeitsgruppe verkettet nicht; die Fraktionssitzungen folgen weiter der Organisation
    services.save_meeting_type_settings(gruppe, {}, fields=["link_previous_meeting"])

    assert _vorherige(f1, f2, g1, g2) == [None, f1.pk, None, None]
    assert (_top(g2).title, _top(g2).approves_meeting_id) == (OHNE_PROTOKOLL, None)
    assert kette.verkettet(FactionMeeting.objects.get(pk=g2.pk)) is False
    assert kette.verkettet(FactionMeeting.objects.get(pk=f2.pk)) is True
    assert agenda.new_meeting_has_previous(org, gruppe) is False
    assert agenda.new_meeting_has_previous(org) is True

    # Wieder „wie die Organisation“
    services.save_meeting_type_settings(gruppe, {}, inherit=["link_previous_meeting"])

    assert _vorherige(g1, g2) == [None, g1.pk]
    assert _top(g2).title == MIT_PROTOKOLL


@pytest.mark.django_db
def test_ohne_kette_nennt_das_protokoll_die_direkte_genehmigung(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.manage", "protocols.approve"], email="v@example.org")
    erste, zweite = _wochen(org, 2)
    _status(erste, "ongoing", "completed")
    g = protokoll.genehmigung(FactionMeeting.objects.get(pk=erste.pk))
    assert (g.sitzung, g.ohne_kette) == (zweite, False)

    _schalter(org, vorsitz, an=False)

    abgeschlossen = FactionMeeting.objects.get(pk=erste.pk)
    g = protokoll.genehmigung(abgeschlossen)
    assert (g.sitzung, g.ohne_kette) == (None, True)
    v = protokoll.versand(abgeschlossen)
    fuer_vorsitz = protokoll.info(abgeschlossen, protokoll.Rechte(genehmigen=True), g, v, alt=False)
    assert "genehmigt das Protokoll direkt" in fuer_vorsitz.text and fuer_vorsitz.aktion == "genehmigen"
    fuer_mitglieder = protokoll.info(abgeschlossen, protokoll.Rechte(), g, v, alt=False)
    assert fuer_mitglieder.text == "Der Vorsitz genehmigt es direkt."
    schritt = protokoll.schritte(abgeschlossen, protokoll.angaben(abgeschlossen), g, v, genehmigen=True)[1]
    assert "nicht verkettet" in schritt["text"]
    for text in (fuer_vorsitz.text, fuer_mitglieder.text, schritt["text"]):
        assert "Sitzung unter TOP" not in text
    dialog = render_to_string(
        "work/faction/neu/_pr_dialoge.html",
        {
            "rechte": protokoll.Rechte(genehmigen=True),
            "genehmigung": g,
            "versand": v,
            "meeting": abgeschlossen,
            "org_slug": org.slug,
        },
    )
    assert "nicht verkettet" in dialog and "Üblich ist die Genehmigung" not in dialog
    # Der direkte Weg bleibt, wie er ist
    assert cast(Any, ProtocolApprovalService).approve_protocol(abgeschlossen, None, vorsitz) is True
    assert protokoll.genehmigung(FactionMeeting.objects.get(pk=erste.pk)).direkt is True


@pytest.mark.django_db
def test_laufende_sitzung_behaelt_ihren_ersten_top_beim_einschalten(org: Any, make_member: Any) -> None:
    """TOP 1 der laufenden Sitzung ist schon beraten: Das Einschalten hängt ihr kein Protokoll mehr an."""
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    _schalter(org, vorsitz, an=False)
    erste, zweite, dritte = _wochen(org, 3)
    _status(erste, "ongoing", "completed")
    _status(zweite, "ongoing")

    _schalter(org, vorsitz, an=True)

    assert _vorherige(erste, zweite, dritte) == [None, None, zweite.pk]
    assert (_top(zweite).title, _top(zweite).approves_meeting_id) == (OHNE_PROTOKOLL, None)
    assert _top(dritte).title == MIT_PROTOKOLL
    # Auch der stündliche Lauf lässt sie in Ruhe; das Protokoll der ersten genehmigt der Vorsitz direkt
    assert kette.alle_ordnen() == 0
    assert FactionMeeting.objects.get(pk=erste.pk).protocol_status == "draft"


@pytest.mark.django_db
def test_schalter_im_bestand_wirkt_mit_dem_erzeugungslauf(org: Any) -> None:
    """Update: Der Schalter stand schon auf „aus“, ohne zu wirken. Der erste Lauf danach löst die Kette."""
    erste, zweite, dritte = _wochen(org, 3)
    _status(erste, "ongoing", "completed")
    assert _vorherige(erste, zweite, dritte) == [None, erste.pk, zweite.pk]
    org.settings = {"faction": {"link_previous_meeting": False}}
    org.save(update_fields=["settings"])

    ergebnis = run_faction_schedule_pass(now=JETZT)

    assert ergebnis["relinked"] == 2
    assert _vorherige(erste, zweite, dritte) == [None, None, None]
    assert [_top(s).title for s in (zweite, dritte)] == [OHNE_PROTOKOLL] * 2
    assert kette.alle_ordnen() == 0


@pytest.mark.django_db
def test_ganzes_formular_ohne_den_schalter_schaltet_ab(org: Any, make_member: Any) -> None:
    """Das alte Formular schickt jeden eingeschalteten Schalter mit; fehlt das Feld, ist er aus."""
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    erste, zweite = _wochen(org, 2)

    services.save_faction_settings(org, vorsitz, {"auto_create_approval_item": "on"})

    assert _vorherige(erste, zweite) == [None, None]
    assert _top(zweite).title == OHNE_PROTOKOLL

    services.save_faction_settings(org, vorsitz, {"auto_create_approval_item": "on", "link_previous_meeting": "on"})

    assert _vorherige(erste, zweite) == [None, erste.pk]
    assert _top(zweite).title == MIT_PROTOKOLL


@pytest.mark.django_db
def test_fehler_beim_ordnen_laesst_die_einstellung_gespeichert(
    org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    vorsitz = make_member(org, ["faction.manage"], email="v@example.org")
    erste, zweite = _wochen(org, 2)

    def kaputt(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("Datenbank weg")

    echt = kette.organisation_ordnen
    monkeypatch.setattr(kette, "organisation_ordnen", kaputt)

    _schalter(org, vorsitz, an=False)

    org.refresh_from_db()
    assert org.settings["faction"]["link_previous_meeting"] is False
    assert _vorherige(erste, zweite) == [None, erste.pk]
    # Der stündliche Lauf holt es nach
    monkeypatch.setattr(kette, "organisation_ordnen", echt)
    assert kette.alle_ordnen() == 1
    assert _vorherige(erste, zweite) == [None, None]
