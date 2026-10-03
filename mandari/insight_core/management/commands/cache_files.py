# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: OParl-Dokumente lokal zwischenspeichern.

Cronjob (stündlich, neueste Dokumente zuerst, stoppt bei knappem Speicher):
    python manage.py cache_files --limit 400
Eine Kommune komplett nachladen:
    python manage.py cache_files --body koeln --limit 100000
Statistik (Belegung, Abdeckung, Abrufe der letzten 30 Tage):
    python manage.py cache_files --stats
Gemessene Größe vorhandener Kopien nachtragen (einmalig nach dem Update, wiederholbar):
    python manage.py cache_files --sizes
"""

from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Lädt OParl-Dateien (PDFs) aus den Ratsinformationssystemen in den lokalen Dokument-Cache"

    def add_arguments(self, parser):
        parser.add_argument("--body", help="Kommune (Slug oder Name-Teil); Standard: alle")
        parser.add_argument("--limit", type=int, default=400, help="Max. Dateien je Lauf")
        parser.add_argument("--retry-errors", action="store_true", help="Auch fehlgeschlagene Abrufe erneut versuchen")
        parser.add_argument("--sleep", type=float, default=0.05, help="Pause zwischen Abrufen (Sekunden)")
        parser.add_argument("--stats", action="store_true", help="Nur Statistik ausgeben")
        parser.add_argument(
            "--sizes", action="store_true", help="Gemessene Größe vorhandener Kopien nachtragen (local_size)"
        )

    def handle(self, *args, **options):
        from insight_core.models import OParlBody
        from insight_core.services.file_cache import backfill_sizes, cache_pending, cache_stats

        if options["sizes"]:
            results = backfill_sizes()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Größen nachgetragen: {results['updated']}, Datei fehlt auf der Platte: {results['missing']}"
                )
            )
            return
        if options["stats"]:
            self._print_stats(cache_stats())
            return

        body = None
        if options["body"]:
            key = options["body"]
            body = OParlBody.objects.filter(Q(slug=key) | Q(name__icontains=key) | Q(short_name__icontains=key)).first()
            if body is None:
                self.stderr.write(f"Kommune „{key}“ nicht gefunden")
                return

        results = cache_pending(
            body, limit=options["limit"], retry_errors=options["retry_errors"], sleep=options["sleep"]
        )
        summary = ", ".join(f"{k}={v}" for k, v in sorted(results.items())) or "nichts zu tun"
        if results.get("disk_full"):
            self.stdout.write(
                self.style.WARNING("Festplatten-Schutz aktiv: freier Speicher unter FILE_CACHE_MIN_FREE_GB")
            )
        self.stdout.write(self.style.SUCCESS(f"Fertig: {summary}"))
        self._print_stats(cache_stats())

    def _print_stats(self, stats):
        self.stdout.write(
            f"Cache {stats['root']}: {stats['ok']} von {stats['total']} Dokumenten lokal ({stats['coverage']} %), "
            f"belegt {stats['stored_bytes'] / 1024**3:.2f} GB (je Datei gezählt {stats['cached_gb']} GB), "
            f"{stats['disk_free_bytes'] / 1024**3:.1f} GB frei "
            f"(Schutzgrenze {stats['min_free_gb']} GB); offen={stats['pending']}, 404={stats['missing']}, "
            f"Fehler={stats['error']}, zu groß={stats['too_large']}, "
            f"wartend auf Quellen in Schonung={stats['paused']}, ausgeblendet (nicht gecacht)={stats['unlisted']}"
        )
        if stats.get("without_size"):
            self.stdout.write(
                f"  {stats['without_size']} Kopien ohne gemessene Größe (Summe teils aus OParl): cache_files --sizes"
            )
        self._print_access()
        for row in stats["per_body"]:
            self.stdout.write(
                f"  - {row['body']}: {row['files']} Dateien, {row['cached_bytes'] / 1024**3:.2f} GB lokal "
                "(je Datei gezählt)"
            )

    def _print_access(self) -> None:
        from insight_core.services.file_access import summary

        access = summary(30)
        counts = {key: value["count"] for key, value in access["by_outcome"].items()}
        if not counts:
            self.stdout.write("Abrufe der letzten 30 Tage: keine")
            return
        rate = f"{access['hit_rate']} %" if access["hit_rate"] is not None else "-"
        alter = ", ".join(f"{key}={value}" for key, value in sorted(access["by_age"].items()))
        self.stdout.write(
            f"Abrufe der letzten 30 Tage: Treffer={counts.get('hit', 0)}, von der Quelle={counts.get('miss', 0)}, "
            f"nicht ausgeliefert={counts.get('failed', 0)}, gesperrt={counts.get('blocked', 0)}, "
            f"Trefferquote {rate}; nach Alter: {alter}"
        )
