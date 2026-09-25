# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schutz gegen Formel-Injektion in CSV-Exporten (Issues #225, #221).

Tabellenkalkulationen werten Zellen, die mit ``=``, ``+``, ``-``, ``@``, Tabulator oder
Wagenrücklauf beginnen, als Formel aus. Stammen die Werte aus Eingaben Dritter (Namen, Betreffe,
User-Agent-Kennungen), ließe sich so beim Öffnen des Exports Code ausführen oder Daten abfließen.
Solche Zellen bekommen ein vorangestelltes Hochkomma und werden damit als Text gelesen.
"""

from __future__ import annotations

from typing import Any

#: Zeichen, mit denen eine Zelle nicht beginnen darf (OWASP „CSV Injection“)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value: Any) -> str:
    """Zellwert als Text; Formel-Anfänge werden mit einem Hochkomma entschärft."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text
