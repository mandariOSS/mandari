# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Druckansicht eines Dokuments für die Satz-Kette (Issue #1584).

Die Druckansicht setzt den Text mit derselben Kette wie die Seitenansicht des Editors (``frontend/satz/druck.ts``).
Für das PDF geht sie samt allen Dateien an headless Chromium (``apps/common/pdf_chromium.py``); dieselbe Ansicht ist
unter ``documents/<id>/druck/`` im Browser zu sehen (Kontrolle, Druck aus dem Browser).

Das Paket für Chromium enthält nur flache Dateinamen: ``index.html``, das gebündelte Skript ``satz-druck.js``,
``dokument-satz.css``, ``dokument-druck.css`` und je verwendeter Schrift ihre vier Schnitte samt einer passenden ``schriften.css``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.utils.html import escape

from apps.common import schriften

from .export_service import (
    MAX_PDF_TABLE_CELLS,
    PDF_TABLES_TOO_LARGE,
    ExportTooLargeError,
    clean_editor_html,
    get_signature_text,
    table_cell_count,
)
from .satz import satz_konfig

if TYPE_CHECKING:
    from .models import Motion

#: Klassisches Skript der Druckansicht im Vite-Ausgabeordner: Chromium lädt die Seite aus Dateien, dort gehen keine
#: ES-Module (frontend/vite/satz-druck.ts)
SKRIPT = "satz-druck.js"
_SCHRIFT_IM_TEXT_RE = re.compile(r"font-family:\s*(&quot;[^&]{1,64}&quot;|&#x27;[^&]{1,64}&#x27;|[^;\"]{1,64})")


class DruckNichtMoeglichError(RuntimeError):
    """Das Druckpaket lässt sich nicht bauen (z. B. fehlt das gebaute Frontend)."""


def druck_inhalt(motion: Motion) -> str:
    """Bereinigter Text des Dokuments samt Signaturblock der Vorlage, wie ihn die Druckansicht setzt."""
    inhalt = clean_editor_html(motion.content or "")
    if table_cell_count(inhalt) > MAX_PDF_TABLE_CELLS:
        raise ExportTooLargeError(PDF_TABLES_TOO_LARGE)
    signatur = get_signature_text(motion)
    if signatur:
        zeilen = "<br>".join(escape(zeile) for zeile in signatur.splitlines())
        inhalt += f'<p data-signatur="true">{zeilen}</p>'
    return inhalt


def verwendete_schriften(inhalt: str, basis: str) -> list[schriften.Schrift]:
    """Grundschrift des Dokuments und alle Schriften, die der Text an Stellen setzt (Reihenfolge wie im Text)."""
    gefunden: dict[str, schriften.Schrift] = {}
    erste = schriften.schrift(basis) or schriften.aufloesen(None)
    gefunden[erste.schluessel] = erste
    # Code setzt der Satz in Cousine (dokument-satz.css)
    if "<code" in inhalt or "<pre" in inhalt:
        cousine = schriften.schrift("cousine")
        if cousine is not None:
            gefunden.setdefault(cousine.schluessel, cousine)
    for treffer in _SCHRIFT_IM_TEXT_RE.finditer(inhalt):
        name = treffer.group(1).replace("&quot;", '"').replace("&#x27;", "'")
        schrift = schriften.finden(name)
        if schrift is not None:
            gefunden.setdefault(schrift.schluessel, schrift)
    return list(gefunden.values())


def schriften_css(liste: list[schriften.Schrift]) -> str:
    """``@font-face`` für flache Dateinamen (Paket für Chromium)."""
    regeln = []
    for schrift in liste:
        for schnitt in schrift.schnitte:
            datei = Path(schrift.datei(schnitt)).name
            format_ = "truetype" if schrift.unveraendert else "woff2"
            regeln.append(
                f'@font-face {{ font-family: "{schrift.name}"; font-style: {"italic" if schnitt.kursiv else "normal"}; '
                f'font-weight: {schnitt.gewicht}; font-display: block; src: url("{datei}") format("{format_}"); }}'
            )
    return "\n".join(regeln) + "\n"


def _statisch(pfad: str) -> bytes:
    gefunden = finders.find(pfad)
    if not gefunden:
        raise DruckNichtMoeglichError(f"Statische Datei fehlt: {pfad}")
    datei = gefunden if isinstance(gefunden, str) else cast("list[str]", gefunden)[0]
    try:
        return Path(datei).read_bytes()
    except OSError as fehler:
        raise DruckNichtMoeglichError(f"Statische Datei nicht lesbar: {pfad}") from fehler


def druck_skript() -> bytes:
    """Gebündelte Druckansicht (static/dist/satz-druck.js, frontend/vite/satz-druck.ts)."""
    pfad = Path(str(settings.DJANGO_VITE["default"]["manifest_path"])).parent / SKRIPT
    if not pfad.exists():
        raise DruckNichtMoeglichError("Frontend ist nicht gebaut (static/dist/satz-druck.js fehlt)")
    try:
        return pfad.read_bytes()
    except OSError as fehler:
        raise DruckNichtMoeglichError("static/dist/satz-druck.js nicht lesbar") from fehler


def druck_kontext(motion: Motion, *, paket: bool) -> dict[str, Any]:
    """Kontext der Druckansicht; ``paket`` = flache Dateinamen für Chromium statt Adressen der Ablage."""
    konfig = satz_konfig(motion.letterhead)
    # Beim generierten Briefkopf steht der Titel im Kopf der ersten Seite (wie im Editor und im bisherigen PDF)
    konfig["titel"] = motion.title
    inhalt = druck_inhalt(motion)
    if paket:
        # Chromium liest keine fremden Server: Bilder aus dem Text vorher abgesichert laden und einbetten
        from apps.work.pdf_bilder import inline_remote_images

        inhalt = inline_remote_images(inhalt)
    return {"title": motion.title, "konfig": konfig, "inhalt": inhalt, "paket": paket}


def druck_dateien(motion: Motion) -> dict[str, bytes]:
    """Alle Dateien für die PDF-Erzeugung über Chromium."""
    kontext = druck_kontext(motion, paket=True)
    liste = verwendete_schriften(kontext["inhalt"], kontext["konfig"]["schrift"]["schluessel"])
    # Chromium lädt die Schriften des Texts selbst nach: Die Konfiguration nennt alle, die das Paket mitbringt
    kontext["konfig"]["schriften"] = [{"schluessel": s.schluessel, "name": s.name} for s in liste]
    dateien: dict[str, bytes] = {
        "index.html": render_to_string("work/motions/satz/druck.html", kontext).encode("utf-8"),
        "dokument-satz.css": _statisch("css/dokument-satz.css"),
        "dokument-druck.css": _statisch("css/dokument-druck.css"),
        "schriften.css": schriften_css(liste).encode("utf-8"),
        SKRIPT: druck_skript(),
    }
    for schrift in liste:
        for schnitt in schrift.schnitte:
            pfad = schrift.datei(schnitt)
            dateien[Path(pfad).name] = _statisch(pfad)
    return dateien
