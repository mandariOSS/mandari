# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stammdaten aus Bestandssystemen in einen Session-Mandanten übernehmen (Issue #762, L11a).

Wahlperioden, Gremien, Fraktionen, Ämter, Personen und Besetzungen aus Listen der Verwaltung (CSV oder XLSX,
eine Datei je Objektart). Erst prüfen, dann importieren; ein zweiter Lauf mit denselben Dateien ändert nichts.
Anleitung und Spalten: docs/SESSION_STAMMDATEN_IMPORT.md.

    # Leere Vorlagen für die Verwaltung
    python manage.py session_stammdaten_import --vorlagen /tmp/vorlagen

    # Vorlagen vorbefüllt mit dem öffentlichen Bestand einer Körperschaft im RIS-Bestand (z. B. aus dem
    # SessionNet-Adapter): Gremien, Personen, Besetzungen, Wahlperioden; Kontakt- und Bankdaten ergänzt die
    # Verwaltung
    python manage.py session_stammdaten_import --vorlagen /tmp/vorlagen --aus-ris "https://ratsinfo.example.de/bi/"

    # Prüflauf: liest und prüft alles, schreibt nichts; Bericht zusätzlich als Datei
    python manage.py session_stammdaten_import --tenant musterstadt /daten/import --dry-run \\
        --bericht /daten/import/pruefbericht.txt

    # Gegenprobe mit den öffentlichen Mitgliederlisten im RIS-Bestand (Body-Kennung oder UUID)
    python manage.py session_stammdaten_import --tenant musterstadt /daten/import --dry-run \\
        --gegenprobe https://ratsinfo.example.de/bi/

    # Importieren (nur ohne Fehler, ganz oder gar nicht)
    python manage.py session_stammdaten_import --tenant musterstadt /daten/import --bericht /daten/bericht.json
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.session.models import SessionTenant
from apps.session.services import stammdaten_import
from insight_core.services import public_members

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Übernimmt Stammdaten (Wahlperioden, Gremien, Personen, Besetzungen) aus CSV/XLSX in einen Mandanten."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("verzeichnis", nargs="?", help="Verzeichnis mit personen.csv, gremien.csv, …")
        parser.add_argument("--tenant", help="Slug des Session-Mandanten")
        parser.add_argument("--dry-run", action="store_true", help="Prüflauf: prüfen und berichten, nichts schreiben")
        parser.add_argument("--bericht", help="Bericht zusätzlich in diese Datei schreiben (.json oder Text)")
        parser.add_argument(
            "--gegenprobe",
            help="Body im RIS-Bestand (OParl-Kennung oder UUID), gegen dessen Mitgliederlisten geprüft wird",
        )
        parser.add_argument("--stichtag", help="Stichtag der Gegenprobe (JJJJ-MM-TT, Standard: heute)")
        parser.add_argument("--vorlagen", help="Vorlagen (CSV) in dieses Verzeichnis schreiben und beenden")
        parser.add_argument(
            "--aus-ris",
            help="Mit --vorlagen: Vorlagen mit dem öffentlichen Bestand dieses Bodys im RIS-Bestand vorbefüllen "
            "(OParl-Kennung oder UUID; Besetzungen am --stichtag)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["vorlagen"]:
            self._write_templates(Path(options["vorlagen"]), options["aus_ris"], self._day(options["stichtag"]))
            return
        if options["aus_ris"]:
            raise CommandError("--aus-ris nur zusammen mit --vorlagen.")

        if not options["tenant"] or not options["verzeichnis"]:
            raise CommandError("Bitte --tenant und das Verzeichnis mit den Importdateien angeben.")
        tenant = SessionTenant.objects.filter(slug=options["tenant"]).first()
        if tenant is None:
            raise CommandError(f"Mandant „{options['tenant']}“ nicht gefunden.")
        directory = Path(options["verzeichnis"])
        if not directory.is_dir():
            raise CommandError(f"Verzeichnis „{directory}“ nicht gefunden.")
        day = self._day(options["stichtag"])
        body = None
        if options["gegenprobe"]:
            body = public_members.find_body(options["gegenprobe"])
            if body is None:
                raise CommandError("Body für die Gegenprobe nicht im RIS-Bestand gefunden.")

        plan = stammdaten_import.plan_import(tenant, directory)
        if body is not None:
            stammdaten_import.cross_check(plan, public_members.current_members(body, day), day)
        if not options["dry_run"] and not plan.errors:
            try:
                stammdaten_import.apply_plan(plan)
            except Exception:
                logger.exception("Stammdaten-Import für Mandant %s fehlgeschlagen", tenant.slug)
                raise CommandError("Import fehlgeschlagen, nichts geschrieben (Einzelheiten im Log).") from None

        self.stdout.write(plan.as_text())
        if options["bericht"]:
            self._write_report(plan, Path(options["bericht"]))
        if plan.errors:
            raise CommandError(
                f"{len(plan.errors)} Fehler – nichts geschrieben. Dateien korrigieren und erneut prüfen."
            )
        if plan.applied:
            self.stdout.write(self.style.SUCCESS("Import ausgeführt."))
        else:
            self.stdout.write(self.style.SUCCESS("Prüflauf ohne Fehler. Zum Import ohne --dry-run aufrufen."))
        self.stdout.write(
            "Die Importdateien enthalten personenbezogene Daten (ggf. Bankdaten): nach dem Import sicher löschen."
        )

    def _write_templates(self, directory: Path, reference: str | None, day: date) -> None:
        roster = None
        if reference:
            body = public_members.find_body(reference)
            if body is None:
                raise CommandError("Body für die Vorlagen nicht im RIS-Bestand gefunden.")
            roster = public_members.public_roster(body, day)
        try:
            written = stammdaten_import.write_templates(directory, roster, day)
        except stammdaten_import.TemplateExistsError as exc:
            raise CommandError(
                f"Im Verzeichnis liegen schon Importdateien ({exc}); bitte ein leeres Verzeichnis angeben."
            ) from None
        for path, rows in written.items():
            self.stdout.write(f"Vorlage: {path}" + (f" ({rows} Zeilen)" if roster is not None else ""))
        if roster is not None:
            self.stdout.write(
                "Vorbefüllt aus dem öffentlichen Bestand: Namen, Arten und Funktionen prüfen, Kontakt- und "
                "Bankdaten ergänzen, dann den Prüflauf starten."
            )

    @staticmethod
    def _day(value: str | None) -> date:
        if not value:
            return timezone.localdate()
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise CommandError("--stichtag: Datum bitte im Format JJJJ-MM-TT angeben.") from None

    def _write_report(self, plan: stammdaten_import.ImportPlan, path: Path) -> None:
        if path.suffix.lower() == ".json":
            content = json.dumps(plan.as_dict(), ensure_ascii=False, indent=2, default=str) + "\n"
        else:
            content = plan.as_text()
        path.write_text(content, encoding="utf-8")
        self.stdout.write(f"Bericht: {path}")
