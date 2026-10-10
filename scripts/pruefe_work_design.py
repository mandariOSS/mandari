# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfskript für das neue Design in Work (Issue #884): alle Bereiche, alle Rollen, alle Breiten, Schalter an und aus.

Meldet sich je Rolle an, erkundet die Work-Seiten der Organisation über die Links, die diese Rolle sieht (nur
GET-Aufrufe von Seiten unter ``/work/<org>/``, keine Aktionen, Downloads oder Schnittstellen), und prüft jede
gefundene Seite in jeder Breite:

- Serverfehler: Status ≥ 500 oder eine Fehlerseite
- Toter Link: Ein Link, den die Rolle sieht, endet mit 403 oder 404
- Konsole: JavaScript-Ausnahmen, Fehler in der Konsole (Alpine, CSP), eigene Ressourcen mit Status ≥ 400
- Überlauf: Die Seite läuft seitlich über (breiter als das Fenster)
- Leerfläche (Regel #841, für Work 1.280 bis 2.560 px): Ab 1.280 px bleibt rechts neben dem Inhalt höchstens ein
  Viertel der Breite frei (Text, Bedienelemente und Flächen mit Rahmen, Hintergrund oder Schatten zählen als Inhalt).
  Gilt als Fehler nur im neuen Design (Schalter an bzw. Seite mit ``data-rahmen="neu"``); im bisherigen ein Hinweis.
- Design passt nicht zum Schalter: Die Seite trägt ``data-rahmen="neu"``, obwohl der Schalter aus ist (Fehler). Mit
  eingeschaltetem Schalter ist eine Seite ohne diese Kennzeichnung ein Hinweis (Seite außerhalb des Rahmens).
- Seiten ohne neue Gestaltung: Im neuen Rahmen kennzeichnet eine Seite mit ``data-gestaltung="neu"`` (Block
  ``gestaltung`` in ``work/base_work_neu.html``), dass ihr Inhalt schon im neuen Erscheinungsbild steht. Das Skript
  zählt die übrigen je Adressmuster über alle Rollen (Ziel aus Issue #951: 0). Mit ``--gestaltung-pflicht`` ist jede
  davon ein Fehler, sonst ein Hinweis in der Zusammenfassung.

Am Ende stehen je Rolle die erreichbaren Seiten und alle Befunde; mit ``--bericht`` zusätzlich als JSON.
Exit-Code 1, sobald es einen Fehler gibt (Hinweise zählen nicht).

Aufruf (aus dem Repo-Root; Playwright mit Chromium: ``pip install playwright && playwright install chromium``):

    # lokal gegen runserver, Demo-Daten aus setup_demo_environment (mit DEMO_INSTANCE=true und DEMO_PASSWORD)
    MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis http://localhost:8000
    # lokal mit Schalter an und aus (schaltet per Verwaltungsbefehl work_neues_design und stellt danach zurück)
    MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis http://localhost:8000 --schalter beide
    # wie viele Seiten stehen noch nicht im neuen Erscheinungsbild? (Abnahme von #951: Exit-Code 0)
    MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis http://localhost:8000 --schalter an \\
        --gestaltung-pflicht
    # gegen die öffentliche Demo (Schalter wie eingestellt; die Demo-Datenbank ändert nur der nächtliche Neuaufbau)
    MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis https://demo.mandari.de --bericht demo.json
    # gegen Staging mit eigenen Konten je Rolle
    MANDARI_PRUEF_VORSITZ_EMAIL=… MANDARI_PRUEF_VORSITZ_PASSWORT=… python scripts/pruefe_work_design.py \\
        --basis https://staging.example --org <slug> --rollen vorsitz

Zugänge kommen nur aus der Umgebung: ``MANDARI_PRUEF_PASSWORT`` für alle Demo-Konten, je Rolle überschreibbar mit
``MANDARI_PRUEF_<ROLLE>_EMAIL`` und ``MANDARI_PRUEF_<ROLLE>_PASSWORT`` (Rollen: vorsitz, mitglied, sachkundig,
unvereidigt, gast). Ohne Zugang wird die Rolle übersprungen.

Gegen eine entfernte Instanz schaltet das Skript den Schalter nie selbst um. ``--schalter an|aus|beide`` geht dort
nur mit ``--schalt-befehl`` (etwa ein ``docker exec``-Aufruf, Platzhalter ``{aktion}`` und ``{org}``) – das ist eine
bewusste Entscheidung der Person, die prüft. Außerdem ruft das Skript dort nur Seiten der Erlaubnisliste
``ERLAUBT_ENTFERNT`` auf (auch mit ``--nur``); andere sichtbare Links nennt der Bericht als „nicht geprüft“. Gedacht
ist es für lokale Instanzen, die CI, die Demo und Staging, nicht für Produktion. Ablauf beim Ausrollen:
docs/WORK_NEUES_DESIGN.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
MANAGE = ROOT / "mandari" / "manage.py"

ROLLEN = ("vorsitz", "mitglied", "sachkundig", "unvereidigt", "gast")
#: Konten der Demo-Umgebung (apps/common/demo_daten.py, DEMO_USERS)
DEMO_EMAILS = {
    "vorsitz": "demo-vorsitz@demo.mandari.de",
    "mitglied": "demo-mitglied@demo.mandari.de",
    "sachkundig": "demo-sachkundig@demo.mandari.de",
    "unvereidigt": "demo-unvereidigt@demo.mandari.de",
    "gast": "demo-gast@demo.mandari.de",
}
DEMO_ORG = "musterfraktion-demo"
BREITEN = (390, 1280, 1440, 1920, 2560)
HOEHE = 900
#: Ab dieser Breite gilt die Regel für Leerflächen (#841; für Work ab 1.280 px), und so viel darf rechts höchstens
#: frei bleiben
LEERFLAECHE_AB = 1280
LEERFLAECHE_ANTEIL = 0.25
#: Je Adressmuster (Kennungen durch {id} ersetzt) so viele Seiten prüfen – 13 Termine einer Reihe sehen gleich aus
JE_MUSTER = 2
#: So lange auf Ruhe im Netz warten (Abfragen nach dem Laden, etwa htmx); länger laufende Abfragen zählen nicht
RUHE_MS = 4000

#: Keine Seiten, sondern Aktionen, Teilstücke, Dateien oder Schnittstellen (auch wenn ein Link dorthin zeigt). Geprüft
#: wird der Pfad hinter ``/work/<org>``.
AUSGESCHLOSSEN = re.compile(
    r"/(?:delete|permanent-delete|restore|remove|resolve|decide|approve|reject|cancel|resend|resend-access|reset"
    r"|restore-defaults|empty|mark-read|submit-ris|upload|rename|move-to-folder|action|meta|checklist|comment"
    r"|download|export|panel|einlesen|import-file|status|share|update|preview|file|count|latest|events|request)/?$"
    r"|/(?:partials|annotations|position|notes|private-note|speech|supplementary|messages|teleprompter)/"
    r"|^/(?:ris/map/data|documents/ai|tasks/api|meetings/speech-documents|meetings/ladungen/[^/]+)/"
    r"|/bezug/suche/"
    r"|\.(?:pdf|ics|csv|docx|zip|json|xml)$"
)
#: Erlaubnisliste für entfernte Instanzen (Demo, Staging): Dort folgt das Skript nur Links auf diese Adressmuster
#: hinter ``/work/<org>`` (``{id}`` = Kennung, ``{name}`` = Kurzname). Die Sperrliste oben schützt nur vor bekannten
#: Aktionen; eine künftige Adresse, deren Aufruf etwas ändert, ruft das Skript dort so nie auf. Andere Links nennt der
#: Bericht als „nicht geprüft“ (Hinweis). Lokal und in der CI gilt nur die Sperrliste, damit neue Seiten auffallen.
#: Jeder Eintrag muss eine Seite von Work sein, deren Aufruf nichts ändert (Test: test_pruefe_work_design.py).
ERLAUBT_ENTFERNT = (
    "",
    "dashboard/",
    # Sitzungen
    "meetings/",
    "meetings/calendar/",
    "meetings/ladungen/",
    "meetings/{id}/",
    "meetings/{id}/prepare/",
    "meetings/{id}/summary/",
    # Fraktionssitzungen
    "faction/",
    "faction/historie/",
    "faction/nachweis/",
    "faction/settings/",
    "faction/{id}/",
    # Dokumente (motions/ leitet auf documents/ weiter)
    "freigaben/",
    "documents/",
    "documents/create/",
    "documents/import/",
    "documents/trash/",
    "documents/{id}/",
    "documents/{id}/revisions/",
    "documents/{id}/revisions/{id}/",
    "motions/",
    "motions/create/",
    "motions/import/",
    "motions/trash/",
    "motions/{id}/",
    "motions/{id}/edit/",
    # Aufgaben und Team
    "tasks/",
    "tasks/create/",
    "tasks/import/",
    "team/",
    "team/{id}/",
    # Recherche (RIS)
    "ris/",
    "ris/search/",
    "ris/papers/",
    "ris/papers/{id}/",
    "ris/meetings/",
    "ris/meetings/{id}/",
    "ris/organizations/",
    "ris/organizations/{id}/",
    "ris/persons/",
    "ris/persons/{id}/",
    "ris/files/",
    "ris/decisions/",
    "ris/map/",
    # Organisation
    "organization/",
    "organization/api/",
    "organization/documents-settings/",
    "organization/email-settings/",
    "organization/faction-settings/",
    "organization/parties/",
    "organization/registration/",
    "organization/verwaltung/",
    "organization/members/",
    "organization/members/invite/",
    "organization/members/invite-guest/",
    "organization/members/{id}/",
    "organization/roles/",
    "organization/roles/create/",
    "organization/roles/{id}/",
    "organization/documents/",
    "organization/documents/types/",
    "organization/documents/types/create/",
    "organization/documents/types/{id}/",
    "organization/documents/topics/",
    "organization/documents/templates/",
    "organization/documents/templates/create/",
    "organization/documents/templates/{id}/",
    "organization/documents/letterheads/",
    "organization/documents/letterheads/create/",
    "organization/documents/letterheads/{id}/",
    # Hilfe, Benachrichtigungen, Profil
    "support/",
    "support/create/",
    "support/{id}/",
    "support/kb/",
    "support/kb/{name}/",
    "support/kb/{name}/{name}/",
    "notifications/",
    "notifications/preferences/",
    "profile/",
    "profile/absence/",
    "profile/activity/",
    "profile/committees/",
    "profile/data/",
    "profile/notifications/",
    "profile/requests/",
    "profile/security/",
    "profile/visibility/",
)
#: Breiten unterhalb dieser Grenze gelten als Handy/Tablet: Die Seite wird dafür neu geladen (Startzustand der
#: Navigation hängt an der Breite beim Laden), darüber reicht es, das Fenster zu verbreitern.
HANDY_BIS = 1024
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ERLAUBT_MUSTER = re.compile(
    "^(?:"
    + "|".join(
        re.escape(f"/{eintrag}").replace(r"\{id\}", UUID.pattern).replace(r"\{name\}", r"[-a-zA-Z0-9_]+")
        for eintrag in ERLAUBT_ENTFERNT
    )
    + ")$"
)
#: Meldungen der Konsole, die nichts über die Seite sagen (Verbindungen der Zusammenarbeit im Editor)
KONSOLE_IGNORIERT = re.compile(r"WebSocket|ws://|wss://", re.IGNORECASE)
#: Meldungen, die nur als Hinweis zählen: fremde Ressourcen (etwa Kartenkacheln), die bei gestörtem Netz oder
#: Namensauflösung nicht laden, sagen nichts über die Seite selbst
HINWEIS_ARTEN = frozenset({"Fremde Ressource"})
FEHLERSEITE = ("Server Error (500)", "Internal Server Error", "TemplateDoesNotExist", "Traceback (most recent call")

#: Rechter Rand des Inhalts im Hauptbereich, Breite der Seite und Kennzeichnung des Rahmens
MESSUNG = """() => {
  const main = document.querySelector('main') || document.body;
  const alle = [...main.querySelectorAll('*')];
  // Nicht Inhalt: Verstecktes und feste Einblendungen (Hinweise, Leisten am Boden, Dialoge) samt ihrem Inhalt
  const fest = alle.filter((el) => getComputedStyle(el).position === 'fixed');
  const ausgenommen = (el) =>
    !!el.closest('[aria-hidden="true"], .sr-only, [hidden], template, dialog:not([open])') ||
    fest.some((f) => f.contains(el));
  let rechts = 0;
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  while (walker.nextNode()) {
    const knoten = walker.currentNode;
    if (!knoten.textContent.trim()) continue;
    const eltern = knoten.parentElement;
    if (!eltern || ausgenommen(eltern)) continue;
    range.selectNodeContents(knoten);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) rechts = Math.max(rechts, r.right);
  }
  // Bedienelemente und Medien; dazu Flächen: Karten, Kästen und Felder mit Rahmen, Hintergrund oder Schatten füllen
  // die Breite, auch wenn ihr Text kurz ist – außer Hüllen über (fast) den ganzen Hauptbereich (Kopfband) und
  // Elementen außerhalb des Fensters (eingeklappte Leisten)
  const ersetzt = new Set(['IMG', 'SVG', 'CANVAS', 'IFRAME', 'INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'TABLE', 'VIDEO']);
  const sichtbar = (farbe) => !!farbe && farbe !== 'transparent' && !farbe.replace(/ /g, '').endsWith(',0)');
  const hauptbreite = main.getBoundingClientRect().width;
  alle.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.right > window.innerWidth + 1 || ausgenommen(el)) return;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden') return;
    if (ersetzt.has(el.tagName.toUpperCase())) {
      rechts = Math.max(rechts, r.right);
      return;
    }
    if (r.width >= hauptbreite * 0.97) return;
    const rand = ['Top', 'Right', 'Bottom', 'Left'].some(
      (s) => parseFloat(st['border' + s + 'Width']) > 0 && sichtbar(st['border' + s + 'Color']),
    );
    if (rand || sichtbar(st.backgroundColor) || (st.boxShadow && st.boxShadow !== 'none')) {
      rechts = Math.max(rechts, r.right);
    }
  });
  const text = document.body ? document.body.innerText.slice(0, 4000) : '';
  // Bei Überlauf: das innerste Element, das die Seitenbreite bestimmt (rechter Rand = Breite der Seite)
  let ueber = null;
  const seitenbreite = document.documentElement.scrollWidth;
  if (seitenbreite > window.innerWidth + 1) {
    let best = null;
    document.body.querySelectorAll('*').forEach((el) => {
      const r = el.getBoundingClientRect();
      const rechterRand = r.right + window.scrollX;
      if (r.width <= 0 || Math.abs(rechterRand - seitenbreite) > 1) return;
      if (!best || best.contains(el)) best = el;
    });
    if (best) {
      const klassen = [...best.classList].slice(0, 3).map((k) => '.' + k).join('');
      ueber = best.tagName.toLowerCase() + (best.id ? '#' + best.id : '') + klassen;
    }
  }
  const gestaltet = document.querySelector('main[data-gestaltung]');
  return {
    breite: window.innerWidth,
    scroll: document.documentElement.scrollWidth,
    ueber,
    rechts: Math.round(rechts),
    rahmen: document.documentElement.dataset.rahmen || null,
    gestaltung: gestaltet ? gestaltet.dataset.gestaltung : null,
    titel: document.title,
    text,
  };
}"""


# =============================================================================
# Ergebnisse
# =============================================================================


@dataclass
class Befund:
    rolle: str
    schalter: str
    pfad: str
    art: str
    detail: str
    breite: int | None = None
    schwere: str = "Fehler"  # Fehler | Hinweis


@dataclass
class Lauf:
    rolle: str
    schalter: str
    erreichbar: list[str] = field(default_factory=list)
    nicht_erreichbar: dict[str, int] = field(default_factory=dict)
    befunde: list[Befund] = field(default_factory=list)
    #: Seiten im neuen Rahmen, deren Inhalt neu gestaltet ist (``data-gestaltung="neu"``) bzw. noch nicht
    neu_gestaltet: list[str] = field(default_factory=list)
    ohne_neue_gestaltung: list[str] = field(default_factory=list)
    #: Gegen entfernte Instanzen: Adressmuster sichtbarer Links, die nicht auf der Erlaubnisliste stehen (nicht
    #: aufgerufen)
    nicht_geprueft: list[str] = field(default_factory=list)


@dataclass
class Zugang:
    rolle: str
    email: str
    passwort: str


# =============================================================================
# Reine Regeln (ohne Browser, getestet in apps/common/tests/test_pruefe_work_design.py)
# =============================================================================


def zugaenge(rollen: Iterable[str], umgebung: dict[str, str] | None = None) -> tuple[list[Zugang], list[str]]:
    """Zugänge je Rolle aus der Umgebung; zweite Liste: Rollen ohne Zugang."""
    env = os.environ if umgebung is None else umgebung
    gemeinsam = env.get("MANDARI_PRUEF_PASSWORT", "")
    gefunden, fehlend = [], []
    for rolle in rollen:
        email = env.get(f"MANDARI_PRUEF_{rolle.upper()}_EMAIL", "") or DEMO_EMAILS.get(rolle, "")
        passwort = env.get(f"MANDARI_PRUEF_{rolle.upper()}_PASSWORT", "") or gemeinsam
        if email and passwort:
            gefunden.append(Zugang(rolle, email, passwort))
        else:
            fehlend.append(rolle)
    return gefunden, fehlend


def seitenpfad(basis: str, org: str, href: str, von: str) -> str | None:
    """Pfad einer Work-Seite der Organisation aus einem Link, sonst ``None`` (fremd, Aktion, Datei, Schnittstelle)."""
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:", "blob:")):
        return None
    ziel, _ = urldefrag(urljoin(von, href))
    teile, eigen = urlsplit(ziel), urlsplit(basis)
    if (teile.scheme, teile.netloc) != (eigen.scheme, eigen.netloc):
        return None
    pfad = teile.path
    praefix = f"/work/{org}/"
    if not (pfad == praefix.rstrip("/") or pfad.startswith(praefix)):
        return None
    rest = pfad[len(praefix) - 1 :]
    if AUSGESCHLOSSEN.search(rest):
        return None
    return pfad if pfad.endswith("/") else f"{pfad}/"


def muster(pfad: str) -> str:
    """Adressmuster: Kennungen durch {id} ersetzt."""
    return UUID.sub("{id}", pfad)


def work_pfad(org: str, angabe: str) -> str:
    """``faction/`` oder ``/work/<org>/faction/`` → ``/work/<org>/faction/`` (Angabe bei ``--nur``)."""
    pfad = angabe if angabe.startswith("/work/") else f"/work/{org}/{angabe.lstrip('/')}"
    return pfad if pfad.endswith("/") else f"{pfad}/"


def erlaubt_entfernt(pfad: str, org: str) -> bool:
    """Steht die Work-Seite ``pfad`` (aus :func:`seitenpfad`) auf der Erlaubnisliste für entfernte Instanzen?"""
    praefix = f"/work/{org}"
    if not (pfad == praefix or pfad.startswith(f"{praefix}/")):
        return False
    rest = pfad[len(praefix) :].rstrip("/") + "/"
    return bool(ERLAUBT_MUSTER.match(rest))


def bewerten(messung: dict[str, Any], *, rolle: str, schalter: str, pfad: str, breite: int) -> list[Befund]:
    """Befunde aus einer Messung (Überlauf, Leerfläche, Fehlerseite, Rahmen passt nicht zum Schalter)."""
    befunde = []

    def melden(art: str, detail: str, schwere: str = "Fehler") -> None:
        befunde.append(Befund(rolle, schalter, pfad, art, detail, breite, schwere))

    text = f"{messung.get('titel', '')}\n{messung.get('text', '')}"
    if any(marke in text for marke in FEHLERSEITE):
        melden("Serverfehler", "Fehlerseite im Inhalt")
    fenster = int(messung["breite"])
    if int(messung["scroll"]) > fenster + 1:
        ursache = f", ragt hinaus: {messung['ueber']}" if messung.get("ueber") else ""
        melden("Überlauf", f"Seite {messung['scroll']} px breit bei {fenster} px Fenster{ursache}")
    rechts = int(messung["rechts"])
    rahmen = messung.get("rahmen")
    # Neues Design: Schalter an, oder (Lauf ohne Umschalten, etwa gegen die Demo) die Seite trägt den neuen Rahmen
    neues_design = schalter == "an" or (schalter == "aktuell" and rahmen == "neu")
    if fenster >= LEERFLAECHE_AB and rechts > 0:
        frei = (fenster - rechts) / fenster
        if frei > LEERFLAECHE_ANTEIL:
            melden(
                "Leerfläche",
                f"rechts {frei:.0%} frei (Inhalt endet bei {rechts} px)",
                "Fehler" if neues_design else "Hinweis",
            )
    if schalter == "aus" and rahmen == "neu":
        melden("Design passt nicht zum Schalter", 'data-rahmen="neu", Schalter aus')
    elif schalter == "an" and rahmen != "neu":
        if rahmen:
            melden("Design passt nicht zum Schalter", f'data-rahmen="{rahmen}", Schalter an')
        else:
            melden("Ohne neuen Rahmen", 'Seite trägt kein data-rahmen="neu" (eigene Vorlage ohne Rahmen?)', "Hinweis")
    return befunde


def gestaltung(messung: dict[str, Any]) -> str | None:
    """
    ``neu`` (Seite im neuen Rahmen, Inhalt neu gestaltet), ``bisher`` (neuer Rahmen, Inhalt noch im bisherigen
    Erscheinungsbild) oder ``None`` (Seite nicht im neuen Rahmen, etwa mit ausgeschaltetem Schalter).
    """
    if messung.get("rahmen") != "neu":
        return None
    return "neu" if messung.get("gestaltung") == "neu" else "bisher"


def ohne_neue_gestaltung(laeufe: Iterable[Lauf]) -> list[str]:
    """Adressmuster aller Rollen, die im neuen Rahmen noch nicht neu gestaltet sind (sortiert, ohne Doppelte)."""
    return sorted({muster(pfad) for lauf in laeufe for pfad in lauf.ohne_neue_gestaltung})


def konsole_einordnen(text: str, quelle: str, basis: str) -> tuple[str, str] | None:
    """
    Art und Text einer Fehlermeldung der Konsole oder ``None`` (nicht melden).

    Lädt eine fremde Ressource nicht (``Failed to load resource`` mit einer Adresse außerhalb der geprüften Instanz),
    ist das ein Hinweis; alles andere, auch Verstöße gegen die CSP, bleibt ein Fehler der Seite.
    """
    if KONSOLE_IGNORIERT.search(text):
        return None
    erste = text.splitlines()[0][:300] if text else ""
    if text.startswith("Failed to load resource") and quelle:
        ziel, eigen = urlsplit(quelle), urlsplit(basis)
        if (ziel.scheme, ziel.netloc) != (eigen.scheme, eigen.netloc):
            return "Fremde Ressource", f"{ziel.netloc or quelle[:80]}: {erste}"
    return "Konsole", erste


def zusammenfassen(laeufe: list[Lauf], *, gestaltung_pflicht: bool = False) -> tuple[str, int]:
    """
    Lesbarer Bericht und Zahl der Fehler. Mit ``gestaltung_pflicht`` zählt jedes Adressmuster im neuen Rahmen ohne
    neue Gestaltung als Fehler (Abnahme von #951), sonst steht die Zahl nur im Bericht.
    """
    zeilen = []
    fehler = 0
    for lauf in laeufe:
        arten = Counter(f"{b.art} ({b.schwere})" for b in lauf.befunde)
        fehler += sum(1 for b in lauf.befunde if b.schwere == "Fehler")
        kopf = f"{lauf.rolle:12} Schalter {lauf.schalter:8} {len(lauf.erreichbar):3} Seiten erreichbar"
        im_rahmen = len(lauf.neu_gestaltet) + len(lauf.ohne_neue_gestaltung)
        if im_rahmen:
            kopf += f", {len(lauf.ohne_neue_gestaltung)} von {im_rahmen} im neuen Rahmen ohne neue Gestaltung"
        zeilen.append(kopf + (f" – {dict(arten)}" if arten else " – ohne Befund"))
    if any(lauf.neu_gestaltet or lauf.ohne_neue_gestaltung for lauf in laeufe):
        offen = ohne_neue_gestaltung(laeufe)
        neu = sorted({muster(p) for lauf in laeufe for p in lauf.neu_gestaltet} - set(offen))
        schwere = "Fehler" if gestaltung_pflicht else "Hinweis"
        zeilen.append("")
        zeilen.append(
            f"Seiten ohne neue Gestaltung (Adressmuster, alle Rollen): {len(offen)} – neu gestaltet: {len(neu)}"
        )
        zeilen.extend(f"  [{schwere}] {m}" for m in offen)
        if gestaltung_pflicht:
            fehler += len(offen)
    ausgelassen = sorted({m for lauf in laeufe for m in lauf.nicht_geprueft})
    if ausgelassen:
        zeilen.append("")
        zeilen.append(f"Nicht geprüft (nicht auf der Erlaubnisliste für entfernte Instanzen): {len(ausgelassen)}")
        zeilen.extend(f"  [Hinweis] {m}" for m in ausgelassen)
    details = [b for lauf in laeufe for b in lauf.befunde]
    if details:
        zeilen.append("")
        zeilen.append("Befunde:")
        for b in sorted(details, key=lambda b: (b.schwere != "Fehler", b.art, b.pfad, b.rolle, b.breite or 0)):
            breite = f" {b.breite} px" if b.breite else ""
            zeilen.append(f"  [{b.schwere}] {b.art}: {b.pfad} ({b.rolle}, Schalter {b.schalter}{breite}) – {b.detail}")
    return "\n".join(zeilen), fehler


# =============================================================================
# Browser
# =============================================================================


class Pruefer:
    """Prüft eine Rolle in einem Schalterzustand: anmelden, Seiten erkunden, je Breite messen."""

    def __init__(
        self,
        browser: Any,
        basis: str,
        org: str,
        *,
        breiten: Iterable[int] = BREITEN,
        max_seiten: int = 80,
        neu_laden: bool = False,
        bilder: Path | None = None,
        nur: Iterable[str] = (),
        nur_erlaubte: bool = False,
        zeitlimit_ms: int = 30000,
        protokoll: Callable[[str], None] = print,
    ) -> None:
        self.browser = browser
        self.basis = basis.rstrip("/")
        self.org = org
        self.breiten = sorted(breiten)
        self.max_seiten = max_seiten
        self.neu_laden = neu_laden
        #: Nur Seiten der Erlaubnisliste aufrufen (entfernte Instanzen)
        self.nur_erlaubte = nur_erlaubte
        #: Nur diese Seiten prüfen (ohne Erkunden), etwa um einen Befund nachzustellen
        self.nur = [self._pfad(p) for p in nur]
        self.bilder = bilder
        self.zeitlimit_ms = zeitlimit_ms
        self.protokoll = protokoll

    def pruefen(self, zugang: Zugang, schalter: str) -> Lauf:
        lauf = Lauf(zugang.rolle, schalter)
        kontext = self.browser.new_context(viewport={"width": 1280, "height": HOEHE}, locale="de-DE")
        seite = kontext.new_page()
        seite.set_default_timeout(self.zeitlimit_ms)
        meldungen: list[tuple[str, str]] = []
        seite.on("pageerror", lambda fehler: meldungen.append(("JS-Ausnahme", str(fehler).splitlines()[0][:300])))
        seite.on("console", lambda m: self._konsole(m, meldungen))
        seite.on("response", lambda antwort: self._ressource(antwort, meldungen))
        try:
            if self._anmelden(seite, zugang, lauf):
                self._erkunden(seite, lauf, meldungen)
        finally:
            kontext.close()
        return lauf

    def _pfad(self, angabe: str) -> str:
        return work_pfad(self.org, angabe)

    # -- Ereignisse ------------------------------------------------------------

    def _konsole(self, meldung: Any, meldungen: list[tuple[str, str]]) -> None:
        if meldung.type != "error":
            return
        ort = meldung.location if isinstance(meldung.location, dict) else {}
        eingeordnet = konsole_einordnen(meldung.text, str(ort.get("url") or ""), self.basis)
        if eingeordnet:
            meldungen.append(eingeordnet)

    def _ressource(self, antwort: Any, meldungen: list[tuple[str, str]]) -> None:
        if antwort.request.resource_type == "document" or not antwort.url.startswith(self.basis):
            return
        if antwort.status >= 400:
            meldungen.append(("Ressource", f"HTTP {antwort.status} {urlsplit(antwort.url).path}"))

    # -- Schritte --------------------------------------------------------------

    def _laden(self, seite: Any, pfad: str) -> int:
        antwort = seite.goto(f"{self.basis}{pfad}", wait_until="load")
        try:
            seite.wait_for_load_state("networkidle", timeout=min(self.zeitlimit_ms, RUHE_MS))
        except Exception:  # laufende Abfragen (etwa Benachrichtigungen) sind kein Fehler der Seite
            pass
        return int(antwort.status) if antwort is not None else 0

    def _anmelden(self, seite: Any, zugang: Zugang, lauf: Lauf) -> bool:
        status = self._laden(seite, "/accounts/login/")
        if status >= 400 or seite.locator("input[name=email]").count() == 0:
            # Falsche Adresse, Host nicht zugelassen oder vorgeschaltete Sperre: als Befund melden statt abzubrechen
            lauf.befunde.append(
                Befund(
                    lauf.rolle,
                    lauf.schalter,
                    "/accounts/login/",
                    "Anmeldung",
                    f"Anmeldeseite nicht nutzbar (HTTP {status}) – Adresse und zugelassene Hosts prüfen",
                )
            )
            return False
        seite.fill("input[name=email]", zugang.email)
        seite.fill("input[name=password]", zugang.passwort)
        seite.click("button[type=submit]")
        try:
            seite.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # Weiterleitung nach der Anmeldung noch unterwegs
            pass
        if "/accounts/login" in urlsplit(seite.url).path or "zwei" in urlsplit(seite.url).path.lower():
            lauf.befunde.append(
                Befund(
                    lauf.rolle, lauf.schalter, "/accounts/login/", "Anmeldung", "Anmeldung gescheitert (Zugang prüfen)"
                )
            )
            return False
        return True

    def _erkunden(self, seite: Any, lauf: Lauf, meldungen: list[tuple[str, str]]) -> None:
        """Breitensuche über die Links, die die Rolle sieht; jede erreichbare Seite wird gleich gemessen."""
        start = f"/work/{self.org}/"
        warteschlange: deque[tuple[str, str]] = deque((p, "Start") for p in (self.nur or [start]))
        gesehen = set(self.nur or [start])
        je_muster: Counter[str] = Counter()
        breit = [b for b in self.breiten if b >= HANDY_BIS] or [1280]
        schmal = [b for b in self.breiten if b < HANDY_BIS]
        while warteschlange and len(lauf.erreichbar) < self.max_seiten:
            pfad, herkunft = warteschlange.popleft()
            seite.set_viewport_size({"width": breit[0], "height": HOEHE})
            meldungen.clear()
            status = self._laden(seite, pfad)
            if status >= 500:
                lauf.befunde.append(Befund(lauf.rolle, lauf.schalter, pfad, "Serverfehler", f"HTTP {status}"))
                continue
            gelandet = urlsplit(seite.url).path
            if status in (401, 403, 404) or not gelandet.startswith(f"/work/{self.org}"):
                lauf.nicht_erreichbar[pfad] = status
                if herkunft != "Start":
                    lauf.befunde.append(
                        Befund(lauf.rolle, lauf.schalter, pfad, "Toter Link", f"HTTP {status}, verlinkt auf {herkunft}")
                    )
                continue
            lauf.erreichbar.append(pfad)
            links = (
                []
                if self.nur
                else seite.eval_on_selector_all("a[href]", "els => els.map(el => el.getAttribute('href'))")
            )
            for href in links:
                ziel = seitenpfad(self.basis, self.org, href, seite.url)
                if ziel is None or ziel in gesehen:
                    continue
                gesehen.add(ziel)
                if self.nur_erlaubte and not erlaubt_entfernt(ziel, self.org):
                    if muster(ziel) not in lauf.nicht_geprueft:
                        lauf.nicht_geprueft.append(muster(ziel))
                    continue
                if je_muster[muster(ziel)] >= JE_MUSTER:
                    continue
                je_muster[muster(ziel)] += 1
                warteschlange.append((ziel, pfad))
            self._messen(seite, pfad, lauf, meldungen, breit, geladen=True)
            if schmal:
                self._messen(seite, pfad, lauf, meldungen, schmal, geladen=False)
        self.protokoll(f"  {lauf.rolle}, Schalter {lauf.schalter}: {len(lauf.erreichbar)} Seiten geprüft")

    def _messen(
        self,
        seite: Any,
        pfad: str,
        lauf: Lauf,
        meldungen: list[tuple[str, str]],
        breiten: list[int],
        *,
        geladen: bool,
    ) -> None:
        """Misst die Seite in den Breiten; lädt neu, wenn sie in dieser Breitenklasse noch nicht geladen ist."""
        for nummer, breite in enumerate(breiten):
            seite.set_viewport_size({"width": breite, "height": HOEHE})
            if (nummer == 0 and not geladen) or self.neu_laden:
                status = self._laden(seite, pfad)
                if status >= 500:
                    lauf.befunde.append(
                        Befund(lauf.rolle, lauf.schalter, pfad, "Serverfehler", f"HTTP {status}", breite)
                    )
                    return
            else:
                time.sleep(0.15)
            messung = seite.evaluate(MESSUNG)
            lauf.befunde.extend(bewerten(messung, rolle=lauf.rolle, schalter=lauf.schalter, pfad=pfad, breite=breite))
            if nummer == 0 and geladen:
                art = gestaltung(messung)
                if art == "neu":
                    lauf.neu_gestaltet.append(pfad)
                elif art == "bisher":
                    lauf.ohne_neue_gestaltung.append(pfad)
            for art, text in dict.fromkeys(meldungen):
                schwere = "Hinweis" if art in HINWEIS_ARTEN else "Fehler"
                lauf.befunde.append(Befund(lauf.rolle, lauf.schalter, pfad, art, text, breite, schwere))
            meldungen.clear()
            if self.bilder:
                name = f"{lauf.schalter}-{lauf.rolle}-{breite}-{muster(pfad).strip('/').replace('/', '_') or 'start'}"
                self.bilder.mkdir(parents=True, exist_ok=True)
                seite.screenshot(path=str(self.bilder / f"{name}.png"), full_page=True)


# =============================================================================
# Schalter
# =============================================================================


def ist_lokal(basis: str) -> bool:
    return (urlsplit(basis).hostname or "") in ("localhost", "127.0.0.1", "::1", "testserver")


def schalt_befehl(vorlage: str | None) -> Callable[[str, str], str]:
    """Führt den Verwaltungsbefehl ``work_neues_design`` aus (Standard: lokales manage.py) und liefert die Ausgabe."""

    def ausfuehren(aktion: str, org: str) -> str:
        if vorlage:
            befehl = shlex.split(vorlage.format(aktion=aktion, org=org))
        else:
            befehl = [sys.executable, str(MANAGE), "work_neues_design", aktion, "--org", org]
        if aktion == "status":
            befehl.append("--json")
        umgebung = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        ergebnis = subprocess.run(
            befehl, capture_output=True, text=True, encoding="utf-8", errors="replace", env=umgebung, check=False
        )
        if ergebnis.returncode != 0:
            raise RuntimeError(f"Schalten fehlgeschlagen ({' '.join(befehl[:4])} …): {ergebnis.stderr.strip()[-400:]}")
        return ergebnis.stdout

    return ausfuehren


def zustaende(schalter: str) -> list[str]:
    return {"aktuell": ["aktuell"], "an": ["an"], "aus": ["aus"], "beide": ["an", "aus"]}[schalter]


def pruefen(
    basis: str,
    org: str,
    zugaenge_: list[Zugang],
    *,
    schalter: str = "aktuell",
    schalten: Callable[[str, str], str] | None = None,
    breiten: Iterable[int] = BREITEN,
    max_seiten: int = 80,
    neu_laden: bool = False,
    bilder: Path | None = None,
    nur: Iterable[str] = (),
    nur_erlaubte: bool = False,
    sichtbar: bool = False,
    browser: Any = None,
    protokoll: Callable[[str], None] = print,
) -> list[Lauf]:
    """
    Alle Rollen in allen gewünschten Schalterzuständen prüfen; stellt den Schalter danach wieder her.

    ``browser``: vorhandener Playwright-Browser (E2E-Tests); sonst startet das Skript Chromium selbst.
    ``nur_erlaubte``: nur Seiten der Erlaubnisliste aufrufen (``ERLAUBT_ENTFERNT``; gegen entfernte Instanzen immer).
    """
    laeufe: list[Lauf] = []
    vorher: bool | None = None
    if schalter != "aktuell":
        if schalten is None:
            raise RuntimeError("Zum Umschalten fehlt der Schaltbefehl.")
        vorher = bool(json.loads(schalten("status", org).strip().splitlines()[-1]).get(org))

    def durchlaufen(offen: Any) -> None:
        pruefer = Pruefer(
            offen,
            basis,
            org,
            breiten=breiten,
            max_seiten=max_seiten,
            neu_laden=neu_laden,
            bilder=bilder,
            nur=nur,
            nur_erlaubte=nur_erlaubte,
            protokoll=protokoll,
        )
        for zustand in zustaende(schalter):
            if zustand != "aktuell" and schalten is not None:
                schalten(zustand, org)
            protokoll(f"Schalter {zustand}:")
            for zugang in zugaenge_:
                laeufe.append(pruefer.pruefen(zugang, zustand))

    try:
        if browser is not None:
            durchlaufen(browser)
        else:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                eigener = p.chromium.launch(headless=not sichtbar)
                try:
                    durchlaufen(eigener)
                finally:
                    eigener.close()
    finally:
        if vorher is not None and schalten is not None:
            schalten("an" if vorher else "aus", org)
    return laeufe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prüft Work mit neuem und bisherigem Design (Issue #884).")
    parser.add_argument("--basis", default="http://localhost:8000", help="Adresse der Instanz (ohne / am Ende)")
    parser.add_argument("--org", default=DEMO_ORG, help="Kurzname der Organisation")
    parser.add_argument("--rollen", default=",".join(ROLLEN), help="Rollen, kommagetrennt")
    parser.add_argument("--breiten", default=",".join(map(str, BREITEN)), help="Breiten in px, kommagetrennt")
    parser.add_argument("--schalter", choices=["aktuell", "an", "aus", "beide"], default="aktuell")
    parser.add_argument("--schalt-befehl", help="Befehl zum Umschalten, Platzhalter {aktion} und {org}")
    parser.add_argument("--max-seiten", type=int, default=80, help="höchstens so viele Seiten je Rolle")
    parser.add_argument("--neu-laden", action="store_true", help="je Breite neu laden (genauer, dauert länger)")
    parser.add_argument("--bilder", type=Path, help="Bildschirmfotos in dieses Verzeichnis")
    parser.add_argument(
        "--nur", action="append", default=[], metavar="PFAD", help="nur diese Seite prüfen, ohne zu erkunden (mehrfach)"
    )
    parser.add_argument("--bericht", type=Path, help="Befunde und erreichbare Seiten als JSON")
    parser.add_argument("--sichtbar", action="store_true", help="Browser sichtbar starten")
    parser.add_argument(
        "--nur-erlaubte",
        action="store_true",
        help="nur Seiten der Erlaubnisliste aufrufen (gegen entfernte Instanzen immer, lokal zum Nachstellen)",
    )
    parser.add_argument(
        "--gestaltung-pflicht",
        action="store_true",
        help="Seiten im neuen Rahmen ohne neue Gestaltung als Fehler zählen (Abnahme von #951)",
    )
    args = parser.parse_args(argv)

    rollen = [r.strip() for r in args.rollen.split(",") if r.strip()]
    unbekannt = [r for r in rollen if r not in ROLLEN]
    if unbekannt:
        parser.error(f"unbekannte Rolle(n): {', '.join(unbekannt)} (möglich: {', '.join(ROLLEN)})")
    gefunden, fehlend = zugaenge(rollen)
    for rolle in fehlend:
        print(f"Übersprungen: {rolle} (kein Zugang in MANDARI_PRUEF_PASSWORT bzw. MANDARI_PRUEF_{rolle.upper()}_…)")
    if not gefunden:
        print("Keine Zugänge – nichts zu prüfen.", file=sys.stderr)
        return 2
    # Entfernte Instanzen (Demo, Staging): nur Seiten der Erlaubnisliste, auch bei --nur
    nur_erlaubte = args.nur_erlaubte or not ist_lokal(args.basis)
    if nur_erlaubte:
        verboten = [p for p in args.nur if not erlaubt_entfernt(work_pfad(args.org, p), args.org)]
        if verboten:
            parser.error(f"nicht auf der Erlaubnisliste für entfernte Instanzen: {', '.join(verboten)}")
    schalten = None
    if args.schalter != "aktuell":
        if not ist_lokal(args.basis) and not args.schalt_befehl:
            parser.error("Gegen entfernte Instanzen schaltet das Skript nur mit --schalt-befehl (bewusst, siehe Doku).")
        schalten = schalt_befehl(args.schalt_befehl)

    laeufe = pruefen(
        args.basis.rstrip("/"),
        args.org,
        gefunden,
        schalter=args.schalter,
        schalten=schalten,
        breiten=[int(b) for b in args.breiten.split(",") if b.strip()],
        max_seiten=args.max_seiten,
        neu_laden=args.neu_laden,
        bilder=args.bilder,
        nur=args.nur,
        nur_erlaubte=nur_erlaubte,
        sichtbar=args.sichtbar,
    )
    text, fehler = zusammenfassen(laeufe, gestaltung_pflicht=args.gestaltung_pflicht)
    print(text)
    if args.bericht:
        args.bericht.write_text(
            json.dumps([asdict(lauf) for lauf in laeufe], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Bericht: {args.bericht}")
    print(f"\n{fehler} Fehler." if fehler else "\nKeine Fehler.")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
