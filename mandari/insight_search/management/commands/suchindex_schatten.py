# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schattenbetrieb des Suchindex (Abonnement ``suchindex``, Issue #526): Stand, Vollbau, Vergleich, Aufräumen.

    manage.py suchindex_schatten status                     # Schalter, Abonnement, Rückstand, Größe, Heap
    manage.py suchindex_schatten aufbauen --trocken         # nur zählen, was der Vollbau schreiben würde
    manage.py suchindex_schatten aufbauen                   # Kommunen aus SEARCH_INDEX_SHADOW_BODIES
    manage.py suchindex_schatten aufbauen --kommune <uuid> --index papers
    manage.py suchindex_schatten vergleichen                # je Index und Kommune: Bestand, live, Schatten
    manage.py suchindex_schatten vergleichen --stichprobe 50 --json
    manage.py suchindex_schatten loeschen --ja [--abonnement]

Der Vollbau hält die Obergrenze ``SEARCH_INDEX_SHADOW_MAX_DOCS`` ein und verlangt ohne gewählte
Kommunen ``--alle``. Er liest den ganzen Bestand der Kommunen (Datenbank und Elasticsearch wie ein
``reindex_elasticsearch``): außerhalb der Hauptlast und als eigener Prozess starten, nicht im Worker.

Der Vergleich gibt den Exit-Code 0 auch bei Abweichungen; ``--streng`` liefert dann 1 (für Prüfungen).
Ausgegeben werden nur Kennungen, Zahlen und Feldnamen, nie Inhalte.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from insight_search import abonnement, schatten
from insight_search.indices import INDEXES


def _mb(anzahl: int | None) -> str:
    return "-" if anzahl is None else f"{anzahl / (1024 * 1024):.1f} MB"


class Command(BaseCommand):
    help = "Schattenbetrieb des Suchindex: status, aufbauen, vergleichen, loeschen."

    def add_arguments(self, parser: CommandParser) -> None:
        unter = parser.add_subparsers(dest="aktion", required=True)
        unter.add_parser("status", help="Schalter, Abonnement, Rückstand und Größe der Schattenindizes.")

        aufbauen = unter.add_parser("aufbauen", help="Schattenindex einmal aus dem Bestand bauen.")
        self._auswahl(aufbauen)
        aufbauen.add_argument("--alle", action="store_true", help="Ohne gewählte Kommunen: alle Kommunen.")
        aufbauen.add_argument("--trocken", action="store_true", help="Nur zählen, nichts schreiben.")

        vergleichen = unter.add_parser("vergleichen", help="Schattenindex mit dem Live-Index vergleichen.")
        self._auswahl(vergleichen)
        vergleichen.add_argument("--stichprobe", type=int, default=20, help="Gemeinsame Dokumente je Kommune (20).")
        vergleichen.add_argument("--beispiele", type=int, default=5, help="Kennungen je Befund (5).")
        vergleichen.add_argument("--seed", type=int, default=None, help="Zufallsstart der Stichprobe.")
        vergleichen.add_argument("--json", action="store_true", help="Ergebnis als JSON (z. B. für ein Protokoll).")
        vergleichen.add_argument("--streng", action="store_true", help="Exit-Code 1 bei Abweichungen.")

        loeschen = unter.add_parser("loeschen", help="Schattenindizes löschen (Rückfall, Speicher freigeben).")
        loeschen.add_argument("--ja", action="store_true", help="Wirklich löschen.")
        loeschen.add_argument(
            "--abonnement", action="store_true", help="Auch das Abonnement (Cursor, geparkte Ereignisse) entfernen."
        )

    @staticmethod
    def _auswahl(parser: CommandParser) -> None:
        parser.add_argument(
            "--kommune", action="append", default=[], metavar="UUID", help="Kommune (mehrfach); sonst die Einstellung."
        )
        parser.add_argument(
            "--index", action="append", default=[], choices=INDEXES, help="Index (mehrfach); sonst alle."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        es = abonnement.client()
        aktion = options["aktion"]
        if aktion == "status":
            self._status(es)
        elif aktion == "aufbauen":
            self._aufbauen(es, options)
        elif aktion == "vergleichen":
            self._vergleichen(es, options)
        else:
            self._loeschen(es, options)

    # --- Hilfen ---------------------------------------------------------------------------------

    @staticmethod
    def _kommunen(angaben: list[str]) -> list[uuid.UUID]:
        try:
            return [uuid.UUID(angabe) for angabe in angaben]
        except ValueError:
            raise CommandError("--kommune: Kennung einer Kommune (UUID) angeben.") from None

    @staticmethod
    def _indizes(angaben: list[str]) -> list[str]:
        return [index for index in INDEXES if index in angaben] if angaben else list(INDEXES)

    # --- status ---------------------------------------------------------------------------------

    def _status(self, es: Any) -> None:
        stand = schatten.status(es)
        kommunen = ", ".join(stand.kommunen) or "alle"
        self.stdout.write(f"Schalter SEARCH_INDEX_SUBSCRIPTION: {stand.schalter}")
        self.stdout.write(f"Kommunen im Schattenbetrieb: {kommunen}")
        self.stdout.write(f"Obergrenze Dokumente: {stand.obergrenze or 'keine'}")
        abo = stand.abonnement
        if not abo.vorhanden:
            self.stdout.write(
                f"Abonnement {abonnement.NAME}: noch nicht angelegt (höchste Folgenummer {abo.hoechste_folgenummer})"
            )
        else:
            geparkt = ", ".join(f"{zustand} {anzahl}" for zustand, anzahl in sorted(abo.geparkt.items())) or "keine"
            self.stdout.write(
                f"Abonnement {abonnement.NAME}: {abo.zustand}, Cursor {abo.cursor} von {abo.hoechste_folgenummer}, "
                f"offen {abo.offen} Ereignisse, Rückstand {abo.rueckstand_sekunden:.0f} s, geparkt: {geparkt}"
            )
        if stand.fehler:
            self.stdout.write(self.style.WARNING(f"Elasticsearch nicht abfragbar ({stand.fehler})."))
            return
        for index in stand.indizes:
            if index.vorhanden:
                self.stdout.write(f"  {index.name:24} {index.dokumente:>10} Dokumente  {_mb(index.bytes):>10}")
            else:
                self.stdout.write(f"  {index.name:24} fehlt")
        self.stdout.write(f"Heap Elasticsearch: {_mb(stand.heap_belegt_bytes)} von {_mb(stand.heap_max_bytes)}")

    # --- aufbauen -------------------------------------------------------------------------------

    def _aufbauen(self, es: Any, options: dict[str, Any]) -> None:
        kommunen = self._kommunen(options["kommune"] or list(settings.SEARCH_INDEX_SHADOW_BODIES))
        if not kommunen and not options["alle"]:
            raise CommandError(
                "Keine Kommunen gewählt (--kommune oder SEARCH_INDEX_SHADOW_BODIES). Für alle Kommunen --alle angeben."
            )
        plan = schatten.plan_build(kommunen, self._indizes(options["index"]))
        for index, anzahl in plan.dokumente.items():
            self.stdout.write(f"  {index:14} {anzahl:>10} Dokumente")
        obergrenze = int(settings.SEARCH_INDEX_SHADOW_MAX_DOCS)
        self.stdout.write(
            f"Gesamt {plan.gesamt} Dokumente, Version (Folgenummer) {plan.version}, Obergrenze {obergrenze or 'keine'}"
        )
        if obergrenze and plan.gesamt > obergrenze:
            raise CommandError(
                f"Der Vollbau überschritte die Obergrenze ({plan.gesamt} > {obergrenze}); weniger Kommunen bzw. "
                "Indizes wählen oder SEARCH_INDEX_SHADOW_MAX_DOCS bewusst anheben."
            )
        if options["trocken"]:
            self.stdout.write("Trockenlauf: nichts geschrieben.")
            return
        ergebnisse = schatten.build(es, plan)
        fehlerhaft = False
        for index, tally in ergebnisse.items():
            werte = ", ".join(f"{name} {anzahl}" for name, anzahl in tally.as_dict().items() if anzahl) or "nichts"
            self.stdout.write(f"  {index:14} {werte}")
            fehlerhaft = fehlerhaft or bool(tally.rejected or tally.unavailable)
        if fehlerhaft:
            raise CommandError("Nicht alle Dokumente geschrieben (abgelehnt bzw. nicht verfügbar); Protokoll prüfen.")
        self.stdout.write(self.style.SUCCESS("Vollbau abgeschlossen."))

    # --- vergleichen ----------------------------------------------------------------------------

    def _vergleichen(self, es: Any, options: dict[str, Any]) -> None:
        kommunen = [str(kommune) for kommune in self._kommunen(options["kommune"])]
        ergebnisse = list(
            schatten.compare(
                es,
                indizes=self._indizes(options["index"]),
                kommunen=kommunen,
                sample=max(0, options["stichprobe"]),
                examples=max(0, options["beispiele"]),
                seed=options["seed"],
            )
        )
        abweichung = any(
            vergleich.abbildung_abweichend or not all(kommune.gleich for kommune in vergleich.kommunen)
            for vergleich in ergebnisse
        )
        if options["json"]:
            daten = {
                "zeitpunkt": timezone.now().isoformat(),
                "abonnement": schatten.subscription_status().__dict__,
                "abweichung": abweichung,
                "indizes": [
                    {
                        "index": vergleich.index,
                        "abbildung_abweichend": vergleich.abbildung_abweichend,
                        "kommunen": [kommune.__dict__ for kommune in vergleich.kommunen],
                    }
                    for vergleich in ergebnisse
                ],
            }
            self.stdout.write(json.dumps(daten, ensure_ascii=False, indent=2))
        else:
            self._vergleich_ausgeben(ergebnisse)
        if abweichung and options["streng"]:
            sys.exit(1)

    def _vergleich_ausgeben(self, ergebnisse: list[schatten.IndexComparison]) -> None:
        abo = schatten.subscription_status()
        if abo.vorhanden:
            self.stdout.write(
                f"Abonnement {abonnement.NAME}: {abo.zustand}, Cursor {abo.cursor} von {abo.hoechste_folgenummer}, "
                f"offen {abo.offen}, Rückstand {abo.rueckstand_sekunden:.0f} s"
            )
        for vergleich in ergebnisse:
            self.stdout.write(f"\n{vergleich.index}")
            if vergleich.abbildung_abweichend:
                felder = ", ".join(vergleich.abbildung_abweichend)
                self.stdout.write(self.style.WARNING(f"  Abbildung abweichend: {felder}"))
            if not vergleich.kommunen:
                self.stdout.write("  keine Kommune im Schattenindex")
            for kommune in vergleich.kommunen:
                zeile = (
                    f"  {kommune.kommune}  Bestand {kommune.bestand}  live {kommune.live}  Schatten {kommune.schatten}  "
                    f"fehlt {kommune.fehlt}  überzählig {kommune.ueberzaehlig}  "
                    f"abweichend {kommune.abweichend}/{kommune.stichprobe}"
                )
                self.stdout.write(zeile if kommune.gleich else self.style.WARNING(zeile))
                if kommune.felder:
                    self.stdout.write("    Felder: " + ", ".join(f"{feld} {n}" for feld, n in kommune.felder.items()))
                for titel, beispiele in (
                    ("fehlt", kommune.beispiele_fehlt),
                    ("überzählig", kommune.beispiele_ueberzaehlig),
                    ("abweichend", kommune.beispiele_abweichend),
                ):
                    if beispiele:
                        self.stdout.write(f"    {titel}: {', '.join(beispiele)}")

    # --- loeschen -------------------------------------------------------------------------------

    def _loeschen(self, es: Any, options: dict[str, Any]) -> None:
        if not options["ja"]:
            raise CommandError("Löschen nur mit --ja.")
        abo = schatten.subscription_status()
        if settings.SEARCH_INDEX_SUBSCRIPTION == "schatten" and abo.vorhanden and abo.zustand != "pausiert":
            raise CommandError(
                "Das Abonnement schreibt noch in den Schattenindex: erst SEARCH_INDEX_SUBSCRIPTION=aus setzen und den "
                "Worker neu starten (oder das Abonnement im Admin pausieren), dann löschen."
            )
        geloescht, abonnement_weg = schatten.drop(es, subscription=options["abonnement"])
        self.stdout.write(f"Gelöscht: {', '.join(geloescht) or 'keine Schattenindizes vorhanden'}")
        if options["abonnement"]:
            self.stdout.write(f"Abonnement {abonnement.NAME}: {'entfernt' if abonnement_weg else 'war nicht angelegt'}")
