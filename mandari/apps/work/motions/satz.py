# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Satz-Konfiguration eines Dokuments (Issue #1584): Seitengröße, Ränder, Schrift und Briefpapier für die Satz-Kette.

Editor (Seitenansicht), Druckansicht und PDF lesen dieselbe Konfiguration (``frontend/satz/vertrag.ts``,
``SatzKonfig``). Längen stehen in Millimetern, die Schriftgröße in Punkt.

Briefpapier:
- als PDF: Die erste Seite der Datei liegt unter jeder Seite (im PDF als Vektor, im Editor als Bild).
- generiert: Kopf mit Logo, Absender und Akzentlinie auf der ersten Seite, Fußzeilen und Seitenzahl auf jeder Seite;
  ab Seite 2 beginnt der Text weiter oben (``FOLGE_OBEN_GENERIERT``), weil dort kein Kopf steht.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from django.template.loader import render_to_string

from apps.common import schriften

if TYPE_CHECKING:
    from .models import OrganizationLetterhead

#: Seite (DIN A4 hoch)
SEITE_MM = {"breite_mm": 210, "hoehe_mm": 297}
#: Ränder ohne Briefpapier (wie der bisherige PDF-Export)
STANDARD_RAENDER = {"oben": 30, "unten": 25, "links": 25, "rechts": 25}
#: Abstand oben ab Seite 2 beim generierten Briefkopf (der Kopf steht nur auf Seite 1)
FOLGE_OBEN_GENERIERT = 25
#: Zeilenabstand des Fließtexts (Vielfaches der Schriftgröße)
ZEILENABSTAND = 1.5
STANDARD_GROESSE_PT = 11
_FARBE_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


def _kopf_html(letterhead: OrganizationLetterhead) -> str:
    """Kopf des generierten Briefkopfs (Logo eingebettet, damit er auch im Druck ohne Netz erscheint)."""
    from apps.work.pdf_bilder import file_data_uri

    from .export_service import generated_letterhead_context

    kontext = generated_letterhead_context(letterhead)
    kontext["logo_src"] = (
        file_data_uri(letterhead.organization.effective_logo) if letterhead.header_logo_enabled else ""
    )
    return render_to_string("work/motions/satz/_kopf.html", kontext).strip()


def satz_konfig(letterhead: OrganizationLetterhead | None, *, pdf_url: str = "") -> dict[str, Any]:
    """
    Konfiguration für ein Dokument mit diesem Briefpapier (oder ohne). ``pdf_url`` ist die Adresse der Datei eines
    PDF-Briefpapiers für den Editor; im Druck bleibt sie leer, dort legt der Server das Briefpapier unter.
    """
    if letterhead is not None and (letterhead.is_generated or letterhead.pdf_file):
        raender = {
            "oben": letterhead.content_margin_top,
            "unten": letterhead.content_margin_bottom,
            "links": letterhead.content_margin_left,
            "rechts": letterhead.content_margin_right,
        }
        schrift = schriften.aufloesen(letterhead.font_family)
        groesse = letterhead.font_size or STANDARD_GROESSE_PT
    else:
        letterhead = None
        raender = dict(STANDARD_RAENDER)
        schrift = schriften.aufloesen(None)
        groesse = STANDARD_GROESSE_PT

    generiert = letterhead is not None and letterhead.is_generated
    briefpapier: dict[str, Any] = {
        "art": "",
        "pdf_url": "",
        "kopf_html": "",
        "akzent": "",
        "fuss_zeilen": [],
        "seitenzahlen": False,
    }
    if letterhead is not None and generiert:
        # Farbe der Akzentlinie als CSS-Variable (boegen.ts): nur eine Hex-Farbe
        farbe = str(letterhead.organization.effective_primary_color or "")
        briefpapier.update(
            akzent=farbe if _FARBE_RE.fullmatch(farbe) else "#4b5563",
            art="generiert",
            kopf_html=_kopf_html(letterhead),
            fuss_zeilen=[zeile.strip() for zeile in letterhead.footer_text.splitlines() if zeile.strip()],
            seitenzahlen=True,
        )
    elif letterhead is not None:
        briefpapier.update(art="pdf", pdf_url=pdf_url)

    konfig: dict[str, Any] = {
        "seite": dict(SEITE_MM),
        "raender": {
            **raender,
            "oben_folge": min(raender["oben"], FOLGE_OBEN_GENERIERT) if generiert else raender["oben"],
            "unten_folge": raender["unten"],
        },
        "schrift": {
            "schluessel": schrift.schluessel,
            "stapel": schrift.css_stapel,
            "groesse_pt": max(schriften.GROESSE_MIN, min(float(groesse), schriften.GROESSE_MAX)),
            "zeilenabstand": ZEILENABSTAND,
        },
        "briefpapier": briefpapier,
        "schriften": [{"schluessel": schrift.schluessel, "name": schrift.name}],
    }
    if letterhead is not None and not generiert and letterhead.satz:
        _treu_uebernehmen(konfig, letterhead.satz)
    return konfig


def _mm(wert: Any, standard: float, *, hoechstens: float = 400.0) -> float:
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return standard
    return max(0.0, min(zahl, hoechstens))


def _treu_uebernehmen(konfig: dict[str, Any], satz: dict[str, Any]) -> None:
    """
    Genaue Maße eines treu übernommenen PDFs (Issue #1600, ``pdf_treu.py``): Seitengröße und Ränder als Gleitkomma,
    Grundschrift, Hintergrundseite je Seite und die Seitenzahl an der Stelle des Originals. ``treu`` schaltet im Satz
    Unterschneidung und Ligaturen ab, wie Word sie standardmäßig setzt.
    """
    seite = satz.get("seite") or {}
    konfig["seite"] = {
        "breite_mm": _mm(seite.get("breite_mm"), SEITE_MM["breite_mm"], hoechstens=1200),
        "hoehe_mm": _mm(seite.get("hoehe_mm"), SEITE_MM["hoehe_mm"], hoechstens=1200),
    }
    raender = satz.get("raender") or {}
    alt = konfig["raender"]
    konfig["raender"] = {schluessel: _mm(raender.get(schluessel), float(alt[schluessel])) for schluessel in alt}
    schrift = schriften.schrift(str(satz.get("schrift") or "")) or schriften.aufloesen(None)
    groesse = _mm(satz.get("groesse_pt"), STANDARD_GROESSE_PT, hoechstens=schriften.GROESSE_MAX)
    konfig["schrift"].update(
        schluessel=schrift.schluessel,
        stapel=schrift.css_stapel,
        groesse_pt=max(schriften.GROESSE_MIN, groesse),
    )
    konfig["schriften"] = [{"schluessel": schrift.schluessel, "name": schrift.name}]
    seiten = satz.get("hintergrund_seiten")
    konfig["briefpapier"]["seiten"] = (
        [int(s) for s in seiten if isinstance(s, int) and 0 <= s < 10_000] if isinstance(seiten, list) else []
    )
    konfig["briefpapier"]["seitenzahl"] = seitenzahl = _seitenzahl(satz.get("seitenzahl"))
    if seitenzahl is not None and seitenzahl["schluessel"] != schrift.schluessel:
        zahl_schrift = schriften.schrift(seitenzahl["schluessel"])
        if zahl_schrift is not None:
            konfig["schriften"].append({"schluessel": zahl_schrift.schluessel, "name": zahl_schrift.name})
    konfig["treu"] = True
    konfig["treu_unterschneidung"] = bool(satz.get("unterschneidung"))


def _seitenzahl(roh: Any) -> dict[str, Any] | None:
    """Seitenzahl des Originals (Text mit ``{n}``/``{gesamt}``, Lage in mm, Schrift) oder ``None``."""
    if not isinstance(roh, dict):
        return None
    text = str(roh.get("text") or "")[:80]
    if "{n}" not in text:
        return None
    schrift = schriften.schrift(str(roh.get("schrift") or "")) or schriften.aufloesen(None)
    ausrichtung = roh.get("ausrichtung") if roh.get("ausrichtung") in ("links", "mitte", "rechts") else "links"
    return {
        "text": text,
        "grund_mm": _mm(roh.get("grund_mm"), 0.0),
        "x_mm": _mm(roh.get("x_mm"), 0.0),
        "mitte_mm": _mm(roh.get("mitte_mm"), 0.0),
        "rechts_mm": _mm(roh.get("rechts_mm"), 0.0),
        "ausrichtung": ausrichtung,
        "stapel": schrift.css_stapel,
        "schluessel": schrift.schluessel,
        "groesse_pt": max(schriften.GROESSE_MIN, _mm(roh.get("groesse_pt"), 10.0, hoechstens=schriften.GROESSE_MAX)),
        "fett": bool(roh.get("fett")),
        "ab_seite": int(_mm(roh.get("ab_seite"), 1.0, hoechstens=10_000)),
    }


def editor_schriften() -> dict[str, Any]:
    """Auswahl für Schriftart und -größe im Editor."""
    return {"liste": schriften.fuer_editor(), "groessen": list(schriften.GROESSEN)}
