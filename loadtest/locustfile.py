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
- ``LiveAbstimmung`` (1): laufende Ratssitzung — Abstimmungsseite aufrufen und Einzelstimmen
  aller Anwesenden erfassen (das ist der HTTP-Anteil der digitalen Abstimmung; die
  WebSocket-Verteilung an die Endgeräte ist hier nicht abgebildet)

Kennzahlen (p95, Durchsatz, Fehlerquote) schreibt Locust nach ``loadtest/results/``.
"""

from __future__ import annotations

import itertools
import os
import random
import re
from typing import Any

from locust import HttpUser, between, task

PROFIL = os.environ.get("LOADTEST_PROFILE", "klein")
PASSWORT = os.environ.get("LOADTEST_PASSWORD", "Lasttest-2026!")
DOMAENE = "lasttest.mandari.invalid"
SESSION_SLUG = f"last-{PROFIL}-stadt"
ORG_SLUG = f"last-{PROFIL}-stadt-fraktion"
#: Konten je Rolle, siehe PROFILE in apps/session/management/commands/generate_load_data.py
KONTEN = {"klein": (20, 8), "mittel": (100, 15), "gross": (400, 25)}
SACHBEARBEITUNG, FRAKTION = KONTEN.get(PROFIL, (20, 8))
#: Aktive Kommune im Portal; leer = automatische Wahl, wenn nur eine Kommune gelistet ist
BODY_ID = os.environ.get("LOADTEST_BODY_ID", "")
SUCHBEGRIFFE = ["Radweg", "Haushalt", "Spielplatz", "Schule", "Bebauungsplan", "Kita", "Ladesäulen"]

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_zaehler_sachbearbeitung = itertools.count()
_zaehler_fraktion = itertools.count()


def _ids(html: str, praefix: str) -> list[str]:
    """UUIDs aus Links wie ``/insight/vorgaenge/<uuid>/`` einsammeln (Detailseiten der Listen)."""
    return list(dict.fromkeys(re.findall(rf"{re.escape(praefix)}({UUID})/", html)))


class _Mandari(HttpUser):
    """Gemeinsame Hilfen: Anmeldung über das Login-Formular, CSRF, Kommune wählen."""

    abstract = True
    wait_time = between(1, 4)

    def csrf_token(self) -> str:
        # Über HTTPS heißt das Cookie __Host-csrftoken (DEPLOYMENT.md, „Ursprünge, Hosts und Cookies“)
        return self.client.cookies.get("__Host-csrftoken") or self.client.cookies.get("csrftoken", "")

    def anmelden(self, email: str) -> None:
        antwort = self.client.get("/accounts/login/", name="/accounts/login/ [GET]")
        token = self.csrf_token()
        self.client.post(
            "/accounts/login/",
            data={"csrfmiddlewaretoken": token, "email": email, "password": PASSWORT, "next": "/insight/"},
            headers={"Referer": antwort.url},
            name="/accounts/login/ [POST]",
        )

    def kommune_waehlen(self) -> None:
        if BODY_ID:
            self.client.get(f"/insight/kommune/{BODY_ID}/", name="/insight/kommune/<id>/")
            return
        antwort = self.client.get("/insight/", name="/insight/ [Auswahl]")
        # Bei genau einer gelisteten Kommune wählt die Startseite selbst; sonst die Lasttest-Kommune suchen
        treffer = re.search(rf"/insight/kommune/({UUID})/[^>]*>[^<]*Lastheim", antwort.text)
        if treffer:
            self.client.get(f"/insight/kommune/{treffer.group(1)}/", name="/insight/kommune/<id>/")

    def csrf(self) -> dict[str, str]:
        return {"X-CSRFToken": self.csrf_token(), "Referer": self.host or ""}


class PortalBesucher(_Mandari):
    """Bürgerportal ohne Anmeldung."""

    weight = 5
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


class LiveAbstimmung(_Mandari):
    """Protokollführung in der laufenden Ratssitzung: Einzelstimmen erfassen."""

    weight = 1
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
