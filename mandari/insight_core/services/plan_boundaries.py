# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Amtliche Umringe von Bebauungsplänen mit Vorlagen verknüpfen (Issue #598).

Kommunen und das Land NRW veröffentlichen die Umringe (Geltungsbereiche) ihrer Bebauungspläne als
Geodienst. Verlinken ist verlässlicher, als sie aus PDF-Anlagen zu rekonstruieren:

1. ``parse_plan_references`` liest aus dem Vorlagentitel „Bebauungsplan Nr. 579“ oder
   „1. Änderung des Bebauungsplans Nr. 579“ die Plannummer samt Änderung.
2. ``refresh_source`` lädt die Umringe einer Quelle (OGC API – Features oder WFS mit GeoJSON) in
   ``PlanBoundary``. Das geschieht einmal täglich über ``sync_plan_boundaries`` – nie je
   Seitenaufruf, mit eigenem User-Agent und Pause zwischen Seiten.
3. ``link_body_papers`` ordnet Vorlagen die Umringe zu. Jeder Bezug wird gespeichert
   (``PaperPlanReference``), auch ohne Treffer: Das ist die protokollierte Lücke. Ein Punkt im
   Umring kommt als Verortung (``source = plan_boundary``) an den Vorgang und wirkt damit in Karte,
   Umkreissuche und Abos wie jede andere Verortung.
4. ``nearby_plan_papers`` ergänzt die Umkreissuche um Vorgänge, deren Umring den Suchkreis berührt,
   auch wenn der Punkt im Umring weiter entfernt liegt.
5. ``paper_plan_context`` liefert Umringe, Planseite und Quellenangabe für die Vorgangsseite.

Keine Abrufe bei Ratsinformationssystemen: Die Daten kommen ausschließlich aus den konfigurierten
Geodiensten.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from insight_core.services import polygon_geometry as geo
from insight_core.services.overpass import OSM_USER_AGENT
from insight_core.services.safe_fetch import (
    BlockedDestinationError,
    DeadlineExceededError,
    TooLargeError,
    download_to,
)

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlPaper, PlanBoundary, PlanBoundarySource

logger = logging.getLogger(__name__)

LOCATION_SOURCE = "plan_boundary"

# Abruf: Obergrenzen gegen Fehlkonfiguration (etwa die Landes-API ohne Gemeindefilter)
PAGE_SIZE = 1000
MAX_PAGES = 50
MAX_FEATURES = 20_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TOTAL_SECONDS = 180.0
PAGE_PAUSE_SECONDS = 1.0
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Umringe außerhalb des Gemeindegebiets (plus Rand) sind Fehlkonfiguration, nicht Daten der Kommune
BODY_MARGIN_DEGREES = 0.05


class PlanSourceError(Exception):
    """Abruf einer Quelle gescheitert; ``message`` ist ein fester Text für Admin und Protokoll."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# =============================================================================
# Plannummern
# =============================================================================


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    for umlaut, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        folded = folded.replace(umlaut, replacement)
    return folded


def normalize_plan_number(value: str) -> str:
    """Vergleichsschlüssel einer Plannummer: „Nr. 0579 a“ → ``579a``, „Albachten 7 A“ → ``albachten7a``."""
    key = re.sub(r"[^a-z0-9]", "", _fold(value))
    key = re.sub(r"(?<![0-9])0+(?=[0-9])", "", key)
    return key[:100]


def split_source_number(raw: Any) -> tuple[str, str, str]:
    """(Anzeige, Schlüssel, Änderung) aus der Plannummer einer Quelle.

    Das Land NRW führt ``<Ortsteil>_<Nummer>_<Teil>_<Änderung>``, etwa ``_579__1`` (1. Änderung des
    Plans 579) oder ``Albachten_7_A_`` (Plan „Albachten 7 A“, keine Änderung); die INSPIRE-Kennung
    stellt ``DE_<AGS>_`` voran (``DE_05515000__579__1``). Andere Quellen liefern die Nummer ohne
    Änderung, etwa ``579`` oder ``Albachten7A``.
    """
    value = str(raw or "").strip()
    parts = value.split("_")
    if len(parts) == 6 and parts[0].upper() == "DE" and parts[1].isdigit():
        parts = parts[2:]
    if len(parts) == 4:
        prefix, number, part, change = (item.strip() for item in parts)
        label = " ".join(item for item in (prefix, number, part) if item)
        return label[:100], normalize_plan_number(prefix + number + part), change[:20]
    return value[:100], normalize_plan_number(value), ""


@dataclass(frozen=True)
class PlanReference:
    """Ein Bebauungsplan, den ein Vorlagentitel nennt."""

    number_label: str
    number_key: str
    change_number: str = ""

    @property
    def label(self) -> str:
        suffix = f", {self.change_number}. Änderung" if self.change_number else ""
        return f"Bebauungsplan Nr. {self.number_label}{suffix}"


_PLAN_WORD = (
    r"(?:vorhabenbezogene[nrs]?\s+)?"
    r"(?:Bebauungspl(?:an(?:e?s)?|äne[n]?)|B-Pl(?:an(?:e?s)?|äne[n]?))"
)
_NUMBER = (
    r"(?:[A-ZÄÖÜ][a-zäöüß]+(?:[ .-][A-ZÄÖÜ][a-zäöüß]+)*\s+)?"  # Ortsteil, etwa „Hiltrup 8“
    r"\d{1,4}(?!\d)"
    r"(?:\s?[a-zA-Z](?![a-zA-ZäöüßÄÖÜ]))?"  # Buchstabe: 579a, 7 A
    r"(?:\s?/\s?\d{1,4})?"  # 22/05
    r"(?:\s+[IVX]{1,4}\b)?"  # römischer Teil: 1 A II
)
_PLAN_RE = re.compile(rf"{_PLAN_WORD}\s+(?:Nr\.?|Nummer)\s*(?P<nr>{_NUMBER})")
_MORE_RE = re.compile(rf"\s*(?:,|und|sowie|bzw\.)\s*(?:Nr\.?\s*)?(?P<nr>{_NUMBER})(?![\d.])")
_CHANGE_BEFORE_RE = re.compile(
    r"(?:(?P<a>\d{1,3})\.\s*(?:vereinfachte[n]?\s+|vorhabenbezogene[n]?\s+|teilweise[n]?\s+)?Änderung"
    r"|Änderung\s+Nr\.?\s*(?P<b>\d{1,3}))"
    r"(?:\s+und\s+(?:Ergänzung|Erweiterung))?\s+(?:des|der|zum)\s+$",
    re.IGNORECASE,
)
_CHANGE_AFTER_RE = re.compile(
    r"^\s*[-–—:,(]?\s*(?:hier:\s*)?(?P<n>\d{1,3})\.\s*(?:vereinfachte\s+)?Änderung",
    re.IGNORECASE,
)


def parse_plan_references(title: str | None) -> list[PlanReference]:
    """Bebauungspläne, die ein Titel nennt – mit Änderungsnummer, falls der Titel eine nennt."""
    if not title:
        return []
    text = unicodedata.normalize("NFC", title)
    found: list[PlanReference] = []
    seen: set[tuple[str, str]] = set()
    for match in _PLAN_RE.finditer(text):
        before = _CHANGE_BEFORE_RE.search(text[max(0, match.start() - 80) : match.start()])
        change = (before.group("a") or before.group("b")) if before else ""
        numbers = [match.group("nr")]
        end = match.end()
        while more := _MORE_RE.match(text, end):
            numbers.append(more.group("nr"))
            end = more.end()
        if not change:
            after = _CHANGE_AFTER_RE.match(text[end : end + 40])
            change = after.group("n") if after else ""
        for number in numbers:
            label = re.sub(r"\s+", " ", number).strip()
            key = normalize_plan_number(label)
            if not key or (key, change) in seen:
                continue
            seen.add((key, change))
            # Die Änderung gilt nur für einen einzeln genannten Plan
            found.append(PlanReference(label[:100], key, change if len(numbers) == 1 else ""))
    return found


# =============================================================================
# Abruf der Quellen
# =============================================================================


def _with_params(url: str, params: dict[str, str], *, case_insensitive: bool = False) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if case_insensitive:
            for existing in [k for k in query if k.upper() == key.upper()]:
                del query[existing]
        query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _get_json(url: str) -> Any:
    buffer = io.BytesIO()
    try:
        download_to(
            buffer,
            url,
            max_bytes=MAX_RESPONSE_BYTES,
            total_seconds=TOTAL_SECONDS,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/geo+json, application/json;q=0.9"},
            user_agent=OSM_USER_AGENT,
        )
    except BlockedDestinationError:
        raise PlanSourceError("Adresse ist nicht zulässig (kein öffentliches Ziel)") from None
    except httpx.HTTPStatusError as exc:
        raise PlanSourceError(f"Dienst antwortete mit HTTP {exc.response.status_code}") from None
    except TooLargeError:
        raise PlanSourceError("Antwort des Dienstes ist zu groß") from None
    except (DeadlineExceededError, httpx.TimeoutException):
        raise PlanSourceError("Zeitüberschreitung beim Abruf") from None
    except httpx.HTTPError:
        raise PlanSourceError("Dienst nicht erreichbar") from None
    try:
        return json.loads(buffer.getvalue().decode("utf-8"))
    except ValueError:
        raise PlanSourceError("Antwort ist kein gültiges GeoJSON") from None


def _feature_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise PlanSourceError("Antwort ist keine GeoJSON-FeatureCollection")
    return [item for item in payload["features"] if isinstance(item, dict)]


def _next_link(payload: dict[str, Any]) -> str | None:
    for link in payload.get("links") or []:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        href = link.get("href")
        kind = str(link.get("type") or "")
        if isinstance(href, str) and (not kind or "json" in kind):
            return href
    return None


def _string_params(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _fetch_wfs(source: PlanBoundarySource, extra: dict[str, str]) -> list[dict[str, Any]]:
    if not source.layer:
        raise PlanSourceError("Für einen WFS fehlt die Ebene (TYPENAMES)")
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": source.layer,
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": "EPSG:4326",
        "COUNT": str(MAX_FEATURES + 1),
    }
    # Zusätzliche Parameter der Quelle ersetzen die Vorgaben, unabhängig von der Schreibweise
    url = _with_params(_with_params(source.url, params, case_insensitive=True), extra, case_insensitive=True)
    features = _feature_list(_get_json(url))
    if len(features) > MAX_FEATURES:
        raise PlanSourceError("Mehr Objekte als erlaubt – Ebene oder Filter prüfen")
    return features


def _fetch_ogc_api(source: PlanBoundarySource, extra: dict[str, str]) -> list[dict[str, Any]]:
    next_url: str | None = _with_params(source.url, {"f": "json", "limit": str(PAGE_SIZE), **extra})
    features: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        if next_url is None:
            return features
        if page:
            _sleep(PAGE_PAUSE_SECONDS)
        payload = _get_json(next_url)
        features.extend(_feature_list(payload))
        if len(features) > MAX_FEATURES:
            raise PlanSourceError("Mehr Objekte als erlaubt – Filter (etwa gkz) prüfen")
        next_url = _next_link(payload)
    if next_url is not None:
        raise PlanSourceError("Zu viele Seiten – Filter (etwa gkz) prüfen")
    return features


def fetch_features(source: PlanBoundarySource) -> list[dict[str, Any]]:
    """Alle GeoJSON-Objekte einer Quelle; wirft ``PlanSourceError`` bei Fehler oder Überlauf."""
    extra = _string_params(source.query_params)
    if source.kind == source.KIND_WFS:
        return _fetch_wfs(source, extra)
    return _fetch_ogc_api(source, extra)


# =============================================================================
# Umringe speichern
# =============================================================================


@dataclass
class ParsedBoundary:
    feature_key: str
    plan_number: str
    number_key: str
    change_number: str
    title: str
    document_url: str
    geometry: dict[str, Any]
    bbox: tuple[float, float, float, float]
    point: tuple[float, float]  # (Länge, Breite)
    area_m2: float


@dataclass
class RefreshResult:
    fetched: int = 0
    stored: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    error: str = ""


def _matches_filter(properties: dict[str, Any], property_filter: Any) -> bool:
    if not isinstance(property_filter, dict):
        return True
    for key, allowed in property_filter.items():
        values = allowed if isinstance(allowed, list) else [allowed]
        if str(properties.get(key)) not in {str(value) for value in values}:
            return False
    return True


def _body_center(body: OParlBody) -> tuple[float, float] | None:
    if body.latitude is None or body.longitude is None:
        return None
    return (float(body.longitude), float(body.latitude))


def _body_box(body: OParlBody) -> tuple[float, float, float, float] | None:
    if body.bbox_south is None or body.bbox_north is None or body.bbox_west is None or body.bbox_east is None:
        return None
    south, north = float(body.bbox_south), float(body.bbox_north)
    west, east = float(body.bbox_west), float(body.bbox_east)
    return (
        south - BODY_MARGIN_DEGREES,
        north + BODY_MARGIN_DEGREES,
        west - BODY_MARGIN_DEGREES,
        east + BODY_MARGIN_DEGREES,
    )


def _needs_swap(polygons: list[geo.Polygon], center: tuple[float, float] | None) -> bool:
    """Liefert der Dienst Breite vor Länge? Entscheidet der Abstand zum Zentrum der Kommune."""
    lon, lat = polygons[0][0][0]
    if abs(lon) > 90:
        return False
    if center is not None:
        c_lon, c_lat = center
        return abs(lat - c_lon) + abs(lon - c_lat) < abs(lon - c_lon) + abs(lat - c_lat)
    # Ohne Zentrum: In Deutschland ist die Breite (47–55°) stets größer als die Länge (6–15°)
    return lon > lat


def _http_url(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 1000 and urlsplit(text).scheme in ("http", "https"):
        return text
    return ""


def parse_feature(
    feature: dict[str, Any],
    source: PlanBoundarySource,
    center: tuple[float, float] | None,
    box: tuple[float, float, float, float] | None,
) -> ParsedBoundary | str:
    """Umring aus einem GeoJSON-Objekt – oder der Grund, warum es übersprungen wird."""
    raw_properties = feature.get("properties")
    properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
    if not _matches_filter(properties, source.property_filter):
        return "Filter"
    raw_number = properties.get(source.number_property)
    label, key, change = split_source_number(raw_number)
    if not key:
        return "ohne Plannummer"
    try:
        polygons = geo.polygons_from_geojson(feature.get("geometry"))
    except geo.GeometryError:
        return "ungültige Geometrie"
    if _needs_swap(polygons, center):
        polygons = geo.swap_axes(polygons)
    south, north, west, east = geo.bounding_box(polygons)
    if box is not None and (north < box[0] or south > box[1] or east < box[2] or west > box[3]):
        return "außerhalb der Kommune"
    geometry = geo.to_geojson(polygons)
    polygons = geo.polygons_from_geojson(geometry)  # gerundet, damit der Vergleich mit dem Bestand stabil ist
    point = geo.representative_point(polygons)
    raw_id = feature.get("id")
    if raw_id not in (None, ""):
        feature_key = str(raw_id)
    else:
        digest = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        feature_key = f"{label}|{change}|{digest}"
    title = str(properties.get(source.title_property) or "") if source.title_property else ""
    link = _http_url(properties.get(source.link_property)) if source.link_property else ""
    return ParsedBoundary(
        feature_key=feature_key[:300],
        plan_number=label,
        number_key=key,
        change_number=change,
        title=" ".join(title.split())[:500],
        document_url=link,
        geometry=geometry,
        bbox=geo.bounding_box(polygons),
        point=(round(point[0], 7), round(point[1], 7)),
        area_m2=round(geo.area_m2(polygons), 1),
    )


BOUNDARY_FIELDS = (
    "plan_number",
    "number_key",
    "change_number",
    "title",
    "plan_status",
    "document_url",
    "geometry",
    "bbox_south",
    "bbox_north",
    "bbox_west",
    "bbox_east",
    "point_lon",
    "point_lat",
    "area_m2",
)


def _boundary_values(parsed: ParsedBoundary, source: PlanBoundarySource) -> dict[str, Any]:
    south, north, west, east = parsed.bbox
    return {
        "plan_number": parsed.plan_number,
        "number_key": parsed.number_key,
        "change_number": parsed.change_number,
        "title": parsed.title,
        "plan_status": source.plan_status,
        "document_url": parsed.document_url,
        "geometry": parsed.geometry,
        "bbox_south": south,
        "bbox_north": north,
        "bbox_west": west,
        "bbox_east": east,
        "point_lon": parsed.point[0],
        "point_lat": parsed.point[1],
        "area_m2": parsed.area_m2,
    }


def refresh_source(source: PlanBoundarySource, *, dry_run: bool = False) -> RefreshResult:
    """Umringe einer Quelle abrufen und den Zwischenspeicher abgleichen (anlegen, ändern, löschen)."""
    from insight_core.models import PlanBoundary, PlanBoundarySource

    result = RefreshResult()
    now = timezone.now()
    try:
        features = fetch_features(source)
    except PlanSourceError as exc:
        result.error = exc.message
        logger.warning("Umring-Quelle %s: %s", source.pk, exc.message)
        if not dry_run:
            PlanBoundarySource.objects.filter(pk=source.pk).update(last_attempt_at=now, last_error=exc.message)
        return result

    result.fetched = len(features)
    center = _body_center(source.body)
    box = _body_box(source.body)
    parsed: dict[str, ParsedBoundary] = {}
    for feature in features:
        item = parse_feature(feature, source, center, box)
        if isinstance(item, str):
            result.skipped[item] += 1
            continue
        key = item.feature_key
        suffix = 2
        while key in parsed:
            key = f"{item.feature_key[:290]}#{suffix}"
            suffix += 1
        item.feature_key = key
        parsed[key] = item
    result.stored = len(parsed)

    if not parsed and source.feature_count:
        # Leere Antwort eines Dienstes, der gestern noch Umringe hatte: Bestand behalten
        result.error = "Quelle lieferte keine Umringe – Bestand bleibt erhalten"
        if not dry_run:
            PlanBoundarySource.objects.filter(pk=source.pk).update(last_attempt_at=now, last_error=result.error)
        return result

    existing = {boundary.feature_key: boundary for boundary in PlanBoundary.objects.filter(source=source)}
    to_create: list[PlanBoundary] = []
    to_update: list[PlanBoundary] = []
    for key, item in parsed.items():
        values = _boundary_values(item, source)
        boundary = existing.pop(key, None)
        if boundary is None:
            to_create.append(
                PlanBoundary(source=source, body_id=source.body_id, feature_key=key, fetched_at=now, **values)
            )
            continue
        if any(getattr(boundary, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(boundary, name, value)
            boundary.fetched_at = now
            to_update.append(boundary)
    result.created = len(to_create)
    result.updated = len(to_update)
    result.deleted = len(existing)
    if dry_run:
        return result

    with transaction.atomic():
        if existing:
            PlanBoundary.objects.filter(pk__in=[boundary.pk for boundary in existing.values()]).delete()
        if to_create:
            PlanBoundary.objects.bulk_create(to_create, batch_size=200)
        if to_update:
            PlanBoundary.objects.bulk_update(to_update, [*BOUNDARY_FIELDS, "fetched_at"], batch_size=200)
        PlanBoundarySource.objects.filter(pk=source.pk).update(
            last_attempt_at=now,
            last_success_at=now,
            last_error="",
            feature_count=len(parsed),
        )
    source.feature_count = len(parsed)
    return result


# =============================================================================
# Vorlagen zuordnen
# =============================================================================


@dataclass(frozen=True)
class _Candidate:
    boundary_id: int
    source_id: int
    priority: int
    change_number: str
    plan_status: str
    point: tuple[float, float]  # (Breite, Länge)
    area_m2: float


def _choose(candidates: list[_Candidate], change: str) -> tuple[str, list[_Candidate]]:
    """Passende Umringe zu einer Plannummer und die Art der Zuordnung.

    Mit Änderung im Titel: die Änderung selbst, sonst das laufende Verfahren, sonst der Ursprungsplan.
    Ohne Änderung: der rechtskräftige Plan, sonst das laufende Verfahren. Von mehreren Quellen gewinnt
    die mit dem kleinsten Rang; mehrteilige Umringe derselben Quelle bleiben zusammen.
    """
    from insight_core.models import PaperPlanReference, PlanBoundarySource

    in_force = PlanBoundarySource.PLAN_STATUS_IN_FORCE
    in_procedure = PlanBoundarySource.PLAN_STATUS_IN_PROCEDURE

    def best(group: list[_Candidate]) -> list[_Candidate]:
        top = min((c.priority, c.source_id) for c in group)
        return [c for c in group if (c.priority, c.source_id) == top]

    if not candidates:
        return PaperPlanReference.MATCH_NONE, []
    base = [c for c in candidates if not c.change_number and c.plan_status == in_force]
    procedure = [c for c in candidates if c.plan_status == in_procedure]
    if change:
        exact = [c for c in candidates if c.change_number == change]
        if exact:
            return PaperPlanReference.MATCH_CHANGE, best(exact)
        if procedure:
            return PaperPlanReference.MATCH_PROCEDURE, best(procedure)
        return PaperPlanReference.MATCH_BASE_PLAN, best(base or candidates)
    if base:
        return PaperPlanReference.MATCH_PLAN, best(base)
    if procedure:
        return PaperPlanReference.MATCH_PROCEDURE, best(procedure)
    return PaperPlanReference.MATCH_PLAN, best(candidates)


LOCATION_NAMES = {
    "change": "{label} (amtlicher Umring)",
    "plan": "{label} (amtlicher Umring)",
    "procedure": "{label} (Umring im Verfahren)",
    "base_plan": "{label} (Umring des Ursprungsplans)",
}


@dataclass
class _Desired:
    reference: PlanReference
    match: str
    boundary_ids: list[int]
    point: tuple[float, float] | None


@dataclass
class LinkResult:
    papers: int = 0
    references: int = 0
    matched: int = 0
    unmatched: int = 0
    papers_changed: int = 0
    locations_changed: int = 0


def _candidates_by_key(body: OParlBody) -> dict[str, list[_Candidate]]:
    from insight_core.models import PlanBoundary

    rows = PlanBoundary.objects.filter(body=body, source__is_active=True).values_list(
        "id",
        "source_id",
        "source__priority",
        "number_key",
        "change_number",
        "plan_status",
        "point_lat",
        "point_lon",
        "area_m2",
    )
    result: dict[str, list[_Candidate]] = {}
    for pk, source_id, priority, key, change, status, lat, lon, area in rows:
        result.setdefault(key, []).append(
            _Candidate(pk, source_id, priority, change, status, (lat, lon), float(area or 0.0))
        )
    return result


def _desired_for(paper: OParlPaper, candidates: dict[str, list[_Candidate]]) -> list[_Desired]:
    desired: list[_Desired] = []
    for reference in parse_plan_references(paper.name):
        match, chosen = _choose(candidates.get(reference.number_key, []), reference.change_number)
        point = max(chosen, key=lambda c: c.area_m2).point if chosen else None
        desired.append(_Desired(reference, match, sorted(c.boundary_id for c in chosen), point))
    return desired


def _plan_entries(desired: list[_Desired], removed: list[tuple[float, float]]) -> list[dict[str, Any]]:
    from insight_core.services.paper_locations import SUPPRESS_RADIUS_M, haversine_m

    entries: list[dict[str, Any]] = []
    for item in desired:
        if item.point is None:
            continue
        lat, lon = item.point
        if any(haversine_m(lat, lon, r_lat, r_lon) <= SUPPRESS_RADIUS_M for r_lat, r_lon in removed):
            continue  # im Admin entfernt: nicht wieder eintragen
        entries.append(
            {
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "name": LOCATION_NAMES[item.match].format(label=item.reference.label),
                "source": LOCATION_SOURCE,
                "confidence": 1.0,
            }
        )
    return entries


def _merge_locations(current: Any, plan_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from insight_core.services.georeferencing import deduplicate_locations

    existing = current if isinstance(current, list) else []
    valid = [
        loc
        for loc in existing
        if isinstance(loc, dict)
        and loc.get("source") != LOCATION_SOURCE
        and loc.get("lat") is not None
        and loc.get("lon") is not None
    ]
    # Offizielle und manuelle Orte behalten Vorrang, dann der Umring, dann die übrigen Funde
    preferred = [loc for loc in valid if loc.get("source") in ("oparl", "manual")]
    rest = [loc for loc in valid if loc.get("source") not in ("oparl", "manual")]
    return deduplicate_locations(preferred + plan_entries + rest)


def _apply_paper(paper: OParlPaper, desired: list[_Desired], result: LinkResult, *, dry_run: bool) -> None:
    from insight_core.models import PaperLocation, PaperPlanReference
    from insight_core.services.paper_locations import sync_paper_locations

    now = timezone.now()
    existing = {(ref.number_key, ref.change_number): ref for ref in paper.plan_references.all()}
    changed = False
    wanted_keys = set()
    for item in desired:
        key = (item.reference.number_key, item.reference.change_number)
        wanted_keys.add(key)
        ref = existing.get(key)
        current_ids = sorted(boundary.pk for boundary in ref.boundaries.all()) if ref is not None else None
        if ref is not None and ref.match == item.match and current_ids == item.boundary_ids:
            continue
        changed = True
        if dry_run:
            continue
        if ref is None:
            ref = PaperPlanReference.objects.create(
                paper=paper,
                body_id=paper.body_id,
                number_key=item.reference.number_key,
                number_label=item.reference.number_label,
                change_number=item.reference.change_number,
                match=item.match,
                matched_at=now,
            )
        else:
            ref.match = item.match
            ref.number_label = item.reference.number_label
            ref.matched_at = now
            ref.save(update_fields=["match", "number_label", "matched_at"])
        ref.boundaries.set(item.boundary_ids)
    stale = [ref.pk for key, ref in existing.items() if key not in wanted_keys]
    if stale:
        changed = True
        if not dry_run:
            PaperPlanReference.objects.filter(pk__in=stale).delete()

    removed = list(
        PaperLocation.objects.filter(paper=paper, status=PaperLocation.STATUS_REMOVED).values_list(
            "latitude", "longitude"
        )
    )
    merged = _merge_locations(paper.locations, _plan_entries(desired, removed))
    new_json = merged or None
    locations_changed = new_json != paper.locations
    if changed:
        result.papers_changed += 1
    if locations_changed:
        result.locations_changed += 1
    if dry_run or not locations_changed:
        return
    paper.locations = new_json
    paper.save(update_fields=["locations", "updated_at"])
    sync_paper_locations(paper)


def link_body_papers(body: OParlBody, *, dry_run: bool = False) -> LinkResult:
    """Vorlagen einer Kommune mit Plannummer im Titel den zwischengespeicherten Umringen zuordnen.

    Ohne Netzabruf. Geschrieben wird nur, was sich geändert hat; Vorgänge, deren Titel keinen Plan
    mehr nennt, verlieren Bezug und Verortung aus dem Umring.
    """
    from insight_core.models import OParlPaper, PaperPlanReference, PlanBoundary

    result = LinkResult()
    candidates = _candidates_by_key(body)
    if not dry_run:
        PaperPlanReference.objects.filter(body=body, paper__deleted=True).delete()
    references = PaperPlanReference.objects.prefetch_related(
        Prefetch("boundaries", queryset=PlanBoundary.objects.only("id"))
    )
    papers = (
        OParlPaper.objects.filter(body=body, deleted=False)
        .filter(Q(name__iregex=r"bebauungspl|b-pl") | Q(plan_references__isnull=False))
        .distinct()
        .only("id", "name", "body_id", "locations")
        .prefetch_related(Prefetch("plan_references", queryset=references))
    )
    for paper in papers.iterator(chunk_size=200):
        desired = _desired_for(paper, candidates)
        result.papers += 1
        result.references += len(desired)
        result.matched += sum(1 for item in desired if item.boundary_ids)
        result.unmatched += sum(1 for item in desired if not item.boundary_ids)
        try:
            with transaction.atomic():
                _apply_paper(paper, desired, result, dry_run=dry_run)
        except Exception:
            logger.exception("Umring-Zuordnung fehlgeschlagen (paper=%s)", paper.pk)
    return result


# =============================================================================
# Umkreissuche und Vorgangsseite
# =============================================================================


@dataclass(frozen=True)
class PlanHit:
    """Vorgang, dessen Umring im Suchkreis liegt."""

    paper_id: Any
    distance: float
    lat: float
    lon: float


def nearby_plan_papers(
    body: OParlBody,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    created_since: Any = None,
) -> dict[Any, PlanHit]:
    """Vorgänge, deren zugeordneter Umring höchstens ``radius_m`` vom Punkt entfernt ist (0 = darin).

    Vorfilter über die Box des Umrings (Index), danach der genaue Abstand zur Fläche. Vorgänge, deren
    Umring-Verortung im Admin entfernt wurde, bleiben außen vor.
    """
    from insight_core.models import PaperLocation, PaperPlanReference, PlanBoundary
    from insight_core.services.paper_locations import bounding_box

    south, north, west, east = bounding_box(lat, lon, float(radius_m))
    boundaries = PlanBoundary.objects.filter(
        body=body,
        source__is_active=True,
        bbox_south__lte=north,
        bbox_north__gte=south,
        bbox_west__lte=east,
        bbox_east__gte=west,
    ).only("id", "geometry", "point_lat", "point_lon")
    distances: dict[int, tuple[float, float, float]] = {}
    for boundary in boundaries:
        try:
            distance = geo.distance_m(geo.polygons_from_geojson(boundary.geometry), lon, lat)
        except geo.GeometryError:
            continue
        if distance <= radius_m:
            distances[boundary.pk] = (distance, boundary.point_lat, boundary.point_lon)
    if not distances:
        return {}

    through = PaperPlanReference.boundaries.through
    links = through.objects.filter(
        planboundary_id__in=list(distances),
        paperplanreference__paper__deleted=False,
    )
    if created_since is not None:
        links = links.filter(paperplanreference__paper__created_at__gte=created_since)
    pairs = list(links.values_list("paperplanreference__paper_id", "planboundary_id"))
    suppressed = set(
        PaperLocation.objects.filter(
            paper_id__in={paper_id for paper_id, _ in pairs},
            source=LOCATION_SOURCE,
            status=PaperLocation.STATUS_REMOVED,
        ).values_list("paper_id", flat=True)
    )
    hits: dict[Any, PlanHit] = {}
    for paper_id, boundary_id in pairs:
        if paper_id in suppressed:
            continue
        distance, point_lat, point_lon = distances[boundary_id]
        known = hits.get(paper_id)
        if known is None or distance < known.distance:
            hits[paper_id] = PlanHit(paper_id, distance, point_lat, point_lon)
    return hits


MATCH_NOTES = {
    "change": "",
    "plan": "",
    "procedure": "Umring aus dem laufenden Verfahren, noch nicht rechtsverbindlich.",
    "base_plan": "Umring des Ursprungsplans; die Änderung betrifft womöglich nur einen Teil davon.",
}


def paper_plan_context(paper: OParlPaper) -> list[dict[str, Any]]:
    """Umringe eines Vorgangs für Karte und Liste: Bezeichnung, Stand, Planseite, Quellenangabe."""
    from insight_core.models import PaperLocation, PaperPlanReference

    if PaperLocation.objects.filter(paper=paper, source=LOCATION_SOURCE, status=PaperLocation.STATUS_REMOVED).exists():
        return []
    references = (
        PaperPlanReference.objects.filter(paper=paper)
        .exclude(match=PaperPlanReference.MATCH_NONE)
        .prefetch_related("boundaries__source")
        .order_by("number_key", "change_number")
    )
    areas: list[dict[str, Any]] = []
    for reference in references:
        boundaries: list[PlanBoundary] = [b for b in reference.boundaries.all() if b.source.is_active]
        if not boundaries:
            continue
        first = boundaries[0]
        areas.append(
            {
                "label": str(reference),
                "status": first.get_plan_status_display(),
                "note": MATCH_NOTES.get(reference.match, ""),
                "title": first.title,
                "document_url": first.document_url,
                "attribution": first.source.attribution,
                "license_url": first.source.license_url,
                "geometries": [boundary.geometry for boundary in boundaries],
            }
        )
    return areas


def _place(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        lat = float(entry["lat"])
        lon = float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"lat": lat, "lon": lon, "name": str(entry.get("name") or "")}


def paper_map_data(places: list[dict[str, Any]], areas: list[dict[str, Any]]) -> dict[str, Any]:
    """Daten für die Karte der Vorgangsseite (``json_script`` → ``frontend/js/paper-map.ts``).

    Zeigt die Karte einen Umring, entfällt der Punkt im Umring als eigener Marker.
    """
    shown = [entry for entry in places if not (areas and entry.get("source") == LOCATION_SOURCE)]
    return {
        "places": [place for place in (_place(entry) for entry in shown) if place is not None],
        "areas": [{"name": area["label"], "geometry": geometry} for area in areas for geometry in area["geometries"]],
        "attribution": sorted({area["attribution"] for area in areas if area["attribution"]}),
    }
