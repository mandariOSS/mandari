# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Datenabgleich mit den Schnittstellen der Kommunen (Issue #1525).

Prüft je Kommune die interne Konsistenz (Sitzungen −14 … +60 Tage: TOPs ohne Beratung bzw. ohne Vorlage in Work,
Beratungen ohne Vorlage, Vorlagen ohne Dateien, lückenhafte Listen), legt das Ergebnis an der Quelle ab und gibt
eine Tabelle mit Ampel aus, zusammen mit dem letzten Abgleich mit der Quelle (``totalElements`` gegen Bestand, schreibt
der Ingestor). Der Betriebsmonitor bewertet die Quellen danach; kritische lösen den Alarm aus.

Zeitplan im Worker: ``befehl:datenabgleich`` täglich 06:05 Uhr. Von Hand:

    python manage.py datenabgleich                     # alle Kommunen, Ergebnis ablegen
    python manage.py datenabgleich --kommune Münster   # nur passende Kommunen
    python manage.py datenabgleich --report            # nur ausgeben, nichts ablegen
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.common.einmalig import EinmaligMixin


def _zahl(intern: dict[str, Any] | None, schluessel: str) -> str:
    if intern is None:
        return "–"
    return str(int(intern.get(schluessel) or 0))


class Command(EinmaligMixin, BaseCommand):
    sperre = "datenabgleich"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    nur_lesend = ("report",)  # nur Ausgabe: läuft auch, wenn der Worker den Zeitplan bedient
    help = (
        "Datenabgleich mit den Schnittstellen der Kommunen: interne Konsistenz prüfen, ablegen und als Tabelle zeigen"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--kommune", default="", help="Nur Kommunen, deren Name oder Kennung den Text enthält (ohne Groß/klein)"
        )
        parser.add_argument("--report", action="store_true", help="Nur ausgeben, nichts an den Quellen ablegen")

    def handle(self, *args: Any, **options: Any) -> None:
        from django.utils import timezone

        from insight_core.services import datenabgleich

        now = timezone.now()
        quellen = datenabgleich.pruefe_alle(options["kommune"], speichern=not options["report"], now=now)
        if options["report"]:
            # Die Tabelle bewertet die eben ermittelten Befunde, auch ohne Ablage
            self.stdout.write("(--report: Befunde nicht abgelegt; Abgleich mit der Quelle wie zuletzt abgelegt)")
        kopf = (
            f"{'Ampel':<10} {'Kommune':<36} {'TOPs':>5} {'Verw.':>5} {'o.Ber':>5} {'o.Vorl':>6} {'Lücke':>5} "
            f"{'Ber.o.V':>7} {'Vorl.o.D':>8} {'Stand':>11}  Quelle"
        )
        self.stdout.write(kopf)
        self.stdout.write("-" * len(kopf))
        zaehler = {"ok": 0, "warning": 0, "critical": 0}
        details: list[str] = []
        alle = [
            (source, kommune)
            for source in quellen
            for kommune in datenabgleich.bewerte_kommunen(source, now)
            if not options["kommune"] or self._passt(kommune, options["kommune"])
        ]
        # Kritische zuerst, über alle Quellen
        alle.sort(key=lambda paar: (-datenabgleich.STUFEN[paar[1].status], paar[1].name))
        for source, kommune in alle:
            zaehler[kommune.status] += 1
            intern = kommune.intern
            stand = f"{timezone.localtime(kommune.stand):%d.%m. %H:%M}" if kommune.stand else "–"
            self.stdout.write(
                f"{kommune.ampel:<10} {kommune.name[:36]:<36} {_zahl(intern, 'tops'):>5} "
                f"{_zahl(intern, 'mit_beratung'):>5} {_zahl(intern, 'beratung_fehlt'):>5} "
                f"{_zahl(intern, 'work_ohne_vorlage'):>6} {_zahl(intern, 'top_luecke'):>5} "
                f"{_zahl(intern, 'beratung_ohne_vorlage'):>7} {_zahl(intern, 'vorlage_ohne_dateien'):>8} "
                f"{stand:>11}  {self._quelle(kommune.quelle)}"
            )
            if kommune.gruende or kommune.hinweise:
                details.append(f"{kommune.name} ({source.name}) – {kommune.ampel}")
                details.extend(f"  ! {grund}" for grund in kommune.gruende)
                details.extend(f"  · {hinweis}" for hinweis in kommune.hinweise)
                if intern and intern.get("beispiele"):
                    details.append("  Beispiele: " + ", ".join(intern["beispiele"]))
        self.stdout.write("")
        self.stdout.write(
            "Spalten: TOPs im Fenster, davon mit Beratungsverweis (Verw.), Beratung fehlt (o.Ber), Work findet keine "
            "Vorlage (o.Vorl), Lücke (eins von beiden), Beratungen ohne Vorlage, Vorlagen ohne Dateien, Stand = "
            "letzter lückenloser Abgleich; Quelle = Abweichung totalElements/Bestand je Liste (Ingestor)."
        )
        if details:
            self.stdout.write("")
            self.stdout.write("\n".join(details))
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{zaehler['critical']} kritisch, {zaehler['warning']} auffällig, {zaehler['ok']} ok"
                + ("" if options["report"] else " (abgelegt)")
            )
        )

    @staticmethod
    def _passt(kommune: Any, text: str) -> bool:
        suche = text.lower()
        return suche in kommune.name.lower() or suche in kommune.kennung.lower()

    @staticmethod
    def _quelle(eintrag: dict[str, Any] | None) -> str:
        """Abgleich mit der Quelle kurz: je Liste ``Quelle/Bestand``, Fehler als ``?``; ohne Eintrag ``–``."""
        if not eintrag or not isinstance(eintrag.get("listen"), dict):
            return "–"
        from insight_core.services.datenabgleich import LISTEN

        teile = []
        for typ in LISTEN:
            werte = eintrag["listen"].get(typ)
            if not isinstance(werte, dict):
                continue
            if werte.get("fehler"):
                teile.append(f"{typ} ?")
            elif isinstance(werte.get("quelle"), int):
                teile.append(f"{typ} {werte['quelle']}/{werte.get('bestand')}")
            else:
                teile.append(f"{typ} –/{werte.get('bestand')}")
        at = str(eintrag.get("at") or "")[:10]
        return ", ".join(teile) + (f" ({at})" if at else "")
