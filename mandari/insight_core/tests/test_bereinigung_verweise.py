# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Bereinigung ohne Verlust von Arbeitsdaten (Issue #421).

``purge_deleted`` und das Löschen einer Kommune (Admin bzw. ``delete_body_data``) dürfen keine RIS-Objekte
entfernen, auf die Daten anderer Module verweisen – weder direkt (Sitzungsvorbereitung an der Sitzung) noch
über die Lösch-Kaskade (Position an einem TOP der Sitzung). Objekte ohne Verweise werden weiterhin gelöscht.

Abos und Ratsfragen des Bürgerportals halten das Löschen ebenso auf; abgeleitete Geodaten gehen mit, Rückmeldungen zu
Seiten bleiben ohne Kommune (Issue #524).
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Task
from apps.work.meetings.models import AgendaItemPosition, AgendaSpeechNote, FileAnnotation, MeetingPreparation
from insight_core.models import (
    Address,
    InsightSubscriber,
    LocationMapping,
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    PageFeedback,
    PaperLocation,
    PaperPlanReference,
    PlanBoundary,
    PlanBoundarySource,
    PublicQuestion,
    Street,
    abgeleitete_geodaten,
)
from insight_core.services.body_deletion import delete_body_data
from insight_core.services.external_references import external_references, split_by_references

pytestmark = pytest.mark.django_db

RIS = "https://ris.beispielstadt.example/oparl"
SLUG = "beispiel"


@pytest.fixture
def body(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Beispiel-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Beispielstadt", slug=SLUG)


def _meeting(body: OParlBody, n: int, *, deleted: bool = False) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{n}",
        body=body,
        name=f"Sitzung {n}",
        deleted=deleted,
        deleted_at=timezone.now() if deleted else None,
    )


def _agenda_item(meeting: OParlMeeting, n: int) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(external_id=f"{RIS}/agenda/{n}", meeting=meeting, number=str(n), name="TOP")


def _geodaten(body: OParlBody, paper: OParlPaper) -> None:
    """Je eine Zeile jeder Art abgeleiteter Geodaten an Kommune und Vorgang."""
    Street.objects.create(
        body=body, osm_id=1, name="Hauptstraße", normalized_name="hauptstrasse", latitude=52, longitude=7
    )
    Address.objects.create(
        body=body,
        osm_id=2,
        street="Hauptstraße",
        normalized_street="hauptstrasse",
        house_number="1",
        normalized_house_number="1",
        latitude=52,
        longitude=7,
    )
    LocationMapping.objects.create(body=body, location_name="Rathaus", latitude=52, longitude=7)
    PaperLocation.objects.create(paper=paper, body=body, name="Hauptstraße", latitude=52.0, longitude=7.0)
    quelle = PlanBoundarySource.objects.create(
        body=body, name="Bebauungspläne", url="https://geo.example.org/items", attribution="Beispiel"
    )
    umring = PlanBoundary.objects.create(
        source=quelle,
        body=body,
        feature_key="1",
        plan_number="12",
        number_key="12",
        geometry={"type": "Polygon", "coordinates": []},
        bbox_south=51.9,
        bbox_north=52.1,
        bbox_west=6.9,
        bbox_east=7.1,
        point_lat=52.0,
        point_lon=7.0,
        fetched_at=timezone.now(),
    )
    bezug = PaperPlanReference.objects.create(
        paper=paper, body=body, number_key="12", number_label="12", match="plan", matched_at=timezone.now()
    )
    bezug.boundaries.add(umring)


def _geodaten_anzahl(queryset: Any) -> int:
    return sum(rows.count() for rows in abgeleitete_geodaten(queryset))


def _ratsfrage(body: OParlBody, person: OParlPerson) -> PublicQuestion:
    return PublicQuestion.objects.create(
        body=body,
        recipient=person,
        questioner_name="Erika",
        questioner_email="erika@example.org",
        subject="Radweg",
        question_text="Wann kommt der Radweg?",
    )


def _purge(*args: str) -> str:
    out = StringIO()
    call_command("purge_deleted", *args, stdout=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Verweisprüfung (Dienst)
# ---------------------------------------------------------------------------


def test_verweise_werden_ueber_die_kaskade_und_je_datensatz_einmal_gezaehlt(
    body: OParlBody, org: Any, make_member: Any
) -> None:
    meeting = _meeting(body, 1)
    item = _agenda_item(meeting, 1)
    author = make_member(org)
    MeetingPreparation.objects.create(organization=org, meeting=meeting)
    AgendaItemPosition.objects.create(organization=org, agenda_item=item)
    # Hängt an Sitzung UND TOP – zählt trotzdem nur einmal
    AgendaSpeechNote.objects.create(organization=org, author=author, meeting=meeting, agenda_item=item)

    references = {ref.label: ref.count for ref in external_references(OParlMeeting.objects.filter(pk=meeting.pk))}

    assert references == {
        "work.AgendaItemPosition": 1,
        "work.AgendaSpeechNote": 1,
        "work.MeetingPreparation": 1,
    }


def test_split_trennt_geschuetzte_von_loeschbaren_objekten(body: OParlBody, org: Any) -> None:
    geschuetzt = _meeting(body, 1)
    AgendaItemPosition.objects.create(organization=org, agenda_item=_agenda_item(geschuetzt, 1))
    frei = _meeting(body, 2)
    _agenda_item(frei, 2)

    deletable, protected = split_by_references(OParlMeeting.objects.filter(body=body))

    assert list(protected.values_list("pk", flat=True)) == [geschuetzt.pk]
    assert list(deletable.values_list("pk", flat=True)) == [frei.pk]


def test_verknuepfung_einer_organisation_mit_der_kommune_zaehlt_als_verweis(body: OParlBody, org: Any) -> None:
    org.body = body
    org.save(update_fields=["body"])

    references = external_references(OParlBody.objects.filter(pk=body.pk))

    assert [(ref.label, ref.count) for ref in references] == [("tenants.Organization", 1)]


# ---------------------------------------------------------------------------
# purge_deleted
# ---------------------------------------------------------------------------


def test_purge_behaelt_objekte_mit_arbeitsdaten_und_loescht_die_uebrigen(body: OParlBody, org: Any) -> None:
    mit_vorbereitung = _meeting(body, 1, deleted=True)
    vorbereitung = MeetingPreparation.objects.create(organization=org, meeting=mit_vorbereitung)
    # Verweis nur über die Kaskade: die Position hängt an einem (nicht markierten) TOP der Sitzung
    mit_position = _meeting(body, 2, deleted=True)
    position = AgendaItemPosition.objects.create(organization=org, agenda_item=_agenda_item(mit_position, 2))
    ohne_verweis = _meeting(body, 3, deleted=True)
    _agenda_item(ohne_verweis, 3)

    output = _purge("--body", SLUG, "--yes")

    assert OParlMeeting.objects.filter(pk=mit_vorbereitung.pk).exists()
    assert OParlMeeting.objects.filter(pk=mit_position.pk).exists()
    assert MeetingPreparation.objects.filter(pk=vorbereitung.pk).exists()
    assert AgendaItemPosition.objects.filter(pk=position.pk).exists()
    assert not OParlMeeting.objects.filter(pk=ohne_verweis.pk).exists()
    assert "Wegen Verweisen anderer Module nicht gelöscht: 2 markierte Objekte" in output


def test_probelauf_meldet_verweise_je_typ_und_loescht_nichts(body: OParlBody, org: Any) -> None:
    meeting = _meeting(body, 1, deleted=True)
    MeetingPreparation.objects.create(organization=org, meeting=meeting)
    AgendaItemPosition.objects.create(organization=org, agenda_item=_agenda_item(meeting, 1))
    _meeting(body, 2, deleted=True)

    output = _purge("--body", SLUG)

    assert "davon 1 mit Verweisen aus anderen Modulen – werden NICHT gelöscht" in output
    assert "(work.MeetingPreparation): 1" in output
    assert "(work.AgendaItemPosition): 1" in output
    assert f"! {meeting.pk}" in output
    assert "1 markierte Objekte bleiben erhalten" in output
    assert "Löschbar: 1" in output
    assert OParlMeeting.objects.filter(body=body).count() == 2


def test_purge_ohne_loeschbare_objekte_bricht_ohne_loeschung_ab(body: OParlBody, org: Any) -> None:
    meeting = _meeting(body, 1, deleted=True)
    MeetingPreparation.objects.create(organization=org, meeting=meeting)

    output = _purge("--body", SLUG, "--yes")

    assert "Nichts zu löschen." in output
    assert OParlMeeting.objects.filter(pk=meeting.pk).exists()


def test_purge_behaelt_gremium_mit_mandanten_verknuepfung(body: OParlBody, org: Any, make_member: Any) -> None:
    """M2M-Verweise (hier: gefolgte Gremien einer Mitgliedschaft) schützen ebenfalls."""
    gefolgt, frei = (
        OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/{n}",
            body=body,
            name=f"Ausschuss {n}",
            deleted=True,
            deleted_at=timezone.now(),
        )
        for n in (1, 2)
    )
    make_member(org).followed_organizations.add(gefolgt)

    output = _purge("--body", SLUG, "--yes")

    assert "(tenants.Membership): 1" in output
    assert OParlOrganization.objects.filter(pk=gefolgt.pk).exists()
    assert not OParlOrganization.objects.filter(pk=frei.pk).exists()


def test_purge_entfernt_lokale_kopie_nur_bei_geloeschten_dateien(
    body: OParlBody, org: Any, make_member: Any, tmp_path: Path
) -> None:
    paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Vorlage")
    kopien = {}
    dateien = {}
    for name in ("annotiert", "frei"):
        kopie = tmp_path / f"{name}.pdf"
        kopie.write_bytes(b"%PDF-1.4")
        kopien[name] = kopie
        dateien[name] = OParlFile.objects.create(
            external_id=f"{RIS}/file/{name}",
            body=body,
            paper=paper,
            name=name,
            local_path=str(kopie),
            deleted=True,
            deleted_at=timezone.now(),
        )
    FileAnnotation.objects.create(organization=org, oparl_file=dateien["annotiert"], author=make_member(org))

    _purge("--body", SLUG, "--yes")

    assert OParlFile.objects.filter(pk=dateien["annotiert"].pk).exists()
    assert kopien["annotiert"].exists()
    assert not OParlFile.objects.filter(pk=dateien["frei"].pk).exists()
    assert not kopien["frei"].exists()


def test_purge_entfernt_abgeleitete_geodaten_geloeschter_vorgaenge(body: OParlBody) -> None:
    geloescht = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/1", body=body, name="Vorlage", deleted=True, deleted_at=timezone.now()
    )
    bleibt = OParlPaper.objects.create(external_id=f"{RIS}/paper/2", body=body, name="Vorlage")
    _geodaten(body, geloescht)
    PaperLocation.objects.create(paper=bleibt, body=body, name="Marktplatz", latitude=52.1, longitude=7.1)

    probelauf = _purge("--body", SLUG)
    assert "abgeleitete Geodaten (Verortungen, Planbezüge), gehen mit: 2" in probelauf
    assert PaperLocation.objects.filter(paper=geloescht).exists()

    _purge("--body", SLUG, "--yes")

    assert not OParlPaper.objects.filter(pk=geloescht.pk).exists()
    assert not PaperLocation.objects.filter(name="Hauptstraße").exists()
    assert not PaperPlanReference.objects.exists()
    # Geodaten der Kommune und des übrigen Vorgangs bleiben
    assert PaperLocation.objects.filter(paper=bleibt).exists()
    assert Street.objects.filter(body=body).exists()
    assert PlanBoundary.objects.filter(body=body).exists()


def test_purge_behaelt_person_mit_ratsfrage(body: OParlBody) -> None:
    gefragt, frei = (
        OParlPerson.objects.create(
            external_id=f"{RIS}/person/{n}", body=body, name=f"Person {n}", deleted=True, deleted_at=timezone.now()
        )
        for n in (1, 2)
    )
    frage = _ratsfrage(body, gefragt)

    output = _purge("--body", SLUG, "--yes")

    assert "(insight_core.PublicQuestion): 1" in output
    assert OParlPerson.objects.filter(pk=gefragt.pk).exists()
    assert PublicQuestion.objects.filter(pk=frage.pk).exists()
    assert not OParlPerson.objects.filter(pk=frei.pk).exists()


# ---------------------------------------------------------------------------
# Kommune löschen (Dienst und Admin)
# ---------------------------------------------------------------------------


def test_kommune_mit_arbeitsdaten_wird_nicht_geloescht(body: OParlBody, org: Any) -> None:
    meeting = _meeting(body, 1)
    AgendaItemPosition.objects.create(organization=org, agenda_item=_agenda_item(meeting, 1))

    result = delete_body_data(str(body.pk))

    assert result["deleted"] == 0
    assert result["blocked_by"] == ["TOP-Positionen (work.AgendaItemPosition): 1"]
    assert OParlBody.objects.filter(pk=body.pk).exists()
    assert OParlMeeting.objects.filter(pk=meeting.pk).exists()


def test_kommune_ohne_verweise_wird_geloescht(body: OParlBody) -> None:
    _agenda_item(_meeting(body, 1), 1)

    result = delete_body_data(str(body.pk))

    assert result["deleted"] > 0
    assert not OParlBody.objects.filter(pk=body.pk).exists()
    assert not OParlMeeting.objects.exists()


def test_kommune_loeschen_nimmt_abgeleitete_geodaten_mit(body: OParlBody) -> None:
    _geodaten(body, OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Vorlage"))
    rueckmeldung = PageFeedback.objects.create(body=body, page_type="vorgang", path="/insight/", helpful=True)
    assert _geodaten_anzahl(OParlBody.objects.filter(pk=body.pk)) == 7

    result = delete_body_data(str(body.pk))

    assert result["detail"]["derived_geodata"] == 7
    assert not OParlBody.objects.filter(pk=body.pk).exists()
    assert not OParlPaper.objects.exists()
    assert not Street.objects.exists()
    assert not PlanBoundarySource.objects.exists()
    # Die Rückmeldung bleibt bis zum Ende ihrer Aufbewahrung, ohne Kommune
    rueckmeldung.refresh_from_db()
    assert rueckmeldung.body is None


@pytest.mark.parametrize("art", ["abo", "ratsfrage"])
def test_kommune_mit_abo_oder_ratsfrage_wird_nicht_geloescht(body: OParlBody, art: str) -> None:
    paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Vorlage")
    _geodaten(body, paper)
    if art == "abo":
        InsightSubscriber.objects.create(email="leserin@example.org", body=body, confirmed=True)
        erwartet = "(insight_core.InsightSubscriber): 1"
    else:
        _ratsfrage(body, OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=body, name="Ratsfrau"))
        erwartet = "(insight_core.PublicQuestion): 1"

    result = delete_body_data(str(body.pk))

    assert result["deleted"] == 0
    assert any(erwartet in eintrag for eintrag in result["blocked_by"])
    # Nichts ist gelöscht, auch die Geodaten nicht
    assert OParlPaper.objects.filter(pk=paper.pk).exists()
    assert _geodaten_anzahl(OParlBody.objects.filter(pk=body.pk)) == 7


@pytest.fixture
def admin_client(db: Any) -> Client:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    return client


def test_admin_bricht_loeschung_der_kommune_bei_verweisen_mit_meldung_ab(
    admin_client: Client, body: OParlBody, org: Any
) -> None:
    MeetingPreparation.objects.create(organization=org, meeting=_meeting(body, 1))
    url = reverse("admin:insight_core_oparlbody_delete", args=[body.pk])

    page = admin_client.get(url)
    content = page.content.decode()
    assert page.status_code == 200
    assert "Kommune „Beispielstadt“ wird nicht gelöscht" in content
    assert "(work.MeetingPreparation): 1" in content

    admin_client.post(url, {"post": "yes"})
    assert not Task.objects.filter(task_path__endswith="kommune_loeschen").exists()
    assert OParlBody.objects.filter(pk=body.pk).exists()


def test_admin_loescht_kommune_ohne_verweise_weiterhin(admin_client: Client, body: OParlBody) -> None:
    url = reverse("admin:insight_core_oparlbody_delete", args=[body.pk])

    # Die Löschung selbst läuft als Auftrag im Worker (Issue #515); hier zählt nur, dass er angelegt wird
    response = admin_client.post(url, {"post": "yes"})

    assert response.status_code == 302
    auftrag = Task.objects.get(task_path="insight_core.background_tasks.kommune_loeschen")
    assert auftrag.args == {"args": [str(body.pk)], "kwargs": {}}
    assert OParlBody.objects.filter(pk=body.pk).exists()


def test_admin_bricht_loeschung_der_quelle_bei_verweisen_ab(admin_client: Client, body: OParlBody, org: Any) -> None:
    MeetingPreparation.objects.create(organization=org, meeting=_meeting(body, 1))
    url = reverse("admin:insight_core_oparlsource_delete", args=[body.source.pk])

    page = admin_client.get(url)
    assert "Quelle „Beispiel-RIS“ wird nicht gelöscht" in page.content.decode()

    admin_client.post(url, {"post": "yes"})
    assert OParlSource.objects.filter(pk=body.source.pk).exists()
    assert MeetingPreparation.objects.filter(meeting__body=body).exists()


def test_admin_bricht_loeschung_der_quelle_mit_geodaten_mit_hinweis_ab(admin_client: Client, body: OParlBody) -> None:
    _geodaten(body, OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Vorlage"))
    url = reverse("admin:insight_core_oparlsource_delete", args=[body.source.pk])

    page = admin_client.get(url)
    assert "An ihren Kommunen hängen 7 abgeleitete Geodaten" in page.content.decode()

    admin_client.post(url, {"post": "yes"})
    assert OParlSource.objects.filter(pk=body.source.pk).exists()
    assert Street.objects.filter(body=body).exists()
