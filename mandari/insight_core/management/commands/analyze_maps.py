# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Karten und Pläne im Dateicache einer Kommune zählen (Issue #599).

Liest nur Dateien, die schon lokal im Dateicache liegen – kein Abruf beim Ratsinformationssystem,
kein Netz – und speichert nichts in der Datenbank:

    python manage.py analyze_maps --body muenster --dry-run
    python manage.py analyze_maps --body muenster --dry-run --limit 2000 --json /tmp/karten.json
    python manage.py analyze_maps --body muenster --dry-run --sample-csv /tmp/stichprobe.csv --sample-size 200
    python manage.py analyze_maps --evaluate /tmp/stichprobe.csv     # von Hand ausgefüllte Stichprobe auswerten

Die Kennzahlen: Kartenseiten, davon mit Maßstab im PDF, GeoPDF, Koordinatenbeschriftung, mindestens
vier Straßennamen aus dem Straßenverzeichnis, Scans ohne Text; dazu Formate, Erzeuger und Laufzeit.
Das Ablegen der Ergebnisse und der Auftrag im Dokument-Worker folgen (siehe docs/INSIGHT_KARTENANALYSE.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from apps.common.einmalig import EinmaligMixin


def _peak_rss_mb() -> float | None:
    """Höchster Speicherbedarf des Prozesses in MB (Linux; im Container des Web-Dienstes)."""
    if sys.platform != "linux":
        return None
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(float(peak) / 1024, 1)  # Linux: Kilobyte


class Command(EinmaligMixin, BaseCommand):
    help = "Karten und Pläne im lokalen Dateicache einer Kommune zählen (ohne Netz, ohne Speichern)"
    sperre = "analyze_maps"
    sperre_ttl = 12 * 3600

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", help="Kommune (Slug oder UUID)")
        parser.add_argument("--dry-run", action="store_true", help="Nur auswerten (derzeit Pflicht)")
        parser.add_argument("--limit", type=int, help="Höchstens so viele Dateien auswerten (neueste zuerst)")
        parser.add_argument("--max-pages", type=int, default=300, help="Höchstens so viele Seiten je Datei")
        parser.add_argument(
            "--max-mb",
            type=int,
            default=int(getattr(settings, "FILE_CACHE_MAX_MB", 80)),
            help="Größere Dateien überspringen",
        )
        parser.add_argument("--json", help="Kennzahlen zusätzlich als JSON in diese Datei schreiben")
        parser.add_argument("--sample-csv", help="Stichprobe von Seiten zum Prüfen von Hand (CSV, Semikolon)")
        parser.add_argument("--sample-size", type=int, default=200, help="Größe der Stichprobe")
        parser.add_argument("--evaluate", help="Von Hand ausgefüllte Stichprobe auswerten (Spalte label: ja/nein)")

    def handle(self, *args: Any, **options: Any) -> None:
        if options.get("evaluate"):
            self._evaluate(Path(options["evaluate"]))
            return
        if not options.get("body"):
            raise CommandError("--body fehlt")
        if not options["dry_run"]:
            raise CommandError(
                "Bitte mit --dry-run aufrufen: Gespeichert wird noch nichts, das folgt mit dem Auftrag im Dokument-Worker."
            )
        body = self._body(str(options["body"]))
        sample_size = max(0, int(options["sample_size"])) if options.get("sample_csv") else 0

        from insight_core.services.map_survey import survey_body, write_sample

        self.stdout.write(f"Werte den Dateicache von {body.get_display_name()} aus (ohne Netz, ohne Speichern) …")
        survey = survey_body(
            body,
            limit=options.get("limit"),
            max_bytes=max(1, int(options["max_mb"])) * 1024 * 1024,
            max_pages=max(1, int(options["max_pages"])),
            sample_size=sample_size,
            progress=lambda count, current: self.stdout.write(
                f"  {count} Dateien, {current.map_pages} Kartenseiten bisher"
            ),
        )
        summary = survey.summary()
        summary["speicher_max_mb"] = _peak_rss_mb()
        self._print(body, summary)
        if options.get("json"):
            Path(options["json"]).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write(f"Kennzahlen gespeichert: {options['json']}")
        if options.get("sample_csv"):
            rows = survey.sample_rows()
            write_sample(rows, Path(options["sample_csv"]))
            self.stdout.write(
                f"Stichprobe mit {len(rows)} Seiten gespeichert: {options['sample_csv']} "
                "(Spalte „label“ mit ja/nein füllen, dann --evaluate)"
            )

    def _body(self, value: str) -> Any:
        from insight_core.models import OParlBody

        query = Q(slug=value)
        if len(value) == 36:
            query |= Q(pk=value)
        body = OParlBody.objects.filter(query).first()
        if body is None:
            raise CommandError(f"Kommune „{value}“ nicht gefunden")
        return body

    def _print(self, body: Any, summary: dict[str, Any]) -> None:
        def percent(part: int, whole: int) -> str:
            return f"{part / whole * 100:.1f} %".replace(".", ",") if whole else "–"

        maps = summary["kartenseiten"]
        streets = summary["kartenseiten_mit_4_strassen"]
        lines = [
            f"Ergebnis für {body.get_display_name()}:",
            f"  Dateien ausgewertet: {summary['dateien']} (davon mit Karte: {summary['dateien_mit_karte']})",
            f"  Seiten: {summary['seiten']}, davon Karten/Pläne: {maps} ({percent(maps, summary['seiten'])})",
            f"  Kartenseiten mit Maßstab im PDF (/VP): {summary['kartenseiten_mit_massstab']} "
            f"({percent(summary['kartenseiten_mit_massstab'], maps)})",
            f"  Kartenseiten als GeoPDF: {summary['kartenseiten_geopdf']}",
            f"  Kartenseiten mit Koordinatenbeschriftung: {summary['kartenseiten_mit_koordinaten']} "
            f"({percent(summary['kartenseiten_mit_koordinaten'], maps)})",
            "  Kartenseiten mit ≥ 4 Straßennamen: "
            + (f"{streets} ({percent(streets, maps)})" if streets is not None else "– (kein Straßenverzeichnis)"),
            f"  Kartenseiten als Scan ohne Text: {summary['kartenseiten_scan_ohne_text']}; "
            f"Scanseiten insgesamt: {summary['scanseiten_ohne_text']}",
            f"  Kartenseiten mit 1-bit-Rastergrundkarte: {summary['kartenseiten_mit_1bit_grundkarte']}; "
            f"Median der Rasterauflösung: {summary['median_dpi_kartenraster'] or '–'} dpi",
            f"  Formate der Kartenseiten: {summary['formate'] or '–'}",
            f"  Häufigste Erzeuger: {summary['erzeuger'] or '–'}",
            f"  Übersprungen: {summary['uebersprungen'] or '–'}; Dateien mit mehr Seiten als ausgewertet: "
            f"{summary['abgeschnitten']}",
            f"  Laufzeit: {summary['sekunden']} s (längste Datei {summary['max_sekunden_je_datei']} s); "
            f"Speicher höchstens: {summary['speicher_max_mb'] or '–'} MB",
        ]
        for line in lines:
            self.stdout.write(line)

    def _evaluate(self, path: Path) -> None:
        from insight_core.services.map_survey import evaluate_labels

        if not path.is_file():
            raise CommandError(f"Datei „{path}“ nicht gefunden")
        result = evaluate_labels(path)
        if not result.labeled:
            raise CommandError("Keine Zeile mit ausgefülltem „label“ (ja/nein) gefunden")
        self.stdout.write(f"Von Hand eingeordnete Seiten: {result.labeled}")
        self.stdout.write(f"  Präzision: {result.precision}  Trefferquote: {result.recall} (Stichprobe)")
        self.stdout.write(
            f"  Präzision: {result.weighted_precision}  Trefferquote: {result.weighted_recall} "
            "(nach Schichten gewichtet, Schätzung für den Bestand)"
        )
        for key in result.false_positives[:20]:
            self.stdout.write(f"  Fehlalarm: {key}")
        for key in result.false_negatives[:20]:
            self.stdout.write(f"  Verpasst: {key}")
