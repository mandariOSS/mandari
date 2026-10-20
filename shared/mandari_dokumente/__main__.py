"""
Prozess der ersten Stufe der Texterkennung (Issue #1107):

    python -m mandari_dokumente textebene [--mime TYP] [--name DATEINAME] [--max-pages N] [--gemischt] PFAD

Gibt das Ergebnis von ``texterkennung.extract_text_layer`` als JSON auf stdout aus. Gestartet von
``texterkennung.extract_text_layer_isolated`` mit Speicher- und Zeitgrenze; für Menschen gibt es nichts zu tun.
"""

from __future__ import annotations

import sys

from .texterkennung import text_layer_main


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "textebene":
        sys.stderr.write("Aufruf: python -m mandari_dokumente textebene [Optionen] PFAD\n")
        return 2
    return text_layer_main(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
