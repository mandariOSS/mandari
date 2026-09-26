# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rücknahme durch mandari Session wirkt überall im Bürgerportal.

Nimmt Session eine Vorlage, Sitzung, einen TOP oder eine Anlage aus der Öffentlichkeit
(nicht-öffentlich gestellt oder gelöscht), darf das Portal davon nichts mehr zeigen: nicht auf
fremden Detailseiten, nicht in der Merkliste und nicht in der KI-Zusammenfassung.
Gelöschtes aus fremden Ratsinformationssystemen bleibt dagegen mit Hinweis sichtbar.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory as _UserFactory
from insight_core.models import (
    Bookmark,
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db
UserFactory = cast(Any, _UserFactory)

BASIS = "https://mandari.example/session/nord/api/oparl/"


@pytest.fixture
def welt() -> dict[str, Any]:
    """Spiegel eines Session-Mandanten: Sitzung mit zwei TOPs, zwei Vorlagen, Beratungen, Anlagen."""
    source = OParlSource.objects.create(name="Bezirk Nord (Session)", url=BASIS + "system/")
    body = OParlBody.objects.create(external_id=BASIS + "body/1/", source=source, name="Bezirk Nord", slug="nord")
    sitzung = OParlMeeting.objects.create(
        external_id=BASIS + "meeting/1/", body=body, name="Hauptausschuss", start=timezone.now()
    )
    top_oe = OParlAgendaItem.objects.create(
        external_id=BASIS + "agendaitem/1/", meeting=sitzung, name="Radweg", number="1", result="angenommen"
    )
    top_noe = OParlAgendaItem.objects.create(
        external_id=BASIS + "agendaitem/2/", meeting=sitzung, name="Grundstück", number="N7", result="vertagt"
    )
    radweg = OParlPaper.objects.create(external_id=BASIS + "paper/1/", body=body, name="Radweg Hauptstraße")
    personalie = OParlPaper.objects.create(external_id=BASIS + "paper/2/", body=body, name="Personalie Meier")
    for nummer, paper, top in ((1, radweg, top_oe), (2, personalie, top_oe), (3, radweg, top_noe)):
        OParlConsultation.objects.create(
            external_id=f"{BASIS}consultation/{nummer}/",
            body=body,
            paper=paper,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
    anlage = OParlFile.objects.create(
        external_id=BASIS + "file/1/", body=body, paper=radweg, name="Begründung", text_content="Öffentlicher Text"
    )
    geheim = OParlFile.objects.create(
        external_id=BASIS + "file/2/", body=body, paper=radweg, name="Kaufvertrag", text_content="GEHEIM Kaufpreis"
    )
    return {
        "body": body,
        "sitzung": sitzung,
        "top_oe": top_oe,
        "top_noe": top_noe,
        "radweg": radweg,
        "personalie": personalie,
        "anlage": anlage,
        "geheim": geheim,
    }


# =============================================================================
# KI-Zusammenfassung
# =============================================================================


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _FakeProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        self.prompts.append("\n".join(m.content for m in messages))
        return _Antwort("Kurzfassung")


class TestZusammenfassung:
    def test_zurueckgenommener_vorgang_liefert_keine_gespeicherte_zusammenfassung(self, welt: dict[str, Any]) -> None:
        paper = welt["personalie"]
        paper.summary = "Die Personalie Meier betrifft GEHEIM"
        paper.save(update_fields=["summary"])
        OParlPaper.objects.filter(pk=paper.pk).update(deleted=True, deleted_at=timezone.now())
        response = Client().get(f"/insight/vorgaenge/{paper.id}/zusammenfassung/")
        assert response.status_code == 410
        assert "GEHEIM" not in response.content.decode()

    def test_zurueckgenommene_anlage_fliesst_nicht_ein(self, welt: dict[str, Any]) -> None:
        from insight_ai.services.summarizer import SummaryService

        OParlFile.objects.filter(pk=welt["geheim"].pk).update(deleted=True, deleted_at=timezone.now())
        provider = _FakeProvider()
        cast(Any, SummaryService)(provider=provider).generate_summary(welt["radweg"], save=False)
        assert "Öffentlicher Text" in provider.prompts[0]
        assert "GEHEIM" not in provider.prompts[0]
        assert "Kaufvertrag" not in provider.prompts[0]

    def test_ruecknahme_der_anlage_verwirft_die_zusammenfassung(self, welt: dict[str, Any]) -> None:
        welt["radweg"].summary = "Kaufpreis laut GEHEIM-Anlage"
        welt["radweg"].save(update_fields=["summary"])
        welt["geheim"].mark_deleted()
        welt["radweg"].refresh_from_db()
        assert not welt["radweg"].summary

    def test_ruecknahme_des_vorgangs_verwirft_die_zusammenfassung(self, welt: dict[str, Any]) -> None:
        welt["personalie"].summary = "Personalie Meier"
        welt["personalie"].save(update_fields=["summary"])
        welt["personalie"].mark_deleted()
        welt["personalie"].refresh_from_db()
        assert not welt["personalie"].summary

    def test_fremdquelle_behaelt_zusammenfassung(self, welt: dict[str, Any]) -> None:
        fremd = OParlPaper.objects.create(
            external_id="https://ris.fremd.example/oparl/paper/9", body=welt["body"], name="Fremd", summary="Bleibt"
        )
        datei = OParlFile.objects.create(external_id="https://ris.fremd.example/oparl/file/9", paper=fremd, name="A")
        datei.mark_deleted()
        fremd.refresh_from_db()
        assert fremd.summary == "Bleibt"

    def test_bestand_wird_bereinigt(self, welt: dict[str, Any]) -> None:
        OParlPaper.objects.filter(pk=welt["radweg"].pk).update(summary="enthält GEHEIM")
        OParlFile.objects.filter(pk=welt["geheim"].pk).update(deleted=True, deleted_at=timezone.now())
        OParlPaper.objects.filter(pk=welt["personalie"].pk).update(
            summary="Personalie", deleted=True, deleted_at=timezone.now()
        )
        fremd = OParlPaper.objects.create(
            external_id="https://ris.fremd.example/oparl/paper/8", body=welt["body"], name="Fremd", summary="Bleibt"
        )
        OParlFile.objects.create(
            external_id="https://ris.fremd.example/oparl/file/8", paper=fremd, name="A", deleted=True
        )
        migration = importlib.import_module("insight_core.migrations.0036_zusammenfassungen_nach_ruecknahme")
        migration.zusammenfassungen_verwerfen(django_apps, None)
        assert OParlPaper.objects.get(pk=welt["radweg"].pk).summary is None
        assert OParlPaper.objects.get(pk=welt["personalie"].pk).summary is None
        assert OParlPaper.objects.get(pk=fremd.pk).summary == "Bleibt"


# =============================================================================
# Sitzungs-, Vorgangs- und Dateiseiten
# =============================================================================


class TestSitzungsUndVorgangsseiten:
    def test_zurueckgenommene_vorlage_nicht_auf_der_sitzungsseite(self, welt: dict[str, Any]) -> None:
        client = Client()
        assert "Personalie Meier" in client.get(f"/insight/termine/{welt['sitzung'].id}/").content.decode()
        welt["personalie"].mark_deleted()
        seite = client.get(f"/insight/termine/{welt['sitzung'].id}/").content.decode()
        assert "Personalie Meier" not in seite
        assert "Radweg Hauptstraße" in seite

    def test_zurueckgenommene_beratung_nicht_auf_der_sitzungsseite(self, welt: dict[str, Any]) -> None:
        OParlConsultation.objects.filter(paper=welt["personalie"]).update(deleted=True, deleted_at=timezone.now())
        seite = Client().get(f"/insight/termine/{welt['sitzung'].id}/").content.decode()
        assert "Personalie Meier" not in seite

    def test_noe_gestellter_top_nicht_auf_der_vorgangsseite(self, welt: dict[str, Any]) -> None:
        client = Client()
        vorher = client.get(f"/insight/vorgaenge/{welt['radweg'].id}/").content.decode()
        assert "TOP N7" in vorher and "vertagt" in vorher
        welt["top_noe"].mark_deleted()
        seite = client.get(f"/insight/vorgaenge/{welt['radweg'].id}/").content.decode()
        assert "TOP N7" not in seite
        assert "vertagt" not in seite
        assert "TOP 1" in seite

    def test_zurueckgenommene_sitzung_nicht_auf_der_vorgangsseite(self, welt: dict[str, Any]) -> None:
        welt["sitzung"].mark_deleted()
        seite = Client().get(f"/insight/vorgaenge/{welt['radweg'].id}/").content.decode()
        assert "Hauptausschuss" not in seite
        assert f"/insight/termine/{welt['sitzung'].id}/" not in seite
        assert "TOP 1" not in seite

    def test_dokumentliste_ohne_zurueckgenommene_sitzung(self, welt: dict[str, Any]) -> None:
        welt["sitzung"].mark_deleted()
        client = Client()
        client.get(f"/insight/kommune/{welt['body'].id}/")
        seite = client.get("/insight/dokumente/").content.decode()
        assert "Begründung" in seite
        assert f"/insight/termine/{welt['sitzung'].id}/" not in seite
        assert "TOP 1" not in seite

    def test_fremdquelle_bleibt_transparent(self, welt: dict[str, Any]) -> None:
        fremd_sitzung = OParlMeeting.objects.create(
            external_id="https://ris.fremd.example/oparl/meeting/1", body=welt["body"], name="Fremdsitzung"
        )
        fremd_top = OParlAgendaItem.objects.create(
            external_id="https://ris.fremd.example/oparl/agendaitem/1", meeting=fremd_sitzung, number="F3"
        )
        OParlConsultation.objects.create(
            external_id="https://ris.fremd.example/oparl/consultation/1",
            paper=welt["radweg"],
            meeting_external_id=fremd_sitzung.external_id,
            agenda_item_external_id=fremd_top.external_id,
        )
        fremd_top.mark_deleted()
        seite = Client().get(f"/insight/vorgaenge/{welt['radweg'].id}/").content.decode()
        assert "TOP F3" in seite


# =============================================================================
# Merkliste
# =============================================================================


class TestMerkliste:
    def test_anonyme_merkliste_ohne_zurueckgenommenes(self, welt: dict[str, Any]) -> None:
        welt["personalie"].mark_deleted()
        ids = f"{welt['personalie'].id},{welt['radweg'].id}"
        seite = Client().get(f"/insight/merkliste/api/entities/?type=paper&ids={ids}").content.decode()
        assert "Personalie Meier" not in seite
        assert "Radweg Hauptstraße" in seite

    @pytest.mark.parametrize("art", ["meeting", "paper", "person", "organization"])
    def test_anonyme_merkliste_mit_ungueltigen_ids(self, welt: dict[str, Any], art: str) -> None:
        response = Client().get(f"/insight/merkliste/api/entities/?type={art}&ids=kaputt,{welt['radweg'].id}")
        assert response.status_code == 200

    def test_anonyme_merkliste_begrenzt(self, welt: dict[str, Any]) -> None:
        import uuid

        ids = ",".join(str(uuid.uuid4()) for _ in range(500))
        response = Client().get(f"/insight/merkliste/api/entities/?type=paper&ids={ids},{welt['radweg'].id}")
        assert response.status_code == 200
        assert "Radweg Hauptstraße" not in response.content.decode()

    def test_merkliste_angemeldet_ohne_zurueckgenommenes(self, welt: dict[str, Any]) -> None:
        user = UserFactory()
        for paper in (welt["personalie"], welt["radweg"]):
            Bookmark.objects.create(user=user, entity_type="paper", entity_id=paper.id)
        Bookmark.objects.create(user=user, entity_type="meeting", entity_id=welt["sitzung"].id)
        welt["personalie"].mark_deleted()
        welt["sitzung"].mark_deleted()
        client = Client()
        client.force_login(user)
        seite = client.get("/insight/gespeichert/").content.decode()
        assert "Personalie Meier" not in seite
        assert "Hauptausschuss" not in seite
        assert "Radweg Hauptstraße" in seite

    def test_merken_mit_ungueltiger_id(self, welt: dict[str, Any]) -> None:
        client = Client()
        client.force_login(UserFactory())
        response = client.post(
            "/insight/merkliste/api/toggle/", {"type": "paper", "id": "kaputt"}, content_type="application/json"
        )
        assert response.status_code == 400
