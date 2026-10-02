# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abnehmer-Durchlauf für Änderungsfeed und Snapshot einer Kommune (Issue #707).

Liest eine Kommune so, wie es ein Abnehmer nach ``docs/OPARL_API.md`` („So liest ein Abnehmer“) tut, und prüft
dabei, was die Schnittstelle zusagt:

1. Der Body nennt ``mandari:changes`` und ``mandari:snapshot``.
2. ``HEAD`` auf den Snapshot liefert den Cursor in der Kopfzeile ``Snapshot-Cursor``.
3. Der Snapshot: erste Zeile mit ``snapshot_cursor``, ``changes`` und ``objects``; danach genau ``objects``
   Zeilen, jede ein Objekt mit ``id`` und ``type``. Gemessen werden Zeit bis zum ersten Byte (so lange baut der
   Server), Gesamtzeit und Größe.
4. Der Feed ab dem Cursor des Snapshots (mit ``--von-vorn`` von Beginn des Journals, dann mit allen Einträgen
   seit dem Einschalten der Erzeuger), Seite für Seite über ``links.next``, bis eine Seite leer ist. Jede
   Seite trägt ``cursor`` und ``links.next``; jeder Eintrag ``cursor``, ``operation``, ``type``, ``id`` und
   ``modified``, ein ``delete`` zusätzlich ``reason``.
5. Eine Stichprobe der Adressen aus Snapshot und Feed ist abrufbar: ``upsert`` und Snapshot-Objekte mit ``200``,
   ``delete`` als gekürztes Objekt (``deleted: true``) oder mit ``404``/``410``.
6. Die letzte Seite noch einmal mit ``If-None-Match``: ``304``, solange sich nichts geändert hat.

Nur lesend, nur gegen die eigene Installation. Zwischen zwei Abrufen liegt eine Pause (Vorgabe 0,6 s), damit
der Durchlauf unter der Ratenbegrenzung der Schnittstelle bleibt (``OPARL_API_RATE_LIMIT``, 120 je Minute).

    python scripts/feed_abnehmer.py https://<installation>/oparl/v1/body/<uuid>
    python scripts/feed_abnehmer.py https://<installation>/oparl/v1/body/<uuid> --stichprobe 20 --seiten 50

Exit-Code 0: alles zugesagte gilt; 1: mindestens ein Befund (Liste am Ende).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any

USER_AGENT = "mandari-feed-abnehmer/1 (Abnehmer-Durchlauf, Issue #707)"
BLOCK = 2**16


@dataclass
class Antwort:
    """Status, Kopfzeilen (Namen klein) und Inhalt eines Abrufs; ``erstes_byte``: Sekunden bis zum ersten Byte."""

    status: int
    kopf: dict[str, str]
    inhalt: IO[bytes]
    erstes_byte: float = 0.0


#: Ein Abruf: (Methode, Adresse, zusätzliche Kopfzeilen) -> Antwort. Austauschbar für Tests.
Abruf = Callable[[str, str, dict[str, str]], Antwort]


def http_abruf(zeitlimit: float) -> Abruf:
    """Abruf über HTTP(S); der Inhalt landet in einer temporären Datei (Snapshots sind groß)."""

    def abrufen(methode: str, adresse: str, kopf: dict[str, str]) -> Antwort:
        anfrage = urllib.request.Request(adresse, method=methode, headers={"User-Agent": USER_AGENT, **kopf})
        start = time.monotonic()
        try:
            antwort = urllib.request.urlopen(anfrage, timeout=zeitlimit)  # noqa: S310 – Adresse gibt der Aufrufer
        except urllib.error.HTTPError as fehler:
            antwort = fehler
        datei = tempfile.TemporaryFile()  # noqa: SIM115 – der Aufrufer liest und schließt sie
        erstes = antwort.read(BLOCK)
        erstes_byte = time.monotonic() - start
        while erstes:
            datei.write(erstes)
            erstes = antwort.read(BLOCK)
        datei.seek(0)
        kopfzeilen = {name.lower(): wert for name, wert in antwort.headers.items()}
        return Antwort(antwort.status, kopfzeilen, datei, erstes_byte)

    return abrufen


@dataclass
class Bericht:
    befunde: list[str] = field(default_factory=list)
    zahlen: dict[str, Any] = field(default_factory=dict)

    def befund(self, text: str) -> None:
        self.befunde.append(text)


def _json(antwort: Antwort) -> Any:
    try:
        return json.loads(antwort.inhalt.read().decode("utf-8"))
    finally:
        antwort.inhalt.close()


def _zeilen(datei: IO[bytes]) -> Iterator[bytes]:
    for zeile in datei:
        if zeile.strip():
            yield zeile


class Durchlauf:
    def __init__(
        self, abruf: Abruf, *, pause: float = 0.6, stichprobe: int = 30, seiten: int = 200, von_vorn: bool = False
    ) -> None:
        self.abruf = abruf
        self.pause = pause
        self.stichprobe = stichprobe
        self.seiten = seiten
        self.von_vorn = von_vorn
        self.bericht = Bericht()
        self._erster = True

    def _holen(self, methode: str, adresse: str, kopf: dict[str, str] | None = None) -> Antwort:
        if not self._erster and self.pause:
            time.sleep(self.pause)
        self._erster = False
        return self.abruf(methode, adresse, kopf or {})

    def lauf(self, body_adresse: str) -> Bericht:
        b = self.bericht
        body = self._holen("GET", body_adresse)
        if body.status != 200:
            b.befund(f"Body: Status {body.status}")
            return b
        daten = _json(body)
        feed, snap = daten.get("mandari:changes"), daten.get("mandari:snapshot")
        if not feed or not snap:
            b.befund("Body nennt mandari:changes oder mandari:snapshot nicht (Feed aus oder Kommune nicht gelistet)")
            return b

        kopf = self._holen("HEAD", snap)
        kopf.inhalt.close()
        if kopf.status != 200 or not kopf.kopf.get("snapshot-cursor"):
            b.befund(f"HEAD Snapshot: Status {kopf.status}, Snapshot-Cursor {'fehlt' if kopf.status == 200 else '-'}")

        weiter, adressen = self._snapshot(snap)
        if weiter is None:
            return b
        # Von vorn liest der Feed alles, was das Journal zur Kommune enthält (solange nie aufgeräumt wurde)
        eintraege = self._feed(feed if self.von_vorn else weiter)
        self._stichprobe(adressen, eintraege)
        return b

    def _snapshot(self, adresse: str) -> tuple[str | None, list[str]]:
        b = self.bericht
        start = time.monotonic()
        antwort = self._holen("GET", adresse)
        if antwort.status != 200:
            b.befund(f"Snapshot: Status {antwort.status}")
            antwort.inhalt.close()
            return None, []
        b.zahlen["snapshot_erstes_byte_s"] = round(antwort.erstes_byte, 1)
        b.zahlen["snapshot_gesamt_s"] = round(time.monotonic() - start, 1)
        adressen: list[str] = []
        with antwort.inhalt as datei:
            datei.seek(0, 2)
            b.zahlen["snapshot_mb"] = round(datei.tell() / 1024 / 1024, 1)
            datei.seek(0)
            zeilen = _zeilen(datei)
            try:
                kopf = json.loads(next(zeilen))
            except (StopIteration, ValueError):
                b.befund("Snapshot: erste Zeile fehlt oder ist kein JSON")
                return None, []
            for name in ("snapshot_cursor", "changes", "objects"):
                if name not in kopf:
                    b.befund(f"Snapshot: Kopfzeile ohne {name}")
            if antwort.kopf.get("snapshot-cursor") != kopf.get("snapshot_cursor"):
                b.befund("Snapshot: Cursor in Kopfzeile und erster Zeile verschieden")
            anzahl = 0
            for zeile in zeilen:
                anzahl += 1
                try:
                    objekt = json.loads(zeile)
                except ValueError:
                    b.befund(f"Snapshot: Zeile {anzahl + 1} ist kein JSON")
                    continue
                if not objekt.get("id") or not objekt.get("type"):
                    b.befund(f"Snapshot: Zeile {anzahl + 1} ohne id oder type")
                    continue
                adressen.append(objekt["id"])
        b.zahlen["snapshot_objekte"] = anzahl
        if anzahl != kopf.get("objects"):
            b.befund(f"Snapshot: {anzahl} Zeilen, angekündigt {kopf.get('objects')} (abgebrochen?)")
        return kopf.get("changes"), adressen

    def _feed(self, adresse: str) -> list[dict[str, Any]]:
        b = self.bericht
        eintraege: list[dict[str, Any]] = []
        seiten = 0
        letzte: Antwort | None = None
        letzte_adresse = adresse
        while seiten < self.seiten:
            antwort = self._holen("GET", adresse)
            seiten += 1
            if antwort.status != 200:
                b.befund(f"Feed: Status {antwort.status} auf Seite {seiten}")
                antwort.inhalt.close()
                return eintraege
            letzte, letzte_adresse = antwort, adresse
            seite = _json(antwort)
            if not seite.get("cursor") or not seite.get("links", {}).get("next"):
                b.befund(f"Feed: Seite {seiten} ohne cursor oder links.next")
                return eintraege
            for eintrag in seite.get("data", []):
                fehlt = [n for n in ("cursor", "operation", "type", "id", "modified") if not eintrag.get(n)]
                if eintrag.get("operation") in ("delete", "redact") and not eintrag.get("reason"):
                    fehlt.append("reason")
                if fehlt:
                    b.befund(f"Feed: Eintrag ohne {', '.join(fehlt)}")
                eintraege.append(eintrag)
            if not seite.get("data"):
                break
            adresse = seite["links"]["next"]
        else:
            b.befund(f"Feed: nach {self.seiten} Seiten noch nicht aktuell (Grenze --seiten)")
        b.zahlen["feed_seiten"] = seiten
        b.zahlen["feed_eintraege"] = dict(Counter(e.get("operation") for e in eintraege))
        if letzte is not None and letzte.kopf.get("etag"):
            erneut = self._holen("GET", letzte_adresse, {"If-None-Match": letzte.kopf["etag"]})
            erneut.inhalt.close()
            if erneut.status not in (200, 304):
                b.befund(f"Feed: erneuter Abruf mit If-None-Match ergab {erneut.status}")
            b.zahlen["feed_304"] = erneut.status == 304
        return eintraege

    def _stichprobe(self, adressen: list[str], eintraege: list[dict[str, Any]]) -> None:
        b = self.bericht
        zufall = random.Random(707)
        proben = [(adresse, "upsert") for adresse in zufall.sample(adressen, min(self.stichprobe, len(adressen)))]
        letzte = {e["id"]: e["operation"] for e in eintraege if e.get("id")}
        proben += zufall.sample(sorted(letzte.items()), min(self.stichprobe, len(letzte)))
        geprueft = 0
        for adresse, operation in proben:
            antwort = self._holen("GET", adresse)
            geprueft += 1
            if operation == "upsert":
                if antwort.status != 200:
                    b.befund(f"Objekt {adresse}: Status {antwort.status}")
                antwort.inhalt.close()
                continue
            if antwort.status == 200:
                if _json(antwort).get("deleted") is not True:
                    b.befund(f"Objekt {adresse}: im Feed gelöscht, ausgeliefert ohne deleted: true")
            elif antwort.status in (404, 410):
                antwort.inhalt.close()
            else:
                antwort.inhalt.close()
                b.befund(f"Objekt {adresse}: Status {antwort.status}")
        b.zahlen["stichprobe_geprueft"] = geprueft


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Abnehmer-Durchlauf für Änderungsfeed und Snapshot einer Kommune")
    parser.add_argument("body", help="Adresse des Body, z. B. https://<installation>/oparl/v1/body/<uuid>")
    parser.add_argument("--stichprobe", type=int, default=30, help="Adressen je Quelle (Snapshot, Feed), Vorgabe 30")
    parser.add_argument("--seiten", type=int, default=200, help="Höchstens so viele Seiten des Feeds, Vorgabe 200")
    parser.add_argument("--pause", type=float, default=0.6, help="Sekunden zwischen zwei Abrufen, Vorgabe 0,6")
    parser.add_argument("--zeitlimit", type=float, default=900, help="Zeitlimit je Abruf in Sekunden, Vorgabe 900")
    parser.add_argument(
        "--von-vorn", action="store_true", help="Feed von vorn statt ab dem Cursor des Snapshots lesen (alle Einträge)"
    )
    args = parser.parse_args(argv)

    durchlauf = Durchlauf(
        http_abruf(args.zeitlimit),
        pause=args.pause,
        stichprobe=args.stichprobe,
        seiten=args.seiten,
        von_vorn=args.von_vorn,
    )
    bericht = durchlauf.lauf(args.body)
    for name, wert in bericht.zahlen.items():
        print(f"{name}: {wert}")
    if bericht.befunde:
        print(f"\n{len(bericht.befunde)} Befund(e):")
        for befund in bericht.befunde:
            print(f"  - {befund}")
        return 1
    print("\nOK: Snapshot und Feed wie zugesagt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
