# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Anbieter auf der Kommandozeile (Issue #1046): Übersicht, Einrichten in einem Schritt, Verbindungsprüfung.

    python manage.py ki_anbieter                       # Vorlagen mit Freigabe, aktuelle Einrichtung
    python manage.py ki_anbieter einrichten --vorlage <vorlage> --modell <modell> \\
        --schluessel-aus-umgebung KI_SCHLUESSEL --work --buergerportal
    python manage.py ki_anbieter einrichten --basis-url https://<host>/v1 --name "<Anbieter>" \\
        --ort "<Verarbeitungsort>" --modell <modell> --schluessel-aus-umgebung <VARIABLE>
    python manage.py ki_anbieter pruefen               # Testaufruf je eingetragenem Modell

Einrichten geht durch das Formular der KI-Einstellungen im Admin (``AISettingsAdminForm``): dieselben Prüfungen
(Positivliste, Vorlage nur für ihren Host, Pflichtangaben beim eigenen Endpunkt, Schlüssel bei Anbieterwechsel)
und dieselbe Verschlüsselung, keine zweite Logik. Der Befehl gibt keinen Host frei: ``KI_ERLAUBTE_HOSTS`` bleibt
Umgebung. Ein Schlüssel kommt nur aus einer benannten Umgebungsvariable, nie von der Kommandozeile, und steht in
keiner Ausgabe. Mit denselben Angaben wiederholt, ändert der Befehl nichts.
"""

from __future__ import annotations

import hmac
import math
import os
import re
from typing import Any, cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.forms.models import model_to_dict
from mandari_dokumente.ki_hosts import gepruefter_host

from apps.common.admin import AISettingsAdminForm
from apps.common.ki_anbieter import (
    ANBIETER_VORLAGEN,
    EIGENER,
    PRUEF_ZEITGRENZE,
    endpunkt_der_einstellungen,
    erlaubte_hosts,
    pruefe_ki_einstellungen,
    vorlage_host,
    vorlage_nutzbar,
    wirksamer_host,
)
from apps.common.models import AISettings

#: Name einer Umgebungsvariable; anderes (etwa ein versehentlich übergebener Schlüssel) wird nie ausgegeben
_VARIABLENNAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")

#: Längere Namen erscheinen nicht in Meldungen: Ein Schlüssel nur aus Großbuchstaben, Ziffern und Unterstrichen
#: ginge sonst als Name durch und stünde in der Meldung „ist leer“
_NAME_IN_MELDUNG = 40

#: Anzeige einer Basis-URL, die so nie durch das Formular gekommen wäre (Altbestand, direkt in der Datenbank)
_UNZULAESSIG = "unzulässig, nicht angezeigt"

#: Felder des Schlüssels im Formular; alle übrigen Formularfelder kommen aus der gespeicherten Einrichtung
_SCHLUESSELFELDER = ("api_key", "api_key_loeschen")

#: Modellfelder und ihre Optionen
_MODELLE = (("model_name", "modell"), ("insight_model", "modell_buergerportal"), ("fallback_model", "ausweichmodell"))


def _ja(wert: bool) -> str:
    return "ja" if wert else "nein"


def _anzeigbare_basis_url(url: str) -> str:
    """
    Gespeicherte Basis-URL für die Ausgabe; leer bleibt leer.

    Nicht angezeigt wird eine Adresse, die das Formular so nicht annähme und die Zugangsdaten enthalten kann: mit
    Zugangsdaten, unzulässigem Schema oder Port (``gepruefter_host``) oder mit Abfrage oder Fragment (etwa
    ``?api-key=…``).
    """
    if not url:
        return ""
    return url if gepruefter_host(url) and not any(zeichen in url for zeichen in "?#") else _UNZULAESSIG


class Command(BaseCommand):
    help = (
        "KI-Anbieter: Vorlagen und Einrichtung anzeigen (ohne Schlüssel), in einem Schritt einrichten, Verbindung "
        "prüfen. Gibt keinen Host frei (KI_ERLAUBTE_HOSTS bleibt Umgebung)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        aktionen = parser.add_subparsers(dest="aktion", metavar="{liste,einrichten,pruefen}")
        aktionen.add_parser("liste", help="Vorlagen mit Host und Freigabe, aktuelle Einrichtung (Standard).")

        einrichten = aktionen.add_parser(
            "einrichten",
            help="KI-Einstellungen in einem Schritt setzen (gleiche Prüfungen wie der Admin).",
        )
        einrichten.add_argument(
            "--vorlage",
            choices=list(ANBIETER_VORLAGEN),
            help="Vorlage eines Anbieters; „eigener“ bzw. nur --basis-url für einen eigenen Endpunkt.",
        )
        einrichten.add_argument(
            "--basis-url",
            help="Basis-URL (https, Host aus KI_ERLAUBTE_HOSTS). Bei einer Vorlage darf nur der Pfad abweichen.",
        )
        einrichten.add_argument("--name", help="Anzeigename in Hinweisen und Einwilligung (eigener Endpunkt: Pflicht).")
        einrichten.add_argument(
            "--ort", help="Verarbeitungsort für Hinweise und Einwilligung (eigener Endpunkt: Pflicht)."
        )
        einrichten.add_argument("--modell", help="Modell beim Anbieter (Work, Standard fürs Bürgerportal).")
        einrichten.add_argument("--modell-buergerportal", help="Eigenes Modell fürs Bürgerportal; leer: wie Work.")
        einrichten.add_argument("--ausweichmodell", help="Ausweichmodell im Bürgerportal; leer: keines.")
        einrichten.add_argument(
            "--schluessel-aus-umgebung",
            metavar="VARIABLE",
            help="Name der Umgebungsvariable mit dem Schlüssel (etwa KI_SCHLUESSEL), nie den Schlüssel selbst.",
        )
        work = einrichten.add_mutually_exclusive_group()
        work.add_argument("--work", dest="work", action="store_const", const=True, help="KI in Work einschalten.")
        work.add_argument("--ohne-work", dest="work", action="store_const", const=False, help="KI in Work ausschalten.")
        portal = einrichten.add_mutually_exclusive_group()
        portal.add_argument(
            "--buergerportal", dest="buergerportal", action="store_const", const=True, help="KI im Bürgerportal ein."
        )
        portal.add_argument(
            "--ohne-buergerportal",
            dest="buergerportal",
            action="store_const",
            const=False,
            help="KI im Bürgerportal aus.",
        )
        einrichten.add_argument("--pruefen", action="store_true", help="Danach die Verbindung prüfen.")
        self._zeitgrenze(einrichten)

        pruefen = aktionen.add_parser("pruefen", help="Testaufruf je eingetragenem Modell (höchstens ein Token).")
        self._zeitgrenze(pruefen)

    @staticmethod
    def _zeitgrenze(parser: CommandParser) -> None:
        parser.add_argument(
            "--zeitgrenze",
            type=float,
            default=PRUEF_ZEITGRENZE,
            metavar="SEKUNDEN",
            help=f"Zeitgrenze je Testaufruf (Standard {PRUEF_ZEITGRENZE:g} s).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        aktion = options.get("aktion") or "liste"
        zeitgrenze = options.get("zeitgrenze", PRUEF_ZEITGRENZE)
        if not (math.isfinite(zeitgrenze) and zeitgrenze > 0):
            # Vor jedem Speichern prüfen, nicht erst bei der Verbindungsprüfung danach
            raise CommandError("--zeitgrenze muss eine Zahl größer als 0 sein.")
        if aktion == "einrichten":
            self._einrichten(options)
        elif aktion == "pruefen":
            self._pruefen(options["zeitgrenze"])
        else:
            self._liste()

    # ------------------------------------------------------------------ Übersicht

    def _liste(self) -> None:
        hosts = erlaubte_hosts()
        self.stdout.write(f"KI_ERLAUBTE_HOSTS: {', '.join(hosts) if hosts else 'leer, jeder KI-Aufruf ist gesperrt'}")
        self.stdout.write("")
        self.stdout.write("Vorlagen (eine Vorlage gibt keinen Host frei):")
        for anbieter, vorlage in ANBIETER_VORLAGEN.items():
            if anbieter == EIGENER:
                self.stdout.write(
                    f"  {anbieter:<15} {vorlage.anzeigename} (Basis-URL frei, Host muss freigegeben sein)"
                )
                continue
            freigabe = "freigegeben" if vorlage_nutzbar(anbieter) else "nicht freigegeben"
            self.stdout.write(f"  {anbieter:<15} {vorlage.anzeigename:<36} {vorlage_host(anbieter)}  {freigabe}")
        self.stdout.write("")
        self._zeige_einrichtung(AISettings.objects.get_or_create(pk=1)[0])

    def _zeige_einrichtung(self, ki: AISettings) -> None:
        from apps.tenants.models import Organization

        vorlage = ANBIETER_VORLAGEN.get(ki.provider or "")
        host = wirksamer_host(ki.provider or "", ki.base_url or "")
        aus_vorlage = " (Vorlage)" if vorlage is not None else ""
        zustand = endpunkt_der_einstellungen(ki)
        basis_url = _anzeigbare_basis_url(ki.base_url or "")
        zeilen = [
            ("Anbieter", f"{ki.provider} ({vorlage.anzeigename})" if vorlage else "nicht eingerichtet"),
            (
                "Basis-URL",
                basis_url or (f"{vorlage.basis_url}{aus_vorlage}" if vorlage and vorlage.basis_url else "–"),
            ),
            ("Host", f"{host}, freigegeben: {_ja(host in erlaubte_hosts())}" if host else "–"),
            (
                "Anzeigename",
                ki.anzeigename
                or (f"{vorlage.anzeigename}{aus_vorlage}" if vorlage and ki.provider != EIGENER else "–"),
            ),
            (
                "Verarbeitungsort",
                ki.verarbeitungsort
                or (f"{vorlage.verarbeitungsort}{aus_vorlage}" if vorlage and vorlage.verarbeitungsort else "–"),
            ),
            ("Modell (Work)", ki.model_name or "–"),
            ("Modell (Bürgerportal)", ki.insight_model or "wie Work"),
            ("Ausweichmodell", ki.fallback_model or "–"),
            ("Schlüssel gesetzt", _ja(bool(ki.api_key_encrypted))),
            ("KI in Work", "an" if ki.enabled else "aus"),
            ("KI im Bürgerportal", "an" if ki.insight_enabled else "aus"),
            ("Zustand", f"aus: {zustand}" if isinstance(zustand, str) else "bereit (Schalter siehe oben)"),
        ]
        self.stdout.write("Einrichtung (KI-Einstellungen, ohne Schlüssel):")
        for name, wert in zeilen:
            self.stdout.write(f"  {name + ':':<23} {wert}")
        anzahl = Organization.objects.filter(ai_api_key_encrypted__isnull=False).count()
        self.stdout.write(f"Organisationen mit eigenem KI-Schlüssel: {anzahl}")

    # ------------------------------------------------------------------ Einrichten

    def _schluessel(self, name: str | None) -> str:
        """Schlüssel aus der benannten Umgebungsvariable; ohne Option leer (der gespeicherte bleibt)."""
        if name is None:
            return ""
        if not _VARIABLENNAME.match(name):
            raise CommandError(
                "--schluessel-aus-umgebung erwartet den Namen einer Umgebungsvariable in Großbuchstaben (etwa "
                "KI_SCHLUESSEL), nicht den Schlüssel selbst."
            )
        wert = os.environ.get(name, "").strip()
        if not wert:
            genannt = name if len(name) <= _NAME_IN_MELDUNG else "<VARIABLE>"
            raise CommandError(
                f"Umgebungsvariable {genannt} ist leer oder nicht gesetzt. Im Container: Variable in der Shell "
                f"setzen und mit „docker exec -e {genannt} …“ übergeben."
            )
        return wert

    def _formulardaten(self, ki: AISettings, optionen: dict[str, Any]) -> dict[str, Any]:
        """Gespeicherte Einrichtung als Formulardaten, darüber die angegebenen Optionen."""
        felder = [name for name in AISettingsAdminForm.base_fields if name not in _SCHLUESSELFELDER]
        daten = {name: wert for name, wert in model_to_dict(ki, fields=felder).items() if wert is not None}
        if optionen["vorlage"] is not None or optionen["basis_url"] is not None:
            # Der Endpunkt wird ganz neu angegeben: Angaben des bisherigen Anbieters gelten nicht weiter
            if not (optionen["modell"] or "").strip():
                raise CommandError("Beim Einrichten eines Anbieters --modell angeben (Modellname beim Anbieter).")
            anbieter = optionen["vorlage"] or EIGENER
            daten.update(
                provider=anbieter,
                base_url=optionen["basis_url"] or "",
                anzeigename=optionen["name"] or "",
                verarbeitungsort=optionen["ort"] or "",
            )
            if wirksamer_host(ki.provider or "", ki.base_url or "") != wirksamer_host(anbieter, daten["base_url"]):
                # Modelle gehören zum Anbieter: Bürgerportal- und Ausweichmodell des bisherigen gelten nicht
                for feld, option in _MODELLE[1:]:
                    if optionen[option] is None:
                        daten[feld] = ""
        else:
            if optionen["name"] is not None:
                daten["anzeigename"] = optionen["name"]
            if optionen["ort"] is not None:
                daten["verarbeitungsort"] = optionen["ort"]
        for feld, option in _MODELLE:
            if optionen[option] is not None:
                daten[feld] = optionen[option].strip()
        if optionen["work"] is not None:
            daten["enabled"] = optionen["work"]
        if optionen["buergerportal"] is not None:
            daten["insight_enabled"] = optionen["buergerportal"]
        return daten

    def _fehlertext(self, form: Any, daten: dict[str, Any]) -> str:
        """Fehler des Formulars, ohne Werte; bei gesperrtem Host mit Hinweis auf die Freigabe in der Umgebung."""
        zeilen = ["Nicht gespeichert:"]
        for feld, fehler in form.errors.as_data().items():
            name = form.fields[feld].label if feld in form.fields else "Einrichtung"
            zeilen.extend(f"  {name}: {' '.join(e.messages)}" for e in fehler)
        codes = {e.code for fehler in form.errors.as_data().values() for e in fehler}
        if "nicht_freigegeben" in codes:
            host = wirksamer_host(daten.get("provider") or "", daten.get("base_url") or "")
            hosts = ", ".join(erlaubte_hosts()) or "leer"
            if host:
                zeilen.append(
                    f"Host {host} steht nicht in KI_ERLAUBTE_HOSTS (derzeit: {hosts}). Host in der Umgebung "
                    "freigeben, Anwendung, Worker und Ingestor neu starten, dann erneut ausführen; der Befehl gibt "
                    "keinen Host frei."
                )
            else:
                zeilen.append("Basis-URL unzulässig: nur https, ohne Zugangsdaten in der Adresse, Port nur 443.")
        return "\n".join(zeilen)

    def _einrichten(self, optionen: dict[str, Any]) -> None:
        schluessel = self._schluessel(optionen["schluessel_aus_umgebung"])
        ki, _ = AISettings.objects.get_or_create(pk=1)
        vorher = model_to_dict(ki)
        bisheriger_schluessel = ki.get_api_key()
        daten = self._formulardaten(ki, optionen)
        daten["api_key"] = schluessel
        # Formular des Admins (untypisiert): dieselben Prüfungen und dieselbe Verschlüsselung
        form: Any = cast(Any, AISettingsAdminForm)(data=daten, instance=ki)
        if not form.is_valid():
            raise CommandError(self._fehlertext(form, daten))

        geaendert = [name for name in form.cleaned_data if name in vorher and form.cleaned_data[name] != vorher[name]]
        neuer_schluessel = bool(schluessel) and not hmac.compare_digest(
            schluessel.encode("utf-8"), bisheriger_schluessel.encode("utf-8")
        )
        if not geaendert and not neuer_schluessel:
            self.stdout.write("Keine Änderung: Die KI-Einstellungen entsprechen schon diesen Angaben.")
        else:
            if not neuer_schluessel:
                # Derselbe Schlüssel: nicht erneut verschlüsseln
                form.cleaned_data["api_key"] = ""
            form.save()
            teile = [f"geändert: {', '.join(geaendert)}"] if geaendert else []
            if neuer_schluessel:
                teile.append("Schlüssel neu gesetzt")
            self.stdout.write(self.style.SUCCESS(f"Gespeichert ({'; '.join(teile)})."))
        self._zeige_einrichtung(AISettings.objects.get(pk=1))
        if optionen["pruefen"]:
            self._pruefen(optionen["zeitgrenze"])
        else:
            self.stdout.write("Verbindung prüfen: python manage.py ki_anbieter pruefen")

    # ------------------------------------------------------------------ Prüfen

    def _pruefen(self, zeitgrenze: float) -> None:
        ergebnisse = pruefe_ki_einstellungen(AISettings.objects.get_or_create(pk=1)[0], zeitgrenze=zeitgrenze)
        for ergebnis in ergebnisse:
            stil = self.style.SUCCESS if ergebnis.ok else self.style.ERROR
            self.stdout.write(stil(f"{'OK' if ergebnis.ok else 'FEHLER'} – {ergebnis.text}"))
        if not all(ergebnis.ok for ergebnis in ergebnisse):
            raise CommandError("Verbindungsprüfung fehlgeschlagen.")
