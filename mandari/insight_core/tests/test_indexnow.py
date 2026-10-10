# SPDX-License-Identifier: AGPL-3.0-or-later
"""IndexNow (Issue #939): Schlüsseldatei, Seiten aus Ereignissen, Meldung an die Suchmaschine und Abonnement."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from django.test import Client

from apps.events import TargetUnavailableError, registry
from apps.events.models import Event
from apps.events.registry import Subscriber, get
from insight_core import publication, subscribers
from insight_core.models import OParlBody, OParlMembership, OParlOrganization, OParlPerson, OParlSource
from insight_core.services import indexnow

pytestmark = pytest.mark.django_db

SCHLUESSEL = "3f8a1c2e9b7d4a6f8e0c1b2a3d4e5f60"
ENDPUNKT = "https://api.indexnow.org/indexnow"


@pytest.fixture
def mit_schluessel(settings: Any) -> None:
    settings.SITE_URL = "https://mandari.de"
    settings.INDEXNOW_KEY = SCHLUESSEL
    settings.INDEXNOW_ENDPOINT = ENDPUNKT


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


def _kommune(nummer: int, **felder: Any) -> OParlBody:
    felder.setdefault("is_listed", True)
    source = OParlSource.objects.create(name=f"RIS {nummer}", url=f"https://ris{nummer}.example.org/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/body/1", source=source, name=f"Stadt {nummer}", **felder
    )


def _ereignis(body: OParlBody | None, art: str, kennung: uuid.UUID, **felder: Any) -> Event:
    """Ereignis wie aus dem Journal (ungespeichert; der Handler liest nur diese Felder)."""
    felder.setdefault("type", f"ris.{art.lower()}.changed")
    felder.setdefault("visibility", "oeffentlich")
    felder.setdefault("payload", {})
    return Event(body_id=body.id if body else None, aggregate_type=art, aggregate_id=kennung, **felder)


def _mock_client(monkeypatch: pytest.MonkeyPatch, antwort: Callable[[httpx.Request], httpx.Response]) -> list[Any]:
    """``httpx.Client`` im Modul mit einer Attrappe der Suchmaschine; gibt die gemeldeten Daten zurück."""
    gemeldet: list[Any] = []
    echter_client = httpx.Client

    def behandeln(anfrage: httpx.Request) -> httpx.Response:
        gemeldet.append(json.loads(anfrage.content))
        return antwort(anfrage)

    monkeypatch.setattr(
        "insight_core.services.indexnow.httpx.Client",
        lambda **kwargs: echter_client(transport=httpx.MockTransport(behandeln), **kwargs),
    )
    return gemeldet


# =============================================================================
# Schlüsseldatei
# =============================================================================


@pytest.mark.usefixtures("mit_schluessel")
class TestSchluesseldatei:
    def test_liefert_den_schluessel_als_text(self, client: Client) -> None:
        antwort = client.get(f"/insight/{SCHLUESSEL}.txt")

        assert antwort.status_code == 200
        assert antwort["Content-Type"] == "text/plain; charset=utf-8"
        assert antwort.content.decode() == SCHLUESSEL

    def test_head_fuer_pruefwerkzeuge(self, client: Client) -> None:
        assert client.head(f"/insight/{SCHLUESSEL}.txt").status_code == 200

    def test_anderer_name_404(self, client: Client) -> None:
        assert client.get("/insight/0123456789abcdef.txt").status_code == 404

    def test_ohne_schluessel_404(self, client: Client, settings: Any) -> None:
        settings.INDEXNOW_KEY = ""
        assert client.get(f"/insight/{SCHLUESSEL}.txt").status_code == 404

    def test_schluessel_ausserhalb_des_protokolls_gilt_nicht(self, client: Client, settings: Any) -> None:
        settings.INDEXNOW_KEY = "kurz"
        assert indexnow.schluessel() == ""
        assert client.get("/insight/kurz.txt").status_code == 404


# =============================================================================
# Seiten aus Ereignissen
# =============================================================================


@pytest.mark.usefixtures("mit_schluessel")
class TestSeiten:
    def test_eigene_seite_je_objekttyp(self) -> None:
        body = _kommune(1)
        vorgang, sitzung, gremium = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        adressen = indexnow.seiten(
            [
                _ereignis(body, "Paper", vorgang, type="ris.paper.released"),
                _ereignis(body, "Meeting", sitzung, type="ris.meeting.scheduled"),
                _ereignis(body, "Organization", gremium),
                _ereignis(body, "Paper", vorgang),
            ]
        )

        assert adressen == [
            f"https://mandari.de/insight/vorgaenge/{vorgang}/",
            f"https://mandari.de/insight/termine/{sitzung}/",
            f"https://mandari.de/insight/gremien/{gremium}/",
        ]

    def test_datei_beratung_und_punkt_melden_vorgang_und_sitzung(self) -> None:
        body = _kommune(1)
        vorgang, sitzung, vorher = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        adressen = indexnow.seiten(
            [
                _ereignis(body, "File", uuid.uuid4(), payload={"file": "x", "paper": str(vorgang)}),
                _ereignis(body, "Consultation", uuid.uuid4(), payload={"paper": str(vorgang), "meeting": None}),
                _ereignis(
                    body,
                    "AgendaItem",
                    uuid.uuid4(),
                    type="ris.agendaitem.changed",
                    payload={"meeting": str(sitzung), "previous_meeting": str(vorher), "paper": "kaputt"},
                ),
            ]
        )

        assert adressen == [
            f"https://mandari.de/insight/vorgaenge/{vorgang}/",
            f"https://mandari.de/insight/termine/{sitzung}/",
            f"https://mandari.de/insight/termine/{vorher}/",
        ]

    def test_ruecknahme_meldet_die_entfallene_adresse(self) -> None:
        body = _kommune(1)
        vorgang = uuid.uuid4()

        adressen = indexnow.seiten(
            [
                _ereignis(
                    body,
                    "Paper",
                    vorgang,
                    type="ris.object.depublished",
                    operation="delete",
                    payload={"object_type": "Paper", "object": str(vorgang), "reason": "deleted_at_source"},
                )
            ]
        )

        assert adressen == [f"https://mandari.de/insight/vorgaenge/{vorgang}/"]

    def test_nur_oeffentliche_ereignisse_freigegebener_kommunen(self) -> None:
        gelistet = _kommune(1)
        ungelistet = _kommune(2, is_listed=False)
        pausiert = _kommune(3)
        publication.set_source_state(pausiert.source, publication.PAUSED)
        oeffentlich = uuid.uuid4()

        adressen = indexnow.seiten(
            [
                _ereignis(gelistet, "Paper", oeffentlich),
                _ereignis(gelistet, "Paper", uuid.uuid4(), visibility="nichtoeffentlich"),
                _ereignis(ungelistet, "Paper", uuid.uuid4()),
                _ereignis(pausiert, "Paper", uuid.uuid4()),
                _ereignis(None, "Paper", uuid.uuid4()),
            ]
        )

        assert adressen == [f"https://mandari.de/insight/vorgaenge/{oeffentlich}/"]

    def test_personen_nur_mit_laufender_mitgliedschaft(self) -> None:
        body = _kommune(1)
        rat = OParlOrganization.objects.create(external_id=f"{body.external_id}/org/1", body=body, name="Rat")
        aktiv = OParlPerson.objects.create(external_id=f"{body.external_id}/person/1", body=body, name="Aktiv")
        ehemalig = OParlPerson.objects.create(external_id=f"{body.external_id}/person/2", body=body, name="Ehemalig")
        OParlMembership.objects.create(external_id="m1", person=aktiv, organization=rat)
        OParlMembership.objects.create(external_id="m2", person=ehemalig, organization=rat, end_date=date(2020, 1, 1))
        unbekannt = uuid.uuid4()

        adressen = indexnow.seiten(
            [
                _ereignis(body, "Person", aktiv.id),
                _ereignis(body, "Person", ehemalig.id),
                # Gelöscht bzw. nicht (mehr) im Bestand: Die Seite ist weg, die Meldung sagt das der Suchmaschine
                _ereignis(body, "Person", unbekannt, type="ris.object.depublished"),
            ]
        )

        assert adressen == [
            f"https://mandari.de/insight/personen/{aktiv.id}/",
            f"https://mandari.de/insight/personen/{unbekannt}/",
        ]


# =============================================================================
# Meldung an die Suchmaschine
# =============================================================================


@pytest.mark.usefixtures("mit_schluessel")
class TestMelden:
    def test_meldet_adressen_unter_insight_einmal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gemeldet = _mock_client(monkeypatch, lambda anfrage: httpx.Response(200))

        angenommen = indexnow.melden(
            [
                "https://mandari.de/insight/vorgaenge/1/",
                "https://mandari.de/insight/vorgaenge/1/",
                "https://mandari.de/kontakt/",
                "https://example.org/insight/vorgaenge/2/",
                "https://mandari.de/insight/termine/3/",
            ]
        )

        assert angenommen == 2
        assert gemeldet == [
            {
                "host": "mandari.de",
                "key": SCHLUESSEL,
                "keyLocation": f"https://mandari.de/insight/{SCHLUESSEL}.txt",
                "urlList": ["https://mandari.de/insight/vorgaenge/1/", "https://mandari.de/insight/termine/3/"],
            }
        ]

    def test_teilt_in_meldungen_nach_der_grenze(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(indexnow, "ADRESSEN_JE_MELDUNG", 2)
        gemeldet = _mock_client(monkeypatch, lambda anfrage: httpx.Response(202))

        assert indexnow.melden([f"https://mandari.de/insight/vorgaenge/{n}/" for n in range(5)]) == 5
        assert [len(daten["urlList"]) for daten in gemeldet] == [2, 2, 1]

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_ueberlastet_oder_gestoert_spaeter_erneut(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _mock_client(monkeypatch, lambda anfrage: httpx.Response(status))

        with pytest.raises(indexnow.SuchmaschineNichtErreichbarError):
            indexnow.melden(["https://mandari.de/insight/vorgaenge/1/"])

    def test_nicht_erreichbar_spaeter_erneut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def verbindung_scheitert(anfrage: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("keine Verbindung", request=anfrage)

        _mock_client(monkeypatch, verbindung_scheitert)

        with pytest.raises(indexnow.SuchmaschineNichtErreichbarError):
            indexnow.melden(["https://mandari.de/insight/vorgaenge/1/"])

    @pytest.mark.parametrize("status", [400, 403, 422])
    def test_abgelehnt_steht_im_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int
    ) -> None:
        _mock_client(monkeypatch, lambda anfrage: httpx.Response(status))

        with caplog.at_level(logging.WARNING, logger="insight_core.services.indexnow"):
            assert indexnow.melden(["https://mandari.de/insight/vorgaenge/1/"]) == 0
        assert f"Antwort {status}" in caplog.text

    def test_ohne_schluessel_keine_meldung(self, monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
        settings.INDEXNOW_KEY = ""
        gemeldet = _mock_client(monkeypatch, lambda anfrage: httpx.Response(200))

        assert indexnow.melden(["https://mandari.de/insight/vorgaenge/1/"]) == 0
        assert gemeldet == []


# =============================================================================
# Abonnement
# =============================================================================


class TestAbonnement:
    def test_ohne_schluessel_nicht_registriert(self, leeres_register: dict[str, Subscriber], settings: Any) -> None:
        settings.INDEXNOW_KEY = ""

        assert subscribers.register_indexnow() is False
        assert indexnow.NAME not in leeres_register

    @pytest.mark.usefixtures("mit_schluessel")
    def test_mit_schluessel_registriert(self, leeres_register: dict[str, Subscriber]) -> None:
        assert subscribers.register_indexnow() is True

        spec = get(indexnow.NAME)
        assert spec.transactional is False, "Fremdsystem: kein Aufruf innerhalb der Transaktion"
        assert spec.from_beginning is False, "Bestand kennen die Suchmaschinen aus den Sitemaps"
        assert spec.queue == "adapter"
        assert "ris.object.depublished" in spec.types

    @pytest.mark.usefixtures("mit_schluessel")
    def test_handler_meldet_die_seiten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _kommune(1)
        vorgang = uuid.uuid4()
        gemeldet = _mock_client(monkeypatch, lambda anfrage: httpx.Response(200))

        indexnow.indexnow([_ereignis(body, "Paper", vorgang)], delivery=None)

        assert gemeldet[0]["urlList"] == [f"https://mandari.de/insight/vorgaenge/{vorgang}/"]

    @pytest.mark.usefixtures("mit_schluessel")
    def test_handler_wartet_wenn_die_suchmaschine_nicht_erreichbar_ist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _kommune(1)
        _mock_client(monkeypatch, lambda anfrage: httpx.Response(503))

        with pytest.raises(TargetUnavailableError):
            indexnow.indexnow([_ereignis(body, "Paper", uuid.uuid4())], delivery=None)

    @pytest.mark.usefixtures("mit_schluessel")
    def test_handler_ohne_betroffene_seite_meldet_nichts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gemeldet = _mock_client(monkeypatch, lambda anfrage: httpx.Response(200))

        indexnow.indexnow([_ereignis(_kommune(1, is_listed=False), "Paper", uuid.uuid4())], delivery=None)

        assert gemeldet == []
