# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsort mit Postanschrift für die strukturierten Daten einer Sitzungsseite (Issue #939).

Google zeigt eine Veranstaltung nur mit einem Ort samt Postanschrift (``location.address``). Die Anschrift
kommt aus dem, was die Quelle zur Sitzung liefert, in dieser Rangfolge:

1. dem Ort der Sitzung in ihren Rohdaten (OParl ``Meeting.location``): eingebettet oder – nur als Verweis
   geliefert – die verknüpfte ``OParlLocation`` mit Straße, Postleitzahl, Ort und Raum;
2. dem Text der Anschrift an der Sitzung (``location_address``, etwa „Musterstraße 1, 48143 Münster“) und
   einer Postleitzahl mit Ort am Ende der Ortsbeschreibung;
3. fehlt der Ort, dem Ort der Kommune – nur bei einer Gemeinde (achtstelliger Gemeindeschlüssel) mit
   eindeutigem Namen aus dem Kommunenverzeichnis bzw. dem gepflegten Anzeigenamen. Kreise, Regionalräte,
   Bezirksregierungen und Körperschaften ohne eigenes Gebiet bekommen keinen erfundenen Ort.

Ohne Straße und ohne Ort gibt es keinen verlässlichen Sitzungsort (``None``); die Seite trägt dann keine
Veranstaltung. Abfragen: höchstens eine für den verknüpften Ort (nur bei einem Verweis) und eine im
Kommunenverzeichnis (nur ohne Ort).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Längste übernommene Angabe (Zeichen); Rohdaten fremder Systeme sind nicht begrenzt
_MAX = 200
#: Postleitzahl und Ort am Ende eines Textes: „…, 48143 Münster“ bzw. „D-48143 Münster“
_PLZ_ORT = re.compile(r"(?:^|,)\s*(?:D-)?(?P<plz>\d{5})\s+(?P<ort>[^\d,][^,]*?)\s*$")


@dataclass(frozen=True)
class Sitzungsort:
    """Ort einer Sitzung: Raum bzw. Gebäude (``name``) und Postanschrift."""

    name: str = ""
    strasse: str = ""
    plz: str = ""
    ort: str = ""

    def postanschrift(self) -> dict[str, str]:
        """``PostalAddress`` nach schema.org; leere Angaben fehlen."""
        anschrift = {
            "@type": "PostalAddress",
            "streetAddress": self.strasse,
            "postalCode": self.plz,
            "addressLocality": self.ort,
            "addressCountry": "DE",
        }
        return {schluessel: wert for schluessel, wert in anschrift.items() if wert}


def _text(wert: Any) -> str:
    return " ".join(wert.split())[:_MAX] if isinstance(wert, str) else ""


def anschrift_aus_text(text: Any, *, nur_mit_plz: bool = False) -> tuple[str, str, str]:
    """Straße, Postleitzahl und Ort aus einem Anschriftstext.

    „Rathaus, Musterstraße 1, 48143 Münster“ ergibt („Musterstraße 1“, „48143“, „Münster“): Vor Postleitzahl und
    Ort steht die Straße, davor ein Gebäude. Ohne Postleitzahl gilt der ganze Text als Straße – außer mit
    ``nur_mit_plz`` (für Beschreibungen, die auch nur einen Raum nennen können).
    """
    text = _text(text)
    treffer = _PLZ_ORT.search(text)
    if treffer is None:
        return ("", "", "") if nur_mit_plz else (text, "", "")
    davor = [teil.strip() for teil in text[: treffer.start()].split(",") if teil.strip()]
    return (davor[-1] if davor else "", treffer["plz"], treffer["ort"])


def _ort_der_quelle(meeting: Any) -> dict[str, Any]:
    """Ort der Sitzung aus den Rohdaten: eingebettet, sonst die verknüpfte ``OParlLocation``."""
    raw = meeting.raw_json if isinstance(meeting.raw_json, dict) else {}
    ort = raw.get("location")
    if isinstance(ort, dict):
        return ort
    if not isinstance(ort, str) or not ort:
        return {}
    from ..models import OParlLocation

    verknuepft = (
        OParlLocation.objects.filter(external_id=ort, deleted=False)
        .only("street_address", "postal_code", "locality", "room", "description")
        .first()
    )
    if verknuepft is None:
        return {}
    return {
        "streetAddress": verknuepft.street_address,
        "postalCode": verknuepft.postal_code,
        "locality": verknuepft.locality,
        "room": verknuepft.room,
        "description": verknuepft.description,
    }


def ort_der_kommune(body: Any) -> str:
    """Ort einer Gemeinde für die Anschrift, leer, wenn er nicht eindeutig bekannt ist.

    Nur Gemeinden haben einen achtstelligen Gemeindeschlüssel; Kreise, Regierungsbezirke und Regionalräte
    tragen ihn gekürzt oder gar nicht. Der Name kommt aus dem Kommunenverzeichnis (ohne Zusatz „Stadt“), sonst
    aus dem gepflegten Anzeigenamen – nie aus dem amtlichen Namen („Stadt Münster“).
    """
    if body is None or getattr(body, "is_non_territorial", False):
        return ""
    ags = str(getattr(body, "ags", "") or "").strip()
    if len(ags) != 8 or not ags.isdigit():
        return ""
    from ..models import Municipality

    namen = {
        _text(name)
        for name in Municipality.objects.filter(ags=ags, is_association=False).values_list("name", flat=True)[:3]
    } - {""}
    if namen:
        return namen.pop() if len(namen) == 1 else ""
    return _text(getattr(body, "display_name", ""))


def sitzungsort(meeting: Any, body: Any) -> Sitzungsort | None:
    """Ort einer Sitzung der Kommune ``body`` mit Postanschrift, ``None`` ohne Straße und ohne Ort."""
    quelle = _ort_der_quelle(meeting)
    name = _text(meeting.location_name) or _text(quelle.get("room")) or _text(quelle.get("description"))
    strasse, plz, ort = (
        _text(quelle.get("streetAddress")),
        _text(quelle.get("postalCode")),
        _text(quelle.get("locality")),
    )
    for text, nur_mit_plz in (
        (meeting.location_address, False),
        (quelle.get("description"), True),
        (meeting.location_name, True),
    ):
        if strasse and plz and ort:
            break
        aus_text = anschrift_aus_text(text, nur_mit_plz=nur_mit_plz)
        strasse, plz, ort = strasse or aus_text[0], plz or aus_text[1], ort or aus_text[2]
    ort = ort or ort_der_kommune(body)
    if not (strasse or ort):
        return None
    return Sitzungsort(name=name, strasse=strasse, plz=plz, ort=ort)
