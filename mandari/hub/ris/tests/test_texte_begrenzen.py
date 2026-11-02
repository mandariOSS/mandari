# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``texte_begrenzen``: gespeicherte Texte über der Obergrenze der Texterkennung zählen (Probelauf) und mit
``--ausfuehren`` stapelweise kürzen, in ``oparl_files`` und ``session_files``; RIS-Dateien mit Vermerk wie bei der
Texterkennung. Die Grenze ist für den Test herabgesetzt.
"""

from __future__ import annotations

import importlib
import uuid
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from apps.session.models import SessionFile, SessionTenant
from insight_core.models import OParlBody, OParlFile, OParlSource

pytestmark = pytest.mark.django_db

GRENZE = 1_000


@pytest.fixture(autouse=True)
def kleine_grenze(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        befehl = importlib.import_module("hub.ris.management.commands.texte_begrenzen")
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(befehl, "MAX_TEXT_CHARS", GRENZE)


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


def test_probelauf_zaehlt_und_aendert_nichts_ausfuehren_kuerzt_stapelweise() -> None:
    lang = _ris_datei("ä" * (GRENZE + 500))
    mit_hinweis = _ris_datei("b" * (GRENZE + 1), text_extraction_error="Seite 3 übersprungen")
    kurz = _ris_datei("c" * GRENZE)
    # Mehr Bytes als die Grenze, aber nicht mehr Zeichen: nur im Vorfilter
    umlaute = _ris_datei("ö" * (GRENZE - 100))
    tenant = SessionTenant.objects.create(name="Stadt Muster", slug="texte-begrenzen")
    anlage = SessionFile.objects.create(tenant=tenant, name="Anlage.pdf", text_content="d" * (GRENZE + 10))

    probe = _befehl()

    assert "oparl_files: 2 Dateien über der Obergrenze" in probe
    assert "session_files: 1 Dateien über der Obergrenze" in probe and "Nichts geändert" in probe
    lang.refresh_from_db()
    assert len(lang.text_content or "") == GRENZE + 500

    ausgefuehrt = _befehl("--ausfuehren", "--stapel", "1")

    assert "oparl_files: 2 Texte gekürzt" in ausgefuehrt and "session_files: 1 Texte gekürzt" in ausgefuehrt
    for datei in (lang, mit_hinweis, kurz, umlaute):
        datei.refresh_from_db()
    anlage.refresh_from_db()
    assert lang.text_content == "ä" * GRENZE and mit_hinweis.text_content == "b" * GRENZE
    assert kurz.text_content == "c" * GRENZE and kurz.text_extraction_error is None
    assert umlaute.text_content == "ö" * (GRENZE - 100) and umlaute.text_extraction_error is None
    assert lang.text_extraction_error == "Text nach 1.000 Zeichen gekürzt (Obergrenze je Datei)"
    assert (
        mit_hinweis.text_extraction_error
        == "Seite 3 übersprungen; Text nach 1.000 Zeichen gekürzt (Obergrenze je Datei)"
    )
    assert anlage.text_content == "d" * GRENZE

    # Wiederholbar: nichts mehr über der Grenze, nichts doppelt vermerkt
    erneut = _befehl("--ausfuehren")
    assert "oparl_files: 0 Dateien" in erneut and "session_files: 0 Dateien" in erneut
    lang.refresh_from_db()
    assert lang.text_extraction_error == "Text nach 1.000 Zeichen gekürzt (Obergrenze je Datei)"
