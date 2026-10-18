# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Untergrenze des Abruftakts je Quelle (Issue #1025).

Standard ist eine Anfrage je Sekunde und Server (``mandari_oparl.pacing.DEFAULT_INTERVAL``, höchstens zwei
gleichzeitig offen). Eine Quelle darf über ``sync_config["request_interval"]`` nur langsamer werden: Werte unter
einer Sekunde, auch 0, werden auf eine Sekunde angehoben und im Log gemeldet. Das gilt für alle Nutzer der
gemeinsamen Drossel (OParl-Abruf, Textextraktion, Django) und für den Abstand der Scraper. Die
Betriebsvariable ``INGESTOR_REQUEST_INTERVAL`` (0 = Drossel aus, Tests und Notfälle) bleibt, wie sie ist.
"""

from __future__ import annotations

import logging
import math

import pytest
from mandari_oparl import pacing
from mandari_oparl.pacing import DEFAULT_INTERVAL, MIN_REQUEST_INTERVAL, interval_for, request_interval

from src.client.oparl_client import OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.scrapers.base import ScraperConfig

pytestmark = pytest.mark.usefixtures("echte_drossel")


@pytest.fixture(autouse=True)
def _meldungen_je_test() -> None:
    """Jede Unterschreitung meldet sich einmal je Prozess; im Test je Test neu."""
    pacing._reported.clear()


def test_standard_bleibt_eine_anfrage_je_sekunde():
    assert DEFAULT_INTERVAL == MIN_REQUEST_INTERVAL == 1.0
    assert type(settings).model_fields["request_interval"].default == 1.0
    assert type(settings).model_fields["host_max_concurrent"].default == 2


@pytest.mark.parametrize("wert", [0, 0.0, 0.25, 0.5, 0.999])
def test_schnellerer_takt_wird_angehoben(wert, caplog):
    with caplog.at_level(logging.WARNING, logger="mandari_oparl.pacing"):
        assert request_interval(wert) == 1.0
        assert request_interval(wert) == 1.0
    meldungen = [r for r in caplog.records if "Untergrenze" in r.getMessage()]
    assert len(meldungen) == 1  # einmal je Wert, nicht bei jedem Abruf
    assert meldungen[0].levelno == logging.WARNING
    assert "request_interval" in meldungen[0].getMessage()


@pytest.mark.parametrize("wert", [1, 1.0, 2.5, 30])
def test_koeln_bonn_und_langsamere_werte_bleiben(wert, caplog):
    with caplog.at_level(logging.WARNING, logger="mandari_oparl.pacing"):
        assert request_interval(wert) == float(wert)
    assert "Untergrenze" not in caplog.text


@pytest.mark.parametrize("wert", [None, True, "0.5", -1, 31, math.nan, math.inf])
def test_ungueltige_werte_gelten_als_nicht_gesetzt(wert):
    assert request_interval(wert) is None


def test_quellenoptionen_und_django_nutzen_dieselbe_grenze():
    # Ingestor: OParl-Abruf und Textextraktion lesen SourceFetchOptions
    assert SourceFetchOptions.from_sync_config({"request_interval": 0.2}).request_interval == 1.0
    assert SourceFetchOptions.from_sync_config({"request_interval": 0}).request_interval == 1.0
    assert SourceFetchOptions.from_sync_config({"request_interval": 1.0}).request_interval == 1.0
    # Django (insight_core/services/host_pacing.py) liest interval_for
    assert interval_for({"request_interval": 0.2}) == 1.0
    assert interval_for({"request_interval": 0}, default=0.0) == 1.0
    assert interval_for({}, default=0.0) == 0.0  # RIS_REQUEST_INTERVAL=0 schaltet weiter ab


def test_client_einer_quelle_wird_nicht_schneller():
    options = SourceFetchOptions.from_sync_config({"request_interval": 0.1})
    client = OParlClient(**options.client_kwargs())
    assert client.effective_interval == 1.0


def test_betriebsvariable_null_schaltet_weiter_ab(monkeypatch):
    monkeypatch.setattr(settings, "request_interval", 0.0)
    assert OParlClient().effective_interval == 0.0
    # Eine Quelle mit eigenem Wert bleibt auch dann bei mindestens einer Sekunde
    client = OParlClient(**SourceFetchOptions.from_sync_config({"request_interval": 0}).client_kwargs())
    assert client.effective_interval == 1.0


@pytest.mark.parametrize(("wert", "erwartet"), [(None, 2.0), (0, 1.0), (0.3, 1.0), (1.5, 1.5), (5, 5.0)])
def test_scraper_abstand_mindestens_eine_sekunde(wert, erwartet):
    scraper: dict[str, object] = {"base_url": "https://rat.example.de/bi/"}
    if wert is not None:
        scraper["rate_limit_seconds"] = wert
    assert ScraperConfig.from_sync_config({"scraper": scraper}).rate_limit_seconds == erwartet
