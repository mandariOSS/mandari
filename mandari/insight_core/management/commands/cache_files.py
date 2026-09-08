# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: OParl-Dokumente lokal zwischenspeichern.

Cronjob (stündlich, neueste Dokumente zuerst, stoppt bei knappem Speicher):
    python manage.py cache_files --limit 400
Eine Kommune komplett nachladen:
    python manage.py cache_files --body koeln --limit 100000
Statistik:
    python manage.py cache_files --stats
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

    def handle(self, *args, **options):
        from insight_core.models import OParlBody
        from insight_core.services.file_cache import cache_pending, cache_stats

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
            f"{stats['cached_gb']} GB belegt, {stats['disk_free_bytes'] / 1024**3:.1f} GB frei "
            f"(Schutzgrenze {stats['min_free_gb']} GB); offen={stats['pending']}, 404={stats['missing']}, "
            f"Fehler={stats['error']}, zu groß={stats['too_large']}, "
            f"wartend auf Quellen in Schonung={stats['paused']}"
        )
        for row in stats["per_body"]:
            self.stdout.write(
                f"  - {row['body']}: {row['files']} Dateien, {row['cached_bytes'] / 1024**3:.2f} GB lokal"
            )
