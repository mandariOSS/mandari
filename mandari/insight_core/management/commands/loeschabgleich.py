# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Löschabgleich der Dokumente mit den Quellen (Issue #787, docs/FILE_CACHE.md).

Ein Lauf (stündlich um :15 als Zeitplan ``befehl:loeschabgleich`` im Worker, ``apps/common/schedules.py``):
    1. von der Quelle geänderte Dokumente neu laden und per SHA-256 vergleichen
    2. gedrosselte HEAD-Stichproben je gelisteter Kommune (404/410 sperrt, abweichende Größe gleicht ab);
       gesperrte Dokumente werden nach 1, 7 und 25 Tagen erneut geprüft
    3. Kopie und Text von Dokumenten löschen, die länger als FILE_PURGE_AFTER_DAYS gesperrt sind
       (nicht mehr abrufbare vorher noch einmal per GET prüfen)
    4. Inhalte ohne Referenz aus der Ablage löschen (Ablage nach SHA-256): Eine ersetzte oder gelöschte
       Fassung bleibt so nicht liegen, auch wenn der Zeitplan für ``dokumentablage --aufraeumen`` aus ist

Ein Lauf hält eine Sperre im gemeinsamen Cache: Überlappende Läufe (langsamer Lauf, zweiter Server)
enden sofort, statt die Quellen doppelt zu belasten.

Von Hand, während der Worker den Zeitplan bedient, mit ``--trotz-zeitplan``; ``--robots`` läuft immer:

    python manage.py loeschabgleich --body beispielstadt --head-limit 200 --trotz-zeitplan
    python manage.py loeschabgleich --nur-loeschen --trotz-zeitplan
    python manage.py loeschabgleich --robots      # Quellen, deren robots.txt Dateiabrufe sperrt
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from apps.common.einmalig import EinmaligMixin

logger = logging.getLogger(__name__)


class Command(EinmaligMixin, BaseCommand):
    help = "Gleicht Dokumente mit den Quellen ab: Änderungen per Hash, Stichproben per HEAD, Löschen nach Frist"

    sperre = "loeschabgleich"
    #: Verfällt vor dem nächsten stündlichen Termin, falls ein Lauf an seiner Zeitgrenze abgebrochen wird
    #: oder abstürzt; ein regulärer Lauf gibt sie sofort frei
    sperre_ttl = 3000
    nur_lesend = ("robots",)

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
        from insight_core.services import file_reconcile, file_store

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
        # Die Schritte hängen nicht aneinander: Scheitert einer unerwartet (etwa an einer Adresse aus einer Quelle,
        # die sich nicht auswerten lässt), laufen die übrigen trotzdem. Sonst unterbliebe mit dem Abgleich auch das
        # Löschen nach Frist für alle Quellen. Am Ende meldet der Lauf den Fehlschlag (Exit-Code 1).
        gescheitert: list[str] = []
        with file_reconcile.fetch_client() as client:
            if not options["nur_loeschen"]:
                # Bricht ein Abgleich ab, verfallen seine Vormerkungen: Der nächste Schritt sperrte sie sonst ungeprüft
                self._schritt(
                    gescheitert,
                    "Geänderte Dokumente",
                    lambda: file_reconcile.check_changed(body, limit=options["changed_limit"], client=client, run=run),
                    bei_fehler=run.discard_pending,
                )
                self._schritt(
                    gescheitert,
                    "Stichproben",
                    lambda: file_reconcile.sample_heads(body, per_source=options["head_limit"], client=client, run=run),
                    bei_fehler=run.discard_pending,
                )
            if not options["ohne_loeschen"]:
                restored = self._schritt(gescheitert, None, file_reconcile.restore_reappeared)
                if restored:
                    self.stdout.write(f"Wieder freigegeben (Text wird neu erkannt): {restored}")
                self._schritt(
                    gescheitert,
                    f"Gelöscht nach {file_reconcile.purge_after_days()} Tagen Sperre",
                    lambda: file_reconcile.purge_expired(body=body, client=client, run=run),
                )
        if not options["ohne_loeschen"] and file_store.uses_blobs():
            self._schritt(gescheitert, "Verwaiste Inhalte", file_store.cleanup_orphans)
        if run.braked:
            self.stderr.write(
                f"Bremse: {len(run.braked)} Quelle(n) lieferten zu viele Dokumente nicht mehr; nichts davon "
                "gesperrt. Quelle prüfen (Umstellung, Wartung, Sperre) und den Lauf danach wiederholen."
            )
        if gescheitert:
            raise CommandError("Schritte unerwartet gescheitert: " + ", ".join(gescheitert))

    def _schritt(
        self,
        gescheitert: list[str],
        title: str | None,
        schritt: Callable[[], Any],
        bei_fehler: Callable[[], None] | None = None,
    ) -> Any:
        """
        Einen Schritt des Laufs ausführen und melden; eine unerwartete Ausnahme beendet nur diesen Schritt.
        ``bei_fehler`` räumt danach auf, was der Schritt halb fertig hinterlassen hat.
        """
        try:
            ergebnis = schritt()
        except Exception as exc:
            logger.exception("Löschabgleich: Schritt unerwartet gescheitert")
            gescheitert.append(f"{title or 'Wieder freigeben'} ({type(exc).__name__})")
            if bei_fehler is not None:
                bei_fehler()
            return None
        if title is not None:
            self._report(title, ergebnis)
        return ergebnis

    def _report(self, title: str, results: Counter[str]) -> None:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(results.items())) or "nichts zu tun"
        self.stdout.write(f"{title}: {summary}")

    def _robots(self, body: Any) -> None:
        """Je Quelle eine Download-Adresse gegen die robots.txt prüfen (Liste gesperrter Quellen)."""
        from insight_core.models import OParlFile, OParlSource
        from insight_core.services import file_robots

        sources = OParlSource.objects.order_by("name")
        if body is not None:
            sources = sources.filter(pk=body.source_id)
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
            status = file_robots.robots_status(source, url)
            if note:
                locked = status != file_robots.ALLOWED
                line = f"{source.name}: Ausnahme ({note})" + (" – robots.txt sperrt" if locked else "")
            else:
                line = f"{source.name}: {status}"
            self.stdout.write(line)
