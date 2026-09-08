# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Prüfung: Django-Inline-Kommentare {# … #} dürfen nicht mehrzeilig sein.

Django wertet {# … #} nur innerhalb einer Zeile aus. Ein mehrzeiliger Kommentar
landet als sichtbarer Text in der Seite — das hat schon Produktionsseiten
(Vergleichstabellen, Design-Leck) getroffen. Mehrzeilig gehört in
{% comment %} … {% endcomment %}.

Aufruf: python scripts/check_template_comments.py [pfad …]  (Standard: mandari/templates)
"""

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"\{#(.*?)#\}", re.S)


def main(paths):
    roots = [Path(p) for p in paths] or [Path("mandari/templates")]
    hits = []
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.html"))
        for path in files:
            if "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in PATTERN.finditer(text):
                if "\n" in match.group(1):
                    line = text.count("\n", 0, match.start()) + 1
                    hits.append(f"{path}:{line}: mehrzeiliger {{# … #}}-Kommentar — bitte {{% comment %}} verwenden")
    for hit in hits:
        print(hit)
    print(f"{len(hits)} Fund(e) in {len(roots)} Pfad(en)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
