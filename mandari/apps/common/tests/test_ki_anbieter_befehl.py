# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbieterwechsel ohne Umweg über den Admin (Issue #1046): Befehl ``ki_anbieter`` und Verbindungsprüfung.

- ``pruefe_verbindung`` stellt einen Testaufruf mit höchstens einem Token Antwort an die Chat-Schnittstelle, folgt
  keiner Weiterleitung und meldet nur Art des Ergebnisses, Host und Modell, nie Schlüssel oder Antworttext.
- ``ki_anbieter`` (Übersicht) nennt Vorlagen mit Host und Freigabe und die Einrichtung ohne Schlüssel.
- ``ki_anbieter einrichten`` geht durch das Formular des Admins: Abbruch bei nicht freigegebenem Host, leerer
  Variable und unvollständigem eigenen Endpunkt, kein Schlüssel in Ausgabe oder Protokoll, wiederholbar ohne
  Änderung.
- ``ki_anbieter pruefen`` und „Speichern und Verbindung prüfen“ im Admin nutzen dieselbe Prüfung, der Admin erst
  nach dem Commit; während der Testaufrufe liegt die Datenbankverbindung beim Pool.
- Die Befehle in DEPLOYMENT.md („KI-Anbieter“) gibt es so wirklich.

Netzwerk gibt es in keinem Test: Die Prüfung läuft gegen eine Attrappe (``httpx.MockTransport``).
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import shlex
import ssl
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, override_settings

from apps.common import admin as admin_modul
from apps.common import ki_anbieter
from apps.common.ki_anbieter import FREMDER_HOST, KiEndpunkt, pruefe_ki_einstellungen, pruefe_verbindung
from apps.common.models import AISettings
from apps.common.tests.verbindungsprobe import Verbindungsprobe

befehl_modul = importlib.import_module("apps.common.management.commands.ki_anbieter")

NEBIUS_HOST = "api.tokenfactory.nebius.com"
NEBIUS = f"https://{NEBIUS_HOST}/v1"
STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
STACKIT = f"https://{STACKIT_HOST}/v1"
SCHLUESSEL = "ki-befehl-testschluessel-GEHEIM-0123456789"
ZWEITER = "ki-befehl-zweiter-schluessel-GEHEIM-9876543210"
VARIABLE = "KI_TEST_SCHLUESSEL"
ANTWORTTEXT = "ANTWORTTEXT-DES-ANBIETERS-NIE-AUSGEBEN"
WECHSEL = "Bei einem Anbieterwechsel bitte den Schlüssel des neuen Anbieters eintragen."

REPO = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


@pytest.fixture(autouse=True)
def _freigabe() -> Iterator[None]:
    """Die Positivliste hat keinen Standard: Die Tests geben den Nebius-Host ausdrücklich frei."""
    with override_settings(KI_ERLAUBTE_HOSTS=[NEBIUS_HOST, STACKIT_HOST]):
        yield


class Attrappe:
    """Anbieter im Test: zeichnet Anfragen auf und antwortet nach ``antwort`` (Standard: Erfolg)."""

    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self.antwort: Callable[[httpx.Request], httpx.Response] = lambda _anfrage: httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}
        )

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        return self.antwort(anfrage)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def anbieter(monkeypatch: pytest.MonkeyPatch) -> Attrappe:
    """Befehl und Admin prüfen gegen die Attrappe statt gegen das Netz."""
    attrappe = Attrappe()

    def mit_attrappe(ki: AISettings | None = None, **optionen: Any) -> list[ki_anbieter.Pruefergebnis]:
        return pruefe_ki_einstellungen(ki, **{**optionen, "transport": attrappe.transport()})

    monkeypatch.setattr(befehl_modul, "pruefe_ki_einstellungen", mit_attrappe)
    monkeypatch.setattr(admin_modul, "pruefe_ki_einstellungen", mit_attrappe)
    return attrappe


def _ki(provider: str = "nebius", schluessel: str = SCHLUESSEL, **werte: Any) -> AISettings:
    """Gespeicherte KI-Einstellungen, ohne Formularprüfung (Stand aus der Datenbank)."""
    ki, _ = AISettings.objects.get_or_create(pk=1)
    felder = {"provider": provider, "model_name": "moonshotai/Kimi-K2.6", **werte}
    for name, wert in felder.items():
        setattr(ki, name, wert)
    ki.set_api_key(schluessel)
    ki.save()
    return ki


def _gespeichert() -> AISettings:
    cache.delete(AISettings.CACHE_KEY)
    return AISettings.objects.get_or_create(pk=1)[0]


def _befehl(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("ki_anbieter", *argumente, stdout=ausgabe, stderr=ausgabe)
    return ausgabe.getvalue()


def _endpunkt(**werte: Any) -> KiEndpunkt:
    angaben = {
        "anbieter": "nebius",
        "anzeigename": "Nebius Token Factory",
        "verarbeitungsort": "Ort",
        "base_url": NEBIUS,
        "api_key": SCHLUESSEL,
        "modell": "moonshotai/Kimi-K2.6",
        **werte,
    }
    return KiEndpunkt(**angaben)


# =============================================================================
# Verbindungsprüfung
# =============================================================================


class TestVerbindungspruefung:
    def test_erfolg_mit_einem_token(self) -> None:
        attrappe = Attrappe()
        ergebnis = pruefe_verbindung(_endpunkt(), zweck="Work", transport=attrappe.transport())
        assert (ergebnis.ok, ergebnis.art) == (True, "ok")
        assert (
            ergebnis.text == "Work: Modell moonshotai/Kimi-K2.6 an api.tokenfactory.nebius.com: Verbindung in Ordnung"
        )
        (anfrage,) = attrappe.anfragen
        assert anfrage.method == "POST" and str(anfrage.url) == f"{NEBIUS}/chat/completions"
        assert anfrage.headers["Authorization"] == f"Bearer {SCHLUESSEL}"
        inhalt = json.loads(anfrage.content)
        assert inhalt["model"] == "moonshotai/Kimi-K2.6" and inhalt["max_tokens"] == 1
        assert "max_completion_tokens" not in inhalt and inhalt["stream"] is False
        assert SCHLUESSEL not in repr(ergebnis) and SCHLUESSEL not in ergebnis.text

    def test_antwortlaenge_wie_bei_echten_aufrufen(self) -> None:
        """An Hosts, die ``max_tokens`` nicht beachten, geht die Grenze wie sonst auch als ``max_completion_tokens``."""
        attrappe = Attrappe()
        pruefe_verbindung(_endpunkt(auch_max_completion_tokens=True), transport=attrappe.transport())
        inhalt = json.loads(attrappe.anfragen[0].content)
        assert inhalt["max_tokens"] == 1 and inhalt["max_completion_tokens"] == 1

    @pytest.mark.parametrize(
        ("status", "art", "hinweis"),
        [
            (401, "schluessel", "Schlüssel abgelehnt"),
            (403, "schluessel", "Schlüssel abgelehnt"),
            (404, "nicht_gefunden", "Modellname und Basis-URL prüfen"),
            (400, "abgelehnt", "unbekannten Modells"),
            (422, "abgelehnt", "unbekannten Modells"),
            (402, "limit", "Guthaben"),
            (429, "limit", "Ratenlimit"),
            (500, "anbieter", "Fehler beim Anbieter"),
            (503, "anbieter", "Fehler beim Anbieter"),
            (418, "unerwartet", "Unerwartete Antwort"),
        ],
    )
    def test_fehlerarten_ohne_antworttext(self, status: int, art: str, hinweis: str) -> None:
        attrappe = Attrappe()
        attrappe.antwort = lambda _anfrage: httpx.Response(status, text=f"{ANTWORTTEXT} {SCHLUESSEL}")
        ergebnis = pruefe_verbindung(_endpunkt(), transport=attrappe.transport())
        assert (ergebnis.ok, ergebnis.art) == (False, art)
        assert hinweis in ergebnis.meldung and f"HTTP {status}" in ergebnis.meldung
        assert ANTWORTTEXT not in ergebnis.text and SCHLUESSEL not in ergebnis.text

    def test_umleitung_wird_nicht_verfolgt(self) -> None:
        attrappe = Attrappe()
        attrappe.antwort = lambda _anfrage: httpx.Response(302, headers={"Location": "https://anderer.example/v1"})
        ergebnis = pruefe_verbindung(_endpunkt(), transport=attrappe.transport())
        assert ergebnis.art == "umleitung" and len(attrappe.anfragen) == 1

    def test_antwort_ohne_openai_format(self) -> None:
        attrappe = Attrappe()
        attrappe.antwort = lambda _anfrage: httpx.Response(200, text="<html>Anmeldung</html>")
        assert pruefe_verbindung(_endpunkt(), transport=attrappe.transport()).art == "format"

    def test_zeitueberschreitung(self) -> None:
        def zu_langsam(anfrage: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("zu langsam", request=anfrage)

        ergebnis = pruefe_verbindung(_endpunkt(), zeitgrenze=2.5, transport=httpx.MockTransport(zu_langsam))
        assert ergebnis.art == "zeit" and "2.5 s" in ergebnis.meldung

    def test_keine_verbindung(self) -> None:
        def kein_netz(anfrage: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"getaddrinfo failed für {anfrage.url}", request=anfrage)

        ergebnis = pruefe_verbindung(_endpunkt(), transport=httpx.MockTransport(kein_netz))
        assert ergebnis.art == "verbindung" and "/chat/completions" not in ergebnis.text

    def test_tls_fehler(self) -> None:
        def falsches_zertifikat(anfrage: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Verbindung abgebrochen", request=anfrage) from ssl.SSLCertVerificationError(
                "certificate verify failed"
            )

        ergebnis = pruefe_verbindung(_endpunkt(), transport=httpx.MockTransport(falsches_zertifikat))
        assert ergebnis.art == "tls" and "TLS" in ergebnis.meldung

    def test_protokoll_ohne_schluessel_und_inhalt(self, caplog: pytest.LogCaptureFixture) -> None:
        attrappe = Attrappe()
        attrappe.antwort = lambda _anfrage: httpx.Response(401, text=f"{ANTWORTTEXT} {SCHLUESSEL}")
        with caplog.at_level(logging.DEBUG):
            pruefe_verbindung(_endpunkt(), transport=attrappe.transport())
        assert NEBIUS_HOST in caplog.text and "schluessel" in caplog.text
        assert SCHLUESSEL not in caplog.text and ANTWORTTEXT not in caplog.text and "OK." not in caplog.text

    def test_unzulaessige_zeichen_im_schluessel(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ein mitkopiertes unsichtbares Zeichen: eigene Fehlerart statt Ausnahme, ohne Schlüssel und Ausnahmetext."""
        attrappe = Attrappe()
        with caplog.at_level(logging.DEBUG):
            ergebnis = pruefe_verbindung(_endpunkt(api_key=SCHLUESSEL + "​"), transport=attrappe.transport())
        assert (ergebnis.ok, ergebnis.art) == (False, "zeichen")
        assert "unzulässige Zeichen" in ergebnis.meldung and attrappe.anfragen == []
        for text in (ergebnis.text, repr(ergebnis), caplog.text):
            assert SCHLUESSEL not in text and "ascii" not in text and "u200b" not in text


@pytest.mark.django_db(transaction=True)
def test_keine_pool_verbindung_waehrend_der_pruefung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Je Testaufruf bis zu 15 s: Die Datenbankverbindung liegt solange beim Pool (wie bei den echten Aufrufen)."""
    ki = _ki(model_name="modell-a", insight_model="modell-b")
    probe = Verbindungsprobe()
    attrappe = Attrappe()
    erfolg = attrappe.antwort

    def antwort(anfrage: httpx.Request) -> httpx.Response:
        probe.aufruf()
        return erfolg(anfrage)

    attrappe.antwort = antwort
    with probe.mitschreiben(monkeypatch):
        ergebnisse = pruefe_ki_einstellungen(ki, transport=attrappe.transport())
    assert [e.ok for e in ergebnisse] == [True, True]
    probe.pruefen(aufrufe=2)


@pytest.mark.django_db
class TestPruefeKiEinstellungen:
    def test_jedes_modell_einmal(self) -> None:
        ki = _ki(model_name="modell-a", insight_model="modell-b", fallback_model="modell-a")
        attrappe = Attrappe()
        ergebnisse = pruefe_ki_einstellungen(ki, transport=attrappe.transport())
        assert [(e.modell, e.zweck) for e in ergebnisse] == [
            ("modell-a", "Work, Ausweichmodell"),
            ("modell-b", "Bürgerportal"),
        ]
        assert [json.loads(a.content)["model"] for a in attrappe.anfragen] == ["modell-a", "modell-b"]

    def test_unabhaengig_von_den_schaltern(self) -> None:
        """Ein Anbieter lässt sich vor dem Einschalten prüfen."""
        ki = _ki(enabled=False, insight_enabled=False)
        ergebnisse = pruefe_ki_einstellungen(ki, transport=Attrappe().transport())
        assert [(e.ok, e.zweck) for e in ergebnisse] == [(True, "Work, Bürgerportal")]

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST])
    def test_nicht_freigegebener_host_ohne_aufruf(self) -> None:
        attrappe = Attrappe()
        (ergebnis,) = pruefe_ki_einstellungen(_ki(), transport=attrappe.transport())
        assert ergebnis.art == "nicht_eingerichtet" and "KI_ERLAUBTE_HOSTS" in ergebnis.text
        assert NEBIUS_HOST in ergebnis.text and attrappe.anfragen == []

    def test_ohne_schluessel_ohne_aufruf(self) -> None:
        attrappe = Attrappe()
        (ergebnis,) = pruefe_ki_einstellungen(_ki(schluessel=""), transport=attrappe.transport())
        assert ergebnis.text == "KI-Einstellungen: kein Schlüssel gespeichert" and attrappe.anfragen == []

    @override_settings(DEMO_INSTANCE=True)
    def test_demo_ohne_aufruf(self) -> None:
        attrappe = Attrappe()
        (ergebnis,) = pruefe_ki_einstellungen(_ki(), transport=attrappe.transport())
        assert "Demo" in ergebnis.meldung and attrappe.anfragen == []


# =============================================================================
# Befehl: Übersicht
# =============================================================================


@pytest.mark.django_db
class TestBefehlListe:
    @override_settings(KI_ERLAUBTE_HOSTS=[])
    def test_ohne_einrichtung(self) -> None:
        ausgabe = _befehl()
        assert "KI_ERLAUBTE_HOSTS: leer, jeder KI-Aufruf ist gesperrt" in ausgabe
        for anbieter in ("stackit", "ionos", "scaleway", "ovh", "deutschlandgpt", "nebius"):
            zeile = next(z for z in ausgabe.splitlines() if z.strip().startswith(anbieter + " "))
            assert zeile.rstrip().endswith("nicht freigegeben"), zeile
        assert re.search(r"Anbieter:\s+nicht eingerichtet", ausgabe)
        assert re.search(r"Schlüssel gesetzt:\s+nein", ausgabe)
        assert _befehl("liste") == ausgabe

    def test_mit_freigabe_und_schluessel(self, org: Any) -> None:
        _ki(insight_enabled=True)
        org.ai_provider = "nebius"
        org.set_ai_api_key("org-schluessel-GEHEIM")
        org.save()
        ausgabe = _befehl("liste")
        assert re.search(rf"nebius\s+Nebius Token Factory\s+{re.escape(NEBIUS_HOST)}\s+freigegeben", ausgabe)
        assert re.search(r"Anbieter:\s+nebius \(Nebius Token Factory\)", ausgabe)
        assert re.search(rf"Host:\s+{re.escape(NEBIUS_HOST)}, freigegeben: ja", ausgabe)
        assert re.search(r"Schlüssel gesetzt:\s+ja", ausgabe)
        assert re.search(r"Zustand:\s+bereit", ausgabe)
        assert "Organisationen mit eigenem KI-Schlüssel: 1" in ausgabe
        assert SCHLUESSEL not in ausgabe and "org-schluessel-GEHEIM" not in ausgabe

    def test_unzulaessige_adresse_wird_nicht_ausgegeben(self) -> None:
        """Eine Adresse mit Zugangsdaten (direkt in der Datenbank) erscheint nicht, auch nicht im Grund."""
        _ki(
            provider="eigener",
            base_url="https://nutzer:passwort-GEHEIM@ki.example/v1",
            anzeigename="Eigen",
            verarbeitungsort="Ort",
        )
        ausgabe = _befehl()
        assert "passwort-GEHEIM" not in ausgabe and "unzulässig, nicht angezeigt" in ausgabe
        assert re.search(r"Zustand:\s+aus: Endpunkt gesperrt", ausgabe)

    @pytest.mark.parametrize("anhang", ["?api-key=abfrage-GEHEIM", "#abfrage-GEHEIM", "/?x=abfrage-GEHEIM"])
    def test_adresse_mit_abfrage_wird_nicht_ausgegeben(self, anhang: str) -> None:
        """Altbestand nach common/0011 (Basis-URL bleibt stehen): Abfrage oder Fragment erscheint nicht."""
        _ki(provider="eigener", base_url=STACKIT + anhang, anzeigename="Eigen", verarbeitungsort="Ort")
        ausgabe = _befehl()
        assert "abfrage-GEHEIM" not in ausgabe
        assert re.search(r"Basis-URL:\s+unzulässig, nicht angezeigt", ausgabe)

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST])
    def test_nennt_den_grund(self) -> None:
        _ki()
        ausgabe = _befehl()
        assert re.search(rf"Zustand:\s+aus: Endpunkt gesperrt, Host {re.escape(NEBIUS_HOST)}", ausgabe)
        assert re.search(rf"Host:\s+{re.escape(NEBIUS_HOST)}, freigegeben: nein", ausgabe)


# =============================================================================
# Befehl: Einrichten
# =============================================================================


@pytest.mark.django_db
class TestBefehlEinrichten:
    NEBIUS_ARGS = ("einrichten", "--vorlage", "nebius", "--modell", "moonshotai/Kimi-K2.6")

    def test_nebius_in_einem_schritt(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Wie in DEPLOYMENT.md: Schlüssel aus NEBIUS_API_KEY, Work und Bürgerportal an."""
        monkeypatch.setenv("NEBIUS_API_KEY", SCHLUESSEL)
        with caplog.at_level(logging.DEBUG):
            ausgabe = _befehl(
                *self.NEBIUS_ARGS, "--schluessel-aus-umgebung", "NEBIUS_API_KEY", "--work", "--buergerportal"
            )
        ki = _gespeichert()
        assert (ki.provider, ki.base_url, ki.model_name) == ("nebius", "", "moonshotai/Kimi-K2.6")
        assert ki.enabled is True and ki.insight_enabled is True
        assert ki.get_api_key() == SCHLUESSEL
        assert "Schlüssel neu gesetzt" in ausgabe and "ki_anbieter pruefen" in ausgabe
        erfasst = capsys.readouterr()
        for text in (ausgabe, caplog.text, erfasst.out, erfasst.err):
            assert SCHLUESSEL not in text
        endpunkt = ki_anbieter.endpunkt_fuer_insight()
        assert endpunkt is not None and endpunkt.base_url == NEBIUS

    @pytest.mark.parametrize(("freigabe", "derzeit"), [([STACKIT_HOST], STACKIT_HOST), ([], "leer")])
    def test_abbruch_bei_nicht_freigegebenem_host(
        self, monkeypatch: pytest.MonkeyPatch, freigabe: list[str], derzeit: str
    ) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        with override_settings(KI_ERLAUBTE_HOSTS=freigabe), pytest.raises(CommandError) as fehler:
            _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE)
        meldung = str(fehler.value)
        assert f"Host {NEBIUS_HOST} steht nicht in KI_ERLAUBTE_HOSTS (derzeit: {derzeit})" in meldung
        assert "gibt keinen Host frei" in meldung and SCHLUESSEL not in meldung
        ki = _gespeichert()
        assert ki.provider == "" and ki.api_key_encrypted is None

    @pytest.mark.parametrize("wert", [None, "", "   "])
    def test_abbruch_bei_leerer_variable(self, monkeypatch: pytest.MonkeyPatch, wert: str | None) -> None:
        if wert is None:
            monkeypatch.delenv(VARIABLE, raising=False)
        else:
            monkeypatch.setenv(VARIABLE, wert)
        with pytest.raises(CommandError, match=f"Umgebungsvariable {VARIABLE} ist leer oder nicht gesetzt"):
            _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE)
        assert _gespeichert().provider == ""

    def test_schluessel_statt_variablenname_wird_nicht_ausgegeben(self) -> None:
        versehentlich = "sk-live-" + SCHLUESSEL.lower()
        with pytest.raises(CommandError) as fehler:
            _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", versehentlich)
        assert "nicht den Schlüssel selbst" in str(fehler.value)
        assert versehentlich not in str(fehler.value) and SCHLUESSEL.lower() not in str(fehler.value)

    @pytest.mark.parametrize("laenge", [41, 64, 65])
    def test_langer_name_nicht_in_meldung(self, monkeypatch: pytest.MonkeyPatch, laenge: int) -> None:
        """Ein Schlüssel nur aus Großbuchstaben, Ziffern und Unterstrichen ginge als Name durch: nie wiedergeben."""
        versehentlich = ("SK_LIVE_" + "0123456789ABCDEF" * 5)[:laenge]
        monkeypatch.delenv(versehentlich, raising=False)
        with pytest.raises(CommandError) as fehler:
            _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", versehentlich)
        assert versehentlich not in str(fehler.value)
        if laenge > 64:
            assert "nicht den Schlüssel selbst" in str(fehler.value)
        else:
            assert "Umgebungsvariable <VARIABLE> ist leer" in str(fehler.value)
        assert _gespeichert().provider == ""

    def test_wiederholt_ohne_aenderung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        argumente = (*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE, "--work", "--ohne-buergerportal")
        assert "Gespeichert" in _befehl(*argumente)
        vorher = _gespeichert()
        ausgabe = _befehl(*argumente)
        nachher = _gespeichert()
        assert "Keine Änderung" in ausgabe and "Gespeichert" not in ausgabe
        assert vorher.api_key_encrypted is not None and nachher.api_key_encrypted is not None
        assert bytes(nachher.api_key_encrypted) == bytes(vorher.api_key_encrypted)
        assert nachher.updated_at == vorher.updated_at

    def test_modell_ist_beim_anbieter_pflicht(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        with pytest.raises(CommandError, match="--modell angeben"):
            _befehl("einrichten", "--vorlage", "nebius", "--schluessel-aus-umgebung", VARIABLE)

    def test_eigener_endpunkt_mit_den_pruefungen_des_admins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gleiche Logik wie im Admin: dieselben Fehlermeldungen des Formulars."""
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        basis = ("einrichten", "--basis-url", STACKIT + "/", "--modell", "m", "--schluessel-aus-umgebung", VARIABLE)
        with pytest.raises(CommandError) as fehler:
            _befehl(*basis)
        assert "Beim eigenen Endpunkt ist der Anzeigename Pflicht." in str(fehler.value)
        assert "Beim eigenen Endpunkt ist der Verarbeitungsort Pflicht." in str(fehler.value)
        _befehl(*basis, "--name", "Eigener Dienst", "--ort", "Rechenzentrum in Frankfurt")
        ki = _gespeichert()
        assert (ki.provider, ki.base_url, ki.anzeigename) == ("eigener", STACKIT, "Eigener Dienst")

    def test_vorlage_mit_fremdem_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        with pytest.raises(CommandError) as fehler:
            _befehl(*self.NEBIUS_ARGS, "--basis-url", STACKIT, "--schluessel-aus-umgebung", VARIABLE)
        assert FREMDER_HOST in str(fehler.value)

    def test_nach_migration_schluessel_des_neuen_anbieters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """common/0011 setzt den Anbieter auf leer; der alte Schlüssel geht nie stillschweigend an den neuen."""
        _ki(provider="")
        with pytest.raises(CommandError) as fehler:
            _befehl(*self.NEBIUS_ARGS)
        assert WECHSEL in str(fehler.value)
        monkeypatch.setenv(VARIABLE, ZWEITER)
        _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE)
        assert _gespeichert().get_api_key() == ZWEITER

    def test_schluessel_ohne_anbieter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        with pytest.raises(CommandError, match="Anbieter wählen oder den API Key löschen."):
            _befehl("einrichten", "--schluessel-aus-umgebung", VARIABLE)

    def test_anbieterwechsel_leert_modelle_des_bisherigen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ki(provider="stackit", insight_model="stackit-modell", fallback_model="stackit-ausweich")
        monkeypatch.setenv(VARIABLE, ZWEITER)
        _befehl(*self.NEBIUS_ARGS, "--ausweichmodell", "nebius-ausweich", "--schluessel-aus-umgebung", VARIABLE)
        ki = _gespeichert()
        assert (ki.insight_model, ki.fallback_model) == ("", "nebius-ausweich")

    def test_gleicher_host_behaelt_modelle_und_schluessel(self) -> None:
        _ki(insight_model="portal-modell")
        ausgabe = _befehl("einrichten", "--vorlage", "nebius", "--modell", "anderes-modell")
        ki = _gespeichert()
        assert (ki.model_name, ki.insight_model, ki.get_api_key()) == ("anderes-modell", "portal-modell", SCHLUESSEL)
        assert "geändert: model_name" in ausgabe and "Schlüssel neu gesetzt" not in ausgabe

    def test_nur_schalter(self) -> None:
        _ki(enabled=True, insight_enabled=False)
        _befehl("einrichten", "--ohne-work", "--buergerportal")
        ki = _gespeichert()
        assert (ki.enabled, ki.insight_enabled, ki.provider) == (False, True, "nebius")
        assert ki.get_api_key() == SCHLUESSEL

    def test_schluessel_wechseln(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ki()
        monkeypatch.setenv(VARIABLE, ZWEITER)
        ausgabe = _befehl("einrichten", "--schluessel-aus-umgebung", VARIABLE)
        assert "Schlüssel neu gesetzt" in ausgabe and ZWEITER not in ausgabe
        assert _gespeichert().get_api_key() == ZWEITER

    def test_einrichten_und_pruefen(self, monkeypatch: pytest.MonkeyPatch, anbieter: Attrappe) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        ausgabe = _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE, "--pruefen")
        assert "OK – Work, Bürgerportal: Modell moonshotai/Kimi-K2.6 an api.tokenfactory.nebius.com" in ausgabe
        assert len(anbieter.anfragen) == 1 and SCHLUESSEL not in ausgabe

    @pytest.mark.parametrize("zeitgrenze", ["0", "-1", "nan", "inf"])
    def test_zeitgrenze_vor_dem_speichern(
        self, monkeypatch: pytest.MonkeyPatch, anbieter: Attrappe, zeitgrenze: str
    ) -> None:
        monkeypatch.setenv(VARIABLE, SCHLUESSEL)
        with pytest.raises(CommandError, match="--zeitgrenze"):
            _befehl(*self.NEBIUS_ARGS, "--schluessel-aus-umgebung", VARIABLE, "--pruefen", "--zeitgrenze", zeitgrenze)
        assert _gespeichert().provider == "" and anbieter.anfragen == []


# =============================================================================
# Befehl: Prüfen
# =============================================================================


@pytest.mark.django_db
class TestBefehlPruefen:
    def test_erfolg(self, anbieter: Attrappe) -> None:
        _ki()
        ausgabe = _befehl("pruefen")
        assert "OK – Work, Bürgerportal: Modell moonshotai/Kimi-K2.6 an api.tokenfactory.nebius.com" in ausgabe
        assert "Verbindung in Ordnung" in ausgabe and len(anbieter.anfragen) == 1

    def test_fehler_bricht_ab(self, anbieter: Attrappe) -> None:
        _ki()
        anbieter.antwort = lambda _anfrage: httpx.Response(401, text=f"{ANTWORTTEXT} {SCHLUESSEL}")
        ausgabe = StringIO()
        with pytest.raises(CommandError, match="Verbindungsprüfung fehlgeschlagen"):
            call_command("ki_anbieter", "pruefen", stdout=ausgabe)
        assert "FEHLER" in ausgabe.getvalue() and "Schlüssel abgelehnt (HTTP 401)" in ausgabe.getvalue()
        assert SCHLUESSEL not in ausgabe.getvalue() and ANTWORTTEXT not in ausgabe.getvalue()

    def test_nicht_eingerichtet(self, anbieter: Attrappe) -> None:
        ausgabe = StringIO()
        with pytest.raises(CommandError):
            call_command("ki_anbieter", "pruefen", stdout=ausgabe)
        assert "kein Schlüssel gespeichert" in ausgabe.getvalue() and anbieter.anfragen == []

    def test_zeitgrenze_positiv(self) -> None:
        with pytest.raises(CommandError, match="--zeitgrenze"):
            _befehl("pruefen", "--zeitgrenze", "0")


# =============================================================================
# Admin: „Speichern und Verbindung prüfen“
# =============================================================================


def _admin_daten(ki: AISettings, **werte: Any) -> dict[str, Any]:
    """POST-Daten des Formulars der KI-Einstellungen (Häkchen nur, wenn gesetzt)."""
    daten: dict[str, Any] = {
        "provider": ki.provider,
        "base_url": ki.base_url,
        "anzeigename": ki.anzeigename,
        "verarbeitungsort": ki.verarbeitungsort,
        "api_key": "",
        "model_name": ki.model_name,
        "insight_model": ki.insight_model,
        "fallback_model": ki.fallback_model,
        "max_output_tokens": ki.max_output_tokens,
        "insight_max_output_tokens": ki.insight_max_output_tokens,
        "default_org_monthly_token_limit": ki.default_org_monthly_token_limit,
    }
    for feld in ("enabled", "insight_enabled"):
        if getattr(ki, feld):
            daten[feld] = "on"
    return {**daten, **werte}


@pytest.mark.django_db
class TestAdmin:
    KNOPF = "common_aisettings_speichern_und_pruefen"

    def test_knopf_neben_speichern(self, admin_client: Client) -> None:
        ki = _ki()
        inhalt = admin_client.get(f"/admin/common/aisettings/{ki.pk}/change/").content.decode()
        assert f'name="{self.KNOPF}"' in inhalt and "Speichern und Verbindung prüfen" in inhalt

    # Geprüft wird erst nach dem Commit (transaction.on_commit): Diese Tests brauchen echte Transaktionen
    @pytest.mark.django_db(transaction=True)
    def test_speichern_und_pruefen(self, admin_client: Client, anbieter: Attrappe) -> None:
        ki = _ki()
        antwort = admin_client.post(
            f"/admin/common/aisettings/{ki.pk}/change/", {**_admin_daten(ki, model_name="neues-modell"), self.KNOPF: ""}
        )
        assert antwort.status_code == 302
        assert _gespeichert().model_name == "neues-modell"
        meldungen = [str(m) for m in get_messages(antwort.wsgi_request)]
        assert any(
            "Verbindungsprüfung – Work, Bürgerportal: Modell neues-modell an api.tokenfactory.nebius.com: "
            "Verbindung in Ordnung" in m
            for m in meldungen
        ), meldungen
        assert [json.loads(a.content)["model"] for a in anbieter.anfragen] == ["neues-modell"]

    @pytest.mark.django_db(transaction=True)
    def test_fehler_als_meldung_ohne_schluessel(self, admin_client: Client, anbieter: Attrappe) -> None:
        ki = _ki()
        anbieter.antwort = lambda _anfrage: httpx.Response(401, text=f"{ANTWORTTEXT} {SCHLUESSEL}")
        antwort = admin_client.post(
            f"/admin/common/aisettings/{ki.pk}/change/", {**_admin_daten(ki), self.KNOPF: ""}, follow=True
        )
        inhalt = antwort.content.decode()
        assert "Schlüssel abgelehnt (HTTP 401)" in inhalt
        assert SCHLUESSEL not in inhalt and ANTWORTTEXT not in inhalt

    @pytest.mark.django_db(transaction=True)
    def test_pruefung_erst_nach_dem_commit(
        self, admin_client: Client, anbieter: Attrappe, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Die Testaufrufe halten weder Transaktion und Zeilensperre noch eine Verbindung aus dem Pool."""
        ki = _ki()
        probe = Verbindungsprobe()
        in_transaktion: list[bool] = []
        erfolg = anbieter.antwort

        def antwort(anfrage: httpx.Request) -> httpx.Response:
            probe.aufruf()
            in_transaktion.append(connection.in_atomic_block)
            return erfolg(anfrage)

        anbieter.antwort = antwort
        with probe.mitschreiben(monkeypatch):
            seite = admin_client.post(
                f"/admin/common/aisettings/{ki.pk}/change/",
                {**_admin_daten(ki, model_name="neues-modell"), self.KNOPF: ""},
                follow=True,
            )
        assert in_transaktion == [False]
        probe.pruefen(aufrufe=1)
        assert "Verbindung in Ordnung" in seite.content.decode()
        assert _gespeichert().model_name == "neues-modell"

    @pytest.mark.django_db(transaction=True)
    def test_ausnahme_in_der_pruefung_rollt_nicht_zurueck(
        self, admin_client: Client, anbieter: Attrappe, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Ein unerwarteter Fehler beim Prüfen: Das Gespeicherte bleibt, Meldung nur mit dem Typ der Ausnahme."""
        ki = _ki()

        def kaputt(_anfrage: httpx.Request) -> httpx.Response:
            raise RuntimeError(f"{ANTWORTTEXT} {SCHLUESSEL}")

        anbieter.antwort = kaputt
        with caplog.at_level(logging.DEBUG):
            seite = admin_client.post(
                f"/admin/common/aisettings/{ki.pk}/change/",
                {**_admin_daten(ki, model_name="neues-modell"), self.KNOPF: ""},
                follow=True,
            )
        inhalt = seite.content.decode()
        assert seite.status_code == 200 and _gespeichert().model_name == "neues-modell"
        assert "Verbindungsprüfung abgebrochen (RuntimeError)" in inhalt
        for text in (inhalt, caplog.text):
            assert SCHLUESSEL not in text and ANTWORTTEXT not in text

    def test_ungueltig_ohne_aufruf(self, admin_client: Client, anbieter: Attrappe) -> None:
        ki = _ki()
        with override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST]):
            antwort = admin_client.post(
                f"/admin/common/aisettings/{ki.pk}/change/", {**_admin_daten(ki), self.KNOPF: ""}
            )
        assert antwort.status_code == 200 and anbieter.anfragen == []

    def test_nur_speichern_ohne_aufruf(self, admin_client: Client, anbieter: Attrappe) -> None:
        ki = _ki()
        antwort = admin_client.post(f"/admin/common/aisettings/{ki.pk}/change/", _admin_daten(ki))
        assert antwort.status_code == 302 and anbieter.anfragen == []


# =============================================================================
# Anleitung
# =============================================================================


def _ki_abschnitt() -> str:
    text = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
    return text.split("### KI-Anbieter", 1)[1].split("\n### ", 1)[0]


def test_anleitung_nebius_weiter_nutzen() -> None:
    abschnitt = _ki_abschnitt()
    assert "**Anbieter wechseln in drei Schritten**" in abschnitt
    nebius = re.sub(r"\s+", " ", abschnitt.split("**Nebius nach `common/0011` weiter nutzen:**", 1)[1])
    nebius = nebius.split("**Rückfall auf ein älteres Image:**", 1)[0]
    assert "KI_ERLAUBTE_HOSTS=api.tokenfactory.nebius.com" in nebius
    assert "--vorlage nebius" in nebius and "--schluessel-aus-umgebung NEBIUS_API_KEY" in nebius
    assert "docker exec -e NEBIUS_API_KEY" in nebius
    # Entfernen erst nach der Übertragung
    assert nebius.index("ki_anbieter einrichten") < nebius.index("`NEBIUS_API_KEY` aus der `.env` entfernen")


def test_befehle_der_anleitung_gibt_es() -> None:
    """Jeder Aufruf von ``ki_anbieter`` in der Anleitung lässt sich mit dem echten Befehl so verarbeiten."""
    abschnitt = _ki_abschnitt().replace("\\\n", " ")
    aufrufe = re.findall(r"manage\.py ki_anbieter([^`\n]*)", abschnitt)
    assert len(aufrufe) >= 3
    parser = befehl_modul.Command().create_parser("manage.py", "ki_anbieter")
    for aufruf in aufrufe:
        argumente = shlex.split(re.sub(r"<[^>]+>", "x", aufruf.replace("<vorlage>", "nebius")))
        parser.parse_args(argumente)
