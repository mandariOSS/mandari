# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schattenbetrieb des RIS-Projektors für Session-Mandanten (Issue #536): Stand, Vollbau, Vergleich, Aufräumen.

    manage.py ris_projektor_schatten status                       # Schalter, Abonnement, Rückstand, Zeilen
    manage.py ris_projektor_schatten aufbauen --trocken           # nur zählen, was der Vollbau schriebe
    manage.py ris_projektor_schatten aufbauen                     # Mandanten aus RIS_SESSION_PROJECTOR_TENANTS
    manage.py ris_projektor_schatten aufbauen --mandant <uuid>
    manage.py ris_projektor_schatten vergleichen [--json] [--streng] [--typ meeting]
    manage.py ris_projektor_schatten loeschen --ja [--mandant <uuid>] [--abonnement]

Der Vollbau ersetzt die Zeilen eines Mandanten durch den aktuellen Stand der Session-Schnittstelle; er läuft als
eigener Prozess, nicht im Worker, und erst, wenn das Abonnement angelegt ist (sonst fehlt, was dazwischen
geschieht). Ohne gewählte Mandanten verlangt er ``--alle``.

Der Vergleich stellt die Schatten-Quelle neben den RIS-Bestand der Kommunen, die der Mandant ins Bürgerportal
übernimmt (``insight_service.session_sources``): je Objekttyp fehlend, überzählig und abweichend mit den Namen der
abweichenden Felder (``hub.projections.ris_vergleich``). Exit-Code 0 auch bei Abweichungen; ``--streng`` liefert
dann 1 (für Prüfungen). Ausgegeben werden nur Kennungen, Zahlen und Feldnamen, nie Inhalte.

``loeschen`` geht nur, wenn das Abonnement nicht mehr zugestellt wird (``RIS_SESSION_PROJECTOR=aus`` mit
Neustart des Workers oder im Admin pausiert); der Eingriff steht im Sicherheitsprotokoll. Der RIS-Bestand ist nie
betroffen.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.events.eingriffe import record_command
from apps.session.models import SessionTenant
from apps.session.ris_projektion import SessionQuelle
from apps.session.services import insight_service
from hub.projections import ris_session, ris_vergleich
from hub.projections.models import RisSchatten


class Command(BaseCommand):
    help = "Schattenbetrieb des RIS-Projektors: status, aufbauen, vergleichen, loeschen."

    def add_arguments(self, parser: CommandParser) -> None:
        unter = parser.add_subparsers(dest="aktion", required=True)
        unter.add_parser("status", help="Schalter, Abonnement, Rückstand und Zeilen der Schatten-Quelle.")

        aufbauen = unter.add_parser("aufbauen", help="Schatten-Quelle einmal aus Session bauen.")
        self._auswahl(aufbauen)
        aufbauen.add_argument("--alle", action="store_true", help="Ohne gewählte Mandanten: alle Mandanten.")
        aufbauen.add_argument("--trocken", action="store_true", help="Nur zählen, nichts schreiben.")

        vergleichen = unter.add_parser("vergleichen", help="Schatten-Quelle mit dem RIS-Bestand vergleichen.")
        self._auswahl(vergleichen)
        vergleichen.add_argument(
            "--typ", action="append", default=[], choices=ris_vergleich.TYPEN, help="Objekttyp (mehrfach); sonst alle."
        )
        vergleichen.add_argument("--beispiele", type=int, default=5, help="Kennungen je Befund (5).")
        vergleichen.add_argument("--json", action="store_true", help="Ergebnis als JSON (z. B. für ein Protokoll).")
        vergleichen.add_argument("--streng", action="store_true", help="Exit-Code 1 bei Abweichungen.")

        loeschen = unter.add_parser("loeschen", help="Zeilen der Schatten-Quelle löschen (Rückfall).")
        self._auswahl(loeschen)
        loeschen.add_argument("--ja", action="store_true", help="Wirklich löschen.")
        loeschen.add_argument(
            "--abonnement", action="store_true", help="Auch das Abonnement (Cursor, geparkte Ereignisse) entfernen."
        )

    @staticmethod
    def _auswahl(parser: CommandParser) -> None:
        parser.add_argument(
            "--mandant",
            action="append",
            default=[],
            metavar="UUID",
            help="Session-Mandant (mehrfach); sonst RIS_SESSION_PROJECTOR_TENANTS.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        aktion = options["aktion"]
        if aktion == "status":
            self._status()
        elif aktion == "aufbauen":
            self._aufbauen(options)
        elif aktion == "vergleichen":
            self._vergleichen(options)
        else:
            self._loeschen(options)

    # --- Hilfen ---------------------------------------------------------------------------------

    @staticmethod
    def _angegeben(angaben: list[str]) -> list[uuid.UUID]:
        try:
            return [uuid.UUID(angabe) for angabe in angaben]
        except ValueError:
            raise CommandError("--mandant: Kennung eines Session-Mandanten (UUID) angeben.") from None

    def _gewaehlt(self, angaben: list[str]) -> list[uuid.UUID]:
        """Angegebene Mandanten, sonst die der Einstellung (leer: keine Auswahl)."""
        return self._angegeben(angaben) or sorted(ris_session.mandanten() or [], key=str)

    @staticmethod
    def _mandanten(kennungen: list[uuid.UUID]) -> list[SessionTenant]:
        gefunden = {tenant.pk: tenant for tenant in SessionTenant.objects.filter(pk__in=kennungen)}
        fehlend = [str(kennung) for kennung in kennungen if kennung not in gefunden]
        if fehlend:
            raise CommandError(f"Unbekannte Session-Mandanten: {', '.join(fehlend)}")
        return [gefunden[kennung] for kennung in kennungen]

    # --- status ---------------------------------------------------------------------------------

    def _status(self) -> None:
        gewaehlt = ris_session.mandanten()
        self.stdout.write(f"Schalter RIS_SESSION_PROJECTOR: {ris_session.modus()}")
        self.stdout.write(
            "Mandanten im Schattenbetrieb: " + (", ".join(sorted(str(m) for m in gewaehlt)) if gewaehlt else "alle")
        )
        abo = ris_vergleich.abonnement_stand()
        if not abo.vorhanden:
            self.stdout.write(
                f"Abonnement {ris_session.NAME}: noch nicht angelegt (höchste Folgenummer {abo.hoechste_folgenummer})"
            )
        else:
            geparkt = ", ".join(f"{zustand} {anzahl}" for zustand, anzahl in sorted(abo.geparkt.items())) or "keine"
            self.stdout.write(
                f"Abonnement {ris_session.NAME}: {abo.zustand}, Cursor {abo.cursor} von {abo.hoechste_folgenummer}, "
                f"offen {abo.offen} Ereignisse, Rückstand {abo.rueckstand_sekunden:.0f} s, geparkt: {geparkt}"
            )
        zeilen = ris_vergleich.zeilen_je_mandant()
        if not zeilen:
            self.stdout.write("Schatten-Quelle: leer")
        for mandant, typen in zeilen.items():
            werte = ", ".join(f"{typ} {anzahl}" for typ, anzahl in typen.items())
            self.stdout.write(f"  {mandant}: {werte}")

    # --- aufbauen -------------------------------------------------------------------------------

    def _aufbauen(self, options: dict[str, Any]) -> None:
        kennungen = self._gewaehlt(options["mandant"])
        if kennungen:
            mandanten = self._mandanten(kennungen)
        elif options["alle"]:
            mandanten = list(SessionTenant.objects.order_by("slug"))
        else:
            raise CommandError(
                "Keine Mandanten gewählt (--mandant oder RIS_SESSION_PROJECTOR_TENANTS). Für alle Mandanten --alle "
                "angeben."
            )
        if not options["trocken"] and not ris_vergleich.abonnement_stand().vorhanden:
            self.stdout.write(
                self.style.WARNING(
                    f"Abonnement {ris_session.NAME} ist noch nicht angelegt: Was bis zu seinem Start in Session "
                    "geschieht, fehlt im Schatten. Erst den Schalter setzen und den Worker starten."
                )
            )
        for tenant in mandanten:
            quelle = SessionQuelle(tenant)
            if not quelle.uebernommen:
                self.stdout.write(f"{tenant.pk}: nicht im Bürgerportal veröffentlicht, nichts gebaut")
                continue
            ergebnis = ris_session.aufbauen(quelle, trocken=options["trocken"])
            werte = ", ".join(f"{typ} {anzahl}" for typ, anzahl in ergebnis.zeilen.items()) or "keine Zeilen"
            self.stdout.write(f"{tenant.pk}: {ergebnis.gesamt} Zeilen ({werte})")
        if options["trocken"]:
            self.stdout.write("Trockenlauf: nichts geschrieben.")
        else:
            self.stdout.write(self.style.SUCCESS("Vollbau abgeschlossen."))

    # --- vergleichen ----------------------------------------------------------------------------

    def _vergleichen(self, options: dict[str, Any]) -> None:
        kennungen = self._gewaehlt(options["mandant"])
        if not kennungen:
            kennungen = sorted(set(RisSchatten.objects.values_list("mandant", flat=True).distinct()), key=str)
        typen = list(ris_vergleich.iter_typen(options["typ"]))
        beispiele = max(0, options["beispiele"])
        ergebnisse: list[ris_vergleich.Vergleich] = []
        for tenant in self._mandanten(kennungen):
            quellen = insight_service.session_sources(tenant).values_list("pk", flat=True)
            kommunen = ris_vergleich.kommunen_der_quellen(quellen)
            ergebnisse.append(ris_vergleich.vergleichen(tenant.pk, kommunen, typen))
        abweichungen = sum(vergleich.abweichungen for vergleich in ergebnisse)
        abo = ris_vergleich.abonnement_stand()
        if options["json"]:
            daten = {
                "zeitpunkt": timezone.now().isoformat(),
                "abonnement": abo.__dict__,
                "abweichungen": abweichungen,
                "mandanten": [vergleich.as_dict(beispiele) for vergleich in ergebnisse],
            }
            self.stdout.write(json.dumps(daten, ensure_ascii=False, indent=2))
        else:
            if abo.vorhanden:
                self.stdout.write(
                    f"Abonnement {ris_session.NAME}: {abo.zustand}, Cursor {abo.cursor} von "
                    f"{abo.hoechste_folgenummer}, offen {abo.offen}, Rückstand {abo.rueckstand_sekunden:.0f} s"
                )
            if not ergebnisse:
                self.stdout.write("Kein Mandant in der Schatten-Quelle.")
            for vergleich in ergebnisse:
                self._ausgeben(vergleich, beispiele)
        if abweichungen and options["streng"]:
            sys.exit(1)

    def _ausgeben(self, vergleich: ris_vergleich.Vergleich, beispiele: int) -> None:
        kommunen = ", ".join(str(kommune) for kommune in vergleich.kommunen) or "keine im Bestand"
        self.stdout.write(f"\nMandant {vergleich.mandant} (Kommunen: {kommunen})")
        for typ in vergleich.typen.values():
            zeile = (
                f"  {typ.typ:16} Bestand {typ.bestand:>6}  Schatten {typ.schatten:>6}  gleich {typ.gleich:>6}  "
                f"fehlt {len(typ.fehlt)}  überzählig {len(typ.ueberzaehlig)}  abweichend {len(typ.abweichend)}"
            )
            self.stdout.write(zeile if not typ.abweichungen else self.style.WARNING(zeile))
            if typ.felder:
                self.stdout.write("    Felder: " + ", ".join(f"{feld} {n}" for feld, n in sorted(typ.felder.items())))
            for titel, liste in (
                ("fehlt", typ.fehlt),
                ("überzählig", typ.ueberzaehlig),
                ("abweichend", typ.abweichend),
            ):
                if liste and beispiele:
                    self.stdout.write(f"    {titel}: {', '.join(sorted(liste)[:beispiele])}")

    # --- loeschen -------------------------------------------------------------------------------

    def _loeschen(self, options: dict[str, Any]) -> None:
        if not options["ja"]:
            raise CommandError("Löschen nur mit --ja.")
        if ris_vergleich.wird_zugestellt(ris_vergleich.abonnement_stand()):
            raise CommandError(
                "Das Abonnement schreibt noch in die Schatten-Quelle: erst RIS_SESSION_PROJECTOR=aus setzen und den "
                "Worker neu starten (oder das Abonnement im Admin pausieren), dann löschen."
            )
        angegeben = self._angegeben(options["mandant"])
        with transaction.atomic():
            ergebnis = ris_vergleich.entfernen(angegeben or None, abonnement=options["abonnement"])
            if ergebnis.zeilen or ergebnis.abonnement_entfernt:
                record_command(
                    "ris_projektor_schatten",
                    "ris_projektor_schatten_loeschen",
                    abonnement=ris_session.NAME,
                    mandanten=[str(kennung) for kennung in angegeben] or "alle",
                    zeilen=ergebnis.zeilen,
                    abonnement_entfernt=ergebnis.abonnement_entfernt,
                    cursor=ergebnis.cursor,
                    geparkt_entfernt=ergebnis.geparkt_entfernt,
                )
        self.stdout.write(f"Gelöscht: {ergebnis.zeilen} Zeilen der Schatten-Quelle")
        if options["abonnement"]:
            weg = "entfernt" if ergebnis.abonnement_entfernt else "war nicht angelegt"
            self.stdout.write(f"Abonnement {ris_session.NAME}: {weg}")
