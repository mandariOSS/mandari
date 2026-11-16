# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Architektur-Kennzahlen der Datendrehscheibe aus dem Quellstand erheben (Issue #569).

Ob die Entkopplung vorankommt, soll messbar sein (``docs/adr/20260929-schichtenmodell.md``, Abschnitt
„Prüfung“). Dieses Skript zählt nach jeder Etappe dieselben Kennzahlen; Zählregeln, Ziele und der Verlauf
stehen in ``docs/DATENDREHSCHEIBE.md#5-architektur-kennzahlen``. Es prüft nichts und schlägt bei keinem Wert fehl.

Wo es schon eine Zählung oder eine Liste gibt, übernimmt das Skript sie, statt sie nachzubauen:

- K3b aus der Liste ``SOFORT`` in ``mandari/apps/common/tests/test_mail_dienst.py``,
- K4 mit der Liste ``ERLAUBT`` aus ``mandari/apps/events/tests/test_zeitplaene_statt_faden.py``,
- K5 aus ``scripts/check_ris_access_ratchet.py`` (Direktzugriffe auf ``OParl*.objects``),
- K6 aus der Liste ``AUSNAHMEN_CASCADE`` in ``mandari/hub/ris/tests/test_loeschsemantik.py``,
- K7 aus ``scripts/check_import_linter_ratchet.py`` (Ausnahmen der Schichtverträge); dieselben Verträge
  bestimmen für K1, welchem Modul ein Modell gehört.

Selbst gezählt wird statisch am Syntaxbaum, ohne Django zu laden: K1 (Funktionen, die Daten eines fremden
Moduls schreiben), K2 (Schreibwege des Suchindex), K3a (Funktionen mit Mailversand) und K4 (Thread-Starts).
Tests, Migrationen und E2E-Tests zählen nie mit. K8 (Ereignis-Rückstand p95) kommt aus den
Produktionsmetriken und wird hier nur genannt.

Die Ausgabe hängt nur vom Quellstand ab (keine Zeit, keine Reihenfolge des Dateisystems):

    python scripts/architektur_kennzahlen.py                    # Tabelle (Markdown)
    python scripts/architektur_kennzahlen.py --json             # alle Werte mit Einzelheiten

``--verlauf --anlass "Etappe 2" --pr 1234`` gibt Kopf und Zeile für die Verlaufstabelle der Dokumentation aus,
mit dem Datum von heute und dem Stand: dem Commit auf ``dev``, auf dem der Zweig aufsetzt, und der Nummer des
Pull Requests, der die Erhebung enthält. Der Commit des Zweigs selbst taugt nicht als Beleg, weil die
Merge-Queue ihn zu einem neuen Commit zusammenfasst.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent

# Fitnessfunktionen, deren Listen übernommen werden: Pfad relativ zur Wurzel und Name der Liste.
#: Mails, die bewusst im Aufruf rausgehen: Datei → Zahl der Stellen mit ``sofort=True``.
MAIL_TEST = (PurePosixPath("mandari/apps/common/tests/test_mail_dienst.py"), "SOFORT")
#: Code, der Threads starten darf (Worker und sein Metrik-Server): Pfadanfänge relativ zu ``mandari/``.
THREAD_TEST = (PurePosixPath("mandari/apps/events/tests/test_zeitplaene_statt_faden.py"), "ERLAUBT")
#: Relationen ``<app>.<Modell>.<Feld>``, die noch mit ``CASCADE`` auf den RIS-Bestand zeigen.
CASCADE_TEST = (PurePosixPath("mandari/hub/ris/tests/test_loeschsemantik.py"), "AUSNAHMEN_CASCADE")
#: App, in der der RIS-Bestand liegt.
RIS_APP = "insight_core"

#: Schreibende Aufrufe an Manager und Querysets bzw. an einzelnen Objekten (jeweils auch asynchron).
_SCHREIBEND = ("create", "bulk_create", "bulk_update", "get_or_create", "update_or_create", "update", "delete", "save")
SCHREIBEND = frozenset(_SCHREIBEND) | {f"a{name}" for name in _SCHREIBEND}
#: Hilfsfunktionen, deren erstes Argument das Modell (oder ein Queryset) ist und die Objekte liefern.
OBJEKT_HOLER = frozenset({"get_object_or_404", "get_list_or_404", "aget_object_or_404", "aget_list_or_404"})
MODELLNAME = re.compile(r"^[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*$")

#: Versandfunktionen des Mail-Dienstes der Plattform (Issue #528), des einzigen Einstiegs für Mails.
MAIL_VERSAND = frozenset({"apps.common.mail.send", "apps.common.mail.send_template"})
#: Management-Befehle laufen als eigene Prozesse, nie in einer Anfrage.
BEFEHLE = "/management/commands/"

THREADS = frozenset({"threading.Thread", "threading.Timer"})

#: Schreibende Dokument-Operationen des Elasticsearch-Clients, erkannt an ihren Schlüsselwort-Argumenten
#: (Index-Verwaltung über ``.indices`` zählt nicht). Die Operationen auf einem Dokument heißen wie Methoden des
#: ORM und zählen deshalb nur mit ``index=`` und ``id=``.
ES_MIT_INDEX = frozenset({"index", "delete_by_query", "update_by_query"})
ES_MIT_INDEX_UND_ID = frozenset({"create", "update", "delete"})
ES_BULK_ARGUMENTE = frozenset({"operations", "body", "index"})
ES_HELFER = frozenset(
    f"elasticsearch.helpers.{name}"
    for name in ("bulk", "streaming_bulk", "parallel_bulk", "async_bulk", "async_streaming_bulk")
)
#: Dieselben Operationen über HTTP (der Ingestor spricht Elasticsearch ohne Client-Bibliothek an).
ES_HTTP_METHODEN = frozenset({"post", "put", "delete"})
ES_HTTP_PFAD = re.compile(r"/_(bulk|doc|create|update|delete_by_query|update_by_query)\b")

MODUL = "<modul>"
#: Zweig, auf dem die Erhebungen zusammenlaufen.
ZIELZWEIG = "origin/dev"


# --- Quellstand lesen -------------------------------------------------------------------------------


def _lade(name: str) -> ModuleType:
    """Lädt ein Nachbarskript aus ``scripts/``, dessen Zählung übernommen wird."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"scripts/{name}.py lässt sich nicht laden")
    modul = importlib.util.module_from_spec(spec)
    sys.modules[name] = modul
    spec.loader.exec_module(modul)
    return modul


def liste_aus_test(wurzel: Path, quelle: tuple[PurePosixPath, str]) -> ast.expr:
    """Wert einer Liste, die eine Fitnessfunktion auf Modulebene führt (als Syntaxbaum, ohne den Test zu laden)."""
    pfad, name = quelle
    for knoten in ast.parse((wurzel / pfad).read_text(encoding="utf-8"), filename=str(pfad)).body:
        if isinstance(knoten, ast.Assign) and any(isinstance(z, ast.Name) and z.id == name for z in knoten.targets):
            return knoten.value
    raise ValueError(f"{pfad}: {name} nicht gefunden")


@dataclass(frozen=True)
class Datei:
    """Eine Python-Datei des Betriebscodes."""

    pfad: str  #: relativ zum Paketverzeichnis, mit „/“
    modul: str  #: gepunkteter Modulname
    bereiche: Mapping[str, Sequence[ast.AST]]  #: Knoten je Funktion, siehe ``geltungsbereiche``
    importe: Mapping[str, str]  #: lokaler Name → Herkunft, z. B. ``mail`` → ``apps.common.mail``

    @property
    def knoten(self) -> Iterator[ast.AST]:
        """Alle Knoten der Datei (jeder genau einmal)."""
        for liste in self.bereiche.values():
            yield from liste


def zaehlt(relativ: PurePosixPath, uebersprungen: frozenset[str]) -> bool:
    """Gehört die Datei zum Betriebscode? Tests, Migrationen und versteckte Verzeichnisse zählen nicht."""
    teile = relativ.parts
    if any(teil in uebersprungen or teil.startswith(".") for teil in teile[:-1]):
        return False
    name = teile[-1]
    return not (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def modulname(relativ: PurePosixPath, praefix: str = "") -> str:
    teile = list(relativ.with_suffix("").parts)
    if teile and teile[-1] == "__init__":
        teile.pop()
    return ".".join(([praefix] if praefix else []) + teile)


def geltungsbereiche(baum: ast.Module) -> dict[str, list[ast.AST]]:
    """Knoten je Funktion (``Klasse.methode``, innere Funktionen eigens) bzw. ``<modul>`` für alles außerhalb."""
    bereiche: dict[str, list[ast.AST]] = {}

    def lauf(knoten: ast.AST, pfad: tuple[str, ...], ort: str) -> None:
        for kind in ast.iter_child_nodes(knoten):
            if isinstance(kind, ast.FunctionDef | ast.AsyncFunctionDef):
                innen = (*pfad, kind.name)
                lauf(kind, innen, ".".join(innen))
            elif isinstance(kind, ast.ClassDef):
                bereiche.setdefault(ort, []).append(kind)
                lauf(kind, (*pfad, kind.name), ort)
            else:
                bereiche.setdefault(ort, []).append(kind)
                lauf(kind, pfad, ort)

    lauf(baum, (), MODUL)
    return bereiche


def importe(alle_knoten: Iterable[ast.AST], modul: str, ist_paket: bool) -> dict[str, str]:
    """Lokale Namen aller Importe der Datei (auch der Importe in Funktionen) mit ihrer Herkunft."""
    paket = modul.split(".") if ist_paket else modul.split(".")[:-1]
    namen: dict[str, str] = {}
    for knoten in alle_knoten:
        if isinstance(knoten, ast.Import):
            for alias in knoten.names:
                if alias.asname:
                    namen[alias.asname] = alias.name
                else:
                    wurzel = alias.name.split(".")[0]
                    namen[wurzel] = wurzel
        elif isinstance(knoten, ast.ImportFrom):
            basis = paket[: len(paket) - (knoten.level - 1)] if knoten.level else []
            quelle = ".".join(basis + ([knoten.module] if knoten.module else []))
            for alias in knoten.names:
                if alias.name != "*":
                    namen[alias.asname or alias.name] = f"{quelle}.{alias.name}" if quelle else alias.name
    return namen


def lies(quelltext: str, pfad: str, praefix: str = "") -> Datei:
    """Eine Datei aus ihrem Quelltext; ``pfad`` relativ zum Paketverzeichnis, mit „/“."""
    relativ = PurePosixPath(pfad)
    modul = modulname(relativ, praefix)
    bereiche = geltungsbereiche(ast.parse(quelltext, filename=pfad))
    alle = (knoten for liste in bereiche.values() for knoten in liste)
    return Datei(pfad, modul, bereiche, importe(alle, modul, relativ.name == "__init__.py"))


def dateien(paketverzeichnis: Path, uebersprungen: frozenset[str], praefix: str = "") -> list[Datei]:
    """Alle Dateien des Betriebscodes unter ``paketverzeichnis``, sortiert nach Pfad."""
    pfade = sorted(pfad.relative_to(paketverzeichnis).as_posix() for pfad in paketverzeichnis.rglob("*.py"))
    return [
        lies((paketverzeichnis / pfad).read_text(encoding="utf-8"), pfad, praefix)
        for pfad in pfade
        if zaehlt(PurePosixPath(pfad), uebersprungen)
    ]


def herkunft(ausdruck: ast.expr, namen: Mapping[str, str]) -> str | None:
    """Herkunft eines Namens oder Attributzugriffs, z. B. ``mail.send`` → ``apps.common.mail.send``."""
    if isinstance(ausdruck, ast.Name):
        return namen.get(ausdruck.id)
    if isinstance(ausdruck, ast.Attribute):
        davor = herkunft(ausdruck.value, namen)
        return f"{davor}.{ausdruck.attr}" if davor else None
    return None


# --- K1: Funktionen, die Daten eines fremden Moduls schreiben ---------------------------------------------


def eigentuemer_einheiten(
    schichten: Sequence[tuple[bool, Mapping[str, bool]]], unabhaengig: Iterable[str]
) -> dict[str, str]:
    """Modul der Schichtverträge → Einheit, der seine Daten gehören.

    Jedes Fachmodul des Vertrags ``module-unabhaengig`` ist eine Einheit für sich; die übrigen Module einer
    Schicht (Drehscheibe, Plattform) bilden zusammen eine Einheit, weil der Schichtenvertrag sie nicht trennt.
    ``schichten`` wie von ``check_import_linter_ratchet._parse_layers``.
    """
    eigen = set(unabhaengig)
    return {
        modul: modul if modul in eigen else f"schicht-{nummer}"
        for nummer, (_, module) in enumerate(schichten, 1)
        for modul in module
    }


def bereich(modul: str, bereiche: Iterable[str]) -> str | None:
    """Das Modul der Schichtverträge (längster Treffer), zu dem ein gepunkteter Name gehört."""
    treffer = [b for b in bereiche if modul == b or modul.startswith(f"{b}.")]
    return max(treffer, key=len) if treffer else None


def _modell(ausdruck: ast.expr, namen: Mapping[str, str]) -> str | None:
    """Herkunft, wenn der Ausdruck eine Modellklasse aus einem ``models``-Modul benennt."""
    quelle = herkunft(ausdruck, namen)
    if not quelle:
        return None
    teile = quelle.split(".")
    return quelle if "models" in teile[:-1] and MODELLNAME.match(teile[-1]) else None


def _modell_der_kette(
    ausdruck: ast.expr, namen: Mapping[str, str], objekte: Mapping[str, str], manager: frozenset[str]
) -> str | None:
    """Modell, an dem eine Aufrufkette hängt: ``Modell.objects…``, ``Modell(…)…`` oder eine Variable daraus."""
    knoten: ast.expr = ausdruck
    while True:
        if isinstance(knoten, ast.Attribute):
            if knoten.attr in manager and (modell := _modell(knoten.value, namen)):
                return modell
            knoten = knoten.value
        elif isinstance(knoten, ast.Call):
            if modell := _modell(knoten.func, namen):
                return modell
            name = knoten.func.attr if isinstance(knoten.func, ast.Attribute) else getattr(knoten.func, "id", "")
            if name in OBJEKT_HOLER and knoten.args:
                return _modell(knoten.args[0], namen) or _modell_der_kette(knoten.args[0], namen, objekte, manager)
            knoten = knoten.func
        elif isinstance(knoten, ast.Subscript | ast.Await | ast.Starred):
            knoten = knoten.value
        elif isinstance(knoten, ast.Name):
            return objekte.get(knoten.id)
        else:
            return None


def _ziele(ziel: ast.expr) -> list[str]:
    """Variablen, die eine Zuweisung bindet; bei ``objekt, neu = …get_or_create(…)`` nur die erste."""
    if isinstance(ziel, ast.Name):
        return [ziel.id]
    if isinstance(ziel, ast.Tuple | ast.List) and ziel.elts and isinstance(ziel.elts[0], ast.Name):
        return [ziel.elts[0].id]
    return []


def geschriebene_modelle(knoten: Sequence[ast.AST], namen: Mapping[str, str], manager: frozenset[str]) -> set[str]:
    """Modelle (Herkunft), die die Knoten eines Geltungsbereichs schreiben."""
    objekte: dict[str, str] = {}
    for eintrag in knoten:
        wert: ast.expr | None = None
        ziele: list[ast.expr] = []
        if isinstance(eintrag, ast.Assign):
            wert, ziele = eintrag.value, list(eintrag.targets)
        elif isinstance(eintrag, ast.AnnAssign | ast.NamedExpr):
            wert, ziele = eintrag.value, [eintrag.target]
        elif isinstance(eintrag, ast.For | ast.AsyncFor | ast.comprehension):
            wert, ziele = eintrag.iter, [eintrag.target]
        if wert is None:
            continue
        if modell := _modell_der_kette(wert, namen, {}, manager):
            for ziel in ziele:
                for name in _ziele(ziel):
                    objekte[name] = modell
    geschrieben: set[str] = set()
    for eintrag in knoten:
        if (
            isinstance(eintrag, ast.Call)
            and isinstance(eintrag.func, ast.Attribute)
            and eintrag.func.attr in SCHREIBEND
        ):
            if modell := _modell_der_kette(eintrag.func.value, namen, objekte, manager):
                geschrieben.add(modell)
    return geschrieben


@dataclass(frozen=True)
class FremderSchreiber:
    """Eine Funktion, die Modelle einer anderen Einheit schreibt."""

    ort: str  #: ``pfad::funktion``
    modul: str  #: Modul der Schichtverträge, in dem die Funktion steht
    eigentuemer: tuple[str, ...]  #: Module, deren Modelle sie schreibt


def fremde_schreiber(
    quellen: Sequence[Datei], einheiten: Mapping[str, str], manager: frozenset[str]
) -> list[FremderSchreiber]:
    """Funktionen, die Modelle schreiben, die ihrer Einheit nicht gehören (``einheiten``: Modul → Einheit)."""
    gefunden: list[FremderSchreiber] = []
    for datei in quellen:
        eigen = bereich(datei.modul, einheiten)
        if eigen is None:
            continue
        for ort, knoten in datei.bereiche.items():
            eigentuemer = {
                bereich(modell, einheiten) for modell in geschriebene_modelle(knoten, datei.importe, manager)
            }
            fremd = sorted(e for e in eigentuemer if e is not None and einheiten[e] != einheiten[eigen])
            if fremd:
                gefunden.append(FremderSchreiber(f"{datei.pfad}::{ort}", eigen, tuple(fremd)))
    return gefunden


# --- K2: Schreibwege des Suchindex --------------------------------------------------------------------


def _text(ausdruck: ast.expr) -> str:
    """Fester Text eines Zeichenketten-Literals oder f-Strings (ohne die eingesetzten Werte)."""
    if isinstance(ausdruck, ast.Constant) and isinstance(ausdruck.value, str):
        return ausdruck.value
    if isinstance(ausdruck, ast.JoinedStr):
        return "".join(_text(teil) for teil in ausdruck.values)
    return ""


def suchindex_schreibstellen(datei: Datei) -> int:
    """Aufrufe in einer Datei, die Dokumente in Elasticsearch schreiben oder löschen."""
    anzahl = 0
    for knoten in datei.knoten:
        if not isinstance(knoten, ast.Call):
            continue
        if herkunft(knoten.func, datei.importe) in ES_HELFER:
            anzahl += 1
            continue
        if not isinstance(knoten.func, ast.Attribute):
            continue
        methode, empfaenger = knoten.func.attr, knoten.func.value
        argumente = {kw.arg for kw in knoten.keywords}
        if isinstance(empfaenger, ast.Attribute) and empfaenger.attr == "indices":
            continue
        if (
            (methode in ES_MIT_INDEX and "index" in argumente)
            or (methode in ES_MIT_INDEX_UND_ID and {"index", "id"} <= argumente)
            or (methode == "bulk" and argumente & ES_BULK_ARGUMENTE)
        ):
            anzahl += 1
        elif methode in ES_HTTP_METHODEN and knoten.args and ES_HTTP_PFAD.search(_text(knoten.args[0])):
            anzahl += 1
    return anzahl


def suchindex_schreiber(quellen: Sequence[tuple[str, Sequence[Datei]]]) -> dict[str, int]:
    """Schreibstellen je Datei (``<verzeichnis>/<pfad>``), nur Dateien mit mindestens einer."""
    gefunden: dict[str, int] = {}
    for verzeichnis, dateiliste in quellen:
        for datei in dateiliste:
            if anzahl := suchindex_schreibstellen(datei):
                gefunden[f"{verzeichnis}/{datei.pfad}"] = anzahl
    return gefunden


# --- K3: Mailversand ---------------------------------------------------------------------------------


def mail_funktionen(quellen: Sequence[Datei]) -> list[str]:
    """Funktionen (``pfad::funktion``), die den Mail-Dienst zum Versand aufrufen; ohne Management-Befehle."""
    gefunden: list[str] = []
    for datei in quellen:
        if BEFEHLE in f"/{datei.pfad}":
            continue
        for ort, knoten in datei.bereiche.items():
            if any(isinstance(k, ast.Call) and herkunft(k.func, datei.importe) in MAIL_VERSAND for k in knoten):
                gefunden.append(f"{datei.pfad}::{ort}")
    return gefunden


def sofort_stellen(wurzel: Path) -> dict[str, int]:
    """Stellen mit ``sofort=True`` je Datei, wie sie die Fitnessfunktion des Mail-Dienstes festschreibt."""
    liste = liste_aus_test(wurzel, MAIL_TEST)
    if not isinstance(liste, ast.Dict):
        raise ValueError(f"{MAIL_TEST[0]}: {MAIL_TEST[1]} ist kein Wörterbuch")
    stellen: dict[str, int] = {}
    for schluessel, wert in zip(liste.keys, liste.values, strict=True):
        if isinstance(schluessel, ast.Call) and schluessel.args:  # Path("apps/…")
            schluessel = schluessel.args[0]
        if schluessel is not None:
            stellen[str(ast.literal_eval(schluessel))] = int(ast.literal_eval(wert))
    return dict(sorted(stellen.items()))


# --- K4: Hintergrund-Threads ---------------------------------------------------------------------------


def worker_pfade(wurzel: Path) -> tuple[str, ...]:
    """Pfadanfänge des Codes, der Threads starten darf, wie sie die Fitnessfunktion der Zeitpläne festschreibt."""
    return tuple(str(pfad) for pfad in ast.literal_eval(liste_aus_test(wurzel, THREAD_TEST)))


def thread_starts(quellen: Sequence[Datei], worker: tuple[str, ...]) -> dict[str, dict[str, int]]:
    """Thread-Starts und abgeleitete Thread-Klassen je Datei, getrennt nach Worker und dem übrigen Code."""
    gefunden: dict[str, dict[str, int]] = {"ausserhalb_des_workers": {}, "worker": {}}
    for datei in quellen:
        anzahl = 0
        for knoten in datei.knoten:
            if isinstance(knoten, ast.Call) and herkunft(knoten.func, datei.importe) in THREADS:
                anzahl += 1
            elif isinstance(knoten, ast.ClassDef):
                anzahl += sum(1 for basis in knoten.bases if herkunft(basis, datei.importe) in THREADS)
        if anzahl:
            gefunden["worker" if datei.pfad.startswith(worker) else "ausserhalb_des_workers"][datei.pfad] = anzahl
    return gefunden


# --- K6: CASCADE auf den RIS-Bestand ---------------------------------------------------------------------


def cascade_ausnahmen(wurzel: Path) -> list[str]:
    """Relationen, die noch mit ``CASCADE`` auf den RIS-Bestand zeigen, laut Fitnessfunktion der Löschsemantik."""
    liste = liste_aus_test(wurzel, CASCADE_TEST)
    if isinstance(liste, ast.Call):  # frozenset({...}) bzw. frozenset()
        if not liste.args:
            return []
        liste = liste.args[0]
    return sorted(str(eintrag) for eintrag in ast.literal_eval(liste))


# --- Erhebung -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kennzahl:
    nummer: str
    name: str
    wert: int | None  #: ``None``: nicht aus dem Quellstand erhebbar
    ziel: str
    quelle: str
    einzelheiten: dict[str, Any] = field(default_factory=dict)

    @property
    def anzeige(self) -> str:
        return "–" if self.wert is None else str(self.wert)


def erheben(wurzel: Path | None = None) -> list[Kennzahl]:
    """Alle Kennzahlen für den Quellstand unter ``wurzel`` (Vorgabe: dieses Repository)."""
    wurzel = wurzel or ROOT
    projekt = wurzel / "mandari"
    schichten = _lade("check_import_linter_ratchet")
    ris = _lade("check_ris_access_ratchet")
    uebersprungen = frozenset(ris.SKIPPED_DIRS)

    linter = schichten.load_config(projekt / "pyproject.toml")
    vertraege = {vertrag.get("id"): vertrag for vertrag in linter.get("contracts", [])}
    einheiten = eigentuemer_einheiten(
        schichten._parse_layers(vertraege.get("schichten", {}).get("layers", [])),
        vertraege.get("module-unabhaengig", {}).get("modules", []),
    )
    ausnahmen, _ = schichten.exceptions(linter)
    alle_ausnahmen = {eintrag for liste in ausnahmen.values() for eintrag in liste}

    anwendung = dateien(projekt, uebersprungen)
    ingestor = wurzel / "ingestor" / "src"
    abgleich = dateien(ingestor, uebersprungen, "src") if ingestor.is_dir() else []

    schreiber = fremde_schreiber(anwendung, einheiten, frozenset(ris.MANAGERS))
    richtungen = Counter(f"{s.modul} -> {eigentuemer}" for s in schreiber for eigentuemer in s.eigentuemer)
    index = suchindex_schreiber([("mandari", anwendung), ("ingestor/src", abgleich)])
    mails = mail_funktionen(anwendung)
    sofort = sofort_stellen(wurzel)
    threads = thread_starts(anwendung, worker_pfade(wurzel))
    zugriffe: dict[str, int] = ris.measure(projekt)
    cascade = cascade_ausnahmen(wurzel)
    cascade_bestand = [eintrag for eintrag in cascade if eintrag.split(".")[0] == RIS_APP]
    cascade_fremd = [eintrag for eintrag in cascade if eintrag.split(".")[0] != RIS_APP]

    skript = "`scripts/architektur_kennzahlen.py`"
    cascade_quelle = f"`{CASCADE_TEST[1]}` in `{CASCADE_TEST[0]}`"
    return [
        Kennzahl(
            "K1",
            "Funktionen, die Daten eines fremden Moduls schreiben",
            len(schreiber),
            "0",
            skript,
            {
                "je_richtung": dict(sorted(richtungen.items())),
                "funktionen": {s.ort: list(s.eigentuemer) for s in schreiber},
            },
        ),
        Kennzahl(
            "K2a",
            "Schreibwege des Suchindex (Dateien mit Schreibzugriff)",
            len(index),
            "1",
            skript,
            {"dateien": index},
        ),
        Kennzahl("K2b", "Schreibstellen des Suchindex (Aufrufe in diesen Dateien)", sum(index.values()), "–", skript),
        Kennzahl(
            "K3a",
            "Funktionen, die Mails über den Mail-Dienst senden",
            len(mails),
            "–",
            skript,
            {"funktionen": mails},
        ),
        Kennzahl(
            "K3b",
            "Mails, die immer im Aufruf rausgehen (Stellen mit `sofort=True`)",
            sum(sofort.values()),
            "–",
            f"`{MAIL_TEST[1]}` in `{MAIL_TEST[0]}`",
            {"dateien": sofort},
        ),
        Kennzahl(
            "K4",
            "Hintergrund-Threads im Webprozess (Thread-Starts außerhalb des Workers)",
            sum(threads["ausserhalb_des_workers"].values()),
            "0",
            f"{skript} mit `{THREAD_TEST[1]}` aus `{THREAD_TEST[0]}`",
            {"dateien": threads["ausserhalb_des_workers"], "worker": threads["worker"]},
        ),
        Kennzahl(
            "K5",
            "`OParl*.objects` außerhalb von `hub` und `insight_core`",
            sum(zugriffe.values()),
            "0",
            "`scripts/check_ris_access_ratchet.py`",
            {"dateien": len(zugriffe)},
        ),
        Kennzahl(
            "K6a",
            f"`CASCADE` von Fachmodulen auf den RIS-Bestand (alle Apps außer `{RIS_APP}`)",
            len(cascade_fremd),
            "0",
            cascade_quelle,
            {"relationen": cascade_fremd},
        ),
        Kennzahl(
            "K6b",
            f"`CASCADE` auf den RIS-Bestand aus `{RIS_APP}` selbst (Bürgerportal, Geodaten)",
            len(cascade_bestand),
            "0",
            cascade_quelle,
            {"relationen": cascade_bestand},
        ),
        Kennzahl(
            "K7",
            "import-linter-Ausnahmen (verschiedene Importe über alle Verträge)",
            len(alle_ausnahmen),
            "0",
            "`scripts/check_import_linter_ratchet.py`",
            {"je_vertrag": {vertrag: len(liste) for vertrag, liste in sorted(ausnahmen.items())}},
        ),
        Kennzahl(
            "K8",
            "Ereignis-Rückstand p95",
            None,
            "≤ 5 s",
            "Produktionsmetrik `mandari_events_lag_seconds` (`docs/MONITORING.md`), nicht aus dem Quellstand",
        ),
    ]


# --- Ausgabe ------------------------------------------------------------------------------------------


def als_markdown(kennzahlen: Sequence[Kennzahl]) -> str:
    zeilen = ["| Nr. | Kennzahl | Wert | Ziel | Quelle |", "|---|---|---:|---:|---|"]
    zeilen += [f"| {k.nummer} | {k.name} | {k.anzeige} | {k.ziel} | {k.quelle} |" for k in kennzahlen]
    return "\n".join(zeilen) + "\n"


def als_json(kennzahlen: Sequence[Kennzahl]) -> str:
    daten = [
        {
            "nr": k.nummer,
            "kennzahl": k.name,
            "wert": k.wert,
            "ziel": k.ziel,
            "quelle": k.quelle,
            "einzelheiten": k.einzelheiten,
        }
        for k in kennzahlen
    ]
    return json.dumps({"kennzahlen": daten}, indent=2, ensure_ascii=False) + "\n"


def verlauf_kopf(kennzahlen: Sequence[Kennzahl]) -> str:
    kopf = ["Datum", "Stand", "Anlass", *(k.nummer for k in kennzahlen)]
    return "| " + " | ".join(kopf) + " |\n|---|---|---|" + "---:|" * len(kennzahlen) + "\n"


def verlauf_zeile(kennzahlen: Sequence[Kennzahl], datum: str, stand: str, anlass: str) -> str:
    return "| " + " | ".join([datum, stand, anlass, *(k.anzeige for k in kennzahlen)]) + " |\n"


def stand_text(commit: str, pr: int | None) -> str:
    """Erhobener Stand: Commit auf ``dev``, dazu der Pull Request, dessen Änderungen mitgezählt sind."""
    return f"`{commit}`" if pr is None else f"`{commit}` + #{pr}"


def basis_commit(wurzel: Path) -> str:
    """Commit auf ``dev``, auf dem der Arbeitsstand aufsetzt; auf ``dev`` selbst und ohne ``origin/dev``: ``HEAD``."""
    for befehl in (["git", "merge-base", "HEAD", ZIELZWEIG], ["git", "rev-parse", "HEAD"]):
        try:
            lauf = subprocess.run(befehl, cwd=wurzel, capture_output=True, text=True, check=False)
        except OSError:
            break
        if lauf.returncode == 0 and lauf.stdout.strip():
            return lauf.stdout.strip()[:8]
    return "unbekannt"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Architektur-Kennzahlen der Datendrehscheibe (Issue #569)")
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true", help="alle Werte mit Einzelheiten als JSON")
    form.add_argument("--verlauf", action="store_true", help="Kopf und Zeile für die Verlaufstabelle der Doku")
    parser.add_argument("--anlass", default="", help="Anlass der Erhebung für --verlauf, z. B. „Etappe 2“")
    parser.add_argument("--pr", type=int, default=None, help="Nummer des Pull Requests der Erhebung, für --verlauf")
    optionen = parser.parse_args(argv)

    kennzahlen = erheben()
    if optionen.json:
        ausgabe = als_json(kennzahlen)
    elif optionen.verlauf:
        heute = datetime.date.today().isoformat()
        stand = stand_text(basis_commit(ROOT), optionen.pr)
        ausgabe = verlauf_kopf(kennzahlen) + verlauf_zeile(kennzahlen, heute, stand, optionen.anlass)
    else:
        ausgabe = als_markdown(kennzahlen)
    sys.stdout.buffer.write(ausgabe.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
