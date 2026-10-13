# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gleichstand der Funktionen zwischen bisheriger und neuer Vorlage einer Seite (Issue #951, Teilpaket 3).

``funktionen(html, pfad)`` sammelt, was eine Seite tun kann, als Menge kurzer Kennungen:

- ``formular:post /work/…/organization/ action=update_general`` – Methode, Ziel (Pfad ohne Abfrage) und die
  unterscheidenden versteckten Felder ``action``/``section``/``channel``; ``kodierung:`` für enctype,
- ``feld:<formular>:<name>`` und ``pflicht:<formular>:<name>`` für jedes benannte Feld,
- ``wert:<formular>:<name>=<wert>`` für versteckte Felder, Checkboxen, Optionsfelder und benannte Knöpfe,
- ``gesperrt:``/``nurlesen:`` für disabled und readonly (auch Alpine ``:disabled``), ``grenze:`` für
  min/max/minlength/maxlength/step, ``accept:`` für Dateifelder, ``daten:`` für data-Attribute an Feldern,
- ``rueckfrage:<formular>:<art>`` für Rückfragen beim Absenden (``data-confirm`` am Formular, ``confirmAction`` mit
  Variante) und ``einmal:<formular>`` für ``data-submit-once``,
- ``rueckfrage:<bezug>:data-confirm-…`` für jede Angabe des Bestätigungsdialogs (Titel, Meldung, Knopftext, Variante
  mit Wert) an Formularen, ``data-post``/``data-action`` und Knöpfen: ``data-post`` fragt nur mit
  ``data-confirm-message`` nach, eine fehlende Angabe ändert also das Verhalten,
- ``link:/work/…?parameter`` für jeden Work-Link (Parameternamen ohne ``ansicht``),
- ``aktion:data-post:/work/…`` samt Variante der Rückfrage (``data-confirm-variant``), ``data-action``/
  ``data-target`` und Knöpfe mit ``data-confirm``,
- ``komponente:``, ``modell:``, ``ref:`` und ``aufruf:`` für die Alpine-Komponenten der Seite.

Die neue Vorlage muss jede Kennung der bisherigen enthalten; Ausnahmen begründet der Test der Seite. Texte von
Rückfragen dürfen sich ändern, ihre Art und Variante nicht.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

#: Aufrufe in Alpine-Ausdrücken, die keine Funktion der Seite sind
_KEINE_AKTION = {
    "if",
    "confirm",
    "submit",
    "trim",
    "includes",
    "toLowerCase",
    "toString",
    "preventDefault",
    "click",
    "then",
    "setTimeout",
    "writeText",
    "indexOf",
    "push",
    "splice",
    "showModal",
    "close",
    "getElementById",
    "querySelector",
}
_AUFRUF = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_VARIANTE = re.compile(r"variant:\s*'(\w+)'")
_UNTERSCHEIDUNG = ("action", "section", "channel")
_WERT_TYPEN = ("hidden", "checkbox", "radio")
_GRENZEN = ("min", "max", "minlength", "maxlength", "step")


class _Sammler(HTMLParser):
    def __init__(self, pfad: str) -> None:
        super().__init__(convert_charrefs=True)
        self.pfad = urlsplit(pfad).path
        self.kennungen: set[str] = set()
        self._formulare: list[dict[str, list[str]]] = []

    def _ziel(self, href: str, mit_abfrage: bool = True) -> str | None:
        if href.startswith(("#", "javascript:", "mailto:", "tel:", "http")):
            return None
        teile = urlsplit(href)
        pfad = teile.path or self.pfad
        if not pfad.startswith("/work/"):
            return None
        if not mit_abfrage:
            return pfad
        parameter = ",".join(sorted(k for k in parse_qs(teile.query, keep_blank_values=True) if k != "ansicht"))
        return f"{pfad}?{parameter}" if parameter else pfad

    def _alpine(self, a: dict[str, str]) -> None:
        for name, wert in a.items():
            if name.startswith(("@", "x-on:")) or name in ("x-init", "x-effect"):
                self.kennungen |= {f"aufruf:{f}" for f in _AUFRUF.findall(wert) if f not in _KEINE_AKTION}
        if "x-data" in a and re.fullmatch(r"[A-Za-z_]\w*", a["x-data"].strip()):
            self.kennungen.add(f"komponente:{a['x-data'].strip()}")
        self.kennungen |= {f"modell:{w.strip()}" for n, w in a.items() if n.split(".")[0] == "x-model" and w.strip()}
        if a.get("x-ref"):
            self.kennungen.add(f"ref:{a['x-ref']}")

    def _rueckfragen(self, bezug: str, a: dict[str, str]) -> None:
        """Angaben des Bestätigungsdialogs (``data-confirm-title/message/text/variant``) an einem Bezug."""
        for name in sorted(k for k in a if k.startswith("data-confirm-")):
            wert = f"={a[name]}" if name == "data-confirm-variant" else ""
            self.kennungen.add(f"rueckfrage:{bezug}:{name}{wert}")

    def _aktionen(self, tag: str, a: dict[str, str]) -> None:
        for daten in ("data-post", "data-href"):
            if a.get(daten):
                variante = a.get("data-confirm-variant", "")
                ziel = self._ziel(a[daten]) or a[daten]
                self.kennungen.add(f"aktion:{daten}:{ziel}:{variante}")
                self._rueckfragen(f"{daten}:{ziel}", a)
        if a.get("data-action"):
            self.kennungen.add(
                f"aktion:data-action:{a['data-action']}:{a.get('data-target', '')}:{a.get('data-confirm-variant', '')}"
            )
            self._rueckfragen(f"data-action:{a['data-action']}:{a.get('data-target', '')}", a)
        if tag in ("button", "a") and "data-confirm" in a:
            self.kennungen.add(f"aktion:data-confirm:{a.get('name', '')}:{a.get('value', '')}")
        if tag in ("button", "a") and not (a.get("data-post") or a.get("data-href") or a.get("data-action")):
            self._rueckfragen(f"knopf:{a.get('name', '')}:{a.get('value', '')}", a)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        self._alpine(a)
        self._aktionen(tag, a)
        if tag == "form":
            ziel = self._ziel(a.get("action") or self.pfad, mit_abfrage=False) or a.get("action", "")
            merkmale: list[str] = []
            if "data-confirm" in a:
                merkmale.append("rueckfrage:data-confirm")
            for name in sorted(k for k in a if k.startswith("data-confirm-")):
                merkmale.append(f"rueckfrage:{name}" + (f"={a[name]}" if name == "data-confirm-variant" else ""))
            absenden = a.get("@submit.prevent", "") + a.get("x-on:submit.prevent", "")
            if "confirmAction" in absenden:
                variante = _VARIANTE.search(absenden)
                merkmale.append(f"rueckfrage:confirmAction:{variante.group(1) if variante else ''}")
            if "data-submit-once" in a:
                merkmale.append("einmal")
            if a.get("enctype"):
                merkmale.append(f"kodierung:{a['enctype']}")
            self._formulare.append(
                {
                    "kopf": [a.get("method", "get").lower(), ziel],
                    "unterscheidung": [],
                    "merkmale": merkmale,
                    "felder": [],
                }
            )
        elif tag in ("input", "select", "textarea", "button") and a.get("name") and self._formulare:
            formular = self._formulare[-1]
            name = a["name"]
            if tag == "input" and a.get("type") == "hidden" and name in _UNTERSCHEIDUNG:
                formular["unterscheidung"].append(f"{name}={a.get('value', '')}")
                return
            felder = formular["felder"]
            felder.append(f"feld:{name}")
            if "required" in a:
                felder.append(f"pflicht:{name}")
            if (tag == "input" and a.get("type") in _WERT_TYPEN) or tag == "button":
                # Das CSRF-Token wechselt bei jeder Antwort; verglichen wird nur, dass es da ist
                wert = "token" if name == "csrfmiddlewaretoken" else a.get("value", "")
                felder.append(f"wert:{name}={wert}")
            if "disabled" in a:
                felder.append(f"gesperrt:{name}={a.get('value', '')}")
            if a.get(":disabled") or a.get("x-bind:disabled"):
                felder.append(f"gesperrt:{name}:alpine")
            if "readonly" in a:
                felder.append(f"nurlesen:{name}")
            for grenze in _GRENZEN:
                if a.get(grenze):
                    felder.append(f"grenze:{name}:{grenze}={a[grenze]}")
            if a.get("type") == "file":
                felder.append(f"accept:{name}={a.get('accept', '')}")
            # Verhalten über Datenattribute am Feld (URL-Kürzel, Kategorie der Rechte-Matrix, Autosubmit …)
            felder.extend(f"daten:{name}:{k}={v}" for k, v in a.items() if k.startswith("data-"))
        elif tag == "button" and self._formulare and a.get("type", "submit") == "submit":
            # Sperre des Absende-Knopfs (z. B. Gast-Limit erreicht, Bestätigung per Eingabe)
            if "disabled" in a:
                self._formulare[-1]["felder"].append("gesperrt:absenden")
            if a.get(":disabled") or a.get("x-bind:disabled"):
                self._formulare[-1]["felder"].append("gesperrt:absenden:alpine")
        elif tag == "a" and a.get("href"):
            link = self._ziel(a["href"])
            if link:
                self.kennungen.add(f"link:{link}")

    def handle_endtag(self, tag: str) -> None:
        if tag != "form" or not self._formulare:
            return
        f = self._formulare.pop()
        kennung = " ".join([*f["kopf"], *sorted(f["unterscheidung"])])
        self.kennungen.add(f"formular:{kennung}")
        for merkmal in f["merkmale"]:
            art, _, rest = merkmal.partition(":")
            self.kennungen.add(f"{art}:{kennung}{':' + rest if rest else ''}")
        for feld in f["felder"]:
            art, _, rest = feld.partition(":")
            self.kennungen.add(f"{art}:{kennung}:{rest}")


def funktionen(html: str, pfad: str) -> set[str]:
    """Kennungen aller Funktionen einer gerenderten Seite (siehe Moduldokumentation)."""
    sammler = _Sammler(pfad)
    sammler.feed(html)
    sammler.close()
    return sammler.kennungen


def fehlend(alt: set[str], neu: set[str], ausnahmen: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Kennungen der bisherigen Vorlage, die in der neuen fehlen (ohne begründete Ausnahmen)."""
    return sorted(alt - neu - set(ausnahmen))
