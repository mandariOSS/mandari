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
    # Die Farbe steht in einem style-Attribut: nur eine Hex-Farbe zulassen
    if not _FARBE_RE.fullmatch(str(kontext.get("primary_color") or "")):
        kontext["primary_color"] = "#4b5563"
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
        "fuss_zeilen": [],
        "seitenzahlen": False,
    }
    if generiert:
        briefpapier.update(
            art="generiert",
            kopf_html=_kopf_html(letterhead),
            fuss_zeilen=[zeile.strip() for zeile in letterhead.footer_text.splitlines() if zeile.strip()],
            seitenzahlen=True,
        )
    elif letterhead is not None:
        briefpapier.update(art="pdf", pdf_url=pdf_url)

    return {
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


def editor_schriften() -> dict[str, Any]:
    """Auswahl für Schriftart und -größe im Editor."""
    return {"liste": schriften.fuer_editor(), "groessen": list(schriften.GROESSEN)}
