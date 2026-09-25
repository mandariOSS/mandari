# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geo-Daten neuer Kommunen automatisch zuordnen (Issue #351, Folge von #54).

Für die Georeferenzierung braucht eine Kommune ihre OSM-Grenze (``osm_relation_id``) und den
Gemeindeschlüssel. Bisher pflegte das der Betreiber je Kommune von Hand. Dieser Service ordnet
automatisch zu, in dieser Reihenfolge:

1. **Ohne eigenes Gebiet:** Zweckverbände, Gesellschaften, Anstalten, Waldgemarkungen und
   Stiftungen werden am Namen erkannt und als „keine Gebietskörperschaft“ gekennzeichnet, mit
   Verweis auf die übergeordnete Körperschaft derselben Quelle (deren Gebiet gilt für die Karte).
   Verbandsgemeinden sind Gebietskörperschaften. Eine Angabe von Hand im Admin bleibt unangetastet.
2. **Schlüssel aus OParl:** ``ags`` bzw. ``rgs`` aus dem Body-Objekt der Quelle. Aus dem
   12-stelligen Regionalschlüssel ergibt sich der Gemeindeschlüssel aus den Stellen 1–5 und 10–12.
3. **Grenze per Schlüssel:** Overpass-Suche nach ``de:amtlicher_gemeindeschluessel``; nur ein
   einziger Treffer wird übernommen.
4. **Namenssuche als Rückfall:** innerhalb der übergeordneten Körperschaft derselben Quelle
   (Ortsgemeinde in der Verbandsgemeinde), sonst bundesweit nach dem genauen Namen. Präfixe wie
   „Ortsgemeinde“ oder „Stadt“ werden abgelöst und bestimmen die erwartete Verwaltungsebene. Ein
   eindeutiger Treffer wird samt Schlüsseln aus den OSM-Tags übernommen; mehrere oder nur
   ungefähre Treffer werden nicht geraten, sondern als ``OParlBodyGeoSuggestion`` im Admin zur
   Bestätigung abgelegt.

Zentrum, Bounding-Box, Straßen und Adressen lädt danach ``resolve_body_geodata`` über
``fetch_osm_geodata`` und ``import_streets``. Ohne Schreibzugriff (``dry_run``) zeigt der Service
nur, was passieren würde; Abfragen an Overpass laufen trotzdem, damit die Vorschau stimmt.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from insight_core.models import OParlBody, OParlBodyGeoSuggestion
from insight_core.services.overpass import overpass_string, run_overpass

# Overpass-Areas: Relation-ID plus dieser Versatz (wie in import_streets)
OSM_AREA_OFFSET = 3600000000

# ---------------------------------------------------------------------------
# Namensregeln
# ---------------------------------------------------------------------------

# Kategorie aus dem Namenspräfix → erwartete OSM-Verwaltungsebenen (admin_level)
LEVELS_BY_CATEGORY: dict[str, frozenset[int]] = {
    "bezirk": frozenset({5}),  # Regierungsbezirk
    "kreis": frozenset({6}),  # Landkreis, Kreis, Städteregion
    "gemeindeverband": frozenset({7}),  # Verbandsgemeinde, Amt, Samtgemeinde, Verwaltungsgemeinschaft
    "stadt": frozenset({6, 8}),  # kreisfreie (6) und kreisangehörige Stadt (8)
    "gemeinde": frozenset({8}),
    "ortsteil": frozenset({9, 10}),  # Ortsbezirk, Stadtbezirk, Ortsteil
}

# Kategorien, in deren Gebiet nach den übrigen Körperschaften derselben Quelle gesucht wird
AREA_CATEGORIES = frozenset({"bezirk", "kreis", "gemeindeverband"})

# Namenspräfix (klein geschrieben) → Kategorie; längere Präfixe zuerst prüfen
_NAME_PREFIXES: dict[str, str] = {
    "verbandsgemeindeverwaltung": "gemeindeverband",
    "verbandsgemeinde": "gemeindeverband",
    "samtgemeinde": "gemeindeverband",
    "verwaltungsgemeinschaft": "gemeindeverband",
    "gemeindeverwaltungsverband": "gemeindeverband",
    "amt": "gemeindeverband",
    "regierungsbezirk": "bezirk",
    "landkreis": "kreis",
    "kreisverwaltung": "kreis",
    "kreis": "kreis",
    "städteregion": "kreis",
    "ortsgemeinde": "gemeinde",
    "gemeindeverwaltung": "gemeinde",
    "gemeinde": "gemeinde",
    "marktgemeinde": "gemeinde",
    "markt": "gemeinde",
    "große kreisangehörige stadt": "stadt",
    "große kreisstadt": "stadt",
    "kreisfreie stadt": "stadt",
    "kreisstadt": "stadt",
    "landeshauptstadt": "stadt",
    "hansestadt": "stadt",
    "universitätsstadt": "stadt",
    "klingenstadt": "stadt",
    "kolpingstadt": "stadt",
    "stadtverwaltung": "stadt",
    "stadt": "stadt",
    "ortsbezirk": "ortsteil",
    "stadtbezirk": "ortsteil",
    "ortsteil": "ortsteil",
    "stadtteil": "ortsteil",
    "bezirk": "ortsteil",
}
NAME_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(_NAME_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True)
)

# Körperschaften ohne eigenes Gebiet. Bewusst ohne bloßes „verband“: „Verbandsgemeinde“ und
# „Regionalverband“ haben ein Gebiet.
_NON_TERRITORIAL_RE = re.compile(
    r"zweckverband|schulverband|wasserverband|abwasserverband|planungsverband|"
    r"gmbh|\bmbh\b|\bag\b|\bkg\b|a\.?ö\.?r\b|anstalt|kommunalunternehmen|eigenbetrieb|"
    r"eigengesellschaft|gesellschaft|genossenschaft|stadtwerke|gemeindewerke|kommunalbetrieb|"
    r"waldgemark|stiftung|sparkasse|beirat",
    re.IGNORECASE,
)

# Namenszusätze, mit denen OSM den amtlichen Namen verlängert („Herxheim bei Landau/Pfalz“)
_NAME_ADDITION_RE = re.compile(r"^(?:bei|b\.|am|an der|an|im|in der|in|ob der|vor der|auf der)\s")

# Klammerzusatz („Willingen (Upland)“), linear auswertbar
_PARENTHESIS_RE = re.compile(r"\([^()]*\)")


@dataclass(frozen=True)
class ParsedName:
    """Name einer Körperschaft, zerlegt in Grundname und Kategorie aus dem Präfix."""

    base: str
    category: str | None

    @property
    def levels(self) -> frozenset[int] | None:
        return LEVELS_BY_CATEGORY.get(self.category) if self.category else None


def parse_body_name(name: str) -> ParsedName:
    """„Ortsgemeinde Boden“ → („Boden“, gemeinde); „Stadt Köln, kreisfreie Stadt“ → („Köln“, stadt)."""
    text = " ".join((name or "").split())
    head, _, tail = text.partition(",")
    head = head.strip()
    category: str | None = None
    lowered = head.casefold()
    for prefix, prefix_category in NAME_PREFIXES:
        if lowered.startswith(prefix + " ") and len(head) > len(prefix) + 1:
            head = head[len(prefix) + 1 :].strip()
            category = prefix_category
            break
    if category is None and tail:
        # „Köln, kreisfreie Stadt“: Kategorie aus dem Zusatz nach dem Komma
        tail_lowered = tail.strip().casefold()
        category = next((c for p, c in NAME_PREFIXES if tail_lowered == p or tail_lowered.endswith(" " + p)), None)
    return ParsedName(base=head, category=category)


def name_key(value: str) -> str:
    """Vergleichsschlüssel: Unicode-normalisiert, Groß/Klein und ß egal, Bindestrich/Schrägstrich = Leerzeichen."""
    text = unicodedata.normalize("NFC", value or "").casefold()
    text = re.sub(r"[-‐–/]", " ", text)
    return " ".join(text.split())


def name_keys(value: str) -> set[str]:
    """Schlüssel mit und ohne Klammerzusatz („Willingen (Upland)“ → „willingen upland“, „willingen“)."""
    keys = {name_key(value), name_key(_PARENTHESIS_RE.sub(" ", value or ""))}
    return {key for key in keys if key}


def is_non_territorial_name(*names: str | None) -> bool:
    """Namensregel „keine Gebietskörperschaft“ (Zweckverband, GmbH, AöR, Waldgemarkung, Stiftung …)."""
    return any(_NON_TERRITORIAL_RE.search(name) for name in names if name)


def categories_compatible(first: str | None, second: str | None) -> bool:
    """Gleiche Kategorie; Stadt und Gemeinde gelten als verträglich (eine Stadt ist eine Gemeinde)."""
    if not first or not second or first == second:
        return True
    return {first, second} == {"gemeinde", "stadt"}


# ---------------------------------------------------------------------------
# Schlüssel (AGS/RGS)
# ---------------------------------------------------------------------------

AGS_LENGTHS = frozenset({2, 3, 5, 8})  # Land, Regierungsbezirk, Kreis, Gemeinde
RGS_LENGTHS = frozenset({2, 3, 5, 9, 12})  # zusätzlich Gemeindeverband (9)


def _clean_key(value: Any, lengths: frozenset[int]) -> str:
    """Nur Ziffern; eine als Zahl verlorene führende Null wird ergänzt (``5315000`` → ``05315000``)."""
    if value is None or isinstance(value, bool):
        return ""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) in lengths:
        return digits
    if len(digits) + 1 in lengths and not digits.startswith("0"):
        return "0" + digits
    return ""


def ags_from_rgs(rgs: str) -> str:
    """Gemeindeschlüssel aus dem Regionalschlüssel: 12 Stellen → Stellen 1–5 und 10–12."""
    if len(rgs) == 12:
        return rgs[:5] + rgs[9:]
    if len(rgs) in (2, 3, 5):
        return rgs
    return ""  # 9 Stellen: Gemeindeverband, hat keinen Gemeindeschlüssel


def keys_from_oparl(raw_json: Mapping[str, Any] | None) -> tuple[str, str]:
    """(AGS, RGS) aus einem OParl-Body-Objekt; fehlt ``ags``, wird er aus ``rgs`` abgeleitet."""
    raw = raw_json if isinstance(raw_json, Mapping) else {}
    ags = _clean_key(raw.get("ags"), AGS_LENGTHS)
    rgs = _clean_key(raw.get("rgs"), RGS_LENGTHS)
    if not ags and rgs:
        ags = ags_from_rgs(rgs)
    return ags, rgs


# ---------------------------------------------------------------------------
# OSM-Grenzen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OsmBoundary:
    """Eine Verwaltungsgrenze (Relation) aus Overpass mit den für uns wichtigen Tags."""

    relation_id: int
    name: str
    admin_level: int | None = None
    ags: str = ""
    rgs: str = ""
    name_prefix: str = ""
    other_names: tuple[str, ...] = ()

    @classmethod
    def from_element(cls, element: Mapping[str, Any]) -> OsmBoundary | None:
        if element.get("type") != "relation" or element.get("id") is None:
            return None
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name:
            return None
        try:
            admin_level: int | None = int(str(tags.get("admin_level")))
        except ValueError:
            admin_level = None
        return cls(
            relation_id=int(element["id"]),
            name=name,
            admin_level=admin_level,
            ags=_clean_key(tags.get("de:amtlicher_gemeindeschluessel"), AGS_LENGTHS),
            rgs=_clean_key(tags.get("de:regionalschluessel"), RGS_LENGTHS),
            name_prefix=str(tags.get("name:prefix") or "").strip(),
            other_names=tuple(
                str(tags[key]).strip() for key in ("official_name", "short_name", "alt_name") if tags.get(key)
            ),
        )

    @property
    def category(self) -> str | None:
        if self.name_prefix:
            prefix = self.name_prefix.casefold()
            category = _NAME_PREFIXES.get(prefix)
            if category:
                return category
        return parse_body_name(self.name).category

    @property
    def keys(self) -> set[str]:
        keys: set[str] = set()
        for value in (self.name, parse_body_name(self.name).base, *self.other_names):
            keys |= name_keys(value)
        return keys

    @property
    def is_german(self) -> bool:
        return bool(self.ags or self.rgs)

    def describe(self) -> str:
        parts = [f"Relation {self.relation_id}", f"„{self.name}“"]
        if self.admin_level is not None:
            parts.append(f"Ebene {self.admin_level}")
        if self.ags:
            parts.append(f"AGS {self.ags}")
        elif self.rgs:
            parts.append(f"RGS {self.rgs}")
        return ", ".join(parts)


def _matches_with_addition(body_keys: set[str], candidate_keys: set[str]) -> bool:
    """„herxheim“ passt zu „herxheim bei landau pfalz“ (amtlicher Namenszusatz)."""
    for body_key in body_keys:
        for candidate_key in candidate_keys:
            if candidate_key.startswith(body_key + " ") and _NAME_ADDITION_RE.match(candidate_key[len(body_key) + 1 :]):
                return True
    return False


def lies_within(boundary: OsmBoundary, *, parent_rgs: str = "", parent_ags: str = "") -> bool:
    """Liegt die Grenze laut Schlüssel in der übergeordneten Körperschaft?

    Gemeinden einer Verbandsgemeinde beginnen im Regionalschlüssel mit deren neun Stellen
    (``071435004`` → ``071435004005``), Gemeinden eines Kreises mit dessen fünf Stellen. Ohne
    vergleichbaren Schlüssel (etwa bei Ortsteilen) gilt die Grenze als enthalten.
    """
    if parent_rgs and boundary.rgs and not (boundary.rgs.startswith(parent_rgs) and boundary.rgs != parent_rgs):
        return False
    if parent_ags:
        keys = [key for key in (boundary.ags, boundary.rgs) if key]
        if keys and not any(key.startswith(parent_ags) and key != parent_ags for key in keys):
            return False
    return True


def _first_word(key: str) -> str:
    return key.split(" ", 1)[0]


def match_boundaries(
    parsed: ParsedName,
    boundaries: Iterable[OsmBoundary],
    *,
    within_area: bool,
    parent_rgs: str = "",
    parent_ags: str = "",
    exclude: Iterable[int] = (),
) -> tuple[list[OsmBoundary], list[OsmBoundary]]:
    """Kandidaten für einen Körperschaftsnamen: (eindeutige Namenstreffer, ungefähre Treffer).

    Eindeutig heißt: gleicher Name (auch ohne Klammerzusatz, im Gebiet auch mit amtlichem Zusatz
    wie „bei Landau“), passende Verwaltungsebene und verträgliche Kategorie. Außerhalb eines
    Gebiets zählen nur deutsche Grenzen (mit AGS oder RGS). Die Schlüssel der übergeordneten
    Körperschaft filtern Grenzen heraus, die nicht in ihr liegen (siehe ``lies_within``).
    Ungefähre Treffer (gleiches erstes Wort, Präfix) gibt es nur innerhalb eines Gebiets.
    """
    body_keys = name_keys(parsed.base)
    levels = parsed.levels
    excluded = set(exclude)
    exact: list[OsmBoundary] = []
    approximate: list[OsmBoundary] = []
    seen: set[int] = set()
    for boundary in boundaries:
        if boundary.relation_id in excluded or boundary.relation_id in seen:
            continue
        seen.add(boundary.relation_id)
        if not within_area and not boundary.is_german:
            continue
        if not lies_within(boundary, parent_rgs=parent_rgs, parent_ags=parent_ags):
            continue
        candidate_keys = boundary.keys
        same_name = bool(body_keys & candidate_keys) or (
            within_area and _matches_with_addition(body_keys, candidate_keys)
        )
        fits_level = levels is None or boundary.admin_level is None or boundary.admin_level in levels
        fits_category = categories_compatible(parsed.category, boundary.category)
        if same_name and fits_level and fits_category:
            exact.append(boundary)
            continue
        if within_area and fits_category:
            body_first = {_first_word(k) for k in body_keys}
            candidate_first = {_first_word(k) for k in candidate_keys}
            prefix_match = any(c.startswith(b) or b.startswith(c) for b in body_keys for c in candidate_keys)
            if same_name or body_first & candidate_first or prefix_match:
                approximate.append(boundary)
    return exact, approximate


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

STATUS_ASSIGNED = "zugeordnet"
STATUS_SUGGESTED = "vorschlag"
STATUS_NOT_FOUND = "kein Treffer"
STATUS_NON_TERRITORIAL = "ohne Gebiet"
STATUS_UNCHANGED = "unverändert"
STATUS_FAILED = "Overpass-Fehler"
STATUS_SKIPPED = "übersprungen"


@dataclass
class BodyResolution:
    """Ergebnis je Kommune: was geändert wurde (bzw. im Probelauf geändert würde)."""

    body: OParlBody
    status: str = STATUS_UNCHANGED
    notes: list[str] = field(default_factory=list)
    changed_fields: set[str] = field(default_factory=set)
    new_relation: bool = False
    suggestions: list[OsmBoundary] = field(default_factory=list)
    suggestion_reason: str = ""


class OverpassUnavailableError(Exception):
    """Overpass hat auf keinem Endpoint geantwortet."""


class BodyGeoResolver:
    """Führt die Schritte 1–4 (siehe Moduldoku) für eine Menge von Kommunen aus."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        pause: float = 3.0,
        timeout: int = 90,
        max_failures: int = 3,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.pause = pause
        self.timeout = timeout
        self.max_failures = max_failures
        self.log = log
        self.aborted = False
        self.queries = 0
        self._consecutive_failures = 0
        self._area_cache: dict[int, list[OsmBoundary]] = {}
        self._name_cache: dict[tuple[str, ...], list[OsmBoundary]] = {}

    # --- Overpass ---------------------------------------------------------

    def _overpass(self, query: str) -> list[OsmBoundary]:
        if self.aborted:
            raise OverpassUnavailableError
        if self.queries and self.pause > 0:
            time.sleep(self.pause)  # Pause zwischen zwei Abfragen, freundlich zu OSM
        self.queries += 1
        elements = run_overpass(f"[out:json][timeout:{self.timeout}];{query}out tags;", self.timeout, log=self.log)
        if elements is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_failures:
                self.aborted = True
            raise OverpassUnavailableError
        self._consecutive_failures = 0
        return [b for b in (OsmBoundary.from_element(el) for el in elements) if b is not None]

    def boundaries_by_ags(self, ags: str) -> list[OsmBoundary]:
        return self._overpass(
            f'relation["boundary"="administrative"]["de:amtlicher_gemeindeschluessel"="{overpass_string(ags)}"];'
        )

    def boundary_by_id(self, relation_id: int) -> OsmBoundary | None:
        found = self._overpass(f"relation({int(relation_id)});")
        return found[0] if found else None

    def boundaries_in_area(self, relation_id: int) -> list[OsmBoundary]:
        if relation_id not in self._area_cache:
            area_id = OSM_AREA_OFFSET + int(relation_id)
            self._area_cache[relation_id] = self._overpass(
                f'area({area_id})->.gebiet;relation(area.gebiet)["boundary"="administrative"]["name"];'
            )
        return self._area_cache[relation_id]

    def boundaries_by_name(self, names: Iterable[str]) -> list[OsmBoundary]:
        unique = tuple(sorted({n for n in names if n}))
        if not unique:
            return []
        if unique not in self._name_cache:
            union = "".join(f'relation["boundary"="administrative"]["name"="{overpass_string(n)}"];' for n in unique)
            self._name_cache[unique] = self._overpass(f"({union});")
        return self._name_cache[unique]

    # --- Ablauf -------------------------------------------------------------

    def resolve(self, bodies: Iterable[OParlBody]) -> list[BodyResolution]:
        """Ordnet die übergebenen Kommunen zu; übergeordnete Körperschaften zuerst."""
        scope = [b for b in bodies if not b.deleted]
        # Alle Körperschaften der beteiligten Quellen: Kandidaten für „übergeordnet“
        by_source: dict[Any, list[OParlBody]] = {}
        instances = {b.id: b for b in scope}
        for other in OParlBody.objects.filter(source_id__in={b.source_id for b in scope}, deleted=False):
            instance = instances.setdefault(other.id, other)
            by_source.setdefault(other.source_id, []).append(instance)

        results = {b.id: BodyResolution(body=b) for b in scope}
        for body in scope:
            self._classify_territory(results[body.id], by_source.get(body.source_id, []))

        # Übergeordnete zuerst: Ihre Grenze ist das Suchgebiet der übrigen Körperschaften der Quelle
        ordered = sorted(scope, key=lambda b: (_resolution_order(b), b.name))
        for body in ordered:
            result = results[body.id]
            if not body.is_non_territorial:
                if self.aborted:
                    result.status = STATUS_SKIPPED
                    result.notes.append("Overpass mehrfach nicht erreichbar – nicht geprüft")
                else:
                    try:
                        self._resolve_territorial(result, by_source.get(body.source_id, []))
                    except OverpassUnavailableError:
                        result.status = STATUS_FAILED
                        result.notes.append("Overpass nicht erreichbar – beim nächsten Aufruf erneut")
            # Je Kommune sofort speichern: Ein abgebrochener Lauf verliert nichts, der nächste macht weiter
            self._save(result)
        return [results[b.id] for b in scope]

    # --- Schritt 1: Körperschaften ohne Gebiet ------------------------------

    def _classify_territory(self, result: BodyResolution, source_bodies: list[OParlBody]) -> None:
        body = result.body
        if body.territory_set_manually:
            if body.is_non_territorial:
                result.status = STATUS_NON_TERRITORIAL
                result.notes.append("keine Gebietskörperschaft (von Hand gesetzt)")
            return
        flag = is_non_territorial_name(body.name, body.classification)
        if flag != body.is_non_territorial:
            body.is_non_territorial = flag
            result.changed_fields.add("is_non_territorial")
        if not flag:
            if body.territory_parent_id is not None:
                body.territory_parent = None
                result.changed_fields.add("territory_parent")
            return
        result.status = STATUS_NON_TERRITORIAL
        result.notes.append("keine Gebietskörperschaft (Namensregel)")
        if body.territory_parent_id is None:
            parent = territory_parent_for(body, source_bodies)
            if parent is not None:
                body.territory_parent = parent
                result.changed_fields.add("territory_parent")
        if body.territory_parent_id is not None and body.territory_parent is not None:
            result.notes.append(f"Gebiet von {body_label(body.territory_parent)}")
        else:
            result.notes.append("übergeordnete Körperschaft nicht eindeutig – im Admin „Gebiet von“ setzen")

    # --- Schritte 2–4: Gebietskörperschaften --------------------------------

    def _resolve_territorial(self, result: BodyResolution, source_bodies: list[OParlBody]) -> None:
        body = result.body
        self._keys_from_oparl(result)

        if body.osm_relation_id:
            if not (body.ags or body.rgs):
                boundary = self.boundary_by_id(body.osm_relation_id)
                if boundary is not None:
                    self._take_keys(result, boundary)
            return

        if body.ags:
            found = self.boundaries_by_ags(body.ags)
            if len(found) == 1:
                self._assign(result, found[0], f"per AGS {body.ags}")
                return
            if len(found) > 1:
                self._suggest(result, found, f"{len(found)} Grenzen mit AGS {body.ags}")
                return
            result.notes.append(f"keine OSM-Grenze mit AGS {body.ags} – Namenssuche")

        parsed = parse_body_name(body.name)
        if not parsed.base:
            result.status = STATUS_NOT_FOUND
            return
        parents = area_parents_for(body, source_bodies)
        if parents:
            self._search_in_parents(result, parsed, parents)
        else:
            self._search_by_name(result, parsed)

    def _search_in_parents(self, result: BodyResolution, parsed: ParsedName, parents: list[OParlBody]) -> None:
        exact: list[OsmBoundary] = []
        approximate: list[OsmBoundary] = []
        for parent in parents:
            assert parent.osm_relation_id is not None
            found_exact, found_approx = match_boundaries(
                parsed,
                self.boundaries_in_area(parent.osm_relation_id),
                within_area=True,
                parent_rgs=parent.rgs or "",
                parent_ags=(parent.ags or "") if parent.is_regional_level else "",
                exclude=[parent.osm_relation_id],
            )
            exact += [b for b in found_exact if b not in exact]
            approximate += [b for b in found_approx if b not in approximate]
        where = " / ".join(body_label(p) for p in parents)
        if len(exact) == 1:
            self._assign(result, exact[0], f"Namenssuche in {where}")
        elif exact:
            self._suggest(result, exact, f"{len(exact)} Namenstreffer in {where}")
        elif approximate:
            self._suggest(result, approximate, f"ungefährer Namenstreffer in {where}")
        else:
            result.status = STATUS_NOT_FOUND
            result.notes.append(f"kein Namenstreffer für „{parsed.base}“ in {where}")

    def _search_by_name(self, result: BodyResolution, parsed: ParsedName) -> None:
        names = {parsed.base, " ".join(result.body.name.partition(",")[0].split())}
        exact, _ = match_boundaries(parsed, self.boundaries_by_name(names), within_area=False)
        if len(exact) == 1:
            self._assign(result, exact[0], "bundesweite Namenssuche")
        elif exact:
            self._suggest(result, exact, f"{len(exact)} gleichnamige Grenzen bundesweit")
        else:
            result.status = STATUS_NOT_FOUND
            result.notes.append(f"keine OSM-Grenze namens „{parsed.base}“")

    # --- Änderungen ---------------------------------------------------------

    def _keys_from_oparl(self, result: BodyResolution) -> None:
        body = result.body
        ags, rgs = keys_from_oparl(body.raw_json)
        if ags and not body.ags:
            body.ags = ags
            result.changed_fields.add("ags")
            result.notes.append(f"AGS {ags} aus OParl")
        elif ags and body.ags and ags != body.ags:
            result.notes.append(f"AGS aus OParl ({ags}) weicht vom gepflegten AGS ({body.ags}) ab – unverändert")
        if rgs and not body.rgs:
            body.rgs = rgs
            result.changed_fields.add("rgs")

    def _take_keys(self, result: BodyResolution, boundary: OsmBoundary) -> None:
        body = result.body
        if boundary.ags and not body.ags:
            body.ags = boundary.ags
            result.changed_fields.add("ags")
            result.notes.append(f"AGS {boundary.ags} aus OSM")
        if boundary.rgs and not body.rgs:
            body.rgs = boundary.rgs
            result.changed_fields.add("rgs")

    def _assign(self, result: BodyResolution, boundary: OsmBoundary, how: str) -> None:
        body = result.body
        body.osm_relation_id = boundary.relation_id
        result.changed_fields.add("osm_relation_id")
        result.new_relation = True
        result.status = STATUS_ASSIGNED
        result.notes.append(f"{boundary.describe()} ({how})")
        self._take_keys(result, boundary)

    def _suggest(self, result: BodyResolution, boundaries: list[OsmBoundary], reason: str) -> None:
        result.status = STATUS_SUGGESTED
        result.suggestions = list(boundaries)
        result.notes.append(f"{reason} – Vorschläge zur Bestätigung im Admin")
        result.suggestion_reason = reason
        for boundary in boundaries:
            result.notes.append(f"  Vorschlag: {boundary.describe()}")

    def _save(self, result: BodyResolution) -> None:
        if self.dry_run:
            return
        body = result.body
        if result.changed_fields:
            body.save(update_fields=sorted(result.changed_fields | {"updated_at"}))
        if result.suggestions:
            reason = result.suggestion_reason[:255]
            OParlBodyGeoSuggestion.objects.filter(body=body).delete()
            OParlBodyGeoSuggestion.objects.bulk_create(
                [
                    OParlBodyGeoSuggestion(
                        body=body,
                        osm_relation_id=b.relation_id,
                        name=b.name[:255],
                        admin_level=b.admin_level,
                        ags=b.ags,
                        rgs=b.rgs,
                        reason=reason,
                    )
                    for b in result.suggestions
                ]
            )
        elif body.osm_relation_id or body.is_non_territorial:
            # Zugeordnet oder ohne Gebiet: offene Vorschläge sind erledigt
            OParlBodyGeoSuggestion.objects.filter(body=body).delete()


def body_label(body: OParlBody) -> str:
    """Lesbarer Name für Ausgaben; Kurznamen aus OParl sind oft Codes wie „00VG“."""
    return body.display_name or body.name


def _category_level(category: str | None) -> int:
    levels = LEVELS_BY_CATEGORY.get(category) if category else None
    return min(levels) if levels else 99


def _resolution_order(body: OParlBody) -> int:
    """Übergeordnete Körperschaften zuerst (Bezirk, Kreis, Verbandsgemeinde), dann der Rest."""
    category = parse_body_name(body.name).category
    return _category_level(category) if category in AREA_CATEGORIES else 99


def area_parents_for(body: OParlBody, source_bodies: list[OParlBody]) -> list[OParlBody]:
    """Übergeordnete Gebietskörperschaften derselben Quelle mit OSM-Grenze, nur die engste Ebene.

    Beispiel: Für „Ortsgemeinde Boden“ ist das „Verbandsgemeinde Montabaur“. Eine Körperschaft sucht
    nur in Gebieten einer höheren Ebene (Verbandsgemeinde im Kreis, nicht umgekehrt).
    """
    own_levels = parse_body_name(body.name).levels
    own_max = max(own_levels) if own_levels else 99
    candidates: list[tuple[int, OParlBody]] = []
    for other in source_bodies:
        if other.id == body.id or other.is_non_territorial or not other.osm_relation_id:
            continue
        category = parse_body_name(other.name).category
        if category not in AREA_CATEGORIES:
            continue
        level = _category_level(category)
        if level < own_max:
            candidates.append((level, other))
    if not candidates:
        return []
    tightest = max(level for level, _ in candidates)
    return [other for level, other in candidates if level == tightest]


def territory_parent_for(body: OParlBody, source_bodies: list[OParlBody]) -> OParlBody | None:
    """Übergeordnete Körperschaft für eine Körperschaft ohne Gebiet (engste eindeutige Ebene).

    Gibt es in der Quelle keine Verbandsgemeinde, keinen Kreis o. Ä., aber genau eine
    Gebietskörperschaft (Stadt mit Stadtwerke-GmbH), gilt deren Gebiet.
    """
    territorial = [
        other
        for other in source_bodies
        if other.id != body.id and not is_non_territorial_name(other.name, other.classification)
    ]
    area = [(_category_level(parse_body_name(o.name).category), o) for o in territorial]
    area = [(level, o) for level, o in area if parse_body_name(o.name).category in AREA_CATEGORIES]
    if area:
        tightest = max(level for level, _ in area)
        at_level = [o for level, o in area if level == tightest]
        return at_level[0] if len(at_level) == 1 else None
    return territorial[0] if len(territorial) == 1 else None


def apply_parent_area(body: OParlBody, *, dry_run: bool = False) -> OParlBody | None:
    """Körperschaft ohne Gebiet: Zentrum und Kartenausschnitt der übergeordneten Körperschaft übernehmen.

    Nur wenn die Körperschaft noch keinen eigenen Ausschnitt hat (Handpflege bleibt erhalten) und die
    übergeordnete einen hat. Gibt die übergeordnete Körperschaft zurück, wenn übernommen wurde.
    """
    parent = body.territory_parent
    if not body.is_non_territorial or parent is None:
        return None
    if body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west:
        return None
    if not (parent.bbox_north and parent.bbox_south and parent.bbox_east and parent.bbox_west):
        return None
    if not dry_run:
        for name in ("latitude", "longitude", "bbox_north", "bbox_south", "bbox_east", "bbox_west"):
            setattr(body, name, getattr(parent, name))
        body.save(
            update_fields=["latitude", "longitude", "bbox_north", "bbox_south", "bbox_east", "bbox_west", "updated_at"]
        )
    return parent


def apply_suggestion(suggestion: OParlBodyGeoSuggestion) -> OParlBody:
    """Admin „Vorschlag übernehmen“: Relation und fehlende Schlüssel setzen, übrige Vorschläge verwerfen."""
    body = suggestion.body
    body.osm_relation_id = suggestion.osm_relation_id
    fields = ["osm_relation_id", "updated_at"]
    if suggestion.ags and not body.ags:
        body.ags = suggestion.ags
        fields.append("ags")
    if suggestion.rgs and not body.rgs:
        body.rgs = suggestion.rgs
        fields.append("rgs")
    body.save(update_fields=fields)
    OParlBodyGeoSuggestion.objects.filter(body=body).delete()
    return body
