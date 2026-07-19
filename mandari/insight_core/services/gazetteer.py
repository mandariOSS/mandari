# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Straßenverzeichnis (Gazetteer) für die Georeferenzierung.

Vorbild "Politik bei Uns" / "Meine Stadt Transparent": Das aus OSM
importierte Straßenverzeichnis der Kommune ist die Wahrheitsquelle.
Nur Kandidaten, die dort vorkommen, werden als Ortsbezug gewertet —
die Geometrie (Zentroid) kommt direkt aus OSM, ohne API-Geocoding.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from insight_core.models import OParlBody

# Mindestlänge eines normalisierten Straßennamens für die Volltextsuche
MIN_STREET_NAME_LEN = 5
# Kürzere Namen (z.B. "Kamp", "Esch") zählen nur MIT Hausnummer
MIN_SHORT_NAME_LEN = 3

# Chunk-Größe für die Alternations-Regexes der Volltextsuche
_CHUNK_SIZE = 500

# Wortbestandteile, die einen Straßennamen als "echten" Straßennamen
# ausweisen (für den Personen-Namensabgleich: "Bismarckstraße" ist sicher,
# der Straßenname "Kamp" könnte auch ein Nachname sein).
_STREET_SUFFIX_RE = re.compile(
    r"(?:strasse|gasse|allee|chaussee|weg|platz|ring|damm|ufer|bruecke|brucke|"
    r"pfad|steig|stieg|kamp|bogen|graben|deich|esch|wall|markt|hof)$"
)


def normalize_street_name(name: str) -> str:
    """
    Kanonische Normalisierung von Straßennamen für Gazetteer-Lookups.

    - Kleinschreibung, ß→ss
    - "Str." / "str." → "strasse" (auch angehängt: "Schillerstr." → "schillerstrasse")
    - Bindestriche → Leerzeichen ("Johann-Krane-Weg" → "johann krane weg")
    - Mehrfach-Leerzeichen zusammenfassen
    """
    s = (name or "").strip().lower()
    s = s.replace("ß", "ss")
    # Abkürzung "str." (mit Punkt) am Wortende → "strasse"
    s = re.sub(r"str\.(?=\s|$)", "strasse", s)
    # Bindestriche und Slashes als Worttrenner behandeln
    s = re.sub(r"[-/]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_house_number(name: str) -> tuple[str, str | None]:
    """Trennt eine ggf. angehängte Hausnummer ab: 'Hauptstraße 12a' → ('Hauptstraße', '12a')."""
    match = re.search(r"\s+(\d{1,4}\s*[a-zA-Z]?)\s*$", name or "")
    if match:
        return name[: match.start()].strip(), match.group(1).replace(" ", "")
    return (name or "").strip(), None


def has_street_suffix(normalized_name: str) -> bool:
    """Prüft, ob das letzte Wort eines normalisierten Namens ein Straßen-Suffix trägt."""
    words = normalized_name.split()
    if not words:
        return False
    return bool(_STREET_SUFFIX_RE.search(words[-1]))


class StreetGazetteer:
    """
    In-Memory-Straßenverzeichnis einer Kommune.

    Lädt alle Street-Einträge des Bodys, gruppiert nach normalisiertem Namen
    (mehrere OSM-Ways pro Straße) und mittelt deren Zentroide.
    """

    def __init__(self, body: OParlBody):
        from insight_core.models import Street

        self.body = body
        self._streets: dict[str, dict] = {}

        rows = Street.objects.filter(body=body).values_list("normalized_name", "name", "latitude", "longitude")
        sums: dict[str, list] = {}
        for normalized, name, lat, lon in rows:
            if not normalized:
                continue
            entry = sums.setdefault(normalized, [name, 0.0, 0.0, 0])
            entry[1] += float(lat)
            entry[2] += float(lon)
            entry[3] += 1

        for normalized, (name, lat_sum, lon_sum, count) in sums.items():
            self._streets[normalized] = {
                "name": name,
                "lat": lat_sum / count,
                "lon": lon_sum / count,
            }

        self._search_patterns: list[re.Pattern] | None = None

    def __len__(self) -> int:
        return len(self._streets)

    def __bool__(self) -> bool:
        return bool(self._streets)

    def lookup(self, raw_name: str) -> dict | None:
        """
        Sucht einen Kandidaten (ggf. mit Hausnummer) im Straßenverzeichnis.

        Returns:
            Dict mit name, lat, lon, house_number oder None.
        """
        base, house_number = strip_house_number(raw_name)
        normalized = normalize_street_name(base)
        # Kurze Namen ("Kamp") nur mit Hausnummer akzeptieren
        min_len = MIN_SHORT_NAME_LEN if house_number else MIN_STREET_NAME_LEN
        if len(normalized) < min_len:
            return None
        entry = self._streets.get(normalized)
        if not entry:
            return None
        return {**entry, "house_number": house_number, "normalized": normalized}

    def _build_search_patterns(self) -> list[re.Pattern]:
        """
        Baut Alternations-Regexes über alle Straßennamen (Volltextsuche).

        Längste Namen zuerst → Longest-Match-Disambiguierung wie bei
        "Politik bei Uns" v2 ("Neubrückenstraße" gewinnt gegen "Brückenstraße").
        """
        names = sorted(
            (n for n in self._streets if len(n) >= MIN_STREET_NAME_LEN),
            key=len,
            reverse=True,
        )
        patterns = []
        for start in range(0, len(names), _CHUNK_SIZE):
            chunk = names[start : start + _CHUNK_SIZE]
            alternation = "|".join(re.escape(n) for n in chunk)
            patterns.append(
                re.compile(
                    r"(?<![a-zäöü0-9])(" + alternation + r")"
                    r"(?![a-zäöü])"
                    r"(?:\s+(\d{1,4})\s*([a-z])?(?![a-z0-9]))?"
                )
            )

        # Kurze Namen ("Kamp", "Esch"): nur mit Hausnummer matchen
        short_names = sorted(
            (n for n in self._streets if MIN_SHORT_NAME_LEN <= len(n) < MIN_STREET_NAME_LEN),
            key=len,
            reverse=True,
        )
        if short_names:
            alternation = "|".join(re.escape(n) for n in short_names)
            patterns.append(
                re.compile(
                    r"(?<![a-zäöü0-9])(" + alternation + r")"
                    r"(?![a-zäöü])"
                    r"\s+(\d{1,4})\s*([a-z])?(?![a-z0-9])"
                )
            )
        return patterns

    def find_in_text(self, text: str) -> list[dict]:
        """
        Direkte Gazetteer-Suche im Volltext (Wortgrenzen, Schreibvarianten).

        Der Text wird mit derselben Normalisierung wie die Straßennamen
        vorbereitet, dadurch matchen "Wolbecker Str.", "WOLBECKER STRASSE"
        und "Wolbecker-Straße" denselben Eintrag.

        Returns:
            Liste von Dicts (name, lat, lon, house_number, normalized),
            dedupliziert pro Straße; Vorkommen mit Hausnummer werden bevorzugt.
        """
        if not self._streets or not text:
            return []
        if self._search_patterns is None:
            self._search_patterns = self._build_search_patterns()

        normalized_text = normalize_street_name(text)

        found: dict[str, dict] = {}
        matched_spans: list[tuple[int, int]] = []
        for pattern in self._search_patterns:
            for match in pattern.finditer(normalized_text):
                span = (match.start(1), match.end(1))
                # Longest-Match über Chunk-Grenzen: bereits durch einen
                # längeren Treffer abgedeckte Bereiche überspringen
                if any(s <= span[0] and span[1] <= e for s, e in matched_spans):
                    continue
                matched_spans.append(span)

                normalized = match.group(1)
                entry = self._streets.get(normalized)
                if not entry:
                    continue
                house_number = None
                if match.group(2):
                    house_number = match.group(2) + (match.group(3) or "")
                existing = found.get(normalized)
                if existing is None or (house_number and not existing.get("house_number")):
                    found[normalized] = {
                        **entry,
                        "house_number": house_number,
                        "normalized": normalized,
                    }
        return list(found.values())


def get_person_name_set(body: OParlBody) -> set[str]:
    """
    Normalisierte Namen aller Personen des Bodys (MST-Trick gegen
    False Positives: Straßenname == Nachname eines Ratsmitglieds).
    """
    from insight_core.models import OParlPerson

    names: set[str] = set()
    for name, family_name, given_name in OParlPerson.objects.filter(body=body).values_list(
        "name", "family_name", "given_name"
    ):
        for value in (name, family_name, given_name):
            if value and len(value.strip()) >= 4:
                names.add(normalize_street_name(value))
    return names
