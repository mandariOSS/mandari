# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gleichstand der Seiten im neuen Erscheinungsbild des Sitzungsdienstes, Teil 2b (Issue #944).

Wie ``test_neues_design._aktionen`` liest der Vergleich, was eine Seite anbietet: Links, Formulare mit Methode, Ziel und
Feldern, htmx-Aufrufe, Adressen für Skripte. Darüber hinaus gehören zur Aktion:

- feste Werte (versteckte Felder, Knöpfe mit ``name``/``value``) wie bisher, dazu die **Optionen** von Auswahlfeldern
  (``<select>``) und die Werte von Options- und Ankreuzfeldern als ``name∈a|b|c``

und je Aktion die Merkmale, die die neue Seite mindestens genauso tragen muss:

- Rückfrage: ``data-confirm``, ``data-confirm-*`` und ``hx-confirm`` mit ihrem Text (Leerraum zusammengefasst)
- ``data-prompt`` mit dem Feld, in das die Antwort geht
- ``data-submit-once`` am Formular (nur einmal absenden)
- ``disabled`` an einem Absendeknopf des Formulars bzw. am Element selbst (gesperrte Aktion)
- htmx: ``hx-target``, ``hx-include``, ``hx-swap`` mit Wert

Eine Aktion der bisherigen Seite gilt als erhalten, wenn die neue Seite dieselbe Aktion mit mindestens denselben
Merkmalen anbietet. In der **Umkehrrichtung** darf die neue Seite keine Aktion anbieten, die die bisherige nicht hatte –
außer Links, die für die Rolle erreichbar sind (Reiter, Unterpunkte, Weg zurück), und begründeten Ausnahmen. So kommt
weder eine Funktion hinzu, die bisher fehlte, noch ein Knopf, der auf eine verbotene Seite führt.

Kein Testmodul (kein ``test_``-Präfix); die Testdateien von Teil 2b importieren daraus.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from django.test import Client

from apps.session.models import SessionRole, SessionTenant
from apps.session.tests.test_neues_design import _HX, _SKRIPT_ADRESSE, Aktion, _Aktionen, _client, _hx_werte, _intern

#: Merkmal einer Aktion, etwa ``data-confirm=Wirklich löschen?`` oder ``hx-target=closest li``
Merkmal = str
#: Aktionen einer Seite mit ihren Merkmalen
Aktionen = dict[Aktion, set[Merkmal]]

_LEER = re.compile(r"\s+")
#: Merkmale mit Wert (Text der Rückfrage, Ziel von htmx …)
_MIT_WERT = ("data-confirm", "hx-confirm", "hx-target", "hx-include", "hx-swap")


def rollen() -> list[str]:
    """Die sechs Standardrollen eines Mandanten (SessionRole.default_role_definitions)."""
    return [definition["name"] for definition in SessionRole.default_role_definitions().values()]


def _merkmale(attrs: dict[str, str]) -> set[Merkmal]:
    """Merkmale eines Elements: Rückfrage mit Text, Abfrage, einmal absenden, gesperrt, htmx-Ziel."""
    merkmale: set[Merkmal] = set()
    for name, wert in attrs.items():
        if name.startswith(_MIT_WERT):
            merkmale.add(f"{name}={_LEER.sub(' ', wert).strip()}")
        elif name == "data-prompt-field":
            merkmale.add(f"data-prompt={wert}")
        elif name == "data-prompt":
            merkmale.add(f"data-prompt-frage={_LEER.sub(' ', wert).strip()}")
        elif name == "data-submit-once":
            merkmale.add(name)
    if "disabled" in attrs:
        merkmale.add("disabled")
    return merkmale


def _gleich(aktion: Aktion) -> Aktion:
    """Anfrageparameter in fester Reihenfolge (``{% querystring %}`` ordnet sie anders als handgebaute Links)."""
    methode, adresse, felder = aktion
    teile = urlsplit(adresse)
    if not teile.query:
        return aktion
    parameter = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(teile.query, keep_blank_values=True)))
    return methode, f"{teile.path}?{parameter}", felder


class _AktionenMitMerkmalen(_Aktionen):
    """``_Aktionen`` mit Optionen der Auswahlfelder und den Merkmalen je Aktion (vereinigt über gleiche Aktionen)."""

    def __init__(self) -> None:
        super().__init__()
        self.merkmale: Aktionen = {}
        self._formular_merkmale: set[Merkmal] = set()
        self._optionen: dict[str, set[str]] = {}
        self._auswahl: str | None = None
        self._option: list[str] | None = None

    def _merke(self, aktion: Aktion, merkmale: Iterable[Merkmal]) -> None:
        self.merkmale.setdefault(_gleich(aktion), set()).update(merkmale)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        im_formular = self._formular is not None
        super().handle_starttag(tag, attrs)
        merkmale = _merkmale(a)
        hx = next(((verb.upper(), a[f"hx-{verb}"]) for verb in _HX if a.get(f"hx-{verb}")), None)
        if tag == "a" and _intern(a.get("href", "")):
            self._merke(("GET", a["href"], ()), merkmale)
        if hx and tag != "form" and _intern(hx[1]):
            self._merke((hx[0], hx[1], _hx_werte(a.get("hx-vals", ""))), merkmale)
        for name, wert in a.items():
            if name.startswith("data-") and wert.startswith("/session/"):
                self._merke(("SKRIPT", wert, ()), merkmale)
        if tag == "form":
            self._formular_merkmale = set(merkmale)
            self._optionen = {}
        elif not im_formular:
            return
        elif tag == "select" and a.get("name"):
            self._auswahl = a["name"]
            self._optionen.setdefault(a["name"], set())
        elif tag == "option" and self._auswahl is not None:
            if "value" in a:
                self._optionen[self._auswahl].add(a["value"])
            else:
                self._option = []
        elif tag == "input" and a.get("type", "").lower() in ("radio", "checkbox") and a.get("name"):
            self._optionen.setdefault(a["name"], set()).add(a.get("value", "on"))
        if im_formular and tag in ("button", "input"):
            typ = a.get("type", "submit" if tag == "button" else "text").lower()
            if typ in ("submit", "image"):
                # Rückfrage oder Sperre am Absendeknopf gilt für das Formular
                self._formular_merkmale |= merkmale

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None and self._auswahl is not None:
            self._optionen[self._auswahl].add(_LEER.sub(" ", "".join(self._option)).strip())
            self._option = None
        elif tag == "select":
            self._auswahl = None
        elif tag == "form" and self._formular:
            for name, werte in self._optionen.items():
                if name in self._felder:
                    self._felder.discard(name)
                    self._felder.add(f"{name}∈{'|'.join(sorted(werte))}")
            self._merke((*self._formular, tuple(sorted(self._felder))), self._formular_merkmale)
            self._formular_merkmale = set()
            self._optionen = {}
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option.append(data)
        if self._im_skript:
            for adresse in _SKRIPT_ADRESSE.findall(data):
                self._merke(("SKRIPT", adresse, ()), ())
        super().handle_data(data)


def aktionen(html: str | bytes | Any) -> Aktionen:
    """Aktionen einer Antwort (oder von HTML) mit ihren Merkmalen; Anfrageparameter ohne Reihenfolge."""
    if hasattr(html, "content"):
        html = html.content
    if isinstance(html, bytes):
        html = html.decode()
    leser = _AktionenMitMerkmalen()
    leser.feed(html)
    leser.close()
    return leser.merkmale


def fehlende(alt: Aktionen, neu: Aktionen, ausnahmen: Iterable[Aktion | tuple[Aktion, Merkmal]] = ()) -> list[str]:
    """
    Was die bisherige Seite anbietet und die neue nicht – oder nicht mit denselben Merkmalen. ``ausnahmen`` enthält
    Aktionen, die entfallen dürfen, oder Paare (Aktion, Merkmal) für ein Merkmal, das sich begründet ändert (etwa
    ``hx-target`` einer Tabellenzeile, die jetzt ein Listeneintrag ist).
    """
    frei_aktion = {_gleich(a) for a in ausnahmen if len(a) == 3}
    frei_merkmal = {(_gleich(a[0]), a[1]) for a in ausnahmen if len(a) == 2}
    fehlt: list[str] = []
    for aktion, merkmale in sorted(alt.items()):
        if aktion in frei_aktion:
            continue
        if aktion not in neu:
            fehlt.append(f"fehlt {aktion}")
            continue
        ohne = sorted(m for m in merkmale - neu[aktion] if (aktion, m) not in frei_merkmal)
        if ohne:
            fehlt.append(f"ohne {ohne}: {aktion}")
    return fehlt


def neue(alt: Aktionen, neu: Aktionen, ausnahmen: Iterable[Aktion] = ()) -> list[Aktion]:
    """Aktionen, die nur die neue Seite anbietet (Umkehrrichtung), ohne die begründeten Ausnahmen."""
    frei = {_gleich(a) for a in ausnahmen}
    return sorted(a for a in neu if a not in alt and a not in frei)


def vergleichen(
    tenant: SessionTenant,
    seiten: dict[str, str],
    *,
    rollen_liste: Iterable[str] | None = None,
    ausnahmen: Iterable[Aktion | tuple[Aktion, Merkmal]] = (),
    neu_erlaubt: Iterable[Aktion] = (),
    client_fuer: Callable[[SessionTenant, str], Client] = _client,
    vorher: Callable[[str, str], None] | None = None,
) -> tuple[list[str], Aktionen]:
    """
    Jede Seite aus ``seiten`` (Name → Adresse) für jede Rolle einmal ohne und einmal mit Schalter abrufen und in beide
    Richtungen vergleichen:

    - Jede Aktion der bisherigen Seite steht mit denselben Merkmalen auf der neuen (``fehlende``; ``ausnahmen``).
    - Die neue Seite bietet nichts an, was die bisherige nicht hatte (``neue``; ``neu_erlaubt``). Neue Links sind
      erlaubt, wenn sie für die Rolle erreichbar sind (Antwort 200 oder Weiterleitung, kein 403/404).

    Liefert die Fehlstellen („Rolle/Seite: …“) und alles, was die bisherigen Seiten gezeigt haben (für die Prüfung,
    dass der Bestand die erwarteten Aktionen wirklich zeigt). ``vorher(rolle, name)`` kann den Bestand vor jedem Abruf
    in einen bestimmten Zustand bringen.
    """
    fehlt: list[str] = []
    gesehen: Aktionen = {}
    erlaubt = list(neu_erlaubt)
    for rolle in rollen_liste or rollen():
        client = client_fuer(tenant, rolle)
        erreichbar: dict[str, bool] = {}
        for name, adresse in seiten.items():
            if vorher is not None:
                vorher(rolle, name)
            SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
            alt = client.get(adresse)
            SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
            neu = client.get(adresse)
            if alt.status_code != neu.status_code:
                fehlt.append(f"{rolle}/{name}: Status {alt.status_code} → {neu.status_code}")
                continue
            if alt.status_code != 200:
                continue
            bisher, jetzt = aktionen(alt), aktionen(neu)
            for aktion, merkmale in bisher.items():
                gesehen.setdefault(aktion, set()).update(merkmale)
            fehlt.extend(f"{rolle}/{name}: {f}" for f in fehlende(bisher, jetzt, ausnahmen))
            for methode, ziel, felder in neue(bisher, jetzt, erlaubt):
                if methode == "GET" and not felder and not ziel.startswith("?"):
                    if ziel not in erreichbar:
                        erreichbar[ziel] = client.get(ziel).status_code in (200, 301, 302)
                    if erreichbar[ziel]:
                        continue
                    fehlt.append(f"{rolle}/{name}: neuer Link nicht erreichbar {ziel}")
                    continue
                if methode == "GET" and ziel.startswith("?"):
                    continue  # Seitenwahl und Filter derselben Seite
                fehlt.append(f"{rolle}/{name}: neu {(methode, ziel, felder)}")
    return fehlt, gesehen


def reiter_ausnahmen(tenant: SessionTenant) -> set[Aktion]:
    """
    Die vier Seiten, die die bisherige Seitenleiste direkt verlinkte und die im neuen Rahmen als Reiter über ihrer Liste
    stehen (Kalender, Zu prüfen, Meine Mitzeichnungen, Umlaufbeschlüsse) – wie in #988 und Teil 2a eine Änderung des
    Rahmens, nicht der Seite.
    """
    from django.urls import reverse

    return {
        ("GET", reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug}), ())
        for name in ("meeting_calendar", "papers_review", "my_cosignatures", "circulars")
    }


def gezeigt(gesehen: Aktionen) -> set[tuple[str, str]]:
    """(Methode, Adresse) aller gesehenen Aktionen, für ``erwartet <= gezeigt(gesehen)``."""
    return {(methode, adresse) for methode, adresse, _ in gesehen}
