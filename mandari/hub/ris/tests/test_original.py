# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Das Original im Ratsinformationssystem (``hub.ris.original``, Issue #1259).

- OParl-Quellen: nur das Attribut ``web``, nur als http(s)-Adresse; ohne ``web`` kein Link (nichts geraten).
- Scraper-Quellen: die Kennung als Detailseite, wenn sie unter der Basisadresse der Quelle liegt, mit deren Schema;
  Sitzungen nur mit gelesener Tagesordnung.
- Dokumente: ``access_url``, sonst ``download_url``.
- Eigene Daten aus mandari Session: kein Link.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hub.ris.original import dokument_im_ris, seite_im_ris
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

OPARL = "https://oparl.beispielstadt.example/oparl"
SCRAPER = "https://ris.scraperstadt.example/bi/"


def _kommune(url: str, sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(name=f"RIS {url}", url=url, sync_config=sync_config or {})
    return OParlBody.objects.create(external_id=f"{url}#body", source=source, name="Beispielstadt")


@pytest.fixture
def oparl() -> OParlBody:
    return _kommune(f"{OPARL}/system")


@pytest.fixture
def scraper() -> OParlBody:
    return _kommune(SCRAPER, {"source_type": "scraper:sessionnet", "scraper": {"base_url": SCRAPER}})


class TestOParl:
    def test_web_der_quelle(self, oparl: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(
            external_id=f"{OPARL}/paper/1", body=oparl, raw_json={"web": "https://ris.beispielstadt.example/vo?id=1"}
        )
        assert seite_im_ris(vorgang) == "https://ris.beispielstadt.example/vo?id=1"

    @pytest.mark.parametrize("web", ["javascript:alert(1)", "ftp://ris.example/vo", "/vo020.asp", 17, None, ""])
    def test_nur_http_adressen(self, oparl: OParlBody, web: Any) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{OPARL}/paper/1", body=oparl, raw_json={"web": web})
        assert seite_im_ris(vorgang) == ""

    def test_ohne_web_kein_link(self, oparl: OParlBody) -> None:
        """Die Kennung einer OParl-Quelle ist die Adresse der Schnittstelle, keine Seite für Menschen."""
        vorgang = OParlPaper.objects.create(external_id=f"{OPARL}/paper/1", body=oparl, raw_json={})
        sitzung = OParlMeeting.objects.create(external_id=f"{OPARL}/meeting/1", body=oparl, raw_json={})
        assert seite_im_ris(vorgang) == ""
        assert seite_im_ris(sitzung) == ""

    def test_mit_web_ohne_abfrage(self, oparl: OParlBody) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{OPARL}/meeting/1", body=oparl, raw_json={"web": "https://ris.beispielstadt.example/si/1"}
        )
        geladen = OParlMeeting.objects.get(pk=sitzung.pk)
        with CaptureQueriesContext(connection) as abfragen:
            assert seite_im_ris(geladen) == "https://ris.beispielstadt.example/si/1"
        assert len(abfragen) == 0

    def test_eigene_daten_aus_session(self, oparl: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(
            external_id="https://mandari.example/session/stadt/api/oparl/paper/1",
            body=oparl,
            raw_json={"web": "https://mandari.example/insight/vorgaenge/1/"},
        )
        assert seite_im_ris(vorgang) == ""


class TestScraper:
    def test_vorgang_ist_seine_detailseite(self, scraper: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{SCRAPER}vo0050.asp?__kvonr=4711", body=scraper)
        assert seite_im_ris(vorgang) == f"{SCRAPER}vo0050.asp?__kvonr=4711"

    def test_schema_der_basisadresse(self) -> None:
        """Die Kennung erzwingt https; die Seite liegt unter dem Schema, mit dem die Quelle eingetragen ist."""
        basis = "http://RIS.Altstadt.example/sessionnet/"
        body = _kommune(basis, {"source_type": "scraper:sessionnet", "scraper": {"base_url": basis}})
        vorgang = OParlPaper.objects.create(
            external_id="https://ris.altstadt.example/sessionnet/vo0050.php?__kvonr=7", body=body
        )
        assert seite_im_ris(vorgang) == "http://RIS.Altstadt.example/sessionnet/vo0050.php?__kvonr=7"

    def test_kennung_ausserhalb_der_basis(self, scraper: OParlBody) -> None:
        """Nach einem Umzug mit festgeschriebener Basis zeigt die Kennung auf die alte Adresse: kein Link."""
        vorgang = OParlPaper.objects.create(
            external_id="https://alt.scraperstadt.example/bi/vo0050.asp?__kvonr=1", body=scraper
        )
        assert seite_im_ris(vorgang) == ""

    def test_sitzung_nur_mit_tagesordnung(self, scraper: OParlBody) -> None:
        angekuendigt = OParlMeeting.objects.create(external_id=f"{SCRAPER}si0057.asp?__ksinr=1", body=scraper)
        mit_tagesordnung = OParlMeeting.objects.create(
            external_id=f"{SCRAPER}si0057.asp?__ksinr=2",
            body=scraper,
            raw_json={"agendaItem": [{"id": f"{SCRAPER}si0057.asp?__ksinr=2#top/1"}]},
        )
        assert seite_im_ris(angekuendigt) == ""
        assert seite_im_ris(mit_tagesordnung) == f"{SCRAPER}si0057.asp?__ksinr=2"

    def test_ohne_basisadresse(self) -> None:
        body = _kommune("https://ris.ohnebasis.example/", {"source_type": "scraper:sessionnet"})
        vorgang = OParlPaper.objects.create(external_id="https://ris.ohnebasis.example/vo0050.asp?__kvonr=1", body=body)
        assert seite_im_ris(vorgang) == ""

    def test_quelle_uebergeben(self, scraper: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{SCRAPER}vo0050.asp?__kvonr=5", body=scraper)
        geladen = OParlPaper.objects.get(pk=vorgang.pk)
        with CaptureQueriesContext(connection) as abfragen:
            assert seite_im_ris(geladen, scraper.source) == f"{SCRAPER}vo0050.asp?__kvonr=5"
        assert len(abfragen) == 0


class TestDokument:
    def test_abrufadresse(self, oparl: OParlBody) -> None:
        datei = OParlFile.objects.create(
            external_id=f"{OPARL}/file/1",
            body=oparl,
            access_url="https://ris.beispielstadt.example/getfile?id=1",
            download_url="https://ris.beispielstadt.example/getfile?id=1&download=1",
        )
        assert dokument_im_ris(datei) == "https://ris.beispielstadt.example/getfile?id=1"

    def test_sonst_downloadadresse(self, oparl: OParlBody) -> None:
        datei = OParlFile.objects.create(
            external_id=f"{OPARL}/file/2", body=oparl, download_url="https://ris.beispielstadt.example/dl/2.pdf"
        )
        assert dokument_im_ris(datei) == "https://ris.beispielstadt.example/dl/2.pdf"

    def test_ohne_http_adresse(self, oparl: OParlBody) -> None:
        datei = OParlFile.objects.create(external_id=f"{OPARL}/file/3", body=oparl, access_url="ftp://ris.example/3")
        assert dokument_im_ris(datei) == ""

    def test_eigene_daten_aus_session(self, oparl: OParlBody) -> None:
        datei = OParlFile.objects.create(
            external_id="https://mandari.example/session/stadt/api/oparl/file/1",
            body=oparl,
            access_url="https://mandari.example/session/stadt/api/oparl/file/1/download",
        )
        assert dokument_im_ris(datei) == ""
