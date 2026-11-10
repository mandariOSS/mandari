# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft Konfiguration und Integrationen der Installation, ohne etwas zu ändern (Issue #964).

    python manage.py doctor                         # lesbare Liste
    python manage.py doctor --json                  # dasselbe als JSON (für Skripte)
    python manage.py doctor --nur smtp --nur cache  # einzelne Prüfungen
    python manage.py doctor --zeitgrenze 20         # Sekunden je Prüfung (Standard 10)
    docker compose exec -T mandari python manage.py doctor --env-datei - < .env   # Abgleich mit der .env

Je Prüfung ``ok``, ``warnung``, ``fehler`` oder ``uebersprungen`` (nicht eingestellt). Beendet sich mit 1, sobald
eine Prüfung einen Fehler meldet, sonst mit 0. Keine Ausgabe nennt Passwörter, Schlüssel oder Tokens; aus
``--env-datei`` erscheinen nur Namen. Die Prüfungen stehen in ``apps/common/doctor`` und im Modul ``doctor`` der Apps.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.common.doctor import ZEITGRENZE, Ergebnis, Kontext, Status, ausfuehren, gesamtstatus, registrierte
from apps.common.doctor.umgebung import ENV_DATEI_MAX, geheime_werte, lies_env_datei

_ETIKETT = {
    Status.OK: "OK",
    Status.WARNUNG: "WARNUNG",
    Status.FEHLER: "FEHLER",
    Status.UEBERSPRUNGEN: "ÜBERSPRUNGEN",
}
#: Stufe, Wort in der Zusammenfassung für genau eine Prüfung und für jede andere Anzahl
_ZAEHLUNG = (
    (Status.OK, "ok", "ok"),
    (Status.WARNUNG, "Warnung", "Warnungen"),
    (Status.FEHLER, "Fehler", "Fehler"),
    (Status.UEBERSPRUNGEN, "übersprungen", "übersprungen"),
)


class Command(BaseCommand):
    help = (
        "Prüft Konfiguration und Integrationen (Umgebung, Datenbank, Cache, Mailversand, Objektspeicher, Worker), "
        "ohne etwas zu ändern und ohne Geheimnisse auszugeben. Exit-Code 1 bei Fehlern."
    )
    # Der Befehl soll gerade dann antworten, wenn etwas nicht stimmt
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--json", action="store_true", help="Ergebnis als JSON statt als Liste.")
        parser.add_argument(
            "--nur",
            action="append",
            metavar="NAME",
            help="Nur diese Prüfung (mehrfach möglich); die Namen stehen in der Liste und im JSON.",
        )
        parser.add_argument(
            "--zeitgrenze",
            type=float,
            default=ZEITGRENZE,
            metavar="SEKUNDEN",
            help=f"Zeitgrenze je Prüfung (Standard {ZEITGRENZE:g} s).",
        )
        parser.add_argument(
            "--env-datei",
            metavar="DATEI",
            help="Die .env der Installation für den Abgleich mit dem Prozess; „-“ liest die Standardeingabe.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        zeitgrenze = float(options["zeitgrenze"])
        if not (math.isfinite(zeitgrenze) and zeitgrenze > 0):
            raise CommandError("--zeitgrenze muss eine Zahl größer als 0 sein.")
        pruefungen = registrierte()
        gewaehlt: Sequence[str] = options["nur"] or ()
        if gewaehlt:
            bekannt = {eintrag.name for eintrag in pruefungen}
            if any(name not in bekannt for name in gewaehlt):
                # Feste Meldung ohne Echo der Eingabe
                raise CommandError(f"--nur kennt nur: {', '.join(eintrag.name for eintrag in pruefungen)}")
            pruefungen = [eintrag for eintrag in pruefungen if eintrag.name in gewaehlt]

        env_datei = self._env_datei(options["env_datei"])
        geheimnisse = geheime_werte(os.environ, env_datei or {})
        ergebnisse = ausfuehren(pruefungen, Kontext(zeitgrenze=zeitgrenze, env_datei=env_datei), geheimnisse)

        if options["json"]:
            self._als_json(ergebnisse)
        else:
            self._als_liste(ergebnisse)
        anzahl = sum(1 for ergebnis in ergebnisse if ergebnis.status is Status.FEHLER)
        if anzahl:
            raise CommandError(f"doctor: {anzahl} Prüfung{'en' if anzahl > 1 else ''} mit Fehler.")

    def _env_datei(self, quelle: str | None) -> dict[str, str] | None:
        """Zuweisungen aus ``--env-datei``; ``None`` ohne die Option. Meldungen nennen weder Pfad noch Inhalt."""
        if quelle is None:
            return None
        try:
            if quelle == "-":
                text = sys.stdin.read(ENV_DATEI_MAX + 1)
            else:
                with Path(quelle).open(encoding="utf-8", errors="replace") as datei:
                    text = datei.read(ENV_DATEI_MAX + 1)
        except OSError as exc:
            raise CommandError(f"--env-datei ist nicht lesbar ({type(exc).__name__}).") from None
        if len(text) > ENV_DATEI_MAX:
            raise CommandError("--env-datei ist zu groß für eine .env.")
        return lies_env_datei(text)

    def _zusammenfassung(self, ergebnisse: Sequence[Ergebnis]) -> dict[str, int]:
        return {status.value: sum(1 for e in ergebnisse if e.status is status) for status, _, _ in _ZAEHLUNG}

    def _als_json(self, ergebnisse: Sequence[Ergebnis]) -> None:
        daten = {
            "status": gesamtstatus(ergebnisse).value,
            "zusammenfassung": self._zusammenfassung(ergebnisse),
            "pruefungen": [ergebnis.als_dict() for ergebnis in ergebnisse],
        }
        self.stdout.write(json.dumps(daten, ensure_ascii=False, indent=2))

    def _als_liste(self, ergebnisse: Sequence[Ergebnis]) -> None:
        stile: dict[Status, Callable[[str], str]] = {
            Status.OK: self.style.SUCCESS,
            Status.WARNUNG: self.style.WARNING,
            Status.FEHLER: self.style.ERROR,
            Status.UEBERSPRUNGEN: self.style.NOTICE,
        }
        breite = max((len(ergebnis.titel) for ergebnis in ergebnisse), default=0)
        etikett_breite = max(len(text) for text in _ETIKETT.values())
        einzug = " " * (etikett_breite + 2)
        for ergebnis in ergebnisse:
            etikett = stile[ergebnis.status](f"{_ETIKETT[ergebnis.status]:<{etikett_breite}}")
            self.stdout.write(f"{etikett}  {ergebnis.titel:<{breite}}  {ergebnis.meldung}")
            for zeile in ergebnis.einzelheiten:
                self.stdout.write(f"{einzug}- {zeile}")
            if ergebnis.hinweis and ergebnis.status is not Status.OK:
                self.stdout.write(f"{einzug}Hinweis: {ergebnis.hinweis}")
        zaehlung = self._zusammenfassung(ergebnisse)
        self.stdout.write("")
        teile = [
            f"{zaehlung[status.value]} {eine if zaehlung[status.value] == 1 else mehrere}"
            for status, eine, mehrere in _ZAEHLUNG
        ]
        self.stdout.write("Ergebnis: " + ", ".join(teile))
