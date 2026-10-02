# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft in einem Pull Request, ob für alle Beteiligten eine Beitragsvereinbarung vorliegt.

Aufgerufen vom Workflow ``.github/workflows/cla.yml``; Ablauf und Pflege stehen in
``docs/cla/README.md``. Das Skript braucht nur die Standardbibliothek und liest ausschließlich
Metadaten über die GitHub-API. Code aus dem Pull Request wird weder ausgecheckt noch ausgeführt.

Regeln:

- Beteiligt sind das Konto, das den Pull Request geöffnet hat, und alle Konten, denen GitHub die
  Commits zuordnet (Autor und Mitautoren aus ``Co-authored-by``).
- Freigestellt sind die Projektkonten und die Bots aus ``.github/cla.json``.
- Alle übrigen Konten brauchen einen Eintrag in der Signaturliste zu einer gültigen Fassung.
  Maßgeblich ist die numerische Kontokennung; sie bleibt gleich, wenn ein Konto umbenannt wird.
- Maßgeblich ist das Konto, das den Pull Request geöffnet hat: Nur dieses ist von GitHub
  angemeldet. Die Autorenangabe eines Commits legt fest, wer den Commit erstellt, und GitHub ordnet
  sie nur über die E-Mail-Adresse einem Konto zu.
- Commits, deren Adresse keinem Konto zugeordnet ist, gelten bei Pull Requests von Projektkonten
  und Bots als deren Beitrag. Bei allen anderen Pull Requests schlägt die Prüfung fehl, bis die
  Adresse einem Konto zugeordnet ist.

Die Signaturliste steht nicht im Repository. Der Workflow übergibt sie in der Umgebungsvariable
``CLA_SIGNATUREN``; ihr Inhalt wird nie ausgegeben.

    python scripts/cla_pruefung.py --repo mandariOSS/mandari --pr 123
    python scripts/cla_pruefung.py --repo mandariOSS/mandari --pr 123 --kein-kommentar
    python scripts/cla_pruefung.py --liste-pruefen signaturen.json

Rückgabewerte: 0 = vollständig, 1 = Zustimmung fehlt, 2 = technischer Fehler oder ungültige
Konfiguration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

WURZEL = Path(__file__).resolve().parent.parent
KONFIGURATION = WURZEL / ".github" / "cla.json"

# Unsichtbare Marke, an der das Skript seinen eigenen Kommentar wiederfindet
MARKE = "<!-- cla-pruefung -->"
KOMMENTAR_KONTO = "github-actions[bot]"

PROJEKT = "projekt"
BOT = "bot"
ZUSTIMMUNG = "zustimmung"
FEHLT = "fehlt"

_SCHLUESSEL_KONFIGURATION = {"gueltige_versionen", "projektkonten", "bots"}
_SCHLUESSEL_SIGNATUR = {"konto", "id", "datum", "version"}
_MAX_AUTOREN_JE_COMMIT = 50

_COMMITS_ABFRAGE = """
query($owner: String!, $name: String!, $nummer: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $nummer) {
      commits(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          commit {
            oid
            authors(first: MAX_AUTOREN) {
              totalCount
              nodes { user { login databaseId } }
            }
          }
        }
      }
    }
  }
}
""".replace("MAX_AUTOREN", str(_MAX_AUTOREN_JE_COMMIT))


class AuswertungError(Exception):
    """Konfiguration, Signaturliste oder API sind nicht auswertbar; keine Aussage über den Beitrag."""


@dataclass(frozen=True)
class Konto:
    login: str
    id: int | None = None


@dataclass(frozen=True)
class Commit:
    sha: str
    # None steht für eine Autorenangabe, die GitHub keinem Konto zuordnet
    autoren: tuple[Konto | None, ...]


@dataclass(frozen=True)
class Signatur:
    konto: str
    id: int
    datum: str
    version: str


@dataclass(frozen=True)
class Konfiguration:
    gueltige_versionen: frozenset[str]
    projektkonten: frozenset[str]
    bots: frozenset[str]


@dataclass(frozen=True)
class PullRequest:
    nummer: int
    autor: Konto
    basiszweig: str
    commits: tuple[Commit, ...]


@dataclass
class Ergebnis:
    # Konto (wie von GitHub geschrieben) → PROJEKT, BOT, ZUSTIMMUNG oder FEHLT
    konten: dict[str, str] = field(default_factory=dict)
    # Kurze Kennungen der Commits ohne zugeordnetes Konto
    commits_ohne_konto: list[str] = field(default_factory=list)
    # True, wenn der Pull Request von einem Projektkonto oder Bot stammt
    autor_freigestellt: bool = False
    # False, solange in der Konfiguration keine gültige Fassung eingetragen ist
    vereinbarung_verfuegbar: bool = False

    @property
    def fehlende_konten(self) -> list[str]:
        return [konto for konto, status in self.konten.items() if status == FEHLT]

    @property
    def offene_commits(self) -> list[str]:
        return [] if self.autor_freigestellt else self.commits_ohne_konto

    @property
    def bestanden(self) -> bool:
        return not self.fehlende_konten and not self.offene_commits


class GitHubZugriff(Protocol):
    def rest(self, methode: str, pfad: str, daten: Mapping[str, Any] | None = None) -> Any: ...

    def graphql(self, abfrage: str, variablen: Mapping[str, Any]) -> Any: ...


# =============================================================================
# Konfiguration und Signaturliste
# =============================================================================


def _textliste(wert: Any, name: str) -> list[str]:
    if not isinstance(wert, list) or not all(isinstance(eintrag, str) and eintrag.strip() for eintrag in wert):
        raise AuswertungError(f"Konfiguration: '{name}' muss eine Liste nicht leerer Texte sein.")
    return [eintrag.strip() for eintrag in wert]


def konfiguration_aus(daten: Any) -> Konfiguration:
    if not isinstance(daten, dict):
        raise AuswertungError("Konfiguration: Es wird ein JSON-Objekt erwartet.")
    unbekannt = sorted(set(daten) - _SCHLUESSEL_KONFIGURATION)
    if unbekannt:
        raise AuswertungError(f"Konfiguration: unbekannte Schlüssel {unbekannt}.")
    fehlend = sorted(_SCHLUESSEL_KONFIGURATION - set(daten))
    if fehlend:
        raise AuswertungError(f"Konfiguration: Schlüssel fehlen {fehlend}.")
    bots = _textliste(daten["bots"], "bots")
    # Konten von Menschen können keine eckigen Klammern tragen; so landet niemand versehentlich hier
    if not all(bot.endswith("[bot]") for bot in bots):
        raise AuswertungError("Konfiguration: Einträge unter 'bots' müssen auf '[bot]' enden.")
    return Konfiguration(
        gueltige_versionen=frozenset(_textliste(daten["gueltige_versionen"], "gueltige_versionen")),
        projektkonten=frozenset(konto.lower() for konto in _textliste(daten["projektkonten"], "projektkonten")),
        bots=frozenset(bot.lower() for bot in bots),
    )


def lade_konfiguration(pfad: Path) -> Konfiguration:
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except OSError as fehler:
        raise AuswertungError(f"Konfiguration {pfad.name} ist nicht lesbar.") from fehler
    except json.JSONDecodeError as fehler:
        raise AuswertungError(f"Konfiguration {pfad.name} ist kein gültiges JSON (Zeile {fehler.lineno}).") from fehler
    return konfiguration_aus(daten)


def signaturen_aus(text: str | None) -> list[Signatur]:
    """
    Liest die Signaturliste: ``{"signaturen": [{"konto": …, "id": …, "datum": …, "version": …}]}``.

    Eine fehlende oder leere Liste ist gültig (es hat noch niemand zugestimmt). Fehlermeldungen
    nennen nur die Position eines Eintrags, nie seinen Inhalt: Sie landen im öffentlichen Protokoll.
    """
    if text is None or not text.strip():
        return []
    try:
        daten = json.loads(text)
    except json.JSONDecodeError as fehler:
        raise AuswertungError("Signaturliste: kein gültiges JSON.") from fehler
    if not isinstance(daten, dict) or not isinstance(daten.get("signaturen"), list):
        raise AuswertungError('Signaturliste: erwartet wird {"signaturen": [...]}.')
    signaturen: list[Signatur] = []
    for nummer, eintrag in enumerate(daten["signaturen"], start=1):
        if not isinstance(eintrag, dict) or set(eintrag) != _SCHLUESSEL_SIGNATUR:
            raise AuswertungError(
                f"Signaturliste: Eintrag {nummer} braucht genau die Felder {sorted(_SCHLUESSEL_SIGNATUR)}."
            )
        konto, kennung, datum, version = (eintrag[name] for name in ("konto", "id", "datum", "version"))
        if not isinstance(konto, str) or not konto.strip():
            raise AuswertungError(f"Signaturliste: Eintrag {nummer} hat keinen Kontonamen.")
        # bool ist in Python ein int; true wäre sonst die Kennung 1
        if not isinstance(kennung, int) or isinstance(kennung, bool) or kennung <= 0:
            raise AuswertungError(f"Signaturliste: Eintrag {nummer} braucht die numerische Kontokennung.")
        if not isinstance(version, str) or not version.strip():
            raise AuswertungError(f"Signaturliste: Eintrag {nummer} nennt keine Fassung.")
        try:
            if not isinstance(datum, str):
                raise ValueError
            date.fromisoformat(datum)
        except ValueError:
            raise AuswertungError(f"Signaturliste: Eintrag {nummer} braucht ein Datum JJJJ-MM-TT.") from None
        signaturen.append(Signatur(konto=konto.strip(), id=kennung, datum=datum, version=version.strip()))
    return signaturen


# =============================================================================
# Prüflogik (ohne Netzwerk, in den Tests direkt aufgerufen)
# =============================================================================


def _status(konto: Konto, konfiguration: Konfiguration, kennungen_mit_zustimmung: set[int]) -> str:
    login = konto.login.lower()
    if login in konfiguration.projektkonten:
        return PROJEKT
    if login in konfiguration.bots:
        return BOT
    if konto.id is not None and konto.id in kennungen_mit_zustimmung:
        return ZUSTIMMUNG
    return FEHLT


def pruefe(
    pr_autor: Konto,
    commits: Iterable[Commit],
    konfiguration: Konfiguration,
    signaturen: Iterable[Signatur],
) -> Ergebnis:
    kennungen = {signatur.id for signatur in signaturen if signatur.version in konfiguration.gueltige_versionen}
    ergebnis = Ergebnis(vereinbarung_verfuegbar=bool(konfiguration.gueltige_versionen))

    status_autor = _status(pr_autor, konfiguration, kennungen)
    ergebnis.konten[pr_autor.login] = status_autor
    ergebnis.autor_freigestellt = status_autor in (PROJEKT, BOT)

    gesehen = {pr_autor.login.lower()}
    for commit in commits:
        if any(autor is None for autor in commit.autoren):
            ergebnis.commits_ohne_konto.append(commit.sha[:7])
        for autor in commit.autoren:
            if autor is None or autor.login.lower() in gesehen:
                continue
            gesehen.add(autor.login.lower())
            ergebnis.konten[autor.login] = _status(autor, konfiguration, kennungen)
    return ergebnis


# =============================================================================
# Texte
# =============================================================================


def _nennungen(konten: Sequence[str], grenze: int = 10) -> str:
    # Begrenzt, damit sich über erfundene Autorenangaben nicht beliebig viele Konten anschreiben lassen
    genannt = ", ".join(f"@{konto}" for konto in konten[:grenze])
    return f"{genannt} und {len(konten) - grenze} weitere" if len(konten) > grenze else genannt


def _kurzliste(commits: Sequence[str], grenze: int = 10) -> str:
    gezeigt = ", ".join(f"`{sha}`" for sha in commits[:grenze])
    return f"{gezeigt} und {len(commits) - grenze} weitere" if len(commits) > grenze else gezeigt


def kommentar_text(ergebnis: Ergebnis, repo: str, basiszweig: str, server: str = "https://github.com") -> str | None:
    """Kommentar für einen Pull Request, dem noch etwas fehlt; ``None``, wenn alles vollständig ist."""
    if ergebnis.bestanden:
        return None
    basis = f"{server}/{repo}/blob/{urllib.parse.quote(basiszweig)}"
    beitragen = f"[CONTRIBUTING.md]({basis}/CONTRIBUTING.md#lizenz-und-urheberrecht)"
    ablauf = f"[docs/cla/README.md]({basis}/docs/cla/README.md)"
    fehlende = ergebnis.fehlende_konten
    zeilen = [MARKE, "### Beitragsvereinbarung", "", "Danke für den Pull Request!", ""]

    if fehlende and not ergebnis.vereinbarung_verfuegbar:
        zeilen += [
            "Beiträge von außerhalb des Projektteams können wir erst annehmen, nachdem eine Beitragsvereinbarung "
            "(Contributor License Agreement) geschlossen wurde. Die Vereinbarung wird derzeit finalisiert und lässt "
            "sich deshalb noch nicht abschließen.",
            "",
            f"Betroffene Konten: {_nennungen(fehlende)}",
            "",
            "**So geht es weiter**",
            "",
            "- Falls es zu diesem Beitrag noch kein Issue gibt: bitte eines öffnen oder das bestehende hier "
            "verlinken, damit wir das Vorgehen abstimmen können.",
            "- Der Pull Request kann offen bleiben. Sobald die Vereinbarung vorliegt, melden wir uns hier.",
        ]
    elif fehlende:
        zeilen += [
            "Für diese Konten liegt noch keine Zustimmung zur aktuellen Fassung der Beitragsvereinbarung "
            f"(Contributor License Agreement) vor: {_nennungen(fehlende)}",
            "",
            "**So geht es weiter**",
            "",
            f"- Der Ablauf für Einzelpersonen und für Organisationen steht in {ablauf}.",
            "- Sobald die Zustimmung eingetragen ist, stoßen wir die Prüfung neu an. Ein weiterer Push ist dafür "
            "nicht nötig.",
        ]

    if ergebnis.offene_commits:
        anzahl = len(ergebnis.offene_commits)
        einleitung = "Außerdem: Bei" if fehlende else "Bei"
        zeilen += [
            "",
            f"{einleitung} {anzahl} {'Commit' if anzahl == 1 else 'Commits'} ({_kurzliste(ergebnis.offene_commits)}) "
            "ist die E-Mail-Adresse der Autorenangabe keinem GitHub-Konto zugeordnet. Damit lässt sich nicht "
            "feststellen, für wen die Vereinbarung gelten muss. Abhilfe: die Adresse im eigenen GitHub-Konto "
            "hinterlegen (Settings → Emails) oder die Commits mit einer dort hinterlegten Adresse neu schreiben.",
        ]

    zeilen += [
        "",
        f"Hintergrund: {beitragen}. Fragen gern hier im Pull Request.",
        "",
        "<sub>English: Thank you for the pull request. We can accept contributions from outside the project team "
        "only once a contributor license agreement is in place for every author; the links above describe the "
        "details. Feel free to ask here in English.</sub>",
    ]
    return "\n".join(zeilen)


def erledigt_text() -> str:
    return "\n".join(
        [
            MARKE,
            "### Beitragsvereinbarung",
            "",
            "Danke! Für alle Beteiligten dieses Pull Requests liegt die Zustimmung zur Beitragsvereinbarung vor.",
        ]
    )


def zusammenfassung(ergebnis: Ergebnis) -> str:
    """Übersicht für das Protokoll. Nennt nur den Status je Konto, nie Datum oder Fassung einer Zustimmung."""
    bezeichnung = {
        PROJEKT: "Projektkonto",
        BOT: "Bot",
        ZUSTIMMUNG: "Zustimmung liegt vor",
        FEHLT: "Zustimmung fehlt",
    }
    zeilen = ["### Beitragsvereinbarung", "", "| Konto | Status |", "|---|---|"]
    zeilen += [f"| {konto} | {bezeichnung[status]} |" for konto, status in ergebnis.konten.items()]
    if ergebnis.commits_ohne_konto:
        bewertung = (
            "zählen zum Pull Request des Projektkontos oder Bots"
            if ergebnis.autor_freigestellt
            else "müssen einem Konto zugeordnet werden"
        )
        zeilen += ["", f"Commits ohne zugeordnetes Konto: {len(ergebnis.commits_ohne_konto)} ({bewertung})."]
    if not ergebnis.vereinbarung_verfuegbar:
        zeilen += ["", "Es ist noch keine Fassung der Vereinbarung freigegeben (`gueltige_versionen` ist leer)."]
    zeilen += ["", "Ergebnis: " + ("vollständig." if ergebnis.bestanden else "**unvollständig.**")]
    return "\n".join(zeilen)


# =============================================================================
# GitHub-API
# =============================================================================


class GitHub:
    """Schmaler Zugriff auf REST und GraphQL mit dem Token des Workflows."""

    def __init__(self, token: str, api_url: str, graphql_url: str) -> None:
        for adresse in (api_url, graphql_url):
            if not adresse.startswith("https://"):
                raise AuswertungError("Die Adresse der GitHub-API muss mit https:// beginnen.")
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._graphql_url = graphql_url

    def _anfrage(self, methode: str, adresse: str, daten: Mapping[str, Any] | None, ziel: str) -> Any:
        anfrage = urllib.request.Request(
            adresse,
            method=methode,
            data=None if daten is None else json.dumps(daten).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "mandari-cla-pruefung",
            },
        )
        try:
            with urllib.request.urlopen(anfrage, timeout=30) as antwort:
                inhalt = antwort.read()
        except urllib.error.HTTPError as fehler:
            raise AuswertungError(f"GitHub-API: {methode} {ziel} antwortet mit HTTP {fehler.code}.") from fehler
        except (urllib.error.URLError, TimeoutError) as fehler:
            raise AuswertungError(f"GitHub-API: {methode} {ziel} ist nicht erreichbar.") from fehler
        return json.loads(inhalt) if inhalt else None

    def rest(self, methode: str, pfad: str, daten: Mapping[str, Any] | None = None) -> Any:
        return self._anfrage(methode, f"{self._api_url}{pfad}", daten, pfad.partition("?")[0])

    def graphql(self, abfrage: str, variablen: Mapping[str, Any]) -> Any:
        antwort = self._anfrage("POST", self._graphql_url, {"query": abfrage, "variables": dict(variablen)}, "graphql")
        if not isinstance(antwort, dict) or antwort.get("errors") or "data" not in antwort:
            raise AuswertungError("GitHub-API: Die GraphQL-Abfrage der Commits ist fehlgeschlagen.")
        return antwort["data"]


def _konto(daten: Any) -> Konto | None:
    if not isinstance(daten, dict) or not isinstance(daten.get("login"), str):
        return None
    kennung = daten.get("id", daten.get("databaseId"))
    return Konto(login=daten["login"], id=kennung if isinstance(kennung, int) else None)


def lade_pull_request(github: GitHubZugriff, repo: str, nummer: int) -> PullRequest:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise AuswertungError("Das Repository muss als 'eigentuemer/name' angegeben werden.")

    kopf = github.rest("GET", f"/repos/{repo}/pulls/{nummer}")
    autor = _konto(kopf.get("user") if isinstance(kopf, dict) else None)
    basiszweig = kopf.get("base", {}).get("ref") if isinstance(kopf, dict) else None
    if autor is None or not isinstance(basiszweig, str):
        raise AuswertungError("GitHub-API: Der Pull Request nennt kein Konto oder keinen Zielzweig.")

    commits: list[Commit] = []
    cursor: str | None = None
    while True:
        daten = github.graphql(_COMMITS_ABFRAGE, {"owner": owner, "name": name, "nummer": nummer, "cursor": cursor})
        try:
            seite = daten["repository"]["pullRequest"]["commits"]
            for knoten in seite["nodes"]:
                commit = knoten["commit"]
                autoren = commit["authors"]
                if autoren["totalCount"] > len(autoren["nodes"]):
                    raise AuswertungError(
                        f"Commit {commit['oid'][:7]} nennt mehr als {_MAX_AUTOREN_JE_COMMIT} Autoren; "
                        "das kann die Prüfung nicht auswerten."
                    )
                commits.append(
                    Commit(sha=commit["oid"], autoren=tuple(_konto(autor.get("user")) for autor in autoren["nodes"]))
                )
            weiter = seite["pageInfo"]["hasNextPage"]
            cursor = seite["pageInfo"]["endCursor"]
        except (KeyError, TypeError, AttributeError) as fehler:
            raise AuswertungError("GitHub-API: Die Antwort zu den Commits hat ein unerwartetes Format.") from fehler
        if not weiter:
            break
    if not commits:
        raise AuswertungError("GitHub-API: Der Pull Request enthält keine Commits.")
    return PullRequest(nummer=nummer, autor=autor, basiszweig=basiszweig, commits=tuple(commits))


def _eigener_kommentar(github: GitHubZugriff, repo: str, nummer: int) -> dict[str, Any] | None:
    seite = 1
    while True:
        kommentare = github.rest("GET", f"/repos/{repo}/issues/{nummer}/comments?per_page=100&page={seite}")
        if not isinstance(kommentare, list):
            raise AuswertungError("GitHub-API: Die Kommentare haben ein unerwartetes Format.")
        for kommentar in kommentare:
            # Nur den Kommentar des Workflows anfassen, nie einen fremden, der die Marke zitiert
            if (kommentar.get("user") or {}).get("login") == KOMMENTAR_KONTO and MARKE in (kommentar.get("body") or ""):
                return dict(kommentar)
        if len(kommentare) < 100:
            return None
        seite += 1


def kommentar_abgleichen(github: GitHubZugriff, repo: str, nummer: int, text: str | None) -> str:
    """
    Hält genau einen Kommentar aktuell. ``text`` ist der Hinweis bei fehlender Zustimmung oder
    ``None``, wenn alles vollständig ist: Dann wird nur ein früherer Hinweis ersetzt, und ein Pull
    Request, dem nie etwas fehlte, bleibt ohne Kommentar.
    """
    vorhanden = _eigener_kommentar(github, repo, nummer)
    if text is None:
        if vorhanden is None:
            return "kein Kommentar nötig"
        text = erledigt_text()
    if vorhanden is None:
        github.rest("POST", f"/repos/{repo}/issues/{nummer}/comments", {"body": text})
        return "Kommentar angelegt"
    if vorhanden.get("body") == text:
        return "Kommentar unverändert"
    github.rest("PATCH", f"/repos/{repo}/issues/comments/{vorhanden['id']}", {"body": text})
    return "Kommentar aktualisiert"


# =============================================================================
# Aufruf
# =============================================================================


def _ausgeben(text: str) -> None:
    print(text)
    ziel = os.environ.get("GITHUB_STEP_SUMMARY")
    if ziel:
        with open(ziel, "a", encoding="utf-8") as datei:
            datei.write(text + "\n")


def _argumente(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prüft die Beitragsvereinbarung für einen Pull Request.")
    parser.add_argument("--repo", help="Repository als eigentuemer/name")
    parser.add_argument("--pr", type=int, help="Nummer des Pull Requests")
    parser.add_argument("--konfiguration", type=Path, default=KONFIGURATION, help="Pfad zu cla.json")
    parser.add_argument("--kein-kommentar", action="store_true", help="nur prüfen, nichts kommentieren")
    parser.add_argument("--liste-pruefen", type=Path, metavar="DATEI", help="nur das Format einer Signaturliste prüfen")
    argumente = parser.parse_args(argv)
    if argumente.liste_pruefen is None and (not argumente.repo or argumente.pr is None):
        parser.error("--repo und --pr sind erforderlich.")
    return argumente


def _lauf(argumente: argparse.Namespace, umgebung: Mapping[str, str], github: GitHubZugriff | None) -> int:
    if argumente.liste_pruefen is not None:
        try:
            text = argumente.liste_pruefen.read_text(encoding="utf-8")
        except OSError as fehler:
            raise AuswertungError("Die Datei mit der Signaturliste ist nicht lesbar.") from fehler
        print(f"Signaturliste gültig: {len(signaturen_aus(text))} Einträge.")
        return 0

    konfiguration = lade_konfiguration(argumente.konfiguration)
    listenfehler: AuswertungError | None = None
    try:
        signaturen = signaturen_aus(umgebung.get("CLA_SIGNATUREN"))
    except AuswertungError as fehler:
        signaturen, listenfehler = [], fehler
    if github is None:
        token = umgebung.get("GITHUB_TOKEN", "")
        if not token:
            raise AuswertungError("GITHUB_TOKEN ist nicht gesetzt.")
        api_url = umgebung.get("GITHUB_API_URL", "https://api.github.com")
        github = GitHub(token, api_url, umgebung.get("GITHUB_GRAPHQL_URL", f"{api_url.rstrip('/')}/graphql"))

    pull_request = lade_pull_request(github, argumente.repo, argumente.pr)
    ergebnis = pruefe(pull_request.autor, pull_request.commits, konfiguration, signaturen)
    if listenfehler is not None:
        # Eine unlesbare Liste darf niemandem als fehlende Zustimmung ausgelegt werden. Pull Requests,
        # die die Liste gar nicht brauchen (Projektkonten, Bots), hält sie nicht auf.
        if ergebnis.fehlende_konten:
            raise listenfehler
        print(f"::warning::{listenfehler}")
    _ausgeben(zusammenfassung(ergebnis))

    if not argumente.kein_kommentar:
        text = kommentar_text(
            ergebnis, argumente.repo, pull_request.basiszweig, umgebung.get("GITHUB_SERVER_URL", "https://github.com")
        )
        try:
            print(kommentar_abgleichen(github, argumente.repo, argumente.pr, text))
        except AuswertungError as fehler:
            # Das Ergebnis der Prüfung gilt auch dann, wenn der Hinweis nicht geschrieben werden kann
            print(f"::warning::Kommentar nicht geschrieben: {fehler}")

    if ergebnis.bestanden:
        return 0
    print("::error::Beitragsvereinbarung unvollständig; Einzelheiten stehen im Kommentar und in der Zusammenfassung.")
    return 1


def main(
    argv: Sequence[str] | None = None,
    umgebung: Mapping[str, str] | None = None,
    github: GitHubZugriff | None = None,
) -> int:
    argumente = _argumente(sys.argv[1:] if argv is None else argv)
    try:
        return _lauf(argumente, os.environ if umgebung is None else umgebung, github)
    except AuswertungError as fehler:
        print(f"::error::Die Prüfung der Beitragsvereinbarung konnte nicht ausgewertet werden: {fehler}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
