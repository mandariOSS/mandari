# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: OParl-Dokumente lokal zwischenspeichern.

Stündlich als Zeitplan ``befehl:cache_files`` im Worker (``apps/common/schedules.py``, Issue #516;
neueste Dokumente zuerst, stoppt bei knappem Speicher):
    python manage.py cache_files --limit 400
Eine Kommune komplett nachladen (von Hand; läuft auch, während der Worker den Zeitplan bedient):
    python manage.py cache_files --body <slug> --limit 100000 --trotz-zeitplan
Statistik (Belegung, Abdeckung, Abrufe der letzten 30 Tage; läuft immer):
    python manage.py cache_files --stats
Gemessene Größe vorhandener Kopien nachtragen (einmalig nach dem Update, wiederholbar):
    python manage.py cache_files --sizes
Mit Obergrenze (FILE_CACHE_MAX_TOTAL_GB, #961) lädt ein Lauf nur bis zur Grenze. Verdrängte Dokumente holt nur
die Vorschau bei Bedarf; ``--verdraengte`` nimmt sie in den Lauf auf (etwa nach Anheben oder Abschalten der Grenze).
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    # Singleton je Cache/Redis (#55); verfällt vor dem nächsten stündlichen Termin, falls ein Lauf an
    # seiner Zeitgrenze abgebrochen wird
    sperre = "cache_files"
    sperre_ttl = 3000
    nur_lesend = ("stats",)
    help = "Lädt OParl-Dateien (PDFs) aus den Ratsinformationssystemen in den lokalen Dokument-Cache"

    def add_arguments(self, parser):
        parser.add_argument("--body", help="Kommune (Slug oder Name-Teil); Standard: alle")
        parser.add_argument("--limit", type=int, default=400, help="Max. Dateien je Lauf")
        parser.add_argument("--retry-errors", action="store_true", help="Auch fehlgeschlagene Abrufe erneut versuchen")
        parser.add_argument(
            "--verdraengte", action="store_true", help="Auch von der Obergrenze verdrängte Dokumente nachladen"
        )
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
            body,
            limit=options["limit"],
            retry_errors=options["retry_errors"],
            retry_evicted=options["verdraengte"],
            sleep=options["sleep"],
        )
        summary = ", ".join(f"{k}={v}" for k, v in sorted(results.items())) or "nichts zu tun"
        if results.get("disk_full"):
            self.stdout.write(
                self.style.WARNING("Festplatten-Schutz aktiv: freier Speicher unter FILE_CACHE_MIN_FREE_GB")
            )
        if results.get("limit"):
            self.stdout.write(
                self.style.WARNING(
                    "Obergrenze erreicht (FILE_CACHE_MAX_TOTAL_GB): Platz schafft das stündliche Aufräumen"
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Fertig: {summary}"))
        self._print_stats(cache_stats())

    def _print_stats(self, stats):
        gb = 1024**3
        if stats.get("object_storage"):
            # Mit Objektspeicher liegt nur ein Teil lokal: Platte und Objektspeicher getrennt ausweisen (#961)
            belegung = (
                f"{stats['ok']} von {stats['total']} Dokumenten abgelegt ({stats['coverage']} %), "
                f"lokal {stats['local_bytes'] / gb:.2f} GB (Platte), im Objektspeicher {stats['remote_bytes'] / gb:.2f} GB, "
                f"zusammen {stats['stored_bytes'] / gb:.2f} GB (je Datei gezählt {stats['cached_gb']} GB)"
            )
        else:
            belegung = (
                f"{stats['ok']} von {stats['total']} Dokumenten lokal ({stats['coverage']} %), "
                f"belegt {stats['stored_bytes'] / gb:.2f} GB (je Datei gezählt {stats['cached_gb']} GB)"
            )
        self.stdout.write(
            f"Cache {stats['root']}: {belegung}, "
            f"{stats['disk_free_bytes'] / 1024**3:.1f} GB frei "
            f"(Schutzgrenze {stats['min_free_gb']} GB); offen={stats['pending']}, 404={stats['missing']}, "
            f"Fehler={stats['error']}, zu groß={stats['too_large']}, verdrängt={stats['evicted']}, "
            f"wartend auf Quellen in Schonung={stats['paused']}, ausgeblendet (nicht gecacht)={stats['unlisted']}"
        )
        if stats["max_total_bytes"]:
            self.stdout.write(
                f"  Obergrenze {stats['max_total_bytes'] / 1024**3:.2f} GB, verdrängt wird bis "
                f"{stats['evict_target_percent']} % der Grenze (FILE_CACHE_MAX_TOTAL_GB)"
            )
        if stats.get("without_size"):
            self.stdout.write(
                f"  {stats['without_size']} Kopien ohne gemessene Größe (Summe teils aus OParl): cache_files --sizes"
            )
        self._print_access()
        for row in stats["per_body"]:
            self.stdout.write(
                f"  - {row['body']}: {row['files']} Dateien, {row['cached_bytes'] / 1024**3:.2f} GB "
                + ("abgelegt" if stats.get("object_storage") else "lokal")
                + " (je Datei gezählt)"
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
