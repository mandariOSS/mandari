# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, dass die Images (mandari, Ingestor) kein Python-Paket ohne Hash-Prüfung installieren.

Die Abhängigkeiten kommen aus ``uv.lock`` und werden mit ``--require-hashes`` installiert. Daneben bauen die
Images die lokalen Pakete (``shared/`` und ``ingestor/``). Dafür braucht der Installer ihr Build-Backend
(``[build-system] requires``). In einer isolierten Build-Umgebung lädt er es frisch aus dem Paketindex, ohne
feste Version und ohne Hash; ``--no-deps`` ändert daran nichts. Dasselbe gilt für jedes gesperrte Paket, das
nur als Quelltext vorliegt und deshalb im Image gebaut werden müsste.

Regeln für jeden Aufruf von pip oder uv, der Pakete installiert (``pip install``/``wheel``/``download``,
``uv pip install``/``sync``, ``uv sync`` …), auch hinter ``python -m``, ``sh -c "…"`` oder ``$(…)``:

- entweder ``--require-hashes`` und nur fertige Wheels (``--only-binary :all:`` bei pip; ``--no-build`` oder
  ``UV_NO_BUILD=1`` bei uv), ohne ``--no-binary``,
- oder ohne Netz: ``RUN --network=none`` und ``--no-index`` (bzw. ``--offline``). Ein Build-Backend kommt dann
  nur aus einer Umgebung, die vorher mit Hashes befüllt wurde (``--no-build-isolation``).

Andere Installer (``python -m build``, ``pyproject-build``, ``uvx``, ``pipx`` …) kennen keine Hash-Prüfung und
laufen nur mit ``RUN --network=none``. Jeder Aufruf von uv läuft mit ``UV_NO_BUILD=1`` (``ENV`` in der Stage)
oder ``--no-build``. ``uv export``, ``uv lock``, ``uv tree`` und ``uv pip compile`` laufen zusätzlich offline
(``--offline``, ``UV_OFFLINE=1`` oder ``RUN --network=none``): Ein lokales Paket, dessen Metadaten nicht
feststehen, baut uv dafür auch mit ``UV_NO_BUILD=1`` und lädt sein Build-Backend sonst ungeprüft aus dem
Paketindex. Heredocs und Installer außerhalb von RUN (``ONBUILD``,
``CMD`` …) lassen sich nicht prüfen und gelten als Fehler. Optionen zählen nur als eigene Wörter des Befehls,
nicht in Shell-Kommentaren.

Außerdem steht das Build-Backend jedes lokalen Pakets in der Abhängigkeitsgruppe ``build-backend`` des
Projekts, dessen Lock-Datei das Image liest, ist dort samt seinen Abhängigkeiten mit Hashes gesperrt, und das
Dockerfile exportiert genau diese Gruppe. Jedes andere Dockerfile im Repository (außer ``docs/examples``), das
pip, uv oder einen anderen Installer aufruft, muss in ``IMAGES`` stehen.

    python scripts/check_image_hashes.py

Nur Standardbibliothek: Der Schritt läuft im Job „Qualität“ und im Release-Workflow vor dem Bau der Images.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

# Abhängigkeitsgruppe in pyproject.toml, aus der die Images das Build-Backend installieren
GRUPPE = "build-backend"


@dataclass(frozen=True)
class Image:
    name: str
    dockerfile: Path
    # Ordner mit pyproject.toml und uv.lock, aus denen das Dockerfile exportiert
    projekt: Path
    # Lokale Pakete, die das Image baut
    lokale_pakete: tuple[Path, ...]


IMAGES = (
    Image("mandari", WURZEL / "mandari" / "Dockerfile", WURZEL / "mandari", (WURZEL / "shared",)),
    Image(
        "ingestor",
        WURZEL / "ingestor" / "Dockerfile",
        WURZEL / "ingestor",
        (WURZEL / "shared", WURZEL / "ingestor"),
    ),
)

# Beispiele für Fremdprojekte, kein Image von mandari
AUSGENOMMEN = {"docs/examples"}
# Ordner ohne eigene Dockerfiles (Versionsverwaltung, Umgebungen, Abhängigkeiten, Caches)
UEBERSPRUNGEN = {
    ".git",
    ".private",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
}

# pip, pip3, pip3.14 – als Programm oder über python -m pip
PIP = re.compile(r"pip(?:3(?:\.\d+)?)?")
PIP_INSTALLIERT = {"install", "wheel", "download", "lock"}
# Unterbefehle von uv, die auflösen und dabei Pakete bauen könnten
UV_BEFEHLE = {"pip", "sync", "run", "build", "tool", "add", "remove", "export", "lock", "tree", "venv", "version"}
UV_INSTALLIERT = {"sync", "run", "build", "tool", "add", "remove", "version"}
# Unterbefehle von uv, die nur auflösen, für lokale Pakete aber deren Metadaten brauchen: Stehen sie nicht fest,
# baut uv das Paket, auch mit UV_NO_BUILD=1. Deshalb nur offline.
UV_AUFLOESEND = {"export", "lock", "tree", "pip compile"}
# Installer ohne Hash-Prüfung
ANDERE = {
    "uvx",
    "pipx",
    "pip-sync",
    "pyproject-build",
    "easy_install",
    "setup.py",
    "poetry",
    "pdm",
    "hatch",
    "conda",
    "mamba",
    "micromamba",
}
# Grobe Suche, wenn sich ein Befehl nicht zerlegen lässt
INSTALLER_GROB = re.compile(
    r"(?<![\w.-])(?:pip[\d.]*|uvx?|pipx|pip-sync|pyproject-build|easy_install|setup\.py|poetry|pdm|hatch|conda|"
    r"mamba|micromamba)(?![\w-])|-m\s*build\b"
)
# Anweisungen, die Heredocs kennen, und solche, die außerhalb von RUN Befehle ausführen
HEREDOC_ANWEISUNGEN = {"RUN", "COPY", "ADD", "ONBUILD"}
AUSFUEHRENDE = {"ONBUILD", "CMD", "ENTRYPOINT", "HEALTHCHECK", "SHELL"}
HEREDOC = re.compile(r"<<(-?)\s*([\"']?)([A-Za-z0-9_.-]+)\2")
ZUWEISUNG = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
WORTGRENZE = set(" \t\n;&|()<>")
OPERATOREN = set(";&|()")
# Shell in Argumenten (sh -c "…", python -c "…") höchstens so tief verfolgen
MAX_TIEFE = 4
# Werte, die uv für UV_NO_BUILD als „an“ liest
WAHR = {"1", "true", "yes", "y", "t", "on"}


def _name(anforderung: str) -> str:
    """Normalisierter Paketname einer Anforderung (PEP 503), ohne Version, Extras und Marker."""
    treffer = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", anforderung)
    if not treffer:
        raise ValueError(f"keine Anforderung: {anforderung!r}")
    return re.sub(r"[-_.]+", "-", treffer.group(1)).lower()


# --- Dockerfile zerlegen -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Anweisung:
    zeile: int
    schluessel: str
    text: str
    heredoc: bool = False


def anweisungen(text: str) -> Iterator[Anweisung]:
    """
    Anweisungen eines Dockerfiles mit ihrer ersten Zeile: Fortsetzungszeilen zusammengefügt, Kommentar- und
    Leerzeilen entfernt (wie Docker, auch mitten in einer Fortsetzung). Der Inhalt eines Heredocs gehört zur
    Anweisung und wird übersprungen; ``heredoc`` markiert sie.
    """
    zeilen = text.splitlines()
    teile: list[str] = []
    beginn = 0
    index = 0
    while index < len(zeilen):
        zeile = zeilen[index]
        index += 1
        if not zeile.strip() or zeile.lstrip().startswith("#"):
            continue
        if not teile:
            beginn = index
        inhalt = zeile.rstrip()
        teile.append(inhalt.removesuffix("\\").strip())
        if inhalt.endswith("\\"):
            continue
        schluessel, _, rest = " ".join(teile).partition(" ")
        teile = []
        schluessel = schluessel.upper()
        heredoc = False
        if schluessel in HEREDOC_ANWEISUNGEN:
            for treffer in HEREDOC.finditer(rest):
                heredoc = True
                ohne_tabs, ende = treffer.group(1) == "-", treffer.group(3)
                while index < len(zeilen):
                    zeile = zeilen[index]
                    index += 1
                    if (zeile.lstrip("\t") if ohne_tabs else zeile) == ende:
                        break
        yield Anweisung(beginn, schluessel, rest.strip(), heredoc)
    if teile:
        schluessel, _, rest = " ".join(teile).partition(" ")
        yield Anweisung(beginn, schluessel.upper(), rest.strip())


def _ohne_kommentare(text: str) -> str:
    """
    Shell-Kommentare entfernen: ``#`` am Wortanfang außerhalb von Anführungszeichen bis zum Zeilenende (Docker
    fügt Fortsetzungszeilen zu einer Zeile zusammen, ein Kommentar reicht also bis zum Ende der Anweisung).
    Backticks werden zu ``;``, damit der Befehl darin ein eigener Befehl wird.
    """
    aus: list[str] = []
    zeichen_davor = " "
    in_quote = ""
    index = 0
    while index < len(text):
        zeichen = text[index]
        if in_quote == "'":
            if zeichen == "'":
                in_quote = ""
        elif zeichen == "\\":
            aus.append(text[index : index + 2])
            zeichen_davor = "x"
            index += 2
            continue
        elif zeichen == "`":
            zeichen = " ; "
        elif in_quote == '"':
            if zeichen == '"':
                in_quote = ""
        elif zeichen in "'\"":
            in_quote = zeichen
        elif zeichen == "#" and zeichen_davor in WORTGRENZE:
            ende = text.find("\n", index)
            if ende < 0:
                break
            index = ende
            continue
        aus.append(zeichen)
        zeichen_davor = zeichen[-1]
        index += 1
    return "".join(aus)


def _woerter(text: str) -> list[str] | None:
    """Wörter und Operatoren eines Shell-Befehls; ``None``, wenn er sich nicht zerlegen lässt."""
    lexer = shlex.shlex(_ohne_kommentare(text), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _befehle(woerter: list[str]) -> Iterator[list[str]]:
    """Einfache Befehle, getrennt an ``&&``, ``||``, ``;``, ``|``, ``&`` und Klammern (auch ``$(…)``)."""
    befehl: list[str] = []
    for wort in woerter:
        if wort and set(wort) <= set("();<>|&") and set(wort) & OPERATOREN:
            if befehl:
                yield befehl
            befehl = []
        else:
            befehl.append(wort)
    if befehl:
        yield befehl


@dataclass(frozen=True)
class Aufruf:
    """Ein Aufruf von pip, uv oder einem anderen Installer."""

    art: str  # "pip", "uv", "anders" oder "unklar" (nicht zerlegbar)
    programm: str
    unterbefehl: str
    woerter: tuple[str, ...]  # alles nach dem Programm
    zuweisungen: tuple[tuple[str, str], ...]  # VAR=wert vor dem Programm
    installiert: bool

    def text(self) -> str:
        return " ".join((self.programm, *self.woerter))


def _aufruf(programm: str, rest: list[str], zuweisungen: dict[str, str]) -> Aufruf | None:
    zuw = tuple(zuweisungen.items())
    if PIP.fullmatch(programm):
        unter = next((wort for wort in rest if wort in PIP_INSTALLIERT), None)
        return Aufruf("pip", programm, unter, tuple(rest), zuw, True) if unter else None
    if programm == "uv":
        unter = next((wort for wort in rest if wort in UV_BEFEHLE), None)
        if unter is None:
            return None
        if unter == "pip":
            danach = rest[rest.index("pip") + 1 :]
            pip_befehl = next((wort for wort in danach if not wort.startswith("-")), "")
            return Aufruf("uv", programm, f"pip {pip_befehl}", tuple(rest), zuw, pip_befehl in {"install", "sync"})
        installiert = unter in UV_INSTALLIERT or (unter == "venv" and "--seed" in rest)
        return Aufruf("uv", programm, unter, tuple(rest), zuw, installiert)
    if programm == "python -m build" or (programm in ANDERE and rest):
        return Aufruf("anders", programm, "", tuple(rest), zuw, True)
    return None


def _aufrufe_im_befehl(befehl: list[str], tiefe: int) -> Iterator[Aufruf]:
    """
    Aufrufe in einem einfachen Befehl. Das Programm darf hinter anderen Befehlen stehen (``xargs pip …``,
    ``find … -exec pip …``, ``python -m pip …``); Wörter mit Leerraum sind Skripte in Argumenten und werden selbst
    zerlegt (``sh -c "…"``).
    """
    zuweisungen: dict[str, str] = {}
    gefunden = False
    for index, wort in enumerate(befehl):
        if re.search(r"\s", wort):
            if tiefe < MAX_TIEFE:
                yield from aufrufe_in_text(wort, tiefe + 1)
            continue
        if gefunden:
            continue
        if ZUWEISUNG.match(wort):
            name, _, wert = wort.partition("=")
            zuweisungen[name] = wert
            continue
        rest = befehl[index + 1 :]
        if wort == "-m" and rest:
            programm, rest = rest[0], rest[1:]
        elif wort.startswith("-m") and not wort.startswith("--") and len(wort) > 2:
            programm = wort[2:]
        else:
            programm = wort.rsplit("/", 1)[-1]
        if programm == "build" and wort.startswith("-m"):
            programm = "python -m build"
        aufruf = _aufruf(programm, rest, zuweisungen)
        if aufruf:
            gefunden = True
            yield aufruf


def aufrufe_in_text(text: str, tiefe: int = 0) -> Iterator[Aufruf]:
    """Aufrufe von pip, uv und anderen Installern in einem Shell-Befehl."""
    woerter = _woerter(text)
    if woerter is None:
        if INSTALLER_GROB.search(text):
            yield Aufruf("unklar", text, "", (), (), True)
        return
    for befehl in _befehle(woerter):
        yield from _aufrufe_im_befehl(befehl, tiefe)


def _aufrufe_der_anweisung(text: str) -> tuple[list[str], list[Aufruf]]:
    """Optionen einer RUN-Anweisung (``--network=none`` …) und die Aufrufe darin, Shell- und Exec-Form."""
    optionen: list[str] = []
    rest = text
    while rest.startswith("--"):
        option, _, rest = rest.partition(" ")
        optionen.append(option)
        rest = rest.lstrip()
    if rest.startswith("["):  # Exec-Form: RUN ["pip", "install", …]
        try:
            woerter = json.loads(rest)
        except ValueError:
            woerter = None
        if isinstance(woerter, list) and all(isinstance(wort, str) for wort in woerter):
            return optionen, list(_aufrufe_im_befehl(woerter, 0))
    return optionen, list(aufrufe_in_text(rest))


# --- Regeln ------------------------------------------------------------------------------------------------------


def _werte(woerter: tuple[str, ...], option: str, mindestlaenge: int) -> list[str]:
    """Werte einer Option, auch abgekürzt ab ``mindestlaenge`` Zeichen (pip nimmt eindeutige Abkürzungen an)."""
    werte = []
    for index, wort in enumerate(woerter):
        schluessel, gleich, wert = wort.partition("=")
        if wort.startswith("--") and len(schluessel) >= mindestlaenge and option.startswith(schluessel):
            werte.append(wert if gleich else (woerter[index + 1] if index + 1 < len(woerter) else ""))
    return werte


def _ist_no_binary(wort: str) -> bool:
    schluessel = wort.partition("=")[0]
    return schluessel.startswith("--no-binary") or (
        len(schluessel) >= len("--no-bi") and "--no-binary".startswith(schluessel)
    )


def _wahr(wert: str) -> bool:
    return wert.strip().lower() in WAHR


def _env(text: str) -> dict[str, str]:
    """Variablen einer ENV-Anweisung (``ENV A=1 B=2`` oder ``ENV A 1``)."""
    try:
        woerter = shlex.split(text)
    except ValueError:
        return {}
    if not woerter:
        return {}
    if "=" not in woerter[0]:
        return {woerter[0]: " ".join(woerter[1:])}
    return {name: wert for name, _, wert in (wort.partition("=") for wort in woerter if "=" in wort)}


def _neue_stufe(text: str, stufen: dict[str, dict[str, str]]) -> dict[str, str]:
    """Umgebung einer neuen Stage: leer, oder die der Stage, auf der sie aufbaut (``FROM builder AS …``)."""
    woerter = [wort for wort in text.split() if not wort.startswith("--")]
    umgebung = dict(stufen.get(woerter[0].lower(), {})) if woerter else {}
    if len(woerter) >= 3 and woerter[1].lower() == "as":
        stufen[woerter[2].lower()] = umgebung
    return umgebung


def _pruefe_aufruf(aufruf: Aufruf, optionen: list[str], umgebung: dict[str, str], stelle: str) -> list[str]:
    if aufruf.art == "unklar":
        return [
            f"{stelle}\n    Befehl lässt sich nicht zerlegen (Anführungszeichen?); die Hash-Prüfung ist so nicht nachweisbar."
        ]
    probleme: list[str] = []
    woerter = aufruf.woerter
    variablen = {**umgebung, **dict(aufruf.zuweisungen)}
    uv_ohne_build = "--no-build" in woerter or _wahr(variablen.get("UV_NO_BUILD", ""))
    ohne_netz = "--network=none" in optionen
    if aufruf.art == "uv" and not uv_ohne_build:
        probleme.append(
            f"{stelle}\n    uv ohne `UV_NO_BUILD=1` (ENV in der Stage) oder `--no-build`: uv baut sonst Quelltextpakete "
            "mit einem frisch geladenen Build-Backend."
        )
    if (
        aufruf.art == "uv"
        and aufruf.unterbefehl in UV_AUFLOESEND
        and not ohne_netz
        and "--offline" not in woerter
        and not _wahr(variablen.get("UV_OFFLINE", ""))
    ):
        probleme.append(
            f"{stelle}\n    uv {aufruf.unterbefehl} ohne `--offline`: Ein lokales Paket, dessen Metadaten nicht "
            "feststehen, baut uv dafür auch mit `UV_NO_BUILD=1` und lädt sein Build-Backend ohne Hash-Prüfung."
        )
    if not aufruf.installiert:
        return probleme
    if aufruf.art == "anders":
        if not ohne_netz:
            probleme.append(
                f"{stelle}\n    `{aufruf.programm}` lädt Pakete ohne Hash-Prüfung: nur mit `RUN --network=none`."
            )
        return probleme
    if "--require-hashes" in woerter:
        if any(_ist_no_binary(wort) for wort in woerter):
            probleme.append(
                f"{stelle}\n    `--no-binary` erzwingt Quelltextpakete; deren Build-Backend käme ohne Hash-Prüfung aus "
                "dem Paketindex."
            )
            return probleme
        nur_binaer = _werte(woerter, "--only-binary", len("--only"))
        nur_wheels = bool(nur_binaer) and all(wert == ":all:" for wert in nur_binaer)
        if aufruf.art == "uv":
            nur_wheels = nur_wheels or uv_ohne_build
        if not nur_wheels:
            probleme.append(
                f"{stelle}\n    Hash-geprüft, aber Quelltextpakete würden mit einem frisch geladenen Build-Backend "
                "gebaut: `--only-binary :all:` (pip) bzw. `--no-build` oder `UV_NO_BUILD=1` (uv) ergänzen."
            )
    elif not (ohne_netz and {"--no-index", "--offline"} & set(woerter)):
        probleme.append(
            f"{stelle}\n    Installation ohne `--require-hashes`. Ohne Hash-Prüfung nur ohne Netz: "
            "`RUN --network=none` und `--no-index`, das Build-Backend vorher mit Hashes installieren "
            "und mit `--no-build-isolation` nutzen."
        )
    return probleme


def pruefe_dockerfile(text: str, name: str = "Dockerfile", gruppe_noetig: bool = True) -> list[str]:
    """Aufrufe, die ein Paket ohne Hash-Prüfung holen oder bauen könnten, und nicht prüfbare Stellen."""
    probleme: list[str] = []
    gruppe_exportiert = False
    stufen: dict[str, dict[str, str]] = {}
    umgebung: dict[str, str] = {}
    for anweisung in anweisungen(text):
        ort = f"{name}:{anweisung.zeile}"
        if anweisung.heredoc:
            probleme.append(
                f"{ort}: Heredoc in {anweisung.schluessel}: Der Inhalt lässt sich nicht prüfen; die Befehle direkt "
                "in die RUN-Anweisung schreiben."
            )
            continue
        if anweisung.schluessel == "FROM":
            umgebung = _neue_stufe(anweisung.text, stufen)
        elif anweisung.schluessel == "ENV":
            umgebung.update(_env(anweisung.text))
        elif anweisung.schluessel == "RUN":
            optionen, aufrufe = _aufrufe_der_anweisung(anweisung.text)
            for aufruf in aufrufe:
                if aufruf.unterbefehl == "export" and GRUPPE in _werte(
                    aufruf.woerter, "--only-group", len("--only-group")
                ):
                    gruppe_exportiert = True
                probleme += _pruefe_aufruf(aufruf, optionen, umgebung, f"{ort}: {aufruf.text()}")
        elif anweisung.schluessel in AUSFUEHRENDE:
            for aufruf in _aufrufe_der_anweisung(anweisung.text)[1]:
                probleme.append(
                    f"{ort}: {anweisung.schluessel} {aufruf.text()}\n    Installer außerhalb einer RUN-Anweisung; "
                    "das lässt sich nicht prüfen."
                )
    if gruppe_noetig and not gruppe_exportiert:
        probleme.append(
            f"{name}: exportiert die Gruppe `{GRUPPE}` nicht (`uv export --only-group {GRUPPE}`); "
            "das Build-Backend der lokalen Pakete käme sonst nicht aus der Lock-Datei."
        )
    return probleme


def ruft_installer_auf(text: str) -> bool:
    """Ob ein Dockerfile pip, uv oder einen anderen Installer aufruft (Heredocs zählen mit, sie sind nicht prüfbar)."""
    for anweisung in anweisungen(text):
        if anweisung.heredoc and anweisung.schluessel in {"RUN", "ONBUILD"}:
            return True
        if anweisung.schluessel == "RUN" or anweisung.schluessel in AUSFUEHRENDE:
            if _aufrufe_der_anweisung(anweisung.text)[1]:
                return True
    return False


def dockerfiles(wurzel: Path) -> Iterator[Path]:
    """Alle Dockerfiles im Repository (``Dockerfile*``, ``*.Dockerfile``) außer Beispielen und Abhängigkeiten."""
    for ordner, unterordner, dateien in os.walk(wurzel):
        basis = Path(ordner)
        unterordner[:] = sorted(
            eintrag
            for eintrag in unterordner
            if eintrag not in UEBERSPRUNGEN and (basis / eintrag).relative_to(wurzel).as_posix() not in AUSGENOMMEN
        )
        for datei in sorted(dateien):
            klein = datei.lower()
            if klein.startswith("dockerfile") or klein.endswith(".dockerfile"):
                yield basis / datei


# --- Lock-Datei -------------------------------------------------------------------------------------------------


def _build_backend(pyproject: dict) -> set[str]:
    return {_name(anforderung) for anforderung in pyproject.get("build-system", {}).get("requires", [])}


def pruefe_build_backend(pyproject: dict, lock: dict, lokale_pakete: dict[str, dict], name: str) -> list[str]:
    """
    Das Build-Backend aller lokalen Pakete steht in der Gruppe ``build-backend`` und ist in der Lock-Datei
    samt Abhängigkeiten nur mit Wheels und Hashes gesperrt.

    ``lokale_pakete``: Name des Pakets → Inhalt seiner pyproject.toml.
    """
    probleme: list[str] = []
    noetig: dict[str, str] = {}
    for paket, inhalt in lokale_pakete.items():
        for backend in sorted(_build_backend(inhalt)):
            noetig.setdefault(backend, paket)

    gruppe = {_name(eintrag) for eintrag in pyproject.get("dependency-groups", {}).get(GRUPPE, [])}
    for backend, paket in sorted(noetig.items()):
        if backend not in gruppe:
            probleme.append(
                f"{name}/pyproject.toml: Build-Backend `{backend}` von {paket} fehlt in [dependency-groups] "
                f"{GRUPPE} (danach `uv lock` im Ordner {name})"
            )

    pakete = {_name(paket["name"]): paket for paket in lock.get("package", [])}
    projekt_name = _name(pyproject["project"]["name"])
    projekt = pakete.get(projekt_name, {})
    gesperrt = {_name(eintrag["name"]) for eintrag in projekt.get("dev-dependencies", {}).get(GRUPPE, [])}
    if gruppe and gesperrt != gruppe:
        probleme.append(
            f"{name}/uv.lock: Gruppe {GRUPPE} passt nicht zu pyproject.toml "
            f"(Lock: {sorted(gesperrt)}, pyproject.toml: {sorted(gruppe)}) – `uv lock` im Ordner {name}"
        )

    # Alles, was die Gruppe nach sich zieht, braucht Wheels mit Hash
    offen, gesehen = sorted(gesperrt), set()
    while offen:
        paket_name = offen.pop()
        if paket_name in gesehen:
            continue
        gesehen.add(paket_name)
        paket = pakete.get(paket_name)
        if paket is None:
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) ist nicht gesperrt")
            continue
        if "registry" not in paket.get("source", {}):
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) kommt nicht aus dem Paketindex")
        wheels = paket.get("wheels", [])
        if not wheels or not all(wheel.get("hash") for wheel in wheels):
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) ohne Wheels mit Hash")
        offen.extend(_name(abhaengigkeit["name"]) for abhaengigkeit in paket.get("dependencies", []))
    return probleme


def _lies_toml(pfad: Path) -> dict:
    return tomllib.loads(pfad.read_text(encoding="utf-8"))


def pruefe(images: tuple[Image, ...] = IMAGES, wurzel: Path = WURZEL) -> list[str]:
    probleme: list[str] = []
    geprueft = set()
    for image in images:
        geprueft.add(image.dockerfile.resolve())
        name = image.projekt.name
        probleme += pruefe_dockerfile(
            image.dockerfile.read_text(encoding="utf-8"), image.dockerfile.relative_to(wurzel).as_posix()
        )
        probleme += pruefe_build_backend(
            _lies_toml(image.projekt / "pyproject.toml"),
            _lies_toml(image.projekt / "uv.lock"),
            {paket.name: _lies_toml(paket / "pyproject.toml") for paket in image.lokale_pakete},
            name,
        )
    for pfad in dockerfiles(wurzel):
        if pfad.resolve() in geprueft:
            continue
        if ruft_installer_auf(pfad.read_text(encoding="utf-8")):
            probleme.append(
                f"{pfad.relative_to(wurzel).as_posix()}: ruft pip, uv oder einen anderen Installer auf, steht aber "
                "nicht in IMAGES von scripts/check_image_hashes.py und wird deshalb nicht geprüft"
            )
    return probleme


def main() -> int:
    probleme = pruefe()
    if probleme:
        print("Die Images würden Pakete ohne Hash-Prüfung installieren:\n")
        for problem in probleme:
            print(f"  - {problem}")
        print(
            "\nAbhängigkeiten kommen aus uv.lock und werden mit --require-hashes installiert; das Build-Backend "
            "der lokalen Pakete steht in der Gruppe build-backend (siehe CONTRIBUTING.md)."
        )
        return 1
    print(f"OK: {', '.join(image.name for image in IMAGES)} installieren nur hash-geprüfte Pakete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
