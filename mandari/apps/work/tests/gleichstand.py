# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gleichstand zweier Vorlagen derselben Seite (Issue #951, Teilpaket 4): Was die bisherige Vorlage anbietet, muss die
neue auch anbieten.

Verglichen wird nur der Inhalt (``<main>``, ohne den Rahmen). Gesammelt werden:

- Formulare mit Methode, Ziel, Kodierung (Dateiupload) und Feldern; feste Werte (``action=update_profile``, Radios,
  Knöpfe mit Wert) zählen mit Wert, Eingaben nur mit Namen
- Optionen jeder Auswahl (``Auswahl priority: high``)
- Links (Pfad und Namen der Parameter, auch ``mailto:`` und externe Ziele)
- htmx-Aufrufe (``HX-POST /work/…``) und Adressen in ``data-*``-Attributen
- deklarative Aktionen (``data-action``, ``data-after-request``, ``data-filter-param``) und Rückfragen
  (``data-confirm-*``, ``data-prompt*``) mit ihren Texten; ``data-submit-once`` und ``data-confirm`` am Formular zählen
  zum Formular
- Sperren: Felder mit ``disabled`` und Alpine-Bindungen von ``disabled``, ``value``, ``checked``, ``required``
- Dateifelder mit ``accept`` und ``multiple``
- Alpine: Komponenten (``x-data="name"``), Aufrufe in Ereignissen, Modelle (``x-model``) und Refs (``x-ref``)
- Server-Daten per ``json_script`` und Pflichtfelder
- Kennungen (``id``), an denen Skripte und HTMX-Ziele hängen

``fehlt(alt, neu, ausnahmen)`` nennt alles, was die neue Vorlage nicht mehr hat. Ausnahmen stehen begründet bei der
jeweiligen Seite im Test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: Aufrufe in Alpine-Ausdrücken, die keine Funktion der Seite sind
KEINE_AKTION = {
    "if",
    "confirm",
    "submit",
    "requestSubmit",
    "trim",
    "includes",
    "toLowerCase",
    "toString",
    "preventDefault",
    "stopPropagation",
    "then",
    "showModal",
    "close",
    "getElementById",
    "querySelector",
    "push",
    "splice",
    "parse",
    "stringify",
    "from",
    "readAsDataURL",
    "FileReader",
    "nextTick",
    "$nextTick",
    "$dispatch",
    "dispatch",
}
_AUFRUF = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
#: Feste Werte gehören zur Aktion („action=confirm“ ist etwas anderes als „action=decline“)
_FESTE_WERTE = {"hidden", "submit", "button", "radio"}
#: data-Attribute, deren Wert die Aktion bestimmt
_DATEN_MIT_WERT = {"data-action", "data-after-request", "data-filter-param", "data-target", "data-parent"}
#: Rückfragen: der Text gehört zur Aktion
_DATEN_TEXT = ("data-confirm", "data-prompt")
#: Attribute am Formular, die zur Aktion gehören
_FORMULAR_DATEN = ("data-submit-once", "data-confirm", "data-prompt", "data-prompt-field", "data-submit-to")
#: Alpine-Bindungen, die Funktionen sperren oder Werte setzen
_BINDUNGEN = ("disabled", "value", "checked", "required", "action", "href")
#: data-Attribute ohne Bedeutung für Funktionen
_DATEN_OHNE = {"data-lucide", "data-testid", "data-gestaltung", "data-rahmen", "data-card-label"}


def _norm(adresse: str, pfad: str) -> str:
    """Ziel ohne Parameterwerte: Pfad und sortierte Namen der Parameter, ``ansicht`` (Rückweg) zählt nicht."""
    if adresse.startswith(("mailto:", "tel:")):
        return adresse
    teile = urlsplit(adresse)
    ziel = f"{teile.scheme}://{teile.netloc}{teile.path}" if teile.netloc else (teile.path or pfad)
    namen = sorted(k for k in parse_qs(teile.query, keep_blank_values=True) if k != "ansicht")
    return f"{ziel}?{','.join(namen)}" if namen else ziel


@dataclass
class Funktionen:
    formulare: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    links: set[str] = field(default_factory=set)
    aktionen: set[str] = field(default_factory=set)
    auswahl: set[str] = field(default_factory=set)
    aufrufe: set[str] = field(default_factory=set)
    komponenten: set[str] = field(default_factory=set)
    modelle: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    json: set[str] = field(default_factory=set)
    pflicht: set[str] = field(default_factory=set)
    gesperrt: set[str] = field(default_factory=set)
    bindungen: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    def alles(self) -> set[str]:
        """Alle Einträge außer den Formularen als ``Art: Wert``."""
        return (
            {f"Link {x}" for x in self.links}
            | {f"Aktion {x}" for x in self.aktionen}
            | {f"Auswahl {x}" for x in self.auswahl}
            | {f"Aufruf {x}()" for x in self.aufrufe}
            | {f"Komponente {x}" for x in self.komponenten}
            | {f"Modell {x}" for x in self.modelle}
            | {f"Ref {x}" for x in self.refs}
            | {f"JSON {x}" for x in self.json}
            | {f"Pflichtfeld {x}" for x in self.pflicht}
            | {f"Gesperrt {x}" for x in self.gesperrt}
            | {f"Bindung {x}" for x in self.bindungen}
            | {f"Kennung {x}" for x in self.ids}
        )


class _Sammler(HTMLParser):
    def __init__(self, pfad: str, nur_main: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.pfad = pfad
        self.nur_main = nur_main
        self.ergebnis = Funktionen()
        self._in_main = not nur_main
        self._formulare: list[tuple[str, set[str]]] = []
        self._auswahl: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "main":
            self._in_main = True
            return
        if not self._in_main:
            return
        e = self.ergebnis
        for name, wert in a.items():
            if name.startswith(("@", "x-on:")) or name == "x-init":
                e.aufrufe |= {f for f in _AUFRUF.findall(wert) if f not in KEINE_AKTION}
            elif name.split(".")[0] in ("x-model", "x-model.number") and wert.strip():
                e.modelle.add(wert.strip())
            elif name.startswith("hx-") and name[3:] in ("get", "post", "put", "patch", "delete") and wert:
                e.aktionen.add(f"HX-{name[3:].upper()} {_norm(wert, self.pfad)}")
            elif name.startswith("data-") and name not in _DATEN_OHNE:
                if wert.startswith("/"):
                    e.aktionen.add(f"{name}={_norm(wert, self.pfad)}")
                elif name in _DATEN_MIT_WERT or name.startswith(_DATEN_TEXT):
                    e.aktionen.add(f"{name}={' '.join(wert.split())}")
                else:
                    e.aktionen.add(name)
            elif name.startswith((":", "x-bind:")) and name.split(":")[-1] in _BINDUNGEN:
                e.bindungen.add(f"{name.split(':')[-1]}={' '.join(wert.split())}")
        if "x-data" in a and re.fullmatch(r"[A-Za-z_]\w*", a["x-data"].strip()):
            e.komponenten.add(a["x-data"].strip())
        if a.get("x-ref"):
            e.refs.add(a["x-ref"])
        if a.get("id"):
            e.ids.add(a["id"])
        if tag == "script" and a.get("type") == "application/json" and a.get("id"):
            e.json.add(a["id"])
        if tag == "a" and a.get("href") and not a["href"].startswith(("#", "javascript:")):
            e.links.add(_norm(a["href"], self.pfad))
        if tag == "form":
            methode = a.get("method", "get").lower()
            ziel = _norm(a["action"], self.pfad) if a.get("action") else self.pfad
            for verb in ("post", "get"):
                if a.get(f"hx-{verb}"):
                    methode, ziel = f"hx-{verb}", _norm(a[f"hx-{verb}"], self.pfad)
            if "multipart" in a.get("enctype", ""):
                methode += "+datei"
            merkmale = {f"@{n}={' '.join(a[n].split())}" if a[n] else f"@{n}" for n in _FORMULAR_DATEN if n in a}
            self._formulare.append((f"{methode.upper()} {ziel}", merkmale))
        elif tag in ("input", "select", "textarea", "button") and a.get("name"):
            name = a["name"]
            if name == "csrfmiddlewaretoken":
                return
            typ = a.get("type", "submit" if tag == "button" else "text").lower()
            eintrag = f"{name}={a.get('value', '')}" if tag in ("input", "button") and typ in _FESTE_WERTE else name
            if typ == "file":
                eintrag = f"{name} (Datei {a.get('accept', '')}{' mehrere' if 'multiple' in a else ''})"
            formular = self._formulare[-1][0] if self._formulare else "ohne Formular"
            if self._formulare:
                self._formulare[-1][1].add(eintrag)
                if "required" in a:
                    e.pflicht.add(f"{formular} {name}")
            if "disabled" in a:
                e.gesperrt.add(f"{formular} {eintrag}")
            if tag == "select":
                self._auswahl = name
        elif tag == "option" and self._auswahl is not None:
            e.auswahl.add(f"{self._auswahl}: {a.get('value', '')}")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main" and self.nur_main:
            self._in_main = False
        elif tag == "form" and self._formulare:
            ziel, felder = self._formulare.pop()
            self.ergebnis.formulare.append((ziel, frozenset(felder)))
        elif tag == "select":
            self._auswahl = None


def funktionen(html: str, pfad: str, nur_main: bool = True) -> Funktionen:
    """Was eine Seite anbietet; ``nur_main=False`` für Fragmente ohne Rahmen."""
    sammler = _Sammler(pfad, nur_main)
    sammler.feed(html)
    return sammler.ergebnis


def fehlt(alt: Funktionen, neu: Funktionen, ausnahmen: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Alles, was ``alt`` anbietet und ``neu`` nicht (ohne begründete Ausnahmen)."""
    luecken = []
    for ziel, felder in alt.formulare:
        if f"Formular {ziel}" in ausnahmen:
            continue
        passend = [f for z, f in neu.formulare if z == ziel]
        weg = {x for x in felder if f"Feld {ziel} {x}" not in ausnahmen}
        if not any(weg <= f for f in passend):
            luecken.append(f"Formular {ziel} mit {sorted(weg)}")
    luecken += sorted((alt.alles() - neu.alles()) - set(ausnahmen))
    return luecken


def vergleiche(client: Any, url: str, ausnahmen: set[str] | frozenset[str] = frozenset()) -> tuple[Funktionen, Any]:
    """
    Seite einmal mit ``?ansicht=bisher`` (bisherige Vorlage) und einmal neu rendern und vergleichen. Liefert die
    Funktionen der neuen Seite und deren Antwort.
    """
    trenner = "&" if "?" in url else "?"
    bisher = client.get(f"{url}{trenner}ansicht=bisher")
    neu = client.get(url)
    assert bisher.status_code == neu.status_code == 200, (url, bisher.status_code, neu.status_code)
    assert 'data-gestaltung="neu"' in neu.content.decode(), f"{url}: neue Vorlage fehlt"
    assert 'data-gestaltung="neu"' not in bisher.content.decode(), f"{url}: ?ansicht=bisher zeigt nicht die bisherige"
    pfad = urlsplit(url).path
    alt, jetzt = funktionen(bisher.content.decode(), pfad), funktionen(neu.content.decode(), pfad)
    luecken = fehlt(alt, jetzt, ausnahmen)
    assert not luecken, f"{url}: in der neuen Vorlage fehlt\n" + "\n".join(luecken)
    return jetzt, neu
