# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Löschabgleich der Dokumente mit den Quellen (Issue #787, docs/FILE_CACHE.md).

Ein Lauf (stündlich per Cron oder Zeitplan):
    1. von der Quelle geänderte Dokumente neu laden und per SHA-256 vergleichen
    2. gedrosselte HEAD-Stichproben je gelisteter Kommune (404/410 sperrt, abweichende Größe gleicht ab);
       gesperrte Dokumente werden nach 1, 7 und 25 Tagen erneut geprüft
    3. Kopie und Text von Dokumenten löschen, die länger als FILE_PURGE_AFTER_DAYS gesperrt sind
       (nicht mehr abrufbare vorher noch einmal per GET prüfen)

Ein Lauf hält eine Sperre im gemeinsamen Cache: Überlappende Läufe (langsamer Lauf, zweiter Server)
enden sofort, statt die Quellen doppelt zu belasten.

    python manage.py loeschabgleich
    python manage.py loeschabgleich --body beispielstadt --head-limit 200
    python manage.py loeschabgleich --nur-loeschen
    python manage.py loeschabgleich --robots      # Quellen, deren robots.txt Dateiabrufe sperrt
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Q

from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    help = "Gleicht Dokumente mit den Quellen ab: Änderungen per Hash, Stichproben per HEAD, Löschen nach Frist"

    sperre = "loeschabgleich"
    #: Verfällt von selbst, falls ein Lauf abstürzt; ein regulärer Lauf gibt sie sofort frei
    sperre_ttl = 6 * 3600

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", help="Kommune (Slug oder Name-Teil); Standard: alle")
        parser.add_argument("--changed-limit", type=int, default=200, help="Höchstens so viele geänderte Dokumente")
        parser.add_argument("--head-limit", type=int, default=30, help="HEAD-Stichproben je Kommune und Lauf")
        parser.add_argument(
            "--interval", type=float, default=2.0, help="Mindestabstand zweier Anfragen an denselben Host (s)"
        )
        parser.add_argument(
            "--max-fehlend",
            type=int,
            default=None,
            help="Bremse: höchstens so viele neu fehlende Dokumente je Quelle und Lauf sperren "
            "(Standard FILE_RECONCILE_MAX_MISSING)",
        )
        parser.add_argument("--nur-loeschen", action="store_true", help="Nur Kopien und Texte nach Frist löschen")
        parser.add_argument("--ohne-loeschen", action="store_true", help="Nichts löschen, nur abgleichen")
        parser.add_argument("--robots", action="store_true", help="robots.txt je Quelle für Dateiabrufe auflisten")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlBody
        from insight_core.services import file_reconcile

        body = None
        if options["body"]:
            key = options["body"]
            body = OParlBody.objects.filter(Q(slug=key) | Q(name__icontains=key) | Q(short_name__icontains=key)).first()
            if body is None:
                self.stderr.write(f"Kommune „{key}“ nicht gefunden")
                return

        if options["robots"]:
            self._robots(body)
            return

        run = file_reconcile.Run(options["interval"], max_missing=options["max_fehlend"])
        with file_reconcile.fetch_client() as client:
            if not options["nur_loeschen"]:
                changed = file_reconcile.check_changed(body, limit=options["changed_limit"], client=client, run=run)
                self._report("Geänderte Dokumente", changed)
                heads = file_reconcile.sample_heads(body, per_source=options["head_limit"], client=client, run=run)
                self._report("Stichproben", heads)
            if not options["ohne_loeschen"]:
                restored = file_reconcile.restore_reappeared()
                if restored:
                    self.stdout.write(f"Wieder freigegeben (Text wird neu erkannt): {restored}")
                purged = file_reconcile.purge_expired(body=body, client=client, run=run)
                self._report(f"Gelöscht nach {file_reconcile.purge_after_days()} Tagen Sperre", purged)
        if run.braked:
            self.stderr.write(
                f"Bremse: {len(run.braked)} Quelle(n) lieferten zu viele Dokumente nicht mehr; nichts davon "
                "gesperrt. Quelle prüfen (Umstellung, Wartung, Sperre) und den Lauf danach wiederholen."
            )

    def _report(self, title: str, results: Counter[str]) -> None:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(results.items())) or "nichts zu tun"
        self.stdout.write(f"{title}: {summary}")

    def _robots(self, body: Any) -> None:
        """Je Quelle eine Download-Adresse gegen die robots.txt prüfen (Liste gesperrter Quellen)."""
        from insight_core.models import OParlFile, OParlSource
        from insight_core.services import file_reconcile, file_robots

        sources = OParlSource.objects.order_by("name")
        if body is not None:
            sources = sources.filter(pk=body.source_id)
        with file_reconcile.fetch_client() as client:
            for source in sources:
                sample = (
                    OParlFile.objects.filter(body__source=source, deleted=False)
                    .exclude(download_url__isnull=True, access_url__isnull=True)
                    .values_list("download_url", "access_url")
                    .first()
                )
                url = (sample[0] or sample[1]) if sample else None
                if not url:
                    continue
                note = file_robots.exception_note(source)
                status = file_robots.ALLOWED if note else file_robots.file_fetch_status(None, url, client)
                if note:
                    rules = file_robots.rules_for(url, client)
                    locked = rules.unreachable or not file_robots.is_allowed(rules, url)
                    line = f"{source.name}: Ausnahme ({note})" + (" – robots.txt sperrt" if locked else "")
                else:
                    line = f"{source.name}: {status}"
                self.stdout.write(line)
