# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lastszenarien für mandari (Issue #228) — Locust.

Läuft gegen eine Instanz mit den Daten aus ``manage.py generate_load_data``;
die Konten und Slugs ergeben sich aus dem Profil (``LOADTEST_PROFILE``, Vorgabe
``klein``). Befehle und Auswertung stehen in ``loadtest/README.md``.

Szenarien (Gewichtung in Klammern):

- ``PortalBesucher`` (5): Bürgerportal — Startseite, Sitzungen, Vorgänge, Detailseiten, Suche
- ``Sitzungsdienst`` (3): Verwaltungs-RIS — Sitzungsliste, Sitzungsdetail, Sitzungsmappe (PDF),
  Vorlagen, übergreifende Suche
- ``Fraktionsmitglied`` (2): Work — Dashboard, Dokumentliste, Editor-Seite
- ``OParlAbnehmer`` (1): offene Schnittstelle — Listen beider Ausgaben (Aggregator und
  Session-Schnittstelle) seitenweise lesen, Einzelobjekte abrufen und Listen mit ``If-None-Match``
  erneut abfragen (``304 Not Modified``, wenn sich nichts geändert hat)
- ``LiveAbstimmung`` (genau ein Nutzer): Protokollführung der laufenden Ratssitzung —
  Abstimmungsseite aufrufen und Einzelstimmen aller Anwesenden erfassen (das ist der HTTP-Anteil
  der digitalen Abstimmung; eine WebSocket-Verteilung an Endgeräte gibt es im Sitzungsdienst nicht)
- ``Sitzungsgeldlauf`` (genau ein Nutzer): Abrechnungslauf des Sitzungsgelds, Monat für Monat
  rückwärts durch die Historie, während die übrigen Szenarien laufen

Kennzahlen: Locust schreibt je Endpunkt Anfragen, Fehler, Perzentile und Durchsatz nach
``<--csv>_stats.csv``. Dieses Modul schreibt zusätzlich ``<--csv>_szenarien.json`` mit den
Perzentilen je Szenario aus allen Einzelwerten, den Antworten der bedingten Anfragen und – bei
Stufenlast – den Kennzahlen je Stufe; ``loadtest/auswerten.py`` macht daraus den Bericht und prüft
die Budgets.

Umgebungsvariablen: ``LOADTEST_PROFILE`` (klein|mittel|gross), ``LOADTEST_PASSWORD``,
``LOADTEST_BODY_ID`` (Kommune im Portal, sonst über ``/insight/k/<slug>/``),
``LOADTEST_TLS_PRUEFEN=0`` (Zertifikat nicht prüfen, z. B. Caddy mit ``tls internal`` in der CI),
``LOADTEST_STUFEN``/``LOADTEST_STUFENDAUER`` (Stufenlast zur Kapazitätsmessung statt fester Nutzerzahl).
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import urllib3
from auswerten import EINSCHWINGEN_S, stufen_lesen, szenario_kennzahlen
from locust import HttpUser, LoadTestShape, between, events, task

PROFIL = os.environ.get("LOADTEST_PROFILE", "klein")
PASSWORT = os.environ.get("LOADTEST_PASSWORD", "Lasttest-2026!")
TLS_PRUEFEN = os.environ.get("LOADTEST_TLS_PRUEFEN", "1").lower() not in ("0", "false", "nein", "no")
DOMAENE = "lasttest.mandari.invalid"
SESSION_SLUG = f"last-{PROFIL}-stadt"
ORG_SLUG = f"last-{PROFIL}-stadt-fraktion"
#: Konten je Rolle, siehe PROFILE in apps/session/management/commands/generate_load_data.py
KONTEN = {"klein": (20, 8), "mittel": (100, 15), "gross": (400, 25)}
SACHBEARBEITUNG, FRAKTION = KONTEN.get(PROFIL, (20, 8))
#: Aktive Kommune im Portal; leer = Einstieg über /insight/k/<slug>/ (Slug der Lasttest-Kommune)
BODY_ID = os.environ.get("LOADTEST_BODY_ID", "")
SUCHBEGRIFFE = ["Radweg", "Haushalt", "Spielplatz", "Schule", "Bebauungsplan", "Kita", "Ladesäulen"]
#: Listen beider OParl-Ausgaben (Pflichtlisten der Spezifikation)
OPARL_LISTEN = ("papers", "meetings", "organizations", "people")
#: Historie des Generators in Monaten (zwei Jahre); der Sitzungsgeldlauf geht sie rückwärts durch
MONATE_HISTORIE = 24
#: Namenszusatz bedingter Anfragen (If-None-Match) in der Statistik
BEDINGT = " [bedingt]"
#: Stufenlast zur Kapazitätsmessung, z. B. ``LOADTEST_STUFEN=25,50,100``: je Stufe so viele Nutzer für
#: ``LOADTEST_STUFENDAUER`` Sekunden (Klasse ``Stufenlast`` unten); leer = Nutzerzahl von der Kommandozeile
STUFEN = stufen_lesen(os.environ.get("LOADTEST_STUFEN", ""))
STUFENDAUER = int(os.environ.get("LOADTEST_STUFENDAUER", "120"))
#: Anlauf je Sekunde beim Wechsel auf die nächste Stufe
STUFEN_ANLAUF = 5

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_zaehler_sachbearbeitung = itertools.count()
_zaehler_fraktion = itertools.count()

if not TLS_PRUEFEN:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _ids(html: str, praefix: str) -> list[str]:
    """UUIDs aus Links wie ``/insight/vorgaenge/<uuid>/`` einsammeln (Detailseiten der Listen)."""
    return list(dict.fromkeys(re.findall(rf"{re.escape(praefix)}({UUID})/", html)))


def _pfad(url: str) -> str:
    """Absolute Adresse der Schnittstelle (``id``, ``links.next``) auf Pfad und Abfrage kürzen."""
    teile = urlsplit(url)
    return f"{teile.path}?{teile.query}" if teile.query else teile.path


def _statistikname(pfad: str) -> str:
    """Name in der Statistik: ohne Abfrage, Kennungen und Mandanten-Slug (eine Zeile je Endpunkt)."""
    name = re.sub(UUID, "<id>", pfad.split("?", 1)[0])
    return name.replace(f"/session/{SESSION_SLUG}/", "/session/")


def _monat(heute: date, zurueck: int) -> tuple[date, date]:
    """Erster und letzter Tag des Monats, der ``zurueck`` Monate vor dem laufenden liegt."""
    index = heute.year * 12 + heute.month - 1 - zurueck
    beginn = date(index // 12, index % 12 + 1, 1)
    folgemonat = date((index + 1) // 12, (index + 1) % 12 + 1, 1)
    return beginn, folgemonat - timedelta(days=1)


# =============================================================================
# Messung je Szenario (zusätzlich zur Statistik je Endpunkt von Locust)
# =============================================================================

_zeiten: dict[str, list[float]] = defaultdict(list)
_fehler: Counter[str] = Counter()
_bedingt: dict[str, Counter[str]] = defaultdict(Counter)
#: Stufenlast: Zeiten und Fehler je Stufe (ohne die Einschwingzeit nach jedem Wechsel)
_stufen_zeiten: dict[str, list[float]] = defaultdict(list)
_stufen_fehler: Counter[str] = Counter()
_beginn = [time.monotonic()]


@events.test_start.add_listener
def _messung_starten(**_kwargs: Any) -> None:
    for sammlung in (_zeiten, _fehler, _bedingt, _stufen_zeiten, _stufen_fehler):
        sammlung.clear()
    _beginn[0] = time.monotonic()


def _stufe() -> str | None:
    """Laufende Stufe der Stufenlast als Schlüssel – oder None (keine Stufenlast, Einschwingzeit)."""
    if not STUFEN:
        return None
    seit_beginn = time.monotonic() - _beginn[0]
    index = int(seit_beginn // STUFENDAUER)
    if index >= len(STUFEN) or seit_beginn - index * STUFENDAUER < EINSCHWINGEN_S:
        return None
    return str(index)


@events.request.add_listener
def _messen(
    name: str,
    response_time: float,
    exception: BaseException | None = None,
    context: dict[str, Any] | None = None,
    response: Any = None,
    **_kwargs: Any,
) -> None:
    szenario = (context or {}).get("szenario", "Sonstige")
    _zeiten[szenario].append(float(response_time))
    if exception is not None:
        _fehler[szenario] += 1
    if name.endswith(BEDINGT):
        _bedingt[name]["anfragen"] += 1
        if getattr(response, "status_code", None) == 304:
            _bedingt[name]["nicht_geaendert"] += 1
    stufe = _stufe()
    if stufe is not None:
        _stufen_zeiten[stufe].append(float(response_time))
        if exception is not None:
            _stufen_fehler[stufe] += 1


@events.test_stop.add_listener
def _messung_schreiben(environment: Any, **_kwargs: Any) -> None:
    praefix = getattr(environment.parsed_options, "csv_prefix", None) if environment.parsed_options else None
    if not praefix:
        return
    dauer = time.monotonic() - _beginn[0]
    daten = {
        "profil": PROFIL,
        "dauer_s": round(dauer, 1),
        "szenarien": szenario_kennzahlen(_zeiten, _fehler, dauer),
        "bedingt": {name: dict(werte) for name, werte in sorted(_bedingt.items())},
    }
    if STUFEN:
        messdauer = STUFENDAUER - EINSCHWINGEN_S
        kennzahlen = szenario_kennzahlen(_stufen_zeiten, _stufen_fehler, messdauer)
        daten["stufen"] = {
            "dauer_s": STUFENDAUER,
            "einschwingen_s": EINSCHWINGEN_S,
            "liste": [{"nutzer": nutzer, **kennzahlen.get(str(i), {})} for i, nutzer in enumerate(STUFEN)],
        }
    Path(f"{praefix}_szenarien.json").write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")


if STUFEN:

    class Stufenlast(LoadTestShape):
        """
        Kapazitätsmessung: Nutzerzahl stufenweise erhöhen (``LOADTEST_STUFEN``), jede Stufe
        ``LOADTEST_STUFENDAUER`` Sekunden. Der Bericht nennt je Stufe Durchsatz, p95, Fehlerquote und
        CPU – und die höchste Stufe, die das System noch innerhalb der Zielwerte trägt.
        """

        def tick(self) -> tuple[int, float] | None:
            index = int(self.get_run_time() // STUFENDAUER)
            if index >= len(STUFEN):
                return None
            return STUFEN[index], STUFEN_ANLAUF


# =============================================================================
# Szenarien
# =============================================================================


class _Mandari(HttpUser):
    """Gemeinsame Hilfen: Anmeldung über das Login-Formular, CSRF, Kommune wählen."""

    abstract = True
    wait_time = between(1, 4)
    #: Szenario in der Auswertung (Budgets in loadtest/budgets.json)
    szenario = "Sonstige"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not TLS_PRUEFEN:
            self.client.verify = False

    def context(self) -> dict[str, Any]:
        return {"szenario": self.szenario}

    def csrf_token(self) -> str:
        # Über HTTPS heißt das Cookie __Host-csrftoken (DEPLOYMENT.md, „Ursprünge, Hosts und Cookies“)
        return self.client.cookies.get("__Host-csrftoken") or self.client.cookies.get("csrftoken", "")

    def anmelden(self, email: str) -> None:
        antwort = self.client.get("/accounts/login/", name="/accounts/login/ [GET]")
        token = self.csrf_token()
        with self.client.post(
            "/accounts/login/",
            data={"csrfmiddlewaretoken": token, "email": email, "password": PASSWORT, "next": "/insight/"},
            headers={"Referer": antwort.url or ""},
            name="/accounts/login/ [POST]",
            catch_response=True,
        ) as ergebnis:
            # Ohne Anmeldung landen alle weiteren Aufrufe auf der Anmeldeseite und messen das Falsche
            if ergebnis.status_code >= 400:
                ergebnis.failure(f"Anmeldung: HTTP {ergebnis.status_code}")
            elif "/accounts/login/" in (ergebnis.url or ""):
                ergebnis.failure("Anmeldung abgewiesen (2FA-Pflicht, Passwort, Cookies über HTTPS?)")

    def kommune_waehlen(self) -> None:
        if BODY_ID:
            self.client.get(f"/insight/kommune/{BODY_ID}/", name="/insight/kommune/<id>/")
        else:
            # Einstieg einer Körperschaft (Issue #317): setzt die aktive Kommune der Sitzung
            self.client.get(f"/insight/k/{SESSION_SLUG}/", name="/insight/k/<slug>/")

    def csrf(self) -> dict[str, str]:
        return {"X-CSRFToken": self.csrf_token(), "Referer": self.host or ""}


class PortalBesucher(_Mandari):
    """Bürgerportal ohne Anmeldung."""

    weight = 5
    szenario = "Portal"
    vorgaenge: list[str]
    termine: list[str]

    def on_start(self) -> None:
        self.kommune_waehlen()
        self.vorgaenge = _ids(
            self.client.get("/insight/vorgaenge/", name="/insight/vorgaenge/").text, "/insight/vorgaenge/"
        )
        self.termine = _ids(self.client.get("/insight/termine/", name="/insight/termine/").text, "/insight/termine/")

    @task(4)
    def startseite(self) -> None:
        self.client.get("/insight/", name="/insight/")

    @task(3)
    def sitzungen(self) -> None:
        self.client.get("/insight/termine/", name="/insight/termine/")

    @task(3)
    def vorgaenge_liste(self) -> None:
        self.client.get("/insight/vorgaenge/", name="/insight/vorgaenge/")

    @task(3)
    def vorgang(self) -> None:
        if self.vorgaenge:
            self.client.get(f"/insight/vorgaenge/{random.choice(self.vorgaenge)}/", name="/insight/vorgaenge/<id>/")

    @task(2)
    def termin(self) -> None:
        if self.termine:
            self.client.get(f"/insight/termine/{random.choice(self.termine)}/", name="/insight/termine/<id>/")

    @task(2)
    def suche(self) -> None:
        begriff = random.choice(SUCHBEGRIFFE)
        self.client.get(f"/insight/suche/?q={begriff}", name="/insight/suche/")
        self.client.get(f"/insight/suche/partials/results/?q={begriff}", name="/insight/suche/partials/results/")


class Sitzungsdienst(_Mandari):
    """Sachbearbeitung im Verwaltungs-RIS."""

    weight = 3
    szenario = "Sitzungsdienst"
    sitzungen: list[str]
    vorlagen: list[str]

    def on_start(self) -> None:
        nummer = next(_zaehler_sachbearbeitung) % SACHBEARBEITUNG + 1
        self.anmelden(f"{SESSION_SLUG}-sachbearbeitung-{nummer}@{DOMAENE}")
        basis = f"/session/{SESSION_SLUG}"
        self.sitzungen = _ids(
            self.client.get(f"{basis}/meetings/", name="/session/meetings/").text, f"{basis}/meetings/"
        )
        self.vorlagen = _ids(self.client.get(f"{basis}/papers/", name="/session/papers/").text, f"{basis}/papers/")

    @task(4)
    def sitzungsliste(self) -> None:
        self.client.get(f"/session/{SESSION_SLUG}/meetings/", name="/session/meetings/")

    @task(4)
    def sitzungsdetail(self) -> None:
        if self.sitzungen:
            self.client.get(
                f"/session/{SESSION_SLUG}/meetings/{random.choice(self.sitzungen)}/", name="/session/meetings/<id>/"
            )

    @task(1)
    def sitzungsmappe(self) -> None:
        if self.sitzungen:
            self.client.get(
                f"/session/{SESSION_SLUG}/meetings/{random.choice(self.sitzungen)}/agenda.pdf",
                name="/session/meetings/<id>/agenda.pdf",
            )

    @task(3)
    def vorlagenliste(self) -> None:
        self.client.get(f"/session/{SESSION_SLUG}/papers/", name="/session/papers/")

    @task(2)
    def vorlage(self) -> None:
        if self.vorlagen:
            self.client.get(
                f"/session/{SESSION_SLUG}/papers/{random.choice(self.vorlagen)}/", name="/session/papers/<id>/"
            )

    @task(1)
    def suche(self) -> None:
        self.client.get(f"/session/{SESSION_SLUG}/search/?q={random.choice(SUCHBEGRIFFE)}", name="/session/search/")


class Fraktionsmitglied(_Mandari):
    """Fraktionsarbeit in Work."""

    weight = 2
    szenario = "Fraktion"
    dokumente: list[str]

    def on_start(self) -> None:
        nummer = next(_zaehler_fraktion) % FRAKTION + 1
        self.anmelden(f"{SESSION_SLUG}-fraktion-{nummer}@{DOMAENE}")
        basis = f"/work/{ORG_SLUG}"
        self.dokumente = _ids(
            self.client.get(f"{basis}/documents/", name="/work/documents/").text, f"{basis}/documents/"
        )

    @task(3)
    def dashboard(self) -> None:
        self.client.get(f"/work/{ORG_SLUG}/", name="/work/")

    @task(3)
    def dokumentliste(self) -> None:
        self.client.get(f"/work/{ORG_SLUG}/documents/", name="/work/documents/")

    @task(2)
    def editor(self) -> None:
        if self.dokumente:
            self.client.get(
                f"/work/{ORG_SLUG}/documents/{random.choice(self.dokumente)}/", name="/work/documents/<id>/"
            )


class OParlAbnehmer(_Mandari):
    """
    Abnehmer der offenen Schnittstelle (Aggregatoren, Apps, Forschung), anonym.

    Liest die Pflichtlisten beider Ausgaben – Aggregator (``/oparl/v1/body/<id>/…``) und
    Session-Schnittstelle des Mandanten (``/session/<slug>/api/oparl/…``) –, blättert weiter, ruft
    Einzelobjekte ab und fragt bekannte Listen mit ``If-None-Match`` erneut ab. Die Schnittstelle hat
    eine Ratenbegrenzung je Adresse (``OPARL_API_RATE_LIMIT``); auf dem Zielsystem des Lasttests muss
    sie aus sein, weil alle simulierten Abnehmer von einer Adresse kommen.
    """

    weight = 1
    szenario = "OParl"
    wait_time = between(2, 6)
    listen: list[str]
    objekte: list[str]
    etags: dict[str, str]

    def on_start(self) -> None:
        self.etags = {}
        self.objekte = []
        body_id = BODY_ID
        if not body_id:
            daten = self.oparl("/oparl/v1/bodies")
            for body in (daten or {}).get("data", []):
                if body.get("name") == f"Stadt Lastheim ({PROFIL})":
                    body_id = _pfad(body["id"]).rstrip("/").rsplit("/", 1)[-1]
        self.listen = [f"/session/{SESSION_SLUG}/api/oparl/{liste}/" for liste in OPARL_LISTEN]
        if body_id:
            self.listen += [f"/oparl/v1/body/{body_id}/{liste}" for liste in OPARL_LISTEN]

    def oparl(self, pfad: str, *, bedingt: bool = False) -> dict[str, Any] | None:
        """JSON abrufen; mit ``bedingt`` und bekanntem ETag als bedingte Anfrage (200 oder 304)."""
        headers = {"Accept": "application/json"}
        name = _statistikname(pfad)
        if bedingt and pfad in self.etags:
            headers["If-None-Match"] = self.etags[pfad]
            name += BEDINGT
        with self.client.get(pfad, headers=headers, name=name, catch_response=True) as antwort:
            if antwort.status_code == 304:
                antwort.success()
                return None
            if antwort.status_code != 200:
                antwort.failure(f"HTTP {antwort.status_code}")
                return None
            if antwort.headers.get("ETag"):
                self.etags[pfad] = antwort.headers["ETag"]
            try:
                daten: dict[str, Any] = antwort.json()
            except ValueError:
                antwort.failure("Antwort ist kein JSON")
                return None
        return daten

    def _merken(self, daten: dict[str, Any] | None) -> None:
        for objekt in (daten or {}).get("data", [])[:20]:
            if isinstance(objekt, dict) and objekt.get("id"):
                self.objekte.append(_pfad(objekt["id"]))
        del self.objekte[:-200]

    @task(4)
    def liste_lesen(self) -> None:
        if self.listen:
            self._merken(self.oparl(random.choice(self.listen), bedingt=True))

    @task(2)
    def blaettern(self) -> None:
        if not self.listen:
            return
        daten = self.oparl(random.choice(self.listen))
        for _ in range(3):
            weiter = ((daten or {}).get("links") or {}).get("next")
            if not weiter:
                break
            daten = self.oparl(_pfad(weiter))
            self._merken(daten)

    @task(2)
    def objekt_lesen(self) -> None:
        if self.objekte:
            self.oparl(random.choice(self.objekte))


class LiveAbstimmung(_Mandari):
    """
    Protokollführung in der laufenden Ratssitzung: Einzelstimmen erfassen.

    Genau ein Nutzer (``fixed_count``): Eine Sitzung hat eine Protokollführung, die die Stimmen aller
    Anwesenden erfasst – bei der Großstadt 90 je Abstimmung. Mit Gewichtung erfassten bei 400 Nutzern
    über 30 Protokollführungen gleichzeitig dieselben Tagesordnungspunkte; das misst Sperrkonflikte, die
    es im Betrieb nicht gibt.
    """

    fixed_count = 1
    szenario = "Live-Abstimmung"
    wait_time = between(5, 15)
    tops: list[str]

    def on_start(self) -> None:
        self.anmelden(f"{SESSION_SLUG}-sachbearbeitung-1@{DOMAENE}")
        liste = self.client.get(f"/session/{SESSION_SLUG}/meetings/?state=in_progress", name="/session/meetings/").text
        sitzungen = _ids(liste, f"/session/{SESSION_SLUG}/meetings/")
        self.tops = []
        if sitzungen:
            detail = self.client.get(
                f"/session/{SESSION_SLUG}/meetings/{sitzungen[0]}/", name="/session/meetings/<id>/"
            ).text
            self.tops = _ids(detail, f"/session/{SESSION_SLUG}/agenda/")

    @task
    def abstimmen(self) -> None:
        if not self.tops:
            return
        url = f"/session/{SESSION_SLUG}/agenda/{random.choice(self.tops)}/voting/"
        seite = self.client.get(url, name="/session/agenda/<id>/voting/ [GET]")
        personen = set(re.findall(rf'name="vote_({UUID})"', seite.text))
        daten: dict[str, Any] = {
            "csrfmiddlewaretoken": self.csrf_token(),
            "voting_method": "roll_call",
            "vote_result": "approved",
        }
        for person in personen:
            daten[f"vote_{person}"] = random.choice(["yes", "yes", "yes", "no", "abstain"])
        self.client.post(url, data=daten, headers=self.csrf(), name="/session/agenda/<id>/voting/ [POST]")


class Sitzungsgeldlauf(_Mandari):
    """
    Sitzungsdienst rechnet Sitzungsgeld ab, während die übrigen Szenarien laufen.

    Genau ein Nutzer (``fixed_count``) mit dem Verwaltungskonto des Mandanten. Jeder Lauf nimmt den
    nächstälteren Monat der zweijährigen Historie, erzeugt also echte Positionen aus den
    Anwesenheiten; ein bereits abgerechneter Monat wäre ein Leerlauf (der Dienst überspringt
    vorhandene Positionen). Das Verwaltungskonto ist Administrator – die 2FA-Pflicht muss auf dem
    Zielsystem aus sein (``TWO_FACTOR_ENFORCEMENT=false``).
    """

    fixed_count = 1
    szenario = "Sitzungsgeldlauf"
    wait_time = between(20, 40)
    monate_zurueck = 0

    def on_start(self) -> None:
        self.anmelden(f"{SESSION_SLUG}-verwaltung@{DOMAENE}")

    @task
    def abrechnen(self) -> None:
        self.monate_zurueck = self.monate_zurueck % MONATE_HISTORIE + 1
        beginn, ende = _monat(date.today(), self.monate_zurueck)
        basis = f"/session/{SESSION_SLUG}/allowances/"
        with self.client.post(
            f"{basis}generate/",
            data={"csrfmiddlewaretoken": self.csrf_token(), "from": beginn.isoformat(), "to": ende.isoformat()},
            headers=self.csrf(),
            name="/session/allowances/generate/ [POST]",
            allow_redirects=False,
            catch_response=True,
        ) as antwort:
            # Erfolg leitet auf die Übersicht weiter; alles andere (Anmeldeseite, 403, 500) ist ein Fehler
            ziel = antwort.headers.get("Location", "")
            if antwort.status_code != 302 or "/accounts/login/" in ziel:
                antwort.failure(f"Abrechnungslauf: HTTP {antwort.status_code} {ziel}".strip())
        self.client.get(basis, name="/session/allowances/")
