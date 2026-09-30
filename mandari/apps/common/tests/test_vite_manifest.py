# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vite-Build: Kein Chunk darf einen Einstieg importieren.

In Produktion benennt der Manifest-Storage jede gesammelte Datei um (``editor-X.js`` →
``editor-X.<hash>.js``); ``{% vite_asset %}`` lädt den Einstieg unter diesem Namen. Importe *innerhalb* der
Bundles schreibt der Storage nicht um – sie zeigen weiter auf den Vite-Namen. Für gemeinsam genutzte Chunks
ist das unschädlich, weil alle Importeure denselben Namen verwenden. Importiert aber ein nachgeladener Chunk
einen **Einstieg**, sieht der Browser zwei verschiedene Adressen und führt den Einstieg ein zweites Mal aus
(Alpine-Komponenten, Editor-Initialisierung). Mit Vite 8 (Rolldown) entstand genau das für das nachgeladene
pdf.js; ``preserveEntrySignatures: 'strict'`` in ``vite.config.ts`` verhindert es.

Weder die Entwicklung noch die E2E-Tests sehen den Fehler (dort gibt es keinen Manifest-Storage), deshalb
prüft dieser Test das gebaute Manifest. Ohne Build (``npm run build``) wird er übersprungen; in der CI ist
das Manifest vor den Tests gebaut.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

MANIFEST = Path(__file__).resolve().parents[3] / "static" / "dist" / "manifest.json"
EINSTIEGE = {
    "frontend/js/main.ts",
    "frontend/editor/index.ts",
    "frontend/js/work.ts",
    "frontend/js/webauthn.ts",
}

pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="Vite-Manifest fehlt (npm run build)")


def _manifest() -> dict[str, dict[str, Any]]:
    daten: dict[str, dict[str, Any]] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return daten


def test_manifest_nennt_die_einstiege() -> None:
    einstiege = {name for name, chunk in _manifest().items() if chunk.get("isEntry")}
    assert einstiege == EINSTIEGE, "Einstiege in vite.config.ts und dieser Test müssen zusammenpassen"


def test_kein_chunk_importiert_einen_einstieg() -> None:
    manifest = _manifest()
    einstiege = {name for name, chunk in manifest.items() if chunk.get("isEntry")}

    verstoesse = [
        f"{name} ({chunk['file']}) importiert den Einstieg {ziel}"
        for name, chunk in manifest.items()
        for ziel in chunk.get("imports", [])
        if ziel in einstiege
    ]

    assert not verstoesse, (
        "Ein Chunk importiert einen Einstieg – in Produktion würde der Einstieg doppelt ausgeführt "
        "(siehe Modulkommentar):\n" + "\n".join(verstoesse)
    )
