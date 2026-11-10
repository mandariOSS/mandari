# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``manage.py doctor`` (Issue #964, erster Teil): Grundgerüst und die Prüfungen für Umgebung, Datenbank, Cache,
Mailversand, Objektspeicher, Aufgaben-Backend und Worker.

- Jede Prüfung meldet ``ok``, ``warnung``, ``fehler`` oder ``uebersprungen``; was nicht eingestellt ist, ist
  übersprungen, kein Fehler.
- Der Lauf hängt nicht: Eine Prüfung ohne Antwort gilt nach der Zeitgrenze als gescheitert.
- Keine Prüfung schreibt: nur ``SELECT`` in der Datenbank, kein Wert im Cache, keine Mail, kein Objekt.
- **Keine Ausgabe nennt ein Geheimnis:** Passwörter, Schlüssel und Tokens stehen weder in der Liste noch im JSON
  noch in einer Fehlermeldung, auch wenn die Fehler der Dienste sie im Text tragen.
- Die Listen in ``doctor/umgebung.py`` passen zu ``.env.example`` und ``docker-compose.yml`` (dieselben Hilfen wie
  ``test_umgebung_compose.py``).

Netzwerk gibt es in keinem Test: Mailserver und Objektspeicher sind Attrappen (moto als S3-Ersatz).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import smtplib
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.common.doctor import (
    Befund,
    Kontext,
    Pruefung,
    Status,
    ausfuehren,
    dienste,
    fehler,
    kern,
    ok,
    registrierte,
    umgebung,
)
from apps.common.models import SiteSettings
from apps.common.tests import test_umgebung_compose as compose
from apps.events import presence
from insight_core import doctor as ablage
from insight_core.services import object_storage

REPO = Path(__file__).resolve().parents[4]
HAUPTSCHLUESSEL = base64.b64encode(bytes(range(32))).decode()

# Erkennbare Werte: Keiner darf je in einer Ausgabe stehen
GEHEIM = {
    "SECRET_KEY": "GEHEIM-django-schluessel-0123456789abcdef",
    "ENCRYPTION_MASTER_KEY": "GEHEIM-hauptschluessel-kein-base64-!!",
    "DATABASE_URL": "postgresql://mandari:GEHEIM-datenbank-passwort-4711@db.invalid:5432/mandari",
    "REDIS_URL": "redis://:GEHEIM-redis-passwort-4711@redis.invalid:6379/0",
    "EMAIL_HOST_PASSWORD": "GEHEIM-smtp-passwort-umgebung-4711",
    "OBJ_KEY": "GEHEIM-objektspeicher-kennung-4711",
    "OBJ_SECRET": "GEHEIM-objektspeicher-schluessel-4711",
    "METRICS_TOKEN": "GEHEIM-metrik-token-4711",
    "WORKER_PUSH_TOKEN": "GEHEIM-worker-token-4711",
    "MISTRAL_API_KEY": "GEHEIM-ocr-api-key-4711",
    "PROVISIONING_V2_API_KEY": "GEHEIM-provisioning-key-4711",
    "INDEXNOW_KEY": "GEHEIM-indexnow-key-4711",
}
SMTP_PASSWORT = "GEHEIM-smtp-passwort-systemeinstellungen-4711"
SMTP_BENUTZER = "GEHEIM-smtp-benutzer@mail.invalid"
KURZ = "kurzGEHEIM"  # kürzer als kern.MINDESTLAENGE: Nur feste Meldungstexte halten es draußen
TEILE = ("GEHEIM", "4711")


def _ohne_geheimnis(text: str) -> None:
    for teil in TEILE:
        assert teil not in text, f"„{teil}“ steht in der Ausgabe"


# ---------------------------------------------------------------------------------------------- Grundgerüst


def _eintrag(name: str, funktion: Any, rang: int = 100) -> Pruefung:
    return Pruefung(name, name.capitalize(), funktion, rang)


def test_lauf_meldet_jede_stufe_mit_meldung_hinweis_und_dauer() -> None:
    eintraege = [
        _eintrag("gut", lambda k: ok("läuft")),
        _eintrag("mahnt", lambda k: kern.warnung("fast", "nachsehen", ["eins", "zwei"])),
        _eintrag("kaputt", lambda k: fehler("geht nicht", "reparieren")),
        _eintrag("aus", lambda k: kern.uebersprungen("nicht eingestellt")),
    ]

    ergebnisse = ausfuehren(eintraege, Kontext(zeitgrenze=5))

    assert [(e.name, e.status) for e in ergebnisse] == [
        ("gut", Status.OK),
        ("mahnt", Status.WARNUNG),
        ("kaputt", Status.FEHLER),
        ("aus", Status.UEBERSPRUNGEN),
    ]
    assert ergebnisse[1].als_dict() == {
        "name": "mahnt",
        "titel": "Mahnt",
        "status": "warnung",
        "meldung": "fast",
        "hinweis": "nachsehen",
        "einzelheiten": ["eins", "zwei"],
        "dauer_ms": ergebnisse[1].dauer_ms,
    }
    assert kern.gesamtstatus(ergebnisse) is Status.FEHLER
    assert kern.gesamtstatus(ergebnisse[:2]) is Status.WARNUNG
    assert kern.gesamtstatus([ergebnisse[0], ergebnisse[3]]) is Status.OK, "übersprungen zählt nicht"


def test_haengende_pruefung_haelt_den_lauf_nicht_auf() -> None:
    ende = threading.Event()

    def haengt(kontext: Kontext) -> Befund:
        ende.wait(30)
        return ok("zu spät")

    beginn = time.monotonic()
    try:
        ergebnisse = ausfuehren([_eintrag("haengt", haengt), _eintrag("gut", lambda k: ok("läuft"))], Kontext(0.3))
    finally:
        ende.set()

    assert time.monotonic() - beginn < 5, "der Lauf wartet nur die Zeitgrenze ab"
    assert ergebnisse[0].status is Status.FEHLER
    assert ergebnisse[0].meldung == "keine Antwort innerhalb von 0.3 s"
    assert ergebnisse[1].status is Status.OK, "die anderen Prüfungen laufen nebenher zu Ende"


def test_haengende_pruefung_laeuft_als_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Thread ohne ``daemon`` hielte das Ende des Prozesses auf, bis die Prüfung doch noch antwortet."""
    gestartet: list[threading.Thread] = []
    start = threading.Thread.start

    def merken(thread: threading.Thread) -> None:
        gestartet.append(thread)
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", merken)

    ausfuehren([_eintrag("gut", lambda k: ok("läuft"))], Kontext(zeitgrenze=5))

    # Nur die Threads des Laufs: Nebenher starten auch andere (Testverteilung, Verbindungspool)
    assert [thread.daemon for thread in gestartet if thread.name.startswith("doctor-")] == [True]


def test_ausnahme_wird_zum_befund_ohne_ihren_text() -> None:
    def bricht(kontext: Kontext) -> Befund:
        raise RuntimeError(f"Anmeldung mit {KURZ} gescheitert")

    (ergebnis,) = ausfuehren([_eintrag("bricht", bricht)], Kontext(zeitgrenze=5))

    assert ergebnis.status is Status.FEHLER
    assert ergebnis.meldung == "Prüfung abgebrochen (RuntimeError)"
    _ohne_geheimnis(json.dumps(ergebnis.als_dict()))


def test_zweites_netz_ersetzt_bekannte_geheime_werte() -> None:
    lang = GEHEIM["OBJ_SECRET"]
    eintrag = _eintrag("plaudert", lambda k: fehler(f"Antwort: {lang}", f"Wert {lang}x", [f"{lang} und {KURZ}"]))

    (ergebnis,) = ausfuehren([eintrag], Kontext(zeitgrenze=5), geheimnisse={lang, lang + "x", KURZ})

    assert ergebnis.meldung == "Antwort: [entfernt]"
    assert ergebnis.hinweis == "Wert [entfernt]", "der längere Wert zuerst, sonst bliebe sein Rest stehen"
    assert ergebnis.einzelheiten == (f"[entfernt] und {KURZ}",), "kurze Werte bleiben: sie zerstückelten Meldungen"


def test_geheime_werte_aus_namen_adressen_und_einstellungen(settings: Any) -> None:
    settings.SECRET_KEY = "aus-den-einstellungen-geheim"
    werte = umgebung.geheime_werte(
        {**GEHEIM, "EMAIL_HOST": "smtp.mail.invalid", "OBJ_BUCKET": "mandari-dokumente", "LEER_TOKEN": ""}
    )

    for name in ("SECRET_KEY", "ENCRYPTION_MASTER_KEY", "EMAIL_HOST_PASSWORD", "OBJ_KEY", "OBJ_SECRET"):
        assert GEHEIM[name] in werte, name
    for name in ("METRICS_TOKEN", "WORKER_PUSH_TOKEN", "MISTRAL_API_KEY", "PROVISIONING_V2_API_KEY", "INDEXNOW_KEY"):
        assert GEHEIM[name] in werte, name
    assert "GEHEIM-datenbank-passwort-4711" in werte and "GEHEIM-redis-passwort-4711" in werte
    assert "aus-den-einstellungen-geheim" in werte
    assert "smtp.mail.invalid" not in werte and "mandari-dokumente" not in werte and "" not in werte


def test_register_kennt_die_pruefungen_in_fester_reihenfolge() -> None:
    assert [eintrag.name for eintrag in registrierte()] == [
        "umgebung",
        "umgebung-datei",
        "datenbank",
        "cache",
        "smtp",
        "objektspeicher",
        "aufgaben",
        "worker",
    ]
    with pytest.raises(ValueError, match="schon registriert"):
        kern.pruefung("datenbank", "Doppelt")(lambda kontext: ok("nein"))


# ---------------------------------------------------------------------------------------------- Umgebung


@pytest.fixture
def pflicht_gesetzt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.DEBUG = False
    monkeypatch.setenv("SECRET_KEY", "x" * 50)
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", HAUPTSCHLUESSEL)
    monkeypatch.setenv("DATABASE_URL", "postgresql://mandari:pw@postgres:5432/mandari")
    monkeypatch.setenv("REDIS_URL", "redis://:pw@redis:6379/0")
    monkeypatch.setenv("SITE_URL", "https://mandari.example.org")


def test_pflichtvariablen_gesetzt(pflicht_gesetzt: None) -> None:
    befund = umgebung.pruefe_pflichtvariablen(Kontext())

    assert (befund.status, befund.meldung) == (Status.OK, "5 von 5 gesetzt")


def test_pflichtvariable_fehlt_oder_traegt_den_platzhalter(
    pflicht_gesetzt: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.setenv("SITE_URL", " ")
    monkeypatch.setenv("SECRET_KEY", "CHANGE_ME_generate_with_openssl_rand_base64_50")

    befund = umgebung.pruefe_pflichtvariablen(Kontext())

    assert befund.status is Status.FEHLER
    assert befund.meldung == "nicht gesetzt: REDIS_URL, SITE_URL; noch der Platzhalter aus .env.example: SECRET_KEY"
    assert "docker compose up -d" in befund.hinweis


def test_fehlende_pflichtvariable_ist_in_der_entwicklung_nur_eine_warnung(
    pflicht_gesetzt: None, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.DEBUG = True
    monkeypatch.delenv("REDIS_URL")

    assert umgebung.pruefe_pflichtvariablen(Kontext()).status is Status.WARNUNG

    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", "CHANGE_ME_generate_with_openssl_rand_base64_32")
    assert umgebung.pruefe_pflichtvariablen(Kontext()).status is Status.FEHLER, "Platzhalter bleibt ein Fehler"


@pytest.mark.parametrize("wert", ["kein base64 !!", base64.b64encode(b"zu kurz").decode()])
def test_hauptschluessel_muss_ein_schluessel_sein(
    pflicht_gesetzt: None, monkeypatch: pytest.MonkeyPatch, wert: str
) -> None:
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", wert)

    befund = umgebung.pruefe_pflichtvariablen(Kontext())

    assert befund.status is Status.FEHLER
    assert befund.meldung == "ENCRYPTION_MASTER_KEY ist kein Schlüssel (Base64 von genau 32 Byte)"
    assert wert not in befund.meldung + befund.hinweis


def test_abgleich_ohne_datei_ist_uebersprungen() -> None:
    befund = umgebung.pruefe_env_datei(Kontext())

    assert befund.status is Status.UEBERSPRUNGEN
    assert "--env-datei - < .env" in befund.hinweis


ENV_DATEI = """
# Kommentar
DOMAIN=mandari.example.org
EMAIL_HOST=smtp.mail.invalid
EMAIL_HOST_PASSWORD='GEHEIM-$mtp-4711'
INDEXNOW_KEY="GEHEIM-indexnow-4711"   # Kommentar dahinter
TASKS_BACKEND=
# OBJ_ENABLED=true
DEBUG=true
DATABASE_URL=postgresql://mandari:GEHEIM-db-4711@localhost:5432/mandari
GEHEIM-verrutschter-wert-ohne-zuweisung-4711
geheim_klein_4711=wert
"""


def test_env_datei_liest_zuweisungen_wie_die_anwendung() -> None:
    assert umgebung.lies_env_datei(ENV_DATEI) == {
        "DOMAIN": "mandari.example.org",
        "EMAIL_HOST": "smtp.mail.invalid",
        "EMAIL_HOST_PASSWORD": "GEHEIM-$mtp-4711",
        "INDEXNOW_KEY": "GEHEIM-indexnow-4711",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql://mandari:GEHEIM-db-4711@localhost:5432/mandari",
    }, "ohne Kommentare, leere Werte (Vorgabe), Zeilen ohne Zuweisung und Namen, die keine Variablennamen sind"


def test_abgleich_alles_angekommen(monkeypatch: pytest.MonkeyPatch) -> None:
    datei = umgebung.lies_env_datei(ENV_DATEI)
    for name, wert in datei.items():
        monkeypatch.setenv(name, wert)
    # Im Container setzt docker-compose.yml diese Werte selbst: Abweichung ist gewollt
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql://mandari:anders@postgres:5432/mandari")

    befund = umgebung.pruefe_env_datei(Kontext(env_datei=datei))

    assert (befund.status, befund.meldung) == (Status.OK, "6 Zuweisungen der Datei sind im Prozess angekommen")


def test_abgleich_meldet_was_nicht_ankommt_und_was_abweicht_nur_mit_namen(monkeypatch: pytest.MonkeyPatch) -> None:
    datei = umgebung.lies_env_datei(ENV_DATEI)
    for name, wert in datei.items():
        monkeypatch.setenv(name, wert)
    monkeypatch.setenv("EMAIL_HOST_PASSWORD", "GEHEIM-altes-passwort-4711")

    abweichend = umgebung.pruefe_env_datei(Kontext(env_datei=datei))

    assert abweichend.status is Status.WARNUNG
    assert abweichend.meldung == "anderer Wert als in der Datei: EMAIL_HOST_PASSWORD"
    assert "docker compose up -d" in abweichend.hinweis

    monkeypatch.delenv("INDEXNOW_KEY")
    monkeypatch.delenv("EMAIL_HOST")
    fehlend = umgebung.pruefe_env_datei(Kontext(env_datei=datei))

    assert fehlend.status is Status.FEHLER
    assert fehlend.meldung == (
        "kommt im Prozess nicht an: EMAIL_HOST, INDEXNOW_KEY; anderer Wert als in der Datei: EMAIL_HOST_PASSWORD"
    )
    for befund in (abweichend, fehlend):
        _ohne_geheimnis(befund.meldung + befund.hinweis + " ".join(befund.einzelheiten))


def test_abgleich_kuerzt_lange_listen() -> None:
    datei = {f"FEHLT_IM_PROZESS_{nummer:02d}": "wert" for nummer in range(25)}

    befund = umgebung.pruefe_env_datei(Kontext(env_datei=datei))

    assert befund.meldung.endswith("FEHLT_IM_PROZESS_19 und 5 weitere")
    assert umgebung.pruefe_env_datei(Kontext(env_datei={})).status is Status.WARNUNG, "leere Datei: nichts geprüft"


class TestListenPassenZuBeispielUndCompose:
    """Die Listen in ``doctor/umgebung.py`` sind von Hand gepflegt; hier fällt auf, wenn sie veralten."""

    def test_pflichtvariablen_setzt_compose_fuer_anwendung_und_worker(self) -> None:
        dienste_compose = compose._dienste()
        for dienst in compose.MIT_ENV_FILE:
            gesetzt = set(dienste_compose[dienst]["environment"])
            assert set(umgebung.PFLICHT) <= gesetzt, f"{dienst}: {sorted(set(umgebung.PFLICHT) - gesetzt)}"

    def test_was_compose_fuer_die_anwendung_verlangt_ist_pflicht(self) -> None:
        """``${NAME:?…}``: Ohne den Wert startet Compose die Anwendung nicht."""
        umgebung_mandari = compose._dienste()["mandari"]["environment"]
        verlangt = {name for name, wert in umgebung_mandari.items() if f"${{{name}:?" in str(wert)}
        assert verlangt, "kein ${NAME:?…} mehr in docker-compose.yml? Dann diesen Test anpassen"
        assert verlangt <= set(umgebung.PFLICHT), sorted(verlangt - set(umgebung.PFLICHT))

    def test_platzhalter_der_pflichtwerte_aus_dem_beispiel_werden_erkannt(self) -> None:
        zeilen = compose.BEISPIEL.read_text(encoding="utf-8").splitlines()
        beispielwerte = dict(zeile.split("=", 1) for zeile in zeilen if re.match(r"[A-Z][A-Z0-9_]*=", zeile))
        for name in ("SECRET_KEY", "ENCRYPTION_MASTER_KEY"):
            assert umgebung.ist_platzhalter(beispielwerte[name]), name
        assert not umgebung.ist_platzhalter(HAUPTSCHLUESSEL)

    def test_abweichung_ist_nur_erlaubt_wo_compose_den_wert_selbst_setzt(self) -> None:
        """
        ``test_compose_ueberschreibt_keine_variable_aus_dem_beispiel`` lässt genau zwei Ausnahmen zu: den Block
        „Entwicklung“ und eigene Werte je Dienst. Nur dort darf der Prozess von der ``.env`` abweichen.
        """
        beispiel = compose._beispiel()
        entwicklung = {name for name, bloecke in beispiel.items() if all("[Entwicklung]" in b for b in bloecke)}
        je_dienst = {name for dienst, name in compose.EIGENE_WERTE_JE_DIENST if dienst in compose.MIT_ENV_FILE}
        assert entwicklung | je_dienst == umgebung.SETZT_COMPOSE_SELBST


# ---------------------------------------------------------------------------------------------- Datenbank und Cache


@pytest.mark.django_db
def test_datenbank_erreichbar() -> None:
    befund = dienste.pruefe_datenbank(Kontext())

    assert befund.status is Status.OK
    assert befund.meldung == f"{connection.display_name} erreichbar"


def test_datenbank_nicht_erreichbar_ohne_text_der_ausnahme(monkeypatch: pytest.MonkeyPatch) -> None:
    class Kaputt:
        display_name = "PostgreSQL"

        def cursor(self) -> Any:
            raise ConnectionError(f"connection to db.invalid failed, password={KURZ}")

    monkeypatch.setattr(dienste, "connection", Kaputt())

    befund = dienste.pruefe_datenbank(Kontext())

    assert (befund.status, befund.meldung) == (Status.FEHLER, "nicht erreichbar (ConnectionError)")
    assert "docker compose ps" in befund.hinweis


class _Cache:
    """Attrappe: nur lesen ist erlaubt (ein ``set`` gäbe es hier nicht)."""

    def __init__(self, fehler: Exception | None = None) -> None:
        self.fehler = fehler
        self.gelesen: list[str] = []

    def get(self, schluessel: str) -> None:
        self.gelesen.append(schluessel)
        if self.fehler:
            raise self.fehler


def test_cache_ohne_redis_ist_uebersprungen(settings: Any) -> None:
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

    befund = dienste.pruefe_cache(Kontext())

    assert befund.status is Status.UEBERSPRUNGEN
    assert befund.meldung == "kein Redis eingestellt (LocMemCache, Speicher je Prozess)"


def test_cache_wird_nur_gelesen(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    attrappe = _Cache()
    monkeypatch.setattr(dienste, "cache", attrappe)
    monkeypatch.setitem(settings.CACHES["default"], "BACKEND", "django.core.cache.backends.redis.RedisCache")

    befund = dienste.pruefe_cache(Kontext())

    assert (befund.status, befund.meldung) == (Status.OK, "RedisCache erreichbar")
    assert attrappe.gelesen == [dienste.CACHE_PROBE]


def test_cache_nicht_erreichbar_ohne_adresse(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dienste, "cache", _Cache(ConnectionError(f"Error connecting to redis://:{KURZ}@redis:6379")))
    monkeypatch.setitem(settings.CACHES["default"], "BACKEND", "django.core.cache.backends.redis.RedisCache")

    befund = dienste.pruefe_cache(Kontext())

    assert (befund.status, befund.meldung) == (Status.FEHLER, "nicht erreichbar (ConnectionError)")


# ---------------------------------------------------------------------------------------------- Mailversand


class _Mailserver:
    """Attrappe für ``smtplib.SMTP``: merkt sich jeden Aufruf; versenden gibt es nicht."""

    aufrufe: list[tuple[Any, ...]] = []
    fehler_bei: dict[str, Exception] = {}

    def __init__(self, host: str, port: int, **optionen: Any) -> None:
        self._schritt("verbinden", host, port, optionen.get("timeout"))

    def _schritt(self, name: str, *angaben: Any) -> None:
        type(self).aufrufe.append((name, *angaben))
        if name in self.fehler_bei:
            raise self.fehler_bei[name]

    def starttls(self, **optionen: Any) -> None:
        self._schritt("starttls")

    def login(self, benutzer: str, passwort: str) -> None:
        self._schritt("login", benutzer, passwort)

    def quit(self) -> None:
        self._schritt("quit")

    def close(self) -> None:
        self._schritt("close")

    def sendmail(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - darf nie laufen
        raise AssertionError("doctor versendet keine Mail")

    send_message = sendmail


@pytest.fixture
def mailserver(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Mailserver]]:
    monkeypatch.setattr(_Mailserver, "aufrufe", [])
    monkeypatch.setattr(_Mailserver, "fehler_bei", {})
    monkeypatch.setattr(smtplib, "SMTP", _Mailserver)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Mailserver)
    yield _Mailserver


def _systemeinstellungen(**felder: Any) -> SiteSettings:
    site = SiteSettings(
        pk=1,
        **{"email_host": "smtp.mail.invalid", "email_port": 587, "email_host_user": SMTP_BENUTZER, **felder},
    )
    site.set_email_host_password(SMTP_PASSWORT)
    site.save()
    return site


@pytest.mark.django_db
def test_smtp_ohne_mailserver_ist_uebersprungen_und_legt_nichts_an(settings: Any) -> None:
    settings.MAIL_BACKEND = "apps.common.email_backend.SiteSettingsEmailBackend"
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "host": ""}

    befund = dienste.pruefe_smtp(Kontext())

    assert befund.status is Status.UEBERSPRUNGEN
    assert befund.meldung == "kein Mailserver eingestellt: mandari verschickt keine Mails"
    assert not SiteSettings.objects.exists(), "die Prüfung legt die Zeile der Systemeinstellungen nicht an"


@pytest.mark.django_db
def test_smtp_ohne_smtp_versandweg_ist_uebersprungen(settings: Any) -> None:
    settings.MAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

    assert dienste.pruefe_smtp(Kontext()).status is Status.UEBERSPRUNGEN

    settings.DEMO_INSTANCE = True
    assert dienste.pruefe_smtp(Kontext()).meldung == "Demo-Instanz: Es verlässt keine Mail das System"


@pytest.mark.django_db
def test_smtp_verbindet_meldet_sich_an_und_sendet_nichts(mailserver: type[_Mailserver]) -> None:
    _systemeinstellungen()

    befund = dienste.pruefe_smtp(Kontext(zeitgrenze=7))

    assert befund.status is Status.OK
    assert befund.meldung == (
        "smtp.mail.invalid:587 (STARTTLS), Zugang aus: Systemeinstellungen: Anmeldung angenommen, keine Mail versendet"
    )
    assert mailserver.aufrufe == [
        ("verbinden", "smtp.mail.invalid", 587, 7),  # die Zeitgrenze des Laufs gilt für die Verbindung
        ("starttls",),
        ("login", SMTP_BENUTZER, SMTP_PASSWORT),
        ("quit",),
    ]


@pytest.mark.django_db
def test_smtp_zugang_aus_der_umgebung(settings: Any, mailserver: type[_Mailserver]) -> None:
    settings.MAIL_BACKEND = "apps.common.email_backend.SiteSettingsEmailBackend"
    settings.SMTP_FALLBACK = {
        "host": "relay.mail.invalid",
        "port": 465,
        "username": "",
        "password": "",
        "use_tls": False,
        "use_ssl": True,
        "timeout": 3,
    }

    befund = dienste.pruefe_smtp(Kontext(zeitgrenze=10))

    assert befund.status is Status.OK
    assert befund.meldung == (
        "relay.mail.invalid:465 (SSL), Zugang aus: Umgebung (EMAIL_*): erreichbar, ohne Anmeldung "
        "(kein Benutzername eingestellt)"
    )
    assert mailserver.aufrufe == [("verbinden", "relay.mail.invalid", 465, 3), ("quit",)]
    assert not SiteSettings.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("schritt", "ausnahme", "meldung"),
    [
        (
            "login",
            smtplib.SMTPAuthenticationError(535, f"5.7.8 {SMTP_BENUTZER} {SMTP_PASSWORT} rejected".encode()),
            "smtp.mail.invalid:587: Anmeldung abgelehnt (SMTP-Code 535)",
        ),
        (
            "starttls",
            smtplib.SMTPNotSupportedError(f"STARTTLS extension not supported {KURZ}"),
            "smtp.mail.invalid:587: Der Server bietet STARTTLS oder die Anmeldung nicht an",
        ),
        (
            "verbinden",
            TimeoutError(f"timed out {KURZ}"),
            "smtp.mail.invalid:587: keine Antwort innerhalb der Zeitgrenze",
        ),
        (
            "verbinden",
            ConnectionRefusedError(f"[Errno 111] {KURZ}"),
            "smtp.mail.invalid:587: Verbindung gescheitert (ConnectionRefusedError)",
        ),
        (
            "verbinden",
            smtplib.SMTPServerDisconnected(f"Connection unexpectedly closed {KURZ}"),
            "smtp.mail.invalid:587: SMTP-Fehler (SMTPServerDisconnected)",
        ),
    ],
)
def test_smtp_fehler_haben_feste_texte(
    mailserver: type[_Mailserver], schritt: str, ausnahme: Exception, meldung: str
) -> None:
    _systemeinstellungen()
    mailserver.fehler_bei[schritt] = ausnahme

    befund = dienste.pruefe_smtp(Kontext())

    assert (befund.status, befund.meldung) == (Status.FEHLER, meldung)
    _ohne_geheimnis(befund.meldung + befund.hinweis)
    assert KURZ not in befund.meldung + befund.hinweis


@pytest.mark.django_db
def test_smtp_warnt_bei_halbem_zugang_und_ohne_verschluesselung(mailserver: type[_Mailserver]) -> None:
    site = _systemeinstellungen(email_use_tls=False)

    unverschluesselt = dienste.pruefe_smtp(Kontext())
    assert unverschluesselt.status is Status.WARNUNG
    assert "unverschlüsselt" in unverschluesselt.meldung

    site.email_use_tls = True
    site.email_host_user = ""
    site.save()
    halb = dienste.pruefe_smtp(Kontext())
    assert halb.status is Status.WARNUNG
    assert "ohne Anmeldung, weil Benutzername oder Passwort fehlt" in halb.meldung

    site.email_use_ssl = True
    site.save()
    beides = dienste.pruefe_smtp(Kontext())
    assert (beides.status, beides.meldung) == (Status.FEHLER, "STARTTLS und SSL sind beide eingeschaltet")


# ---------------------------------------------------------------------------------------------- Objektspeicher

ENDPUNKT = "https://s3.objekte.example"
BUCKET = "mandari-doctor"


@pytest.fixture
def objektspeicher(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPUNKT)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT = ENDPUNKT
    settings.OBJ_BUCKET = BUCKET
    settings.OBJ_KEY = "test"
    settings.OBJ_SECRET = "test"
    settings.OBJ_REGION = ""
    object_storage._client.cache_clear()
    with moto.mock_aws():
        object_storage.client().create_bucket(Bucket=BUCKET)
        yield object_storage.client()
    object_storage._client.cache_clear()


def test_objektspeicher_aus_ist_uebersprungen(settings: Any) -> None:
    settings.OBJ_ENABLED = False

    befund = ablage.pruefe_objektspeicher(Kontext())

    assert (befund.status, befund.meldung) == (Status.UEBERSPRUNGEN, "nicht eingeschaltet (OBJ_ENABLED)")


def test_objektspeicher_eingeschaltet_aber_unvollstaendig_ist_ein_fehler(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Fall aus dem Issue: S3 eingeschaltet, ein Zugangswert fehlt, der Objektspeicher ist stillschweigend aus."""
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT = ENDPUNKT
    settings.OBJ_BUCKET = BUCKET
    settings.OBJ_KEY = GEHEIM["OBJ_KEY"]
    settings.OBJ_SECRET = ""
    monkeypatch.setattr(object_storage, "probe", lambda zeitgrenze: pytest.fail("ohne Zugang keine Anfrage"))

    befund = ablage.pruefe_objektspeicher(Kontext())

    assert befund.status is Status.FEHLER
    assert befund.meldung == "eingeschaltet, aber OBJ_SECRET fehlt: Der Objektspeicher bleibt aus"
    _ohne_geheimnis(befund.meldung + befund.hinweis)


def test_objektspeicher_erreichbar_und_nur_gelesen(objektspeicher: Any) -> None:
    befund = ablage.pruefe_objektspeicher(Kontext(zeitgrenze=4))

    assert (befund.status, befund.meldung) == (Status.OK, "Bucket erreichbar (nur gelesen)")
    assert objektspeicher.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0, "die Probe legt kein Objekt an"


def test_objektspeicher_bucket_fehlt(objektspeicher: Any, settings: Any) -> None:
    settings.OBJ_BUCKET = "gibt-es-nicht"

    befund = ablage.pruefe_objektspeicher(Kontext())

    assert (befund.status, befund.meldung) == (Status.FEHLER, "Bucket nicht gefunden (Code 404)")


def test_objektspeicher_probe_ohne_wiederholung_und_mit_zeitgrenze(
    objektspeicher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gebaut: list[dict[str, Any]] = []
    echt = object_storage._new_client

    def merken(*args: Any, **optionen: Any) -> Any:
        gebaut.append({"timeout": args[5], **optionen})
        return echt(*args, **optionen)

    monkeypatch.setattr(object_storage, "_new_client", merken)

    object_storage.probe(4)

    assert gebaut == [{"timeout": 4, "connect_timeout": 4, "attempts": 1}]


@pytest.mark.parametrize(
    ("ausnahme", "meldung"),
    [
        (type("ClientError", (Exception,), {"response": {"Error": {"Code": "403"}}})(), "Zugang abgelehnt (Code 403)"),
        (
            type("ClientError", (Exception,), {"response": {"Error": {"Code": "SignatureDoesNotMatch"}}})(),
            "Zugang abgelehnt (Code SignatureDoesNotMatch)",
        ),
        (
            type("ClientError", (Exception,), {"response": {"Error": {"Code": f"x {KURZ} y"}}})(),
            "nicht erreichbar (ClientError)",
        ),
        (ConnectionError(f"https://{KURZ}@objekte.example"), "nicht erreichbar (ConnectionError)"),
    ],
)
def test_objektspeicher_fehler_haben_feste_texte(
    settings: Any, monkeypatch: pytest.MonkeyPatch, ausnahme: Exception, meldung: str
) -> None:
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT, settings.OBJ_BUCKET = ENDPUNKT, BUCKET
    settings.OBJ_KEY, settings.OBJ_SECRET = GEHEIM["OBJ_KEY"], GEHEIM["OBJ_SECRET"]

    def kaputt(zeitgrenze: float) -> None:
        raise ausnahme

    monkeypatch.setattr(object_storage, "probe", kaputt)

    befund = ablage.pruefe_objektspeicher(Kontext())

    assert (befund.status, befund.meldung) == (Status.FEHLER, meldung)
    _ohne_geheimnis(befund.meldung + befund.hinweis)


# ---------------------------------------------------------------------------------------------- Aufträge und Worker


@pytest.mark.django_db
def test_aufgaben_sofort_und_im_journal(settings: Any) -> None:
    settings.MAIL_QUEUE = []
    sofort = dienste.pruefe_aufgaben(Kontext())
    assert (sofort.status, sofort.meldung) == (Status.OK, "ImmediateBackend: Aufträge laufen sofort in der Anfrage")

    settings.MAIL_QUEUE = ["work.*"]
    wirkungslos = dienste.pruefe_aufgaben(Kontext())
    assert wirkungslos.status is Status.WARNUNG
    assert wirkungslos.meldung.startswith("MAIL_QUEUE ist gesetzt, wirkt aber nur mit TASKS_BACKEND=journal")

    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    journal = dienste.pruefe_aufgaben(Kontext())
    assert (journal.status, journal.meldung) == (Status.OK, "journal: Aufträge laufen im Worker")


@pytest.mark.django_db
def test_aufgaben_backend_nicht_ladbar(settings: Any) -> None:
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "gibt.es.nicht.Backend"}}

    befund = dienste.pruefe_aufgaben(Kontext())

    assert befund.status is Status.FEHLER
    assert befund.meldung.startswith("Backend nicht ladbar (")


@pytest.mark.django_db
def test_worker_nicht_verlangt_fehlt_oder_laeuft(settings: Any) -> None:
    settings.EVENTS_WORKER_REQUIRED = "false"
    assert dienste.pruefe_worker(Kontext()).status is Status.UEBERSPRUNGEN

    settings.EVENTS_WORKER_REQUIRED = "true"
    fehlt = dienste.pruefe_worker(Kontext())
    assert fehlt.status is Status.FEHLER
    assert fehlt.meldung == "kein Worker für dispatch, scheduler, sequencer, tasks"
    assert "docker compose ps worker" in fehlt.hinweis

    presence.announce("rechner:1:test", sorted(presence.ALL_ROLES), [])
    laeuft = dienste.pruefe_worker(Kontext())
    assert (laeuft.status, laeuft.meldung) == (Status.OK, "1 Worker, Rollen: dispatch, scheduler, sequencer, tasks")


@pytest.mark.django_db
def test_keine_pruefung_schreibt_in_die_datenbank(settings: Any, mailserver: type[_Mailserver]) -> None:
    settings.EVENTS_WORKER_REQUIRED = "true"
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.OBJ_ENABLED = False
    _systemeinstellungen()

    with CaptureQueriesContext(connection) as abfragen:
        for eintrag in registrierte():
            eintrag.funktion(Kontext(env_datei={"DOMAIN": "mandari.example.org"}))

    befehle = [abfrage["sql"].lstrip().split(None, 1)[0].upper() for abfrage in abfragen.captured_queries]
    assert befehle, "die Prüfungen fragen die Datenbank"
    assert set(befehle) == {"SELECT"}, befehle


# ---------------------------------------------------------------------------------------------- Befehl


def _doctor(*argumente: str) -> tuple[str, str, str]:
    """Ausgabe, Fehlerausgabe und Text des Abbruchs (leer bei Exit-Code 0)."""
    ausgabe, fehlerausgabe = io.StringIO(), io.StringIO()
    try:
        call_command("doctor", *argumente, stdout=ausgabe, stderr=fehlerausgabe)
    except CommandError as abbruch:
        assert abbruch.returncode == 1
        return ausgabe.getvalue(), fehlerausgabe.getvalue(), str(abbruch)
    return ausgabe.getvalue(), fehlerausgabe.getvalue(), ""


@pytest.fixture
def nur_feste_pruefungen(monkeypatch: pytest.MonkeyPatch) -> list[Pruefung]:
    """Eigenes Register für den Test: Der Befehl sieht genau diese Prüfungen."""
    eintraege = [
        _eintrag("gut", lambda k: ok("läuft"), 1),
        _eintrag("aus", lambda k: kern.uebersprungen("nicht eingestellt", "bei Bedarf einschalten"), 2),
    ]
    monkeypatch.setattr("apps.common.management.commands.doctor.registrierte", lambda: list(eintraege))
    return eintraege


def test_befehl_gibt_eine_lesbare_liste_aus_und_endet_mit_0(nur_feste_pruefungen: list[Pruefung]) -> None:
    ausgabe, _, abbruch = _doctor("--no-color")

    assert abbruch == "", "ohne Fehler Exit-Code 0; übersprungen ist kein Fehler"
    assert ausgabe.splitlines() == [
        "OK            Gut  läuft",
        "ÜBERSPRUNGEN  Aus  nicht eingestellt",
        "              Hinweis: bei Bedarf einschalten",
        "",
        "Ergebnis: 1 ok, 0 Warnungen, 0 Fehler, 1 übersprungen",
    ]


def test_befehl_endet_bei_fehlern_mit_1_und_bei_warnungen_mit_0(nur_feste_pruefungen: list[Pruefung]) -> None:
    nur_feste_pruefungen.append(_eintrag("mahnt", lambda k: kern.warnung("fast"), 3))
    assert _doctor()[2] == "", "eine Warnung ändert den Exit-Code nicht"

    nur_feste_pruefungen.append(_eintrag("kaputt", lambda k: fehler("geht nicht", "reparieren"), 4))
    ausgabe, _, abbruch = _doctor("--no-color")

    assert abbruch == "doctor: 1 Prüfung mit Fehler."
    assert "FEHLER        Kaputt  geht nicht" in ausgabe and "Hinweis: reparieren" in ausgabe
    assert ausgabe.splitlines()[-1] == "Ergebnis: 1 ok, 1 Warnung, 1 Fehler, 1 übersprungen"


def test_befehl_als_json(nur_feste_pruefungen: list[Pruefung]) -> None:
    nur_feste_pruefungen.append(_eintrag("kaputt", lambda k: fehler("geht nicht", "reparieren"), 4))

    ausgabe, _, abbruch = _doctor("--json")

    daten = json.loads(ausgabe)
    assert abbruch == "doctor: 1 Prüfung mit Fehler.", "auch mit --json Exit-Code 1"
    assert daten["status"] == "fehler"
    assert daten["zusammenfassung"] == {"ok": 1, "warnung": 0, "fehler": 1, "uebersprungen": 1}
    assert [(p["name"], p["status"]) for p in daten["pruefungen"]] == [
        ("gut", "ok"),
        ("aus", "uebersprungen"),
        ("kaputt", "fehler"),
    ]
    assert set(daten["pruefungen"][2]) == {"name", "titel", "status", "meldung", "hinweis", "einzelheiten", "dauer_ms"}


def test_befehl_nur_einzelne_pruefungen_und_ungueltige_angaben(nur_feste_pruefungen: list[Pruefung]) -> None:
    daten = json.loads(_doctor("--json", "--nur", "aus")[0])
    assert [p["name"] for p in daten["pruefungen"]] == ["aus"]

    with pytest.raises(CommandError) as unbekannt:
        call_command("doctor", "--nur", f"{KURZ}-tippfehler")
    assert str(unbekannt.value) == "--nur kennt nur: gut, aus", "feste Meldung ohne Echo der Eingabe"

    for wert in ("0", "-1", "nan", "inf"):
        with pytest.raises(CommandError, match="--zeitgrenze muss eine Zahl größer als 0 sein"):
            call_command("doctor", "--zeitgrenze", wert)


def test_befehl_gibt_die_zeitgrenze_weiter(nur_feste_pruefungen: list[Pruefung]) -> None:
    ende = threading.Event()
    nur_feste_pruefungen.append(_eintrag("haengt", lambda k: ok("zu spät") if ende.wait(30) else ok("nie"), 5))

    try:
        ausgabe, _, abbruch = _doctor("--no-color", "--zeitgrenze", "0.2")
    finally:
        ende.set()

    assert abbruch == "doctor: 1 Prüfung mit Fehler."
    assert "FEHLER        Haengt  keine Antwort innerhalb von 0.2 s" in ausgabe


def test_befehl_liest_die_env_datei_aus_datei_und_standardeingabe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FEHLT_IM_PROZESS", raising=False)
    inhalt = "FEHLT_IM_PROZESS=GEHEIM-wert-4711\n"
    datei = tmp_path / "umgebung.env"
    datei.write_text(inhalt, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(inhalt))

    for quelle in (str(datei), "-"):
        ausgabe, fehlerausgabe, abbruch = _doctor("--no-color", "--nur", "umgebung-datei", "--env-datei", quelle)
        assert "kommt im Prozess nicht an: FEHLT_IM_PROZESS" in ausgabe, quelle
        assert abbruch == "doctor: 1 Prüfung mit Fehler."
        _ohne_geheimnis(ausgabe + fehlerausgabe + abbruch)

    with pytest.raises(CommandError) as fehlt:
        call_command("doctor", "--env-datei", str(tmp_path / f"{KURZ}.env"))
    assert str(fehlt.value) == "--env-datei ist nicht lesbar (FileNotFoundError)."


@pytest.fixture
def alles_voller_geheimnisse(
    settings: Any, monkeypatch: pytest.MonkeyPatch, mailserver: type[_Mailserver], tmp_path: Path
) -> Path:
    """
    Jede Stelle, aus der ein Geheimnis in die Ausgabe gelangen könnte: Umgebung, Einstellungen, ``--env-datei``,
    Systemeinstellungen in der Datenbank und die Fehlertexte von Mailserver, Cache und Objektspeicher.
    """
    for name, wert in GEHEIM.items():
        monkeypatch.setenv(name, wert)
    settings.DEBUG = False
    settings.SECRET_KEY = GEHEIM["SECRET_KEY"]
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "password": GEHEIM["EMAIL_HOST_PASSWORD"]}
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT, settings.OBJ_BUCKET = ENDPUNKT, BUCKET
    settings.OBJ_KEY, settings.OBJ_SECRET = GEHEIM["OBJ_KEY"], GEHEIM["OBJ_SECRET"]
    settings.EVENTS_WORKER_REQUIRED = "true"
    settings.MAIL_QUEUE = []
    monkeypatch.setitem(settings.CACHES["default"], "BACKEND", "django.core.cache.backends.redis.RedisCache")
    monkeypatch.setattr(dienste, "cache", _Cache(ConnectionError(f"Error connecting to {GEHEIM['REDIS_URL']}")))

    def objektspeicher_kaputt(zeitgrenze: float) -> None:
        raise ConnectionError(f"SignatureDoesNotMatch for {GEHEIM['OBJ_KEY']}:{GEHEIM['OBJ_SECRET']}")

    monkeypatch.setattr(object_storage, "probe", objektspeicher_kaputt)
    _systemeinstellungen()
    mailserver.fehler_bei["login"] = smtplib.SMTPAuthenticationError(
        535, f"5.7.8 {SMTP_BENUTZER}:{SMTP_PASSWORT} rejected".encode()
    )
    datei = tmp_path / "umgebung.env"
    zeilen = [f"{name}='{wert}-aus-der-datei'" for name, wert in GEHEIM.items()]
    datei.write_text("\n".join([*zeilen, "GEHEIM-verrutschte-zeile-4711", ""]), encoding="utf-8")
    return datei


# Die Prüfungen laufen in eigenen Threads mit eigener Datenbankverbindung: Sie sehen nur, was festgeschrieben ist
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("zweites_netz", [True, False], ids=["mit-zweitem-netz", "nur-feste-texte"])
def test_keine_ausgabe_nennt_ein_geheimnis(
    alles_voller_geheimnisse: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    zweites_netz: bool,
) -> None:
    """Pflichttest aus Issue #964: weder Liste noch JSON noch eine Fehlermeldung nennen Passwörter, Schlüssel, Tokens."""
    if not zweites_netz:
        # Schon die Meldungen selbst sind sauber; das Ersetzen bekannter Werte ist nur das zweite Netz
        monkeypatch.setattr(kern, "schwaerze", lambda text, geheimnisse: text)
    caplog.set_level(logging.DEBUG)
    gesamt: list[str] = []
    stufen: dict[str, str] = {}

    for format_ in ((), ("--json",)):
        ausgabe, fehlerausgabe, abbruch = _doctor("--no-color", "--env-datei", str(alles_voller_geheimnisse), *format_)
        gesamt += [ausgabe, fehlerausgabe, abbruch]
        if format_:
            stufen = {p["name"]: p["status"] for p in json.loads(ausgabe)["pruefungen"]}

    # Die Prüfungen sind wirklich in ihre Fehlerwege gelaufen, nicht bloß übersprungen
    assert stufen == {
        "umgebung": "fehler",
        "umgebung-datei": "warnung",
        "datenbank": "ok",
        "cache": "fehler",
        "smtp": "fehler",
        "objektspeicher": "fehler",
        "aufgaben": "ok",
        "worker": "fehler",
    }
    aufgezeichnet = capsys.readouterr()
    text = "\n".join([*gesamt, caplog.text, aufgezeichnet.out, aufgezeichnet.err])
    assert "Anmeldung abgelehnt (SMTP-Code 535)" in text and "kommt im Prozess nicht an" not in text
    assert "anderer Wert als in der Datei: " in text
    _ohne_geheimnis(text)
    assert SMTP_BENUTZER not in text and SMTP_PASSWORT not in text


# ---------------------------------------------------------------------------------------------- Doku


def test_deployment_md_nennt_den_aufruf_wie_es_ihn_gibt() -> None:
    from apps.common.management.commands.doctor import Command

    text = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
    abschnitt = text.split("### Konfiguration prüfen: `manage.py doctor`", 1)[1].split("\n## ", 1)[0]
    parser = Command().create_parser("manage.py", "doctor")
    bekannt = {option for aktion in parser._actions for option in aktion.option_strings}

    genannt = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", " ".join(re.findall(r"manage\.py doctor[^\n`]*", abschnitt))))
    assert {"--json", "--env-datei", "--nur", "--zeitgrenze"} <= genannt
    assert genannt <= bekannt, sorted(genannt - bekannt)
    namen = {eintrag.name for eintrag in registrierte()}
    assert set(re.findall(r"^\| `([a-z-]+)` \|", abschnitt, flags=re.MULTILINE)) == namen
