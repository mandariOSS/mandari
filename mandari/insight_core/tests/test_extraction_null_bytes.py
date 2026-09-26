# SPDX-License-Identifier: AGPL-3.0-or-later
"""Extrahierter Text enthält keine Null-Bytes (PostgreSQL lehnt sie in Textfeldern ab)."""

from __future__ import annotations

import logging.config

from apps.common.observability import logging_config
from insight_core.services.document_extraction import extract_text_from_file

NUL = chr(0)


def test_null_bytes_werden_entfernt() -> None:
    daten = f"Rat{NUL}sbeschluss vom{NUL} 25.09.".encode()
    text, _ocr, _seiten, _methode = extract_text_from_file(daten, "text/plain", "anlage.txt")
    assert NUL not in text
    assert text == "Ratsbeschluss vom 25.09."


def test_laute_bibliotheken_sind_gedaempft() -> None:
    logging.config.dictConfig(logging_config(debug=False))
    assert logging.getLogger("elastic_transport").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("pypdf").getEffectiveLevel() >= logging.ERROR
