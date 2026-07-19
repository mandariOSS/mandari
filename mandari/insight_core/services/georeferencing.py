# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Georeferenzierungs-Pipeline: Ortsextraktion + Geocoding für Papers.

2-Pass-Pipeline:
  Pass 1 (Regex + Gazetteer): Straßennamen/Hausnummern aus dem Volltext,
    validiert gegen das OSM-Straßenverzeichnis der Kommune (Street-Modell).
    Geometrie kommt lokal aus dem Gazetteer — kein API-Geocoding nötig.
  Pass 2 (KI): LLM-basierte Extraktion für schwierige Fälle (POIs, Gebäude);
    Geocoding via Photon (komoot.io) mit Nominatim-Fallback.

Ist kein Straßenverzeichnis importiert (import_streets), fällt Pass 1 auf
das alte Verhalten (Photon-Geocoding + Blocklist) zurück.

`locations`-Einträge tragen `source` (oparl|address_match|street_match|ai|manual)
und `confidence`. Einträge mit source oparl/manual werden bei Re-Runs nie
überschrieben (siehe update_paper_georef).
"""

import json
import logging
import math
import re
import time

import httpx
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# =============================================================================
# Photon Geocoder
# =============================================================================

PHOTON_API_URL = getattr(settings, "PHOTON_API_URL", "https://photon.komoot.io/api/")
GEOCODING_RATE_LIMIT = getattr(settings, "GEOCODING_RATE_LIMIT", 5)
# Kappung gilt nur noch für den LLM-Pass (Kosten) und den Legacy-Photon-Pfad
# (externe API-Calls); der Gazetteer-Pass arbeitet auf dem Volltext.
GEOREF_TEXT_MAX_CHARS = getattr(settings, "GEOREF_TEXT_MAX_CHARS", 8000)

# Obergrenze externer Geocoding-Calls pro Paper im Legacy-Pfad (ohne Gazetteer)
MAX_LEGACY_GEOCODES = 40

# Quellen-Konfidenzwerte für locations-Einträge
CONFIDENCE = {
    "oparl": 1.0,
    "address_match": 0.9,
    "street_match": 0.8,
    "ai": 0.6,
}

# Rate limiter state
_last_geocode_time = 0.0


def _rate_limit():
    """Simple rate limiter for geocoding API calls."""
    global _last_geocode_time
    if GEOCODING_RATE_LIMIT <= 0:
        return
    min_interval = 1.0 / GEOCODING_RATE_LIMIT
    elapsed = time.monotonic() - _last_geocode_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_geocode_time = time.monotonic()


# =============================================================================
# Regex-Extraktor (Pass 1)
# =============================================================================

# Case-insensitive suffix patterns (Str./str./STR., Straße/straße, etc.)
_STRONG_SUFFIX = r"(?i:stra(?:ß|ss)e|str\.|gasse|allee|chaussee)"
_WEAK_SUFFIXES = (
    "weg",
    "platz",
    "ring",
    "damm",
    "ufer",
    "brücke",
    "pfad",
    "steig",
    "stieg",
    "kamp",
    "bogen",
    "graben",
    "deich",
    "horst",
)
_WEAK_SUFFIX = r"(?i:" + "|".join(_WEAK_SUFFIXES) + r")"

# Street name pattern: "Wolbecker Str. 12a", "Hauptstraße", "Schillerstraße 5"
# NOTE: bare "str" (without dot) is NOT matched — it produces too many
# false positives from compound words like "Infrastruktur".
STREET_RE = re.compile(
    r"(?<![a-zäöüß/])"  # not preceded by lowercase
    r"("
    r"(?:[A-ZÄÖÜ][a-zäöüß]{2,}(?:[-\s](?:und\s|von\s|der\s)?)?){1,4}"
    + _STRONG_SUFFIX
    + r"(?:\s+\d{1,4}\s*[a-zA-Z]?)?"  # optional house number
    r")"
    r"(?![a-zäöüß])",  # not followed by lowercase
    re.UNICODE,
)

# Weak suffix streets: "Domplatz", "Lublinring", "Johann-Krane-Weg 14"
STREET_WEAK_RE = re.compile(
    r"(?<![a-zäöüß/])"
    r"("
    # Option A: Multi-part name "Johann-Krane-Weg", "Von-Steuben-Ring"
    r"(?:[A-ZÄÖÜ][a-zäöüß]{2,}[-\s](?:und\s|von\s|der\s)?){1,3}"
    r"(?:[A-ZÄÖÜ]?[a-zäöüß]*" + _WEAK_SUFFIX + r")"
    r"(?:\s+\d{1,4}\s*[a-zA-Z]?)?"
    r"|"
    # Option B: Compound word, name part ≥ 4 chars: "Domplatz", "Hafenufer"
    r"[A-ZÄÖÜ][a-zäöüß]{3,}" + _WEAK_SUFFIX + r"(?:\s+\d{1,4}\s*[a-zA-Z]?)?"
    r")"
    r"(?![a-zäöüß])",
    re.UNICODE,
)

# Prepositional streets: "Am Markt 3", "An der Mühle 5", "Auf dem Berg"
PREP_STREET_RE = re.compile(
    r"(?<!\w)"
    r"("
    r"(?:Am|An\sder?|Auf\sde[mr]|Beim?|Zum?r?)\s"
    r"(?:[A-ZÄÖÜ][a-zäöüß]{2,}(?:[-\s])?){1,3}"
    r"(?:\s+\d{1,4}\s*[a-zA-Z]?)?"
    r")"
    r"(?![a-zäöüß])",
    re.UNICODE,
)


# --- Blocklist: words/phrases that are NOT locations ---
_BLOCKLIST = {
    # Generic nouns with street-like suffixes
    "hintergrund",
    "vordergrund",
    "untergrund",
    "themenfeld",
    "arbeitsfeld",
    "aufgabenfeld",
    "baufeld",
    "planungsfeld",
    "handlungsfeld",
    "berufsfeld",
    "sachgebiet",
    "grundlage",
    "grundlagen",
    "sachgrund",
    "antragsgrund",
    "einstieg",
    "ausstieg",
    "aufstieg",
    "umstieg",
    "abstieg",
    "fortgang",
    "zugang",
    "eingang",
    "ausgang",
    "übergang",
    "zustand",
    "gegenstand",
    "bestand",
    "widerstand",
    "abstand",
    "vorfeld",
    "umfeld",
    # Government/institutional
    "stadtrat",
    "stadtverwaltung",
    "stadtverordnete",
    "stadtplan",
    "stadtverband",
    "stadtteil",
    "stadtgebiet",
    "stadtbezirk",
    "bundesstraße",
    "bundesfernstraße",
    "landesstraße",
    "kreisstraße",
    "einbahnstraße",
    "fahrradstraße",
    "spielstraße",
    "sackgasse",
    "anliegerstraße",
    "sammelstraße",
    "erschließungsstraße",
    "haupteinfallstraße",
    "durchgangsstraße",
    "schnellstraße",
    "ortsumgehungsstraße",
    "umgehungsstraße",
    "verbindungsstraße",
    "rettungsgasse",
    "feuergasse",
    "notgasse",
    # Prepositional non-locations
    "zur umsetzung",
    "zur erreichung",
    "zur sicherung",
    "zur förderung",
    "zur verbesserung",
    "zur vermeidung",
    "zur unterstützung",
    "zur schriftführung",
    "zur verfügung",
    "zur kenntnis",
    "zur genehmigung",
    "zur abstimmung",
    "zur beratung",
    "zum schutz",
    "zum schuljahr",
    "zum sachverhalt",
    "zum thema",
    "zum verfahren",
    "zum zeitpunkt",
    "zum anlass",
    "bei auflösung",
    "bei anbietern",
    "bei umbenennungsvorhaben",
    "bei bedarf",
    "bei fragen",
    "bei rückfragen",
    "beim bau",
    "beim umbau",
    "beim neubau",
    "beim ausbau",
    "am standort",
    "am anfang",
    "am ende",
    "am beispiel",
    "an der stelle",
    "an der spitze",
    "auf der grundlage",
    "auf der basis",
    "auf dem markt",
}

# Generic road-type words (valid German but not specific street names)
_GENERIC_ROAD_TYPES = {
    "bundesstraße",
    "bundesfernstraße",
    "landesstraße",
    "kreisstraße",
    "einbahnstraße",
    "fahrradstraße",
    "spielstraße",
    "sackgasse",
    "anliegerstraße",
    "sammelstraße",
    "erschließungsstraße",
    "haupteinfallstraße",
    "durchgangsstraße",
    "schnellstraße",
    "ortsumgehungsstraße",
    "umgehungsstraße",
    "verbindungsstraße",
    "rettungsgasse",
    "feuergasse",
    "notgasse",
    "hauptstraße",  # often used generically
    # Compound nouns that look like streets but aren't
    "vorzugstraße",
    "vorzugstrasse",
    "vorschlagstraße",
    "vorschlagstrasse",
    "zufahrtsstraße",
    "zufahrtstraße",
    "nebenstraße",
    "querstraße",
    "parallelstraße",
    "gegenstraße",
    "rückstraße",
    "wohnstraße",
    "geschäftsstraße",
    "einkaufsstraße",
    "innerortsstraße",
    "außerortsstraße",
    "ringstraße",
    "dorfstraße",
    "bergstraße",
    "waldstraße",
    # Generic path/road type words
    "schotterweg",
    "fußweg",
    "fahrweg",
    "radweg",
    "feldweg",
    "waldweg",
    "gehweg",
    "wanderweg",
    "reitweg",
    "wirtschaftsweg",
    "gemeindeweg",
    "privatweg",
    "fahrgasse",
    "feuergasse",
    "gemeindestraße",
    "landestraße",  # variant without 's'
    # Generic phrases captured by regex
    "die straße",
    "der straße",
    "eine straße",
    "dieser straße",
    "der weg",
    "die gasse",
    "die allee",
    "die brücke",
    "der brücke",
    "einer brücke",
    "umbenennung der straße",
    "sanierung der brücke",
    "umgestaltung der straße",
    "entwässerung der straße",
    "der name",
    "straßennamen",
    "zu beschlusspunkt",
    "zu beschlusspunkten",
    # Not locations
    "carsharing",
}

# Leading articles to strip before blocklist check
_ARTICLES = re.compile(r"^(?:die|der|das|den|dem|eine?|eines?|einem?|einen?)\s+", re.IGNORECASE)

# Normalization map
_NORMALIZE_MAP = {
    "str.": "straße",
    "strasse": "straße",
}


def _normalize_street(raw: str) -> str:
    """Normalize street name abbreviations."""
    result = raw.strip()
    for abbr, full in _NORMALIZE_MAP.items():
        result = re.sub(
            r"(?i)\b" + re.escape(abbr) + r"(?=\s|\d|$)",
            full,
            result,
        )
    return result


def _is_blocked(text: str) -> bool:
    """Check if text matches a known false-positive pattern."""
    lower = text.lower().strip()

    # Strip leading articles: "Die Bundesstraße" → "bundesstraße"
    core = _ARTICLES.sub("", lower).strip()

    # Check both the full text and the article-stripped version
    for check in (lower, core):
        if check in _BLOCKLIST or check in _GENERIC_ROAD_TYPES:
            return True
        for blocked in _BLOCKLIST:
            if check == blocked or check.startswith(blocked + " "):
                return True

    return False


def extract_with_regex(text: str) -> list[dict]:
    """
    Extract location references from text using regex patterns.

    Returns:
        List of dicts with keys: raw, type, normalized
    """
    results = []
    seen = set()

    def _add(raw: str, loc_type: str = "street"):
        raw = raw.strip()
        # Reject if contains newlines (OCR artifact / multi-line match)
        if "\n" in raw or "\r" in raw:
            return
        if len(raw) < 5 or _is_blocked(raw):
            return
        normalized = _normalize_street(raw)
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            results.append(
                {
                    "raw": raw,
                    "type": loc_type,
                    "normalized": normalized,
                }
            )

    for match in STREET_RE.finditer(text):
        _add(match.group(1))

    for match in STREET_WEAK_RE.finditer(text):
        _add(match.group(1))

    for match in PREP_STREET_RE.finditer(text):
        _add(match.group(1))

    return results


# =============================================================================
# KI-Extraktor (Pass 2)
# =============================================================================


def extract_locations_with_ai(text: str, body_name: str) -> list[dict]:
    """
    Extract location references using LLM (Nebius provider).

    Args:
        text: Document text content
        body_name: Name of the municipality for context

    Returns:
        List of dicts with keys: raw, type, normalized
    """
    try:
        from insight_ai.providers.base import ChatMessage
        from insight_ai.providers.nebius import NebiusProvider
        from insight_ai.services.prompts import (
            GEOREF_SYSTEM_PROMPT,
            build_georef_user_prompt,
        )
    except ImportError:
        logger.warning("insight_ai not available, skipping AI extraction")
        return []

    provider = NebiusProvider()
    if not provider.is_available():
        logger.warning("Nebius provider not available, skipping AI extraction")
        return []

    user_prompt = build_georef_user_prompt(text, body_name)
    messages = [
        ChatMessage(role="system", content=GEOREF_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    ]

    try:
        response = provider.chat_completion(messages, max_tokens=2000, temperature=0.1)
        return _parse_ai_locations(response.content)
    except Exception as e:
        logger.error(f"AI location extraction failed: {e}")
        return []


def _parse_ai_locations(content: str) -> list[dict]:
    """Parse JSON array from AI response content."""
    content = content.strip()

    if content.startswith("```"):
        lines = content.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                logger.warning(f"Could not parse AI response as JSON: {content[:200]}")
                return []
        else:
            return []

    if not isinstance(data, list):
        return []

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw", "").strip()
        if not raw:
            continue
        results.append(
            {
                "raw": raw,
                "type": item.get("type", "unknown"),
                "normalized": item.get("normalized", raw).strip(),
            }
        )

    return results


# =============================================================================
# Geocoder (Photon API → Nominatim Fallback)
# =============================================================================

# Photon osm_key values that indicate actual geographic locations
_VALID_OSM_KEYS = {
    "highway",  # streets, roads, paths
    "place",  # named places, districts
    "building",  # specific buildings (when name matches)
    "amenity",  # schools, town halls, etc. (validated)
    "leisure",  # parks, playgrounds
    "natural",  # lakes, rivers
    "waterway",  # canals, rivers
    "landuse",  # specific land areas
    "boundary",  # administrative boundaries
}


def geocode_address(address: str, body) -> dict | None:
    """
    Geocode an address using Photon API, biased towards the body's location.
    Validates that the result actually matches the search term.

    For map markers, street-level accuracy is sufficient, so house numbers
    are stripped if the full address doesn't return a valid result.

    Args:
        address: Address string to geocode
        body: OParlBody instance for location bias

    Returns:
        Dict with lat, lon, name or None if not found
    """
    _rate_limit()

    # Try full address first
    result = _geocode_photon(address, body)
    if result:
        return result

    # Strip house number and retry (Photon works better at street level)
    street_only = re.sub(r"\s+\d{1,4}\s*[a-zA-Z]?\s*$", "", address).strip()
    if street_only != address:
        _rate_limit()
        result = _geocode_photon(street_only, body)
        if result:
            return result

    # Fallback: Nominatim (1 req/sec)
    time.sleep(1.0)
    return _geocode_nominatim(address, body)


def _geocode_photon(address: str, body) -> dict | None:
    """Geocode via Photon API (komoot.io) with result validation."""
    params = {
        "q": f"{address}, {body.name}",
        "limit": "3",  # Get multiple results for validation
        "lang": "de",
    }

    if body.latitude and body.longitude:
        params["lat"] = str(body.latitude)
        params["lon"] = str(body.longitude)

    try:
        response = httpx.get(
            PHOTON_API_URL,
            params=params,
            timeout=10.0,
            headers={"User-Agent": "Mandari/1.0 (https://mandari.de)"},
        )
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            return None

        # Try each result — pick the first one that passes validation
        for feature in features:
            coords = feature["geometry"]["coordinates"]  # [lon, lat]
            props = feature["properties"]
            osm_key = props.get("osm_key", "")
            result_name = props.get("name") or props.get("street") or ""
            result_street = props.get("street") or ""

            # Validation 1: Must be a geographic feature, not random POI
            if osm_key not in _VALID_OSM_KEYS:
                continue

            # Validation 2: Result must be related to the search query.
            # Check if either the result name/street contains part of the
            # search, or the search contains part of the result name.
            if not _result_matches_query(address, result_name, result_street, body_name=body.name):
                continue

            # Prefer the street name for display, fall back to name
            display_name = result_street or result_name or address
            if osm_key == "highway" and result_name:
                display_name = result_name

            return {"lat": coords[1], "lon": coords[0], "name": display_name}

        return None

    except Exception as e:
        logger.debug(f"Photon geocoding failed for '{address}': {e}")
        return None


def _result_matches_query(
    query: str,
    result_name: str,
    result_street: str,
    body_name: str = "",
) -> bool:
    """
    Check if a geocoding result is actually related to the search query.

    Prevents Photon from returning e.g. a courthouse when we searched
    for "Infrastr" or other garbage.
    """
    q_lower = query.lower().strip()
    name_lower = result_name.lower().strip()
    street_lower = result_street.lower().strip()

    # Normalize straße variants for comparison
    def _norm(s):
        return s.replace("str.", "straße").replace("strasse", "straße")

    q_norm = _norm(q_lower)
    name_norm = _norm(name_lower)
    street_norm = _norm(street_lower)

    # Extract the core street name (without house number)
    q_core = re.sub(r"\s+\d{1,4}\s*[a-zA-Z]?$", "", q_norm).strip()

    # Direct match: query is in result or result is in query
    if q_core and (q_core in name_norm or q_core in street_norm):
        return True
    if name_norm and name_norm in q_norm:
        return True
    if street_norm and street_norm in q_norm:
        return True

    # Word-based overlap: at least one significant word (≥4 chars) must match.
    # Exclude generic suffix words that match too broadly.
    _GENERIC_WORDS = {
        "straße",
        "strasse",
        "gasse",
        "allee",
        "chaussee",
        "weg",
        "platz",
        "ring",
        "damm",
        "ufer",
        "brücke",
        "pfad",
        "steig",
        "stieg",
        "stadt",
    }
    # Name der Kommune generisch ausschließen (statt Hardcoding einzelner Städte)
    generic_words = set(_GENERIC_WORDS)
    if body_name:
        generic_words.update(w for w in re.split(r"[-\s,]+", _norm(body_name.lower())) if w)

    q_words = {w for w in re.split(r"[-\s]+", q_core) if len(w) >= 4} - generic_words
    result_words = set()
    for s in (name_norm, street_norm):
        result_words.update(w for w in re.split(r"[-\s]+", s) if len(w) >= 4)
    result_words -= generic_words

    if q_words and result_words and (q_words & result_words):
        return True

    return False


def _geocode_nominatim(address: str, body) -> dict | None:
    """Geocode via Nominatim (OpenStreetMap) as fallback."""
    params = {
        "q": f"{address}, {body.name}",
        "format": "json",
        "limit": "1",
        "accept-language": "de",
    }

    if all([body.bbox_south, body.bbox_north, body.bbox_west, body.bbox_east]):
        params["viewbox"] = f"{body.bbox_west},{body.bbox_north},{body.bbox_east},{body.bbox_south}"
        params["bounded"] = "1"

    try:
        response = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            timeout=10.0,
            headers={"User-Agent": "Mandari/1.0 (https://mandari.de)"},
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            return None

        result = results[0]
        return {
            "lat": float(result["lat"]),
            "lon": float(result["lon"]),
            "name": result.get("display_name", address).split(",")[0],
        }

    except Exception as e:
        logger.debug(f"Nominatim geocoding failed for '{address}': {e}")
        return None


# =============================================================================
# BBox-Validierung
# =============================================================================


def is_within_body(lat: float, lon: float, body) -> bool:
    """
    Check if coordinates are within the body's bounding box (with 10% margin).

    Returns False if no bbox is configured — geocoding without bbox
    is unreliable and may return worldwide results.
    """
    if not all([body.bbox_south, body.bbox_north, body.bbox_west, body.bbox_east]):
        return False  # No bbox → reject (can't validate)

    margin_lat = float(body.bbox_north - body.bbox_south) * 0.1
    margin_lon = float(body.bbox_east - body.bbox_west) * 0.1

    return (
        float(body.bbox_south) - margin_lat <= lat <= float(body.bbox_north) + margin_lat
        and float(body.bbox_west) - margin_lon <= lon <= float(body.bbox_east) + margin_lon
    )


# =============================================================================
# Deduplizierung
# =============================================================================


def deduplicate_locations(locations: list[dict], threshold_meters: float = 50.0) -> list[dict]:
    """
    Deduplicate locations that are within threshold distance of each other.

    Keeps the first occurrence (assumed to be higher quality).
    """
    if not locations:
        return []

    result = []
    for loc in locations:
        is_dup = False
        for existing in result:
            dist = _haversine_distance(
                loc["lat"],
                loc["lon"],
                existing["lat"],
                existing["lon"],
            )
            if dist < threshold_meters:
                is_dup = True
                break
        if not is_dup:
            result.append(loc)

    return result


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters using Haversine formula."""
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# =============================================================================
# Text-Sammlung
# =============================================================================

# Common boilerplate patterns in municipal PDFs (footers, headers, stamps)
_BOILERPLATE_RE = re.compile(
    r"(?:Sparkasse\s+\w+|"
    r"IBAN\s*[:\s]*DE\d{2}|"
    r"BIC\s*[:\s]*[A-Z]{8,11}|"
    r"(?:Tel|Fax|Telefon|Telefax)[.:\s]+[\d\s/\-+]+|"
    r"E-Mail[:\s]+\S+@\S+|"
    r"www\.\S+\.\w{2,4}|"
    r"Gezeichnet\s*:?\s*(?:gez\.|i\.\s*V\.|i\.\s*A\.)|"
    r"Drucksache\s*(?:Nr\.\s*)?\d+/\d+)"
)


def collect_paper_text(paper) -> str:
    """
    Collect all extracted text content from a paper's files.
    Strips common boilerplate (bank details, phone numbers, etc.).

    Only includes files with text_extraction_status=completed.
    """
    texts = []
    files = paper.files.filter(
        text_extraction_status="completed",
        text_content__isnull=False,
    ).exclude(text_content="")

    for f in files:
        if f.text_content:
            texts.append(f.text_content)

    combined = "\n\n".join(texts)

    # Also include paper name for context
    if paper.name:
        combined = f"{paper.name}\n\n{combined}"

    return combined.strip()


# =============================================================================
# Gazetteer-Matching (Pass 1, bevorzugt)
# =============================================================================


def _street_hit_to_location(hit: dict) -> dict:
    """Wandelt einen Gazetteer-Treffer in einen locations-Eintrag um."""
    source = "address_match" if hit.get("house_number") else "street_match"
    name = hit["name"]
    if hit.get("house_number"):
        name = f"{name} {hit['house_number']}"
    return {
        "lat": round(float(hit["lat"]), 7),
        "lon": round(float(hit["lon"]), 7),
        "name": name,
        "source": source,
        "confidence": CONFIDENCE[source],
    }


def _collect_street_hit(street_hits: dict, hit: dict) -> None:
    """Sammelt Treffer pro Straße; Vorkommen mit Hausnummer haben Vorrang."""
    key = hit["normalized"]
    existing = street_hits.get(key)
    if existing is None or (hit.get("house_number") and not existing.get("house_number")):
        street_hits[key] = hit


def _is_person_false_positive(hit: dict, person_names: set[str]) -> bool:
    """
    MST-Trick: Ein Straßenname, der auch der Name einer Person des Bodys ist,
    zählt nur mit Hausnummer oder eindeutigem Straßen-Suffix (z.B. "-straße").
    """
    from insight_core.services.gazetteer import has_street_suffix

    if hit.get("house_number"):
        return False
    if has_street_suffix(hit["normalized"]):
        return False
    return hit["normalized"] in person_names


def extract_with_gazetteer(text: str, gazetteer, person_names: set[str]) -> list[dict]:
    """
    Pass 1 mit Straßenverzeichnis: Regex-Kandidaten + direkte Volltextsuche,
    beides validiert gegen den Gazetteer. Geometrie kommt lokal aus OSM.

    Returns:
        Liste von locations-Einträgen (lat, lon, name, source, confidence).
    """
    street_hits: dict[str, dict] = {}

    # a) Regex-Kandidaten gegen das Straßenverzeichnis validieren
    for candidate in extract_with_regex(text):
        raw = candidate.get("normalized") or candidate.get("raw", "")
        hit = gazetteer.lookup(raw)
        if hit:
            _collect_street_hit(street_hits, hit)

    # b) Direkte Gazetteer-Suche im Volltext (findet auch Schreibvarianten,
    #    die das Regex nicht abdeckt)
    for hit in gazetteer.find_in_text(text):
        _collect_street_hit(street_hits, hit)

    # c) Personen-False-Positives ausschließen
    return [
        _street_hit_to_location(hit) for hit in street_hits.values() if not _is_person_false_positive(hit, person_names)
    ]


# =============================================================================
# Haupt-Pipeline
# =============================================================================


def process_paper_georef(paper, mode: str = "all") -> dict:
    """
    Extract and geocode location references from a paper.

    Args:
        paper: OParlPaper instance (with body relation)
        mode: "regex" | "ai" | "all"

    Returns:
        Dict with status, locations, method, error
    """
    from insight_core.services.gazetteer import StreetGazetteer, get_person_name_set

    # 1. Collect text from all files
    text = collect_paper_text(paper)
    if not text:
        return {"status": "skipped", "reason": "Kein Text verfügbar"}

    body = paper.body
    gazetteer = StreetGazetteer(body) if body else None

    geocoded: list[dict] = []
    method = "none"

    # 2. Pass 1: Regex + Gazetteer (Volltext, lokal) bzw. Legacy-Photon-Pfad
    if mode in ("regex", "all"):
        if gazetteer:
            person_names = get_person_name_set(body)
            geocoded = extract_with_gazetteer(text, gazetteer, person_names)
            if geocoded:
                method = "gazetteer"
        else:
            # Legacy-Fallback ohne Straßenverzeichnis: externes Geocoding,
            # gekappt (Text + Anzahl Calls), Ergebnis per BBox validiert.
            text_capped = text[:GEOREF_TEXT_MAX_CHARS]
            candidates = extract_with_regex(text_capped)
            for loc in candidates[:MAX_LEGACY_GEOCODES]:
                address = loc.get("normalized") or loc.get("raw", "")
                if not address:
                    continue
                result = geocode_address(address, body)
                if result and is_within_body(result["lat"], result["lon"], body):
                    _, house_number = _split_house_number(address)
                    source = "address_match" if house_number else "street_match"
                    result["source"] = source
                    result["confidence"] = CONFIDENCE[source]
                    geocoded.append(result)
            if geocoded:
                method = "regex"

    # 3. Pass 2 (KI) — nur wenn Pass 1 nichts gefunden hat (gekappter Text)
    if not geocoded and mode in ("ai", "all"):
        body_name = body.name if body else ""
        text_capped = text[:GEOREF_TEXT_MAX_CHARS]
        ai_candidates = extract_locations_with_ai(text_capped, body_name)
        for loc in ai_candidates:
            address = loc.get("normalized") or loc.get("raw", "")
            if not address:
                continue

            # Straßen-Kandidaten zuerst lokal im Gazetteer auflösen
            if gazetteer:
                hit = gazetteer.lookup(address)
                if hit:
                    geocoded.append(_street_hit_to_location(hit))
                    continue

            # POIs/sonstige Orte: Photon/Nominatim-Fallback mit BBox-Check
            result = geocode_address(address, body)
            if result and is_within_body(result["lat"], result["lon"], body):
                result["source"] = "ai"
                result["confidence"] = CONFIDENCE["ai"]
                geocoded.append(result)
        if geocoded:
            method = "ai" if method == "none" else f"{method}+ai"

    # 4. Deduplicate
    locations = deduplicate_locations(geocoded)

    if locations:
        return {
            "status": "completed",
            "locations": locations,
            "method": method,
        }
    elif mode == "regex":
        return {"status": "ai_needed", "locations": [], "method": method}
    else:
        return {"status": "no_locations", "locations": [], "method": method}


def _split_house_number(address: str) -> tuple[str, str | None]:
    from insight_core.services.gazetteer import strip_house_number

    return strip_house_number(address)


def update_paper_georef(paper, result: dict) -> None:
    """
    Update a paper's georef fields based on pipeline result.

    Einträge mit source "oparl" (offizielle OParl-Locations) oder "manual"
    bleiben bei Re-Runs erhalten und haben Vorrang bei der Deduplizierung.

    Args:
        paper: OParlPaper instance
        result: Dict from process_paper_georef()
    """
    paper.georef_status = result["status"]
    paper.georef_method = result.get("method")
    paper.georef_error = result.get("error") or result.get("reason")
    paper.georef_extracted_at = timezone.now()

    update_fields = [
        "georef_status",
        "georef_method",
        "georef_error",
        "georef_extracted_at",
        "updated_at",
    ]

    existing = paper.locations if isinstance(paper.locations, list) else []
    preserved = [
        loc
        for loc in existing
        if isinstance(loc, dict)
        and loc.get("source") in ("oparl", "manual")
        and loc.get("lat") is not None
        and loc.get("lon") is not None
    ]

    extracted = result.get("locations", [])
    # Höchste Priorität zuerst: oparl/manual gewinnen bei Dedup (<50m)
    merged = deduplicate_locations(preserved + extracted)

    paper.locations = merged or None
    update_fields.append("locations")

    paper.save(update_fields=update_fields)
