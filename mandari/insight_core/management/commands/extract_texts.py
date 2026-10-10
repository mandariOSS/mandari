# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``extract_texts``: Texterkennung von RIS-Dateien einplanen (Issue #919, ``docs/adr/20261007-dokumentkette.md``,
Abschnitt 1).

Der Befehl lädt nichts und erkennt nicht selbst: Er reiht je Datei einen Auftrag ``file.extract_text`` in die
Warteschlange ``ocr`` ein (Dienst ``worker-heavy``). Der Auftrag liest den Inhalt aus der Ablage bzw. dem
Objektspeicher, nie bei der Quelle (``hub/ris/erkennung.py``). Eingeplant werden Dateien mit abgelegtem Inhalt
(``local_status = ok``), deren Erkennung wartet oder deren Text aus einer älteren Erkennungsversion stammt und für die
noch kein Auftrag wartet. Dateien ohne abgelegten Inhalt holt zuerst der Abruf (``cache_files``).

Verwendung:
    python manage.py extract_texts                    # einplanen, so viele wie in ocr frei sind
    python manage.py extract_texts --dry-run          # nur zählen
    python manage.py extract_texts --limit 100       # 100 Aufträge, auch über die freien Plätze hinaus
    python manage.py extract_texts --body <uuid>     # nur für eine Kommune
    python manage.py extract_texts --pdf-only        # nur PDF-Dateien
    python manage.py extract_texts --reprocess       # auch erledigte, übersprungene und gescheiterte Dateien

Ohne ``--limit`` reiht der Befehl höchstens so viele Aufträge ein, wie in der Warteschlange ``ocr`` frei sind
(``TEXT_EXTRACTION_QUEUE_DEPTH`` minus wartende und laufende); den Rest plant der Zeitplan ``texterkennung_einplanen``
schrittweise ein (ADR Abschnitte 2 und 8). Ein ausdrückliches ``--limit`` gilt auch darüber hinaus (Vorrang, etwa
mit ``--body``); dann kann die Prüfung des Rückstaus anschlagen, bis die Aufträge abgearbeitet sind.

``--reprocess`` markiert erledigte Dateien als „Neuerkennung angefordert“ (der bisherige Text bleibt, bis der neue
gespeichert ist) und setzt übersprungene und gescheiterte zurück auf „wartend“; eingeplant wird wie oben schrittweise.
Braucht
``TEXT_EXTRACTION_RUNNER=worker``; mit ``ingestor`` erkennt der OCR-Worker des Ingestors wartende Dateien selbst.
``--batch-size``, ``--workers`` und ``--verbose`` haben keine Wirkung mehr (die Parallelität bestimmt die
Warteschlange ``ocr``).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hub.ris import erkennung
from insight_core.management.arguments import add_extraction_arguments
from insight_core.models import OParlBody
from insight_core.services.text_extraction_job import runner_is_worker


class Command(BaseCommand):
    help = "Plant die Texterkennung von RIS-Dateien ein (Aufträge file.extract_text); lädt und erkennt nicht selbst."

    def add_arguments(self, parser: CommandParser) -> None:
        add_extraction_arguments(
            parser,
            noun="Dateien",
            batch_size=50,
            workers=4,
            workers_note="; ohne Wirkung",
            limit_help=(
                "Höchstens so viele Aufträge einreihen, auch über die freien Plätze der Warteschlange ocr hinaus "
                "(0 = nur die freien Plätze, TEXT_EXTRACTION_QUEUE_DEPTH; den Rest plant der Zeitplan ein)"
            ),
        )
        parser.add_argument(
            "--pdf-only",
            action="store_true",
            help="Nur PDF-Dateien einplanen",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        if not dry_run and not runner_is_worker():
            raise CommandError(
                "extract_texts plant Aufträge file.extract_text ein und braucht TEXT_EXTRACTION_RUNNER=worker. "
                "Mit ingestor erkennt der OCR-Worker des Ingestors wartende Dateien selbst (--dry-run zählt nur)."
            )
        body_id = None
        if options["body"]:
            body = OParlBody.objects.filter(pk=options["body"]).first()
            if body is None:
                raise CommandError(f"Kommune mit ID {options['body']} nicht gefunden.")
            body_id = body.pk
            self.stdout.write(f"Nur Dateien der Kommune: {body.name}")

        stand = erkennung.befehl_einplanen(
            body_id=body_id,
            pdf_only=bool(options["pdf_only"]),
            reprocess=bool(options["reprocess"]),
            limit=max(0, int(options["limit"] or 0)),
            ausfuehren=not dry_run,
        )

        if options["reprocess"]:
            verb = "würden" if dry_run else "wurden"
            self.stdout.write(
                f"Neuerkennung: {stand.neu_angefordert} erledigte Dateien {verb} markiert, "
                f"{stand.zurueckgesetzt} übersprungene oder gescheiterte {verb} zurückgesetzt"
            )
        if stand.schon_eingereiht:
            self.stdout.write(f"Schon eingereiht: {stand.schon_eingereiht}")
        if stand.verdraengt:
            self.stdout.write(
                self.style.WARNING(
                    f"Von der Obergrenze verdrängt (kein Abruf von selbst, ausdrücklich: cache_files --verdraengte): "
                    f"{stand.verdraengt}"
                )
            )
        if stand.ohne_inhalt:
            self.stdout.write(
                self.style.WARNING(f"Ohne abgelegten Inhalt (holt zuerst der Abruf): {stand.ohne_inhalt}")
            )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Probelauf: {stand.auftraege} Aufträge würden eingereiht"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Eingereiht: {stand.auftraege} Aufträge file.extract_text"))
        if stand.dem_zeitplan:
            self.stdout.write(
                f"Übrige {stand.dem_zeitplan} Dateien plant der Zeitplan texterkennung_einplanen schrittweise ein "
                f"(freie Plätze in ocr: {stand.frei}; mehr nur mit --limit)"
            )
        if stand.auftraege > stand.frei:
            self.stdout.write(
                self.style.WARNING(
                    f"--limit über den freien Plätzen in ocr ({stand.frei}): "
                    "die Prüfung des Rückstaus kann anschlagen, bis die Aufträge abgearbeitet sind"
                )
            )
