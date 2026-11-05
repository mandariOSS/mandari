# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die OpenAPI-Beschreibung der Schnittstelle als Datei schreiben oder prüfen (Issue #1205).

    python manage.py schnittstelle_beschreibung            # docs/schnittstelle/openapi.json neu schreiben
    python manage.py schnittstelle_beschreibung --pruefen  # Exit-Code 1, wenn die Datei veraltet ist

Die Beschreibung entsteht aus dem Code (``hub.api.entry.description``) und ist eingecheckt, damit die CI
brechende Änderungen gegenüber ``dev`` erkennt. Wer einen Endpunkt ändert, schreibt sie neu und committet
sie mit; ``hub/api/tests/test_schnittstelle.py`` scheitert sonst.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

#: Ort der eingecheckten Beschreibung, vom Repo-Wurzelverzeichnis aus
DESCRIPTION_PATH: Final = Path("docs") / "schnittstelle" / "openapi.json"


def description_file() -> Path:
    return Path(settings.BASE_DIR).parent / DESCRIPTION_PATH


def current_text() -> str:
    from hub.api.entry import description
    from hub.api.openapi.build import render

    return render(description())


class Command(BaseCommand):
    help = "Schreibt oder prüft die eingecheckte OpenAPI-Beschreibung (docs/schnittstelle/openapi.json)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--pruefen", action="store_true", help="Nur prüfen, ob die Datei aktuell ist.")

    def handle(self, *args: Any, **options: Any) -> None:
        path = description_file()
        text = current_text()
        if options["pruefen"]:
            stored = path.read_text(encoding="utf-8") if path.exists() else ""
            if stored != text:
                raise CommandError(
                    f"{DESCRIPTION_PATH.as_posix()} ist veraltet. Neu schreiben mit "
                    "`python manage.py schnittstelle_beschreibung` (in mandari/) und mitcommitten."
                )
            self.stdout.write(f"{DESCRIPTION_PATH.as_posix()} ist aktuell.")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        self.stdout.write(f"{DESCRIPTION_PATH.as_posix()} geschrieben.")
