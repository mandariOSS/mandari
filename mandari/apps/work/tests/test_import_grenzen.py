# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenzen des DOCX-Imports: Das entpackte Paket wird geprüft, bevor python-docx es liest.
"""

from __future__ import annotations

import io
import zipfile

import docx
import pytest

from apps.work.motions import import_service


def _paket(teile: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
        for name, inhalt in teile.items():
            archiv.writestr(name, inhalt)
    return puffer.getvalue()


def test_gewoehnliches_dokument_besteht_die_pruefung() -> None:
    dokument = docx.Document()
    dokument.add_paragraph("Text")
    puffer = io.BytesIO()
    dokument.save(puffer)

    import_service.check_docx_package(puffer.getvalue())
    # Kein Archiv: Das meldet wie bisher python-docx, nicht diese Prüfung
    import_service.check_docx_package(b"kein Archiv")


@pytest.mark.parametrize(
    "teile",
    [
        {"word/document.xml": b" " * (26 * 1024 * 1024)},
        {"word/media/bild.bin": bytes(151 * 1024 * 1024)},
        {f"word/teil{i}.txt": b"x" for i in range(5_001)},
    ],
)
def test_zu_grosses_paket_wird_vor_dem_lesen_abgelehnt(teile: dict[str, bytes]) -> None:
    daten = _paket(teile)
    assert len(daten) < 2 * 1024 * 1024  # gepackt klein, entpackt groß

    with pytest.raises(import_service.DocxTooLargeError):
        import_service.check_docx_package(daten)
