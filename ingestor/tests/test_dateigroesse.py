"""Größenangabe einer Datei aus OParl (Issue #786): unbrauchbare Werte werden nicht übernommen."""

from __future__ import annotations

import pytest

from src.sync.processor import OParlProcessor, file_size


@pytest.mark.parametrize(
    ("angabe", "erwartet"),
    [
        (4711, 4711),
        ("4711", 4711),
        (4711.0, 4711),
        (0, 0),
        (None, None),
        ("", None),
        ("vier", None),
        (-1, None),
        (2**31, None),
        (True, None),
        (12.5, None),
    ],
)
def test_groessenangabe_der_quelle(angabe: object, erwartet: int | None) -> None:
    assert file_size(angabe) == erwartet


def test_datei_mit_unsinniger_groesse() -> None:
    datei = {
        "id": "https://ris.example.org/oparl/file/1",
        "type": "https://schema.oparl.org/1.1/File",
        "accessUrl": "https://ris.example.org/oparl/file/1/download",
        "size": 2**40,
    }
    assert OParlProcessor().process_file(datei, "https://ris.example.org/oparl/body/1").size is None
