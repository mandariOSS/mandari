# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Bereinigung ohne Verlust von Arbeitsdaten (Issue #421).

``purge_deleted`` und das Löschen einer Kommune (Admin bzw. ``delete_body_data``) dürfen keine RIS-Objekte
entfernen, auf die Daten anderer Module verweisen – weder direkt (Sitzungsvorbereitung an der Sitzung) noch
über die Lösch-Kaskade (Position an einem TOP der Sitzung). Objekte ohne Verweise werden weiterhin gelöscht.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.work.meetings.models import AgendaItemPosition, AgendaSpeechNote, FileAnnotation, MeetingPreparation
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
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

    with mock.patch("insight_core.admin.threading.Thread") as thread:
        admin_client.post(url, {"post": "yes"})
    thread.assert_not_called()
    assert OParlBody.objects.filter(pk=body.pk).exists()


def test_admin_loescht_kommune_ohne_verweise_weiterhin(admin_client: Client, body: OParlBody) -> None:
    url = reverse("admin:insight_core_oparlbody_delete", args=[body.pk])

    # Die Löschung selbst läuft in einem Hintergrund-Thread; hier zählt nur, dass sie gestartet wird
    with mock.patch("insight_core.admin.threading.Thread") as thread:
        response = admin_client.post(url, {"post": "yes"})

    assert response.status_code == 302
    thread.return_value.start.assert_called_once_with()


def test_admin_bricht_loeschung_der_quelle_bei_verweisen_ab(admin_client: Client, body: OParlBody, org: Any) -> None:
    MeetingPreparation.objects.create(organization=org, meeting=_meeting(body, 1))
    url = reverse("admin:insight_core_oparlsource_delete", args=[body.source.pk])

    page = admin_client.get(url)
    assert "Quelle „Beispiel-RIS“ wird nicht gelöscht" in page.content.decode()

    admin_client.post(url, {"post": "yes"})
    assert OParlSource.objects.filter(pk=body.source.pk).exists()
    assert MeetingPreparation.objects.filter(meeting__body=body).exists()
