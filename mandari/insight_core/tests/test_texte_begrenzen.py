# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``texte_begrenzen``: gespeicherte Texte über der Obergrenze der Texterkennung zählen (Probelauf, mit jeder
Kennung) und mit ``--ausfuehren`` stapelweise kürzen, in ``oparl_files`` und ``session_files``; RIS-Dateien mit
Vermerk wie bei der Texterkennung und danach neu im Suchindex. Die Bedingung steht im ``UPDATE``: Eine inzwischen
kürzere Datei bleibt unberührt. Die Grenze ist für den Test herabgesetzt.
"""

from __future__ import annotations

import importlib
import uuid
from io import StringIO
from types import ModuleType
from typing import Any

import pytest
from django.core.management import call_command

from apps.session.models import SessionFile, SessionTenant
from insight_core.models import OParlBody, OParlFile, OParlSource

pytestmark = pytest.mark.django_db

GRENZE = 1_000
VERMERK = "Text nach 1.000 Zeichen gekürzt (Obergrenze je Datei)"


@pytest.fixture(autouse=True)
def befehl(monkeypatch: pytest.MonkeyPatch) -> ModuleType | None:
    try:
        modul = importlib.import_module("insight_core.management.commands.texte_begrenzen")
    except ModuleNotFoundError:
        return None
    monkeypatch.setattr(modul, "MAX_TEXT_CHARS", GRENZE)
    return modul


@pytest.fixture
def suchindex(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Was in den Suchindex geschrieben würde (Index, Kennung, Dokument)."""
    geschrieben: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "insight_core.signals._index_document",
        lambda index, kennung, dokument: geschrieben.append((index, kennung, dokument)),
    )
    return geschrieben


def _ris_datei(text: str, **felder: Any) -> OParlFile:
    kennung = uuid.uuid4().hex[:8]
    source = OParlSource.objects.create(name=f"Quelle {kennung}", url=f"https://{kennung}.example.org/oparl/system")
    body = OParlBody.objects.create(source=source, external_id=f"https://{kennung}.example.org/oparl/body/1")
    return OParlFile.objects.create(
        body=body, external_id=f"https://{kennung}.example.org/oparl/file/1", name="Anlage", text_content=text, **felder
    )


def _befehl(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("texte_begrenzen", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def test_probelauf_zaehlt_und_aendert_nichts_ausfuehren_kuerzt_stapelweise(
    suchindex: list[tuple[str, str, dict[str, Any]]],
) -> None:
    lang = _ris_datei("ä" * (GRENZE + 500))
    mit_hinweis = _ris_datei("b" * (GRENZE + 1), text_extraction_error="Seite 3 übersprungen")
    kurz = _ris_datei("c" * GRENZE)
    # Mehr Bytes als die Grenze, aber nicht mehr Zeichen: nur im Vorfilter
    umlaute = _ris_datei("ö" * (GRENZE - 100))
    tenant = SessionTenant.objects.create(name="Stadt Muster", slug="texte-begrenzen")
    anlage = SessionFile.objects.create(tenant=tenant, name="Anlage.pdf", text_content="d" * (GRENZE + 10))
    # Das Anlegen schreibt schon in den Suchindex (Signal beim Speichern); gezählt wird ab hier
    suchindex.clear()

    probe = _befehl()

    assert "oparl_files: 2 Dateien über der Obergrenze" in probe
    assert "session_files: 1 Dateien über der Obergrenze" in probe and "Nichts geändert" in probe
    for datei in (lang, mit_hinweis, anlage):
        assert str(datei.pk) in probe
    assert str(kurz.pk) not in probe and str(umlaute.pk) not in probe
    lang.refresh_from_db()
    assert len(lang.text_content or "") == GRENZE + 500 and suchindex == []

    ausgefuehrt = _befehl("--ausfuehren", "--stapel", "1")

    assert "oparl_files: 2 Texte gekürzt" in ausgefuehrt and "session_files: 1 Texte gekürzt" in ausgefuehrt
    for datei in (lang, mit_hinweis, kurz, umlaute):
        datei.refresh_from_db()
    anlage.refresh_from_db()
    assert lang.text_content == "ä" * GRENZE and mit_hinweis.text_content == "b" * GRENZE
    assert kurz.text_content == "c" * GRENZE and kurz.text_extraction_error is None
    assert umlaute.text_content == "ö" * (GRENZE - 100) and umlaute.text_extraction_error is None
    assert lang.text_extraction_error == VERMERK
    assert mit_hinweis.text_extraction_error == f"Seite 3 übersprungen; {VERMERK}"
    assert anlage.text_content == "d" * GRENZE
    # Die gekürzten RIS-Dateien stehen danach mit dem gekürzten Text im Suchindex
    neu = {kennung: dokument for index, kennung, dokument in suchindex if index == "files"}
    assert set(neu) == {str(lang.pk), str(mit_hinweis.pk)}
    assert all(len(dokument["text_content"]) == GRENZE for dokument in neu.values())

    # Wiederholbar: nichts mehr über der Grenze, nichts doppelt vermerkt
    erneut = _befehl("--ausfuehren")
    assert "oparl_files: 0 Dateien" in erneut and "session_files: 0 Dateien" in erneut
    lang.refresh_from_db()
    assert lang.text_extraction_error == VERMERK


def test_kuerzen_prueft_die_laenge_im_update(befehl: ModuleType | None) -> None:
    # Gezählt über der Grenze, inzwischen neu erkannt und kürzer: bleibt unberührt
    datei = _ris_datei("e" * (GRENZE - 1))

    assert befehl is not None
    assert befehl._kuerzen(OParlFile, [datei.pk]) == []
    datei.refresh_from_db()
    assert datei.text_content == "e" * (GRENZE - 1) and datei.text_extraction_error is None
