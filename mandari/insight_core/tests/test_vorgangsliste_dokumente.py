# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlage und Anlagen direkt in der Vorgangsliste (Issue #1103), wie in der Trefferliste von SessionNet.

Je Vorgang zeigt die Liste unter dem Titel das Hauptdokument (OParl ``mainFile``, sonst das erste Dokument) als Link
mit Dateityp und Größe und die Anlagen zum Aufklappen. Es gelten die Regeln der Vorgangsseite: keine zurückgenommenen
Anlagen, keine durch den Löschabgleich gesperrten Dokumente (#787). Die Dateien der Seite kommen in einer Abfrage.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services import vorgang_dokumente
from insight_core.services.vorgang_dokumente import ANLAGEN_IN_DER_LISTE, dokumente_anhaengen, dokumentzeile
from insight_core.templatetags.insight_listen import dateityp
from insight_core.views import papers as papers_views

pytestmark = pytest.mark.django_db

RIS = "https://ris.listenstadt.example/oparl"
#: Kennung einer Datei aus mandari Session (zurückgenommen = gelöscht mit diesen Markern)
SESSION = "https://mandari.example/session/listenstadt/api/oparl"
LISTE = "/insight/k/listenstadt/vorgaenge/"


@pytest.fixture
def kommune() -> OParlBody:
    source = OParlSource.objects.create(name="RIS Listenstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Listenstadt", slug="listenstadt")


def _vorgang(body: OParlBody, nummer: int, **felder: Any) -> OParlPaper:
    felder.setdefault("name", f"Radweg am Kanal {nummer}")
    felder.setdefault("reference", f"V/{nummer:04d}/2026")
    return OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=body, **felder)


def _datei(vorgang: OParlPaper, kennung: str, name: str, **felder: Any) -> OParlFile:
    felder.setdefault("mime_type", "application/pdf")
    felder.setdefault("download_url", f"https://ris.listenstadt.example/datei/{name}.pdf")
    return OParlFile.objects.create(
        external_id=felder.pop("external_id", f"{RIS}/file/{kennung}"),
        body=vorgang.body,
        paper=vorgang,
        name=name,
        **felder,
    )


@pytest.fixture
def vorgang_mit_anlagen(kommune: OParlBody) -> OParlPaper:
    """Hauptdokument (laut ``mainFile``), eine Anlage, eine zurückgenommene und eine gesperrte Anlage."""
    vorgang = _vorgang(
        kommune,
        591,
        raw_json={"mainFile": {"id": f"{RIS}/file/haupt"}, "auxiliaryFile": [{"id": f"{RIS}/file/lageplan"}]},
    )
    _datei(vorgang, "haupt", "Zeichnung der Vorlage", size=245760)
    _datei(vorgang, "lageplan", "Anlage 1 Lageplan", size=81920)
    _datei(vorgang, "x", "Anlage 2 zurückgenommen", external_id=f"{SESSION}/file/9", deleted=True)
    _datei(vorgang, "weg", "Anlage 3 entfernt", source_missing_since=timezone.now())
    return vorgang


class TestVorgangsliste:
    def test_hauptdokument_und_eine_anlage_ohne_zurueckgenommene_und_gesperrte(
        self, vorgang_mit_anlagen: OParlPaper
    ) -> None:
        html = Client().get(LISTE).content.decode()

        haupt = OParlFile.objects.get(external_id=f"{RIS}/file/haupt")
        anlage = OParlFile.objects.get(external_id=f"{RIS}/file/lageplan")
        assert f'href="/insight/dokumente/{haupt.pk}/preview/" rel="nofollow"' in html
        assert f'href="/insight/dokumente/{anlage.pk}/preview/" rel="nofollow"' in html
        # Verständlicher Linktext: sichtbar „Vorlage“ mit Typ und Größe, für Screenreader mit Nummer
        assert 'Vorlage<span class="sr-only"> V/0591/2026 öffnen,</span>' in html
        assert "PDF, 240.0 KB" in html
        assert '1 Anlage<span class="sr-only"> zu V/0591/2026</span>' in html and "<details" in html
        assert "Anlage 1 Lageplan" in html and "PDF, 80.0 KB" in html
        assert "Anlage 2 zurückgenommen" not in html and "Anlage 3 entfernt" not in html
        # Das Hauptdokument erscheint nicht noch einmal unter den Anlagen
        assert html.count(f"/insight/dokumente/{haupt.pk}/preview/") == 1

    def test_ohne_dokumente_keine_dokumentzeile(self, kommune: OParlBody) -> None:
        _vorgang(kommune, 1)
        ohne_adresse = _vorgang(kommune, 2)
        _datei(ohne_adresse, "ohne", "Ohne Adresse", download_url=None, access_url=None)

        html = Client().get(LISTE).content.decode()

        assert "Radweg am Kanal 1" in html and "Radweg am Kanal 2" in html
        assert 'data-testid="vorgang-dokumente"' not in html

    def test_htmx_ausschnitt_mit_dokumenten(self, vorgang_mit_anlagen: OParlPaper) -> None:
        html = Client().get(LISTE, {"q": "Radweg"}, headers={"HX-Request": "true"}).content.decode()

        assert "<html" not in html and 'data-testid="vorgang-dokumente"' in html and "1 Anlage" in html

    def test_hoechstens_eine_abfrage_mehr_und_keine_je_vorgang(
        self, kommune: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def abfragen() -> int:
            client = Client()
            client.get(LISTE)  # Caches anwärmen
            with CaptureQueriesContext(connection) as erfasst:
                assert client.get(LISTE).status_code == 200
            return len(erfasst.captured_queries)

        for nummer in range(3):
            vorgang = _vorgang(kommune, nummer)
            _datei(vorgang, f"{nummer}-a", f"Vorlage {nummer}")
            _datei(vorgang, f"{nummer}-b", f"Anlage {nummer}")
        wenige = abfragen()
        for nummer in range(3, 25):
            vorgang = _vorgang(kommune, nummer)
            _datei(vorgang, f"{nummer}-a", f"Vorlage {nummer}")
            _datei(vorgang, f"{nummer}-b", f"Anlage {nummer}")
        viele = abfragen()
        monkeypatch.setattr(papers_views, "dokumente_anhaengen", lambda papers: None)
        ohne_dokumente = abfragen()

        assert viele == wenige, "keine Abfrage je Vorgang"
        assert viele - ohne_dokumente <= 1, "höchstens eine Abfrage mehr als ohne Dokumentzeile"


class TestDokumentzeile:
    def test_hauptdokument_aus_mainfile_anlagen_in_der_reihenfolge_der_quelle(self, kommune: OParlBody) -> None:
        vorgang = _vorgang(
            kommune,
            7,
            raw_json={
                "mainFile": f"{RIS}/file/c",
                "auxiliaryFile": [{"id": f"{RIS}/file/b"}, {"id": f"{RIS}/file/a"}],
            },
        )
        for kennung, name in (("a", "Alpha"), ("b", "Beta"), ("c", "Zeta"), ("d", "Delta")):
            _datei(vorgang, kennung, name)

        dokumente_anhaengen([vorgang])

        zeile = cast(Any, vorgang).dokumentzeile
        assert zeile.haupt.name == "Zeta"
        assert [d.name for d in zeile.anlagen] == ["Beta", "Alpha", "Delta"]

    def test_ohne_mainfile_das_erste_dokument(self) -> None:
        dateien = [SimpleNamespace(external_id=f"x/{n}", name=n, file_name="") for n in ("Beta", "alpha")]

        zeile = dokumentzeile(SimpleNamespace(raw_json={}), dateien)

        assert zeile is not None and zeile.haupt.name == "alpha" and [d.name for d in zeile.anlagen] == ["Beta"]
        assert dokumentzeile(SimpleNamespace(raw_json={}), []) is None

    def test_zurueckgestellte_rohdaten_ohne_abfrage_je_vorgang(
        self, kommune: OParlBody, django_assert_num_queries: Any
    ) -> None:
        for nummer in range(3):
            _datei(_vorgang(kommune, nummer, raw_json={"mainFile": "x"}), str(nummer), f"Vorlage {nummer}")
        vorgaenge = list(OParlPaper.objects.defer("raw_json"))

        with django_assert_num_queries(1):
            dokumente_anhaengen(vorgaenge)

        assert all(cast(Any, v).dokumentzeile is not None for v in vorgaenge)

    def test_lange_listen_nennen_nur_die_ersten_anlagen(self, kommune: OParlBody) -> None:
        vorgang = _vorgang(kommune, 8)
        for nummer in range(ANLAGEN_IN_DER_LISTE + 3):
            _datei(vorgang, str(nummer), f"Anlage {nummer:02d}")

        dokumente_anhaengen([vorgang])
        html = Client().get(LISTE).content.decode()

        zeile = cast(Any, vorgang).dokumentzeile
        assert len(zeile.anlagen) == ANLAGEN_IN_DER_LISTE + 2 and zeile.weitere == 2
        assert len(zeile.anlagen_kurz) == ANLAGEN_IN_DER_LISTE
        assert "12 Anlagen" in html and f'href="/insight/vorgaenge/{vorgang.pk}/#dokumente-titel"' in html
        assert "Alle 12 Anlagen beim Vorgang" in html and "Anlage 12" not in html

    def test_leere_liste_ohne_abfrage(self, django_assert_num_queries: Any) -> None:
        with django_assert_num_queries(0):
            vorgang_dokumente.dokumente_anhaengen([])


@pytest.mark.parametrize(
    ("mime_type", "file_name", "erwartet"),
    [
        ("application/pdf", "", "PDF"),
        ("", "Lageplan.PDF", "PDF"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Antrag.docx", "DOCX"),
        ("", "ohne-endung", "Dokument"),
        ("", "seltsam.endung-zu-lang", "Dokument"),
        (None, None, "Dokument"),
    ],
)
def test_dateityp(mime_type: str | None, file_name: str | None, erwartet: str) -> None:
    assert dateityp(SimpleNamespace(mime_type=mime_type, file_name=file_name)) == erwartet
