# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schutz gegen Formel-Injektion in CSV-Exporten (Issues #225, #221).

Tabellenkalkulationen werten Zellen, die mit ``=``, ``+``, ``-``, ``@``, Tabulator oder
Wagenrücklauf beginnen, als Formel aus. Stammen die Werte aus Eingaben Dritter (Namen, Betreffe,
User-Agent-Kennungen), ließe sich so beim Öffnen des Exports Code ausführen oder Daten abfließen.
Solche Zellen bekommen ein vorangestelltes Hochkomma und werden damit als Text gelesen. Reine
Zahlen (auch negative, mit Dezimalkomma) bleiben unverändert, damit Beträge Zahlen bleiben.

Alle CSV-Ausgaben der Anwendung schreiben über :func:`writer` – ein ``csv.writer``, der jede
Zelle durch :func:`csv_safe_cell` leitet.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

#: Zeichen, mit denen eine Zelle nicht beginnen darf (OWASP „CSV Injection“)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Reine Zahl: Vorzeichen, Ziffern, Dezimal- bzw. Tausendertrenner – keine Formel
_ZAHL = re.compile(r"[+-]?\d+(?:[.,]\d+)*")


def csv_safe_cell(value: Any) -> str:
    """Zellwert als Text; Formel-Anfänge werden mit einem Hochkomma entschärft."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float | Decimal):
        return str(value)
    text = str(value)
    if text.startswith(FORMULA_PREFIXES) and not _ZAHL.fullmatch(text):
        return "'" + text
    return text


def csv_unescape_cell(text: str) -> str:
    """Gegenstück für Importe eigener Exporte: das vorangestellte Hochkomma wieder entfernen."""
    if text.startswith("'") and text[1:].startswith(FORMULA_PREFIXES):
        return text[1:]
    return text


class SafeCsvWriter:
    """``csv.writer`` mit :func:`csv_safe_cell` für jede Zelle."""

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self._writer = csv.writer(target, **kwargs)

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow([csv_safe_cell(cell) for cell in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def writer(target: Any, **kwargs: Any) -> SafeCsvWriter:
    """Wie ``csv.writer(target, **kwargs)``, aber jede Zelle ist gegen Formel-Injektion geschützt."""
    return SafeCsvWriter(target, **kwargs)
