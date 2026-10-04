# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ortsbezug der Suche (Konzept Insight-Suche, P0.8): Straße erkennen und „Was passiert in der Nähe?“.

Die Erkennung ist eine feste Regel über das eigene Straßenverzeichnis, kein Sprachmodell und kein externer
Dienst (Suchanfragen gehen nie an Dritte, auch nicht an Photon). Sie löst nur aus, wenn

* der Name mindestens vier Zeichen hat und kein Allgemeinwort ist („Schule“, „Markt“, „Hafen“),
* die Eingabe als ganze Wörter in **genau einem** Straßennamen vorkommt („witzleben“ → Von-Witzleben-Straße;
  „Hauptstraße“ in drei Ortsteilen bleibt ohne Band),
* mindestens die Hälfte der Vorgänge der Kommune verortet ist (``INSIGHT_SEARCH_PLACES_MIN_SHARE``) und
* der Schalter ``INSIGHT_SEARCH_PLACES`` an ist.

Das Band zeigt die neuesten Vorgänge im Umkreis (``nearby_papers`` nach Datum), fasst gleichlautende Vorlagen
zusammen (eine je Bezirksvertretung) und stellt Sammelvorlagen mit vielen Orten zurück, wenn sie die Straße
nicht selbst nennen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.urls import reverse
from django.utils.http import urlencode

from .gazetteer import normalize_street_name
from .search_presentation import german_date, normalize_paper_type

if TYPE_CHECKING:
    from insight_core.models import OParlBody

#: Grundwörter (normalisiert wie im Straßenverzeichnis)
_SUFFIXES: Final = ("strasse", "allee", "gasse", "platz", "damm", "pfad", "ring", "ufer", "wall", "weg")
#: Wörter, die allein nie einen Ort meinen, auch wenn ein Straßenname sie enthält
_GENERAL_WORDS: Final = frozenset(
    {
        *_SUFFIXES,
        "am",
        "an",
        "auf",
        "bahnhof",
        "berg",
        "brucke",
        "bruecke",
        "dorf",
        "feld",
        "garten",
        "hafen",
        "haushalt",
        "hof",
        "im",
        "in",
        "kirche",
        "friedhof",
        "kindergarten",
        "kita",
        "markt",
        "marktplatz",
        "parkplatz",
        "spielplatz",
        "sportplatz",
        "muhle",
        "park",
        "radweg",
        "rathaus",
        "schule",
        "see",
        "stadt",
        "von",
        "wald",
        "zentrum",
        "zum",
        "zur",
    }
)
_HOUSE_NUMBER: Final = re.compile(r"\s+\d+\s*[a-z]?$", re.IGNORECASE)
MIN_NAME_CHARS: Final = 4
#: Ab so vielen Orten gilt ein Vorgang als Sammelvorlage (Berichte, Programme über die ganze Stadt)
SAMMEL_ORTE: Final = 20
#: So nah liegt ein Ort, wenn der Vorgang die Straße selbst nennt
NENNT_STRASSE_M: Final = 50
MAX_ROWS: Final = 5


@dataclass(frozen=True)
class Place:
    """Erkannter Ort: Name aus dem Verzeichnis und Mittelpunkt."""

    name: str
    lat: float
    lon: float


def enabled() -> bool:
    return bool(getattr(settings, "INSIGHT_SEARCH_PLACES", True))


def _variants(text: str) -> list[str]:
    """Normalisierte Lesarten: wie eingegeben und mit abgetrenntem Grundwort („witzlebenstrasse“)."""
    normalized = normalize_street_name(_HOUSE_NUMBER.sub("", text))
    variants = [normalized]
    for suffix in _SUFFIXES:
        if normalized.endswith(suffix) and not normalized.endswith(" " + suffix):
            stem = normalized[: -len(suffix)].strip()
            if len(stem) >= MIN_NAME_CHARS:
                variants.append(f"{stem} {suffix}")
            break
    return variants


def _unique_street(body: OParlBody, text: str) -> Place | None:
    from django.db.models import Avg

    from insight_core.models import Street

    for normalized in _variants(text):
        letters = normalized.replace(" ", "")
        if len(letters) < MIN_NAME_CHARS or normalized in _GENERAL_WORDS:
            continue
        rows = list(
            Street.objects.filter(body=body, normalized_name__contains=normalized)
            .values("normalized_name")
            .annotate(lat=Avg("latitude"), lon=Avg("longitude"))
            .order_by("normalized_name")[:50]
        )
        # nur ganze Wörter: „schule“ trifft nicht „schulenburg strasse“
        matches = [row for row in rows if f" {normalized} " in f" {row['normalized_name']} "]
        if len(matches) == 1:
            name = (
                Street.objects.filter(body=body, normalized_name=matches[0]["normalized_name"])
                .values_list("name", flat=True)
                .first()
            )
            return Place(name=str(name or text), lat=float(matches[0]["lat"]), lon=float(matches[0]["lon"]))
        if matches:
            return None  # mehrdeutig („Hauptstraße“): lieber kein Band als das falsche
    return None


def detect_place(body: OParlBody | None, query: str) -> Place | None:
    """Eindeutige Straße zur Anfrage (ganz oder ein Wort mit Grundwort, z. B. „Radweg Hafenstraße“)."""
    if body is None or not enabled():
        return None
    text = " ".join((query or "").split())
    if len(text) < MIN_NAME_CHARS:
        return None
    candidates = [text]
    candidates += [token for token in text.split() if any(normalize_street_name(token).endswith(s) for s in _SUFFIXES)]
    for candidate in dict.fromkeys(candidates):
        place = _unique_street(body, candidate)
        if place is not None:
            return place
    return None


def place_band(body: OParlBody, place: Place, query: str = "") -> dict[str, Any] | None:
    """Inhalt des Ortsbands oder ``None`` (zu wenig verortet, nichts in der Nähe)."""
    from .paper_locations import count_nearby_papers, located_share, location_counts, nearby_papers

    if located_share(body) < float(getattr(settings, "INSIGHT_SEARCH_PLACES_MIN_SHARE", 0.5)):
        return None
    radius = int(getattr(settings, "INSIGHT_SEARCH_PLACES_RADIUS", 500))
    candidates = nearby_papers(body, place.lat, place.lon, radius, limit=50, order="date")
    if not candidates:
        return None
    orte = location_counts([c["id"] for c in candidates])
    sammel = [c for c in candidates if orte.get(c["id"], 0) >= SAMMEL_ORTE and c["distance"] > NENNT_STRASSE_M]
    lokal = [c for c in candidates if c not in sammel]

    zeilen: dict[tuple[str, Any], dict[str, Any]] = {}
    for kandidat in lokal:
        schluessel = (" ".join(str(kandidat["name"] or "").lower().split()), kandidat["date"])
        zeile = zeilen.get(schluessel)
        if zeile is None:
            zeilen[schluessel] = {
                "title": kandidat["name"] or kandidat["reference"] or "Vorgang",
                "url": kandidat["url"],
                "date": german_date(kandidat["date"]),
                "art": normalize_paper_type(kandidat["paper_type"], body.slug or ""),
                "count": 1,
                "distance": kandidat["distance"],
                "lat": kandidat["lat"],
                "lon": kandidat["lon"],
            }
        else:
            zeile["count"] += 1
            zeile["distance"] = min(zeile["distance"], kandidat["distance"])
    rows = list(zeilen.values())[:MAX_ROWS]
    if not rows and not sammel:
        return None
    for row in rows:
        row["nennt"] = row["distance"] <= NENNT_STRASSE_M

    ort: dict[str, str] = {"lat": f"{place.lat:.5f}", "lon": f"{place.lon:.5f}", "name": place.name}
    return {
        "name": place.name,
        "radius": radius,
        "rows": rows,
        "sammel": len(sammel),
        "sammel_beispiel": (sammel[0]["name"] or "") if sammel else "",
        "total": count_nearby_papers(body, place.lat, place.lon, radius),
        "map_url": reverse("insight_core:insight:neighborhood") + "?" + urlencode({**ort, "radius": str(radius)}),
        "abo_url": reverse("insight_core:insight:notifications") + "?" + urlencode({"type": "location", **ort}),
        "map_data": {
            "center": [place.lat, place.lon],
            "radius": radius,
            "name": place.name,
            "points": [[row["lat"], row["lon"]] for row in rows],
        },
    }
