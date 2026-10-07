# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Veröffentlichung im Bürgerportal beendet: Seiten, Suche, Sitemaps, OParl und Beschlussseiten
verhalten sich je Möglichkeit gleich (Issue #618).

- vorübergehend abgeschaltet: Hinweis statt Inhalt (503, Retry-After), nichts gelöscht
- Archiv: lesbar mit Hinweis „nicht mehr aktuell“, keine Abos mehr
- dauerhaft zurückgenommen: 410, raus aus Suche und Sitemaps, OParl meldet gelöscht
- wieder veröffentlicht: alles wie vorher
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from apps.session.services import portal_publication
from insight_core import publication
from insight_core.models import (
    DecisionSubscription,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services import decision_tracking

pytestmark = pytest.mark.django_db

PAUSED = SessionTenant.PORTAL_END_PAUSED
ARCHIVED = SessionTenant.PORTAL_END_ARCHIVED
WITHDRAWN = SessionTenant.PORTAL_END_WITHDRAWN


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _ohne_elasticsearch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suche über den Datenbank-Rückfall, ohne auf einen Suchdienst zu warten."""

    def _nicht_erreichbar() -> Any:
        raise ConnectionError("kein Suchdienst im Test")

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", _nicht_erreichbar)


@pytest.fixture
def welt() -> dict[str, Any]:
    """Veröffentlichender Mandant mit gespiegelter Kommune, veröffentlichtem Beschluss und einer fremden Kommune."""
    tenant = SessionTenant.objects.create(
        name="Bezirk Nord",
        slug="nord",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
    )
    source = OParlSource.objects.get(sync_config__session_tenant="nord")
    basis = source.url
    body = OParlBody.objects.create(external_id=f"{basis}body/", source=source, name="Bezirk Nord", slug="bezirk-nord")
    tenant.oparl_body = body
    tenant.save(update_fields=["oparl_body"])
    ids = {name: uuid.uuid4() for name in ("m", "p", "f", "o", "pe")}
    meeting = OParlMeeting.objects.create(
        external_id=f"{basis}meeting/{ids['m']}/", body=body, name="Sitzung Hafenkante", start=timezone.now()
    )
    paper = OParlPaper.objects.create(external_id=f"{basis}paper/{ids['p']}/", body=body, name="Radweg Hafenkante")
    datei = OParlFile.objects.create(external_id=f"{basis}file/{ids['f']}/", paper=paper, name="Anlage Hafenkante")
    OParlOrganization.objects.create(external_id=f"{basis}organization/{ids['o']}/", body=body, name="Bauausschuss")
    OParlPerson.objects.create(external_id=f"{basis}person/{ids['pe']}/", body=body, name="Erika Hafenkante")

    fremd_quelle = OParlSource.objects.create(name="Fremd", url="https://ris.fremd.example/oparl/system")
    fremd = OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=fremd_quelle, name="Fremdstadt", slug="fremd"
    )
    fremd_vorlage = OParlPaper.objects.create(
        external_id="https://ris.fremd.example/oparl/paper/1", body=fremd, name="Radweg Hafenkante Fremdstadt"
    )

    gremium = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Bauausschuss",
        organization=gremium,
        start=timezone.now() - timedelta(days=3),
        is_public=True,
    )
    beschluss = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg beschlossen",
        vote_result="approved",
        implementation_public=True,
    )
    return {
        "tenant": tenant,
        "source": source,
        "body": body,
        "meeting": meeting,
        "paper": paper,
        "datei": datei,
        "fremd": fremd,
        "fremd_vorlage": fremd_vorlage,
        "beschluss": beschluss,
    }


def _beenden(welt: dict[str, Any], mode: str) -> None:
    tenant = SessionTenant.objects.get(pk=welt["tenant"].pk)
    portal_publication.end_publication(tenant, mode)


def _mit_kommune(welt: dict[str, Any]) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{welt['body'].pk}/")
    return client


def _suche(client: Client) -> str:
    """Kommunenübergreifende Suche (Rückfall ohne Elasticsearch)."""
    client.get("/insight/kommune/alle/")
    return client.get("/insight/suche/partials/results/", {"q": "Hafenkante"}).content.decode()


class TestVorher:
    def test_alles_sichtbar(self, welt: dict[str, Any]) -> None:
        client = _mit_kommune(welt)
        assert client.get(f"/insight/termine/{welt['meeting'].pk}/").status_code == 200
        assert client.get("/insight/termine/").status_code == 200
        assert client.get(f"/insight/beschluesse/{welt['beschluss'].pk}/").status_code == 200
        assert publication.states() == {}


class TestVoruebergehend:
    def test_seiten_zeigen_hinweis_statt_inhalt(self, welt: dict[str, Any]) -> None:
        _beenden(welt, PAUSED)
        client = _mit_kommune(welt)

        for url in (
            f"/insight/termine/{welt['meeting'].pk}/",
            f"/insight/vorgaenge/{welt['paper'].pk}/",
            f"/insight/dokumente/{welt['datei'].pk}/preview/",
            "/insight/termine/",
            "/insight/",
            "/insight/k/bezirk-nord/",
            "/insight/beschluesse/",
            f"/insight/beschluesse/{welt['beschluss'].pk}/",
        ):
            response = client.get(url)
            assert response.status_code == 503, url
            assert response["Retry-After"] == str(publication.RETRY_AFTER_SECONDS), url
            assert "Hafenkante" not in response.content.decode(), url
            assert "Radweg beschlossen" not in response.content.decode(), url

    def test_bestand_bleibt_erhalten(self, welt: dict[str, Any]) -> None:
        _beenden(welt, PAUSED)

        welt["body"].refresh_from_db()
        welt["source"].refresh_from_db()
        assert welt["body"].is_listed is True
        assert welt["source"].is_active is False, "kein Abgleich"
        assert not OParlMeeting.objects.get(pk=welt["meeting"].pk).deleted

    def test_suche_sitemap_und_oparl(self, welt: dict[str, Any]) -> None:
        _beenden(welt, PAUSED)
        client = Client()

        treffer = _suche(client)
        assert "Radweg Hafenkante Fremdstadt" in treffer
        assert f"/insight/vorgaenge/{welt['paper'].pk}/" not in treffer

        sitemap = client.get("/sitemap-insight-bezirk-nord.xml")
        assert sitemap.status_code == 503 and sitemap["Retry-After"]
        # Die nummerierten Dateien (Issue #914) ebenso: Suchmaschinen behalten die Adressen
        for datei in ("/sitemap-insight-bezirk-nord-vorgaenge-1.xml", "/sitemap-insight-bezirk-nord-sitzungen-1.xml"):
            antwort = client.get(datei)
            assert antwort.status_code == 503 and antwort["Retry-After"], datei
        assert client.get("/sitemap-insight-fremd.xml").status_code == 200
        assert client.get("/sitemap-insight-fremd-vorgaenge-1.xml").status_code == 200

        assert client.get(f"/oparl/v1/meeting/{welt['meeting'].pk}").status_code == 503
        assert client.get(f"/oparl/v1/body/{welt['body'].pk}/papers").status_code == 503
        assert client.get(f"/oparl/v1/paper/{welt['fremd_vorlage'].pk}").status_code == 200

    def test_fremde_kommune_unberuehrt(self, welt: dict[str, Any]) -> None:
        _beenden(welt, PAUSED)
        client = Client()
        assert client.get(f"/insight/vorgaenge/{welt['fremd_vorlage'].pk}/").status_code == 200

    def test_suchdienst_ohne_abgeschaltete_kommune(self, welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        _beenden(welt, PAUSED)
        aufrufe: list[dict[str, Any]] = []

        class _Dienst:
            def search_all(self, **kwargs: Any) -> dict[str, Any]:
                aufrufe.append(kwargs)
                return {"results": [], "total": 0, "page": 1, "pages": 0}

            def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
                aufrufe.append(kwargs)
                leer: dict[str, Any] = {}
                return {
                    "groups": [],
                    "counts": leer,
                    "page": 1,
                    "pages": 1,
                    "has_more": False,
                    "similar_spelling": False,
                    "totals_by_index": leer,
                }

        monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _Dienst())
        client = Client()
        client.get("/insight/kommune/alle/")
        client.get("/insight/suche/partials/results/", {"q": "Hafenkante"})

        assert aufrufe and aufrufe[0]["body_ids"] == [str(welt["fremd"].pk)]


class TestArchiv:
    def test_lesbar_mit_hinweis(self, welt: dict[str, Any]) -> None:
        _beenden(welt, ARCHIVED)
        client = _mit_kommune(welt)

        for url in (f"/insight/termine/{welt['meeting'].pk}/", f"/insight/vorgaenge/{welt['paper'].pk}/", "/insight/"):
            response = client.get(url)
            assert response.status_code == 200, url
            assert 'data-testid="archiv-hinweis"' in response.content.decode(), url
        assert "Hafenkante" in client.get(f"/insight/termine/{welt['meeting'].pk}/").content.decode()

    def test_fremde_kommune_ohne_hinweis(self, welt: dict[str, Any]) -> None:
        _beenden(welt, ARCHIVED)
        response = Client().get(f"/insight/vorgaenge/{welt['fremd_vorlage'].pk}/")
        assert response.status_code == 200
        assert 'data-testid="archiv-hinweis"' not in response.content.decode()

    def test_suche_sitemap_und_oparl_bleiben(self, welt: dict[str, Any]) -> None:
        _beenden(welt, ARCHIVED)
        client = Client()

        assert f"/insight/vorgaenge/{welt['paper'].pk}/" in _suche(client)
        assert client.get("/sitemap-insight-bezirk-nord.xml").status_code == 200
        # Sitzungen und Vorgänge stehen seit Issue #914 in nummerierten Dateien
        sitzungen = client.get("/sitemap-insight-bezirk-nord-sitzungen-1.xml")
        assert sitzungen.status_code == 200 and f"/insight/termine/{welt['meeting'].pk}/" in sitzungen.content.decode()
        vorgaenge = client.get("/sitemap-insight-bezirk-nord-vorgaenge-1.xml")
        assert vorgaenge.status_code == 200 and f"/insight/vorgaenge/{welt['paper'].pk}/" in vorgaenge.content.decode()
        antwort = client.get(f"/oparl/v1/meeting/{welt['meeting'].pk}")
        assert antwort.status_code == 200 and not antwort.json().get("deleted")

    def test_beschluss_lesbar_ohne_abos(self, welt: dict[str, Any]) -> None:
        _beenden(welt, ARCHIVED)
        client = Client()

        seite = client.get(f"/insight/beschluesse/{welt['beschluss'].pk}/")
        assert seite.status_code == 200
        inhalt = seite.content.decode()
        assert "Radweg beschlossen" in inhalt
        assert 'data-testid="archiv-keine-abos"' in inhalt
        assert 'data-testid="archiv-hinweis"' in inhalt

        abo = client.post(f"/insight/beschluesse/{welt['beschluss'].pk}/", {"email": "a@example.org", "privacy": "on"})
        assert abo.status_code == 302
        assert not DecisionSubscription.objects.exists()

        beschluss = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=welt["beschluss"].pk)
        assert decision_tracking.notify_status_change(beschluss, "open", None) == 0
        assert list(decision_tracking.public_decisions(welt["body"])) == [beschluss]


class TestDauerhaft:
    def test_seiten_nicht_mehr_verfuegbar(self, welt: dict[str, Any]) -> None:
        _beenden(welt, WITHDRAWN)
        client = _mit_kommune(welt)

        for url in (
            f"/insight/termine/{welt['meeting'].pk}/",
            f"/insight/vorgaenge/{welt['paper'].pk}/",
            "/insight/termine/",
            "/insight/k/bezirk-nord/",
            "/insight/k/nord/",
            f"/insight/beschluesse/{welt['beschluss'].pk}/",
        ):
            response = client.get(url)
            assert response.status_code == 410, url
            assert "Hafenkante" not in response.content.decode(), url

    def test_suche_sitemaps_und_oparl(self, welt: dict[str, Any]) -> None:
        vorher = timezone.now() - timedelta(minutes=1)
        _beenden(welt, WITHDRAWN)
        client = Client()

        assert f"/insight/vorgaenge/{welt['paper'].pk}/" not in _suche(client)
        assert client.get("/sitemap-insight-bezirk-nord.xml").status_code == 410
        assert client.get("/sitemap-insight-bezirk-nord-vorgaenge-1.xml").status_code == 410
        assert client.get("/sitemap-insight-bezirk-nord-sitzungen-1.xml").status_code == 410
        assert "bezirk-nord" not in client.get("/sitemap-insight-index.xml").content.decode()

        objekt = client.get(f"/oparl/v1/meeting/{welt['meeting'].pk}")
        assert objekt.status_code == 200 and objekt.json()["deleted"] is True
        liste = client.get(f"/oparl/v1/body/{welt['body'].pk}/papers", {"modified_since": vorher.isoformat()}).json()
        assert [eintrag.get("deleted") for eintrag in liste["data"]] == [True]


class TestWiederVeroeffentlichen:
    @pytest.mark.parametrize("mode", [PAUSED, ARCHIVED, WITHDRAWN])
    def test_alles_wie_vorher(self, welt: dict[str, Any], mode: str) -> None:
        _beenden(welt, mode)
        tenant = SessionTenant.objects.get(pk=welt["tenant"].pk)
        portal_publication.resume_publication(tenant)
        client = _mit_kommune(welt)

        for url in (
            f"/insight/termine/{welt['meeting'].pk}/",
            "/insight/termine/",
            "/insight/k/bezirk-nord/",
            f"/insight/beschluesse/{welt['beschluss'].pk}/",
            "/sitemap-insight-bezirk-nord.xml",
            "/sitemap-insight-bezirk-nord-vorgaenge-1.xml",
            "/sitemap-insight-bezirk-nord-sitzungen-1.xml",
            f"/oparl/v1/meeting/{welt['meeting'].pk}",
        ):
            response = client.get(url)
            assert response.status_code == 200, url
            assert 'data-testid="archiv-hinweis"' not in response.content.decode(), url
        assert f"/insight/vorgaenge/{welt['paper'].pk}/" in _suche(client)
        assert publication.states() == {}


class TestKommuneErstImView:
    """Seiten, die ihre Kommune erst im View wählen: per ``?kommune=`` oder beim Erstaufruf ohne Session."""

    KALENDER = ("/insight/termine/kalender.ics", "/insight/termine/jahresplan/", "/insight/termine/kalender/")

    @pytest.mark.parametrize(("mode", "status"), [(PAUSED, 503), (WITHDRAWN, 410)])
    def test_kalender_per_parameter_ohne_session(self, welt: dict[str, Any], mode: str, status: int) -> None:
        _beenden(welt, mode)

        for url in self.KALENDER:
            response = Client().get(url, {"kommune": str(welt["body"].pk)})
            assert response.status_code == status, url
            assert "Hafenkante" not in response.content.decode(), url
            if status == 503:
                assert response["Retry-After"] == str(publication.RETRY_AFTER_SECONDS), url

    def test_kalender_per_parameter_bei_anderer_kommune_in_der_session(self, welt: dict[str, Any]) -> None:
        _beenden(welt, PAUSED)
        client = Client()
        client.get(f"/insight/kommune/{welt['fremd'].pk}/")

        for url in self.KALENDER:
            assert client.get(url, {"kommune": str(welt["body"].pk)}).status_code == 503, url

    def test_parameter_der_fremden_kommune_schlaegt_die_session(self, welt: dict[str, Any]) -> None:
        """Kein falscher Hinweis: Die Session steht auf der abgeschalteten Kommune, abgerufen wird die fremde."""
        _beenden(welt, PAUSED)
        client = _mit_kommune(welt)

        for url in self.KALENDER:
            assert client.get(url, {"kommune": str(welt["fremd"].pk)}).status_code == 200, url
        # Ungültige oder unbekannte Angabe: Es gilt wie im View die Kommune der Session
        client.get(f"/insight/kommune/{welt['body'].pk}/")
        assert client.get(self.KALENDER[0], {"kommune": "kein-uuid"}).status_code == 503
        assert client.get(self.KALENDER[0], {"kommune": str(uuid.uuid4())}).status_code == 503

    def test_erstaufruf_mit_einziger_kommune(self, welt: dict[str, Any]) -> None:
        """Self-Hosting: Die einzige Kommune wird erst im View gewählt."""
        OParlBody.objects.filter(pk=welt["fremd"].pk).update(is_listed=False)
        _beenden(welt, PAUSED)

        for url in ("/insight/termine/", "/insight/vorgaenge/", "/insight/termine/partials/calendar-events/"):
            response = Client().get(url)
            assert response.status_code == 503, url
            assert "Hafenkante" not in response.content.decode(), url

    def test_erstaufruf_mit_rueckfall_auf_die_erste_kommune(self, welt: dict[str, Any]) -> None:
        """Teilseiten ohne gewählte Kommune zeigen die erste gelistete – hier die abgeschaltete."""
        _beenden(welt, PAUSED)

        response = Client().get("/insight/termine/partials/calendar-events/")
        assert response.status_code == 503
        assert "Hafenkante" not in response.content.decode()
        # Seiten mit Auswahlzwang leiten weiter statt einen Hinweis zu einer nicht gewählten Kommune zu zeigen
        assert Client().get("/insight/termine/").status_code == 302
        assert Client().get("/insight/").status_code == 200


class TestArchivHinweisNurFuerDieEigeneKommune:
    def test_fremde_vorlage_bei_archiv_in_der_session(self, welt: dict[str, Any]) -> None:
        _beenden(welt, ARCHIVED)
        client = _mit_kommune(welt)

        fremd = client.get(f"/insight/vorgaenge/{welt['fremd_vorlage'].pk}/")
        assert fremd.status_code == 200
        assert 'data-testid="archiv-hinweis"' not in fremd.content.decode()
        # Seiten ohne eigene Kommune (Listen der gewählten) tragen den Hinweis weiter
        assert 'data-testid="archiv-hinweis"' in client.get("/insight/termine/").content.decode()
