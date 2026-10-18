# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abfrage v2 der Volltextsuche (Konzept Insight-Suche, P0.2/P0.3).

Reine Funktionen ohne Elasticsearch-Zugriff; ``search_service`` baut daraus je Index die Abfrage, wenn
``SEARCH_RANKING=v2`` gilt. Gegenüber v1 (``best_fields``, ODER, Unschärfe immer, Synonyme des
Such-Analyzers):

* **UND statt ODER**, Hauptklausel ``cross_fields`` getrennt nach Analyzer: Felder mit ``german_custom``
  bekommen den Analyzer als Override (ohne die Synonyme des Such-Analyzers), Felder mit ``standard``
  (Aktenzeichen, Dateinamen, Namensteile) eine eigene Klausel; beide per ``dis_max`` verbunden. Ein
  einziger Override über alle Felder träfe die ``standard``-Felder nicht, weil er Stammformen bildet.
* **Straßen**: „Str.“ wird zu „Straße“, ein freistehendes Grundwort („Straße“, „Weg“ …) ist optional,
  ein Kompositum („Hafenstraße“) bekommt Alternativen („hafenstr“, Phrase „hafen straße“ und den
  Namensteil, wenn er selten ist).
* **Aktenzeichen** exakt als ``term`` auf ``reference.keyword``, Phrasenbonus auf Titel und Text,
  kuratierte Gleichbedeutungen mit geringem Gewicht statt der breiten Synonymgruppen.
* **Aktualität** als ``function_score``: Faktor zwischen 1 und ``1 + Gewicht``, nur für Dokumente mit
  Datum (``exists``-Filter), sonst gälte ein fehlendes Datum als neu.
* **Unschärfe** nur als Rückfall (``fuzzy_query``), wenn die genaue Suche kaum etwas findet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

#: Grundwörter von Straßennamen; als eigenes Wort nach einem Namensteil optional
STREET_SUFFIXES: Final = (
    "straße",
    "strasse",
    "allee",
    "gasse",
    "platz",
    "damm",
    "pfad",
    "ring",
    "ufer",
    "wall",
    "weg",
)
#: Mindestlänge des Namensteils vor dem Grundwort („Hafen“ ja, „Weg“ in „Zweg“ nein)
MIN_NAME_PART: Final = 4

#: Kuratierte Gleichbedeutungen und Abkürzungen (klein, ohne Stammformen). Bewusst keine Oberbegriffe:
#: „Kita“ ist nicht „Schule“, „Haushalt“ nicht „Finanzamt“.
CURATED_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "kita": ("kindertagesstätte", "kindertageseinrichtung"),
    "kitas": ("kindertagesstätten", "kindertageseinrichtungen"),
    "kindertagesstätte": ("kita",),
    "kindertageseinrichtung": ("kita",),
    "b-plan": ("bebauungsplan",),
    "bplan": ("bebauungsplan",),
    "bebauungsplan": ("b-plan",),
    "vbp": ("vorhabenbezogener bebauungsplan",),
    "bv": ("bezirksvertretung",),
    "bezirksvertretung": ("bv",),
    "ogs": ("offene ganztagsschule",),
}
SYNONYM_BOOST: Final = 0.3
PHRASE_BOOST: Final = 2.0
REFERENCE_BOOST: Final = 10.0
SUFFIX_BOOST: Final = 0.5

#: Felder je Index, getrennt nach Analyzer. ``titel``/``text`` sind ``german_custom``-Felder für den
#: Phrasenbonus. Keyword-Felder (``paper_type``, ``organization_type``) stehen nicht in ``cross_fields``.
FIELDS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "papers": {
        "german": ("name^3", "file_contents_preview", "organization_names"),
        "standard": ("reference^2", "file_names"),
        "phrase": ("name^2", "file_contents_preview"),
    },
    "meetings": {
        "german": ("name^3", "organization_names^2"),
        "standard": ("location_name",),
        "phrase": ("name",),
    },
    "persons": {
        "german": ("name^3",),
        "standard": ("given_name^2", "family_name^2", "title"),
        "phrase": ("name",),
    },
    "organizations": {
        "german": ("name^3",),
        "standard": ("short_name^2",),
        "phrase": ("name",),
    },
    "files": {
        "german": ("name^2", "text_content", "paper_name^2", "organization_names"),
        "standard": ("file_name", "paper_reference"),
        "phrase": ("name^2", "paper_name^2", "text_content"),
    },
}
#: Datumsfeld je Index für Aktualität, Sortierung „Neueste“ und Zeitraum
DATE_FIELD_BY_INDEX: Final[dict[str, str]] = {
    "papers": "date",
    "meetings": "start",
    "files": "meeting_date",
}

#: „Hafenstr.“ und „Hafenstr“ (Namensteil mit mindestens vier Buchstaben) → „hafenstraße“
_STR_ABBREVIATION = re.compile(r"(?<=[^\W\d_]{4})str\.?(?=\s|$)")
#: „Str.“ als eigenes Wort oder nach Bindestrich („Von-Witzleben-Str.“)
_STANDALONE_STR = re.compile(r"(?:(?<=[\s-])|^)str\.?(?=\s|$)")
_REFERENCE = re.compile(r"^(?=.*\d)[\w.\-]{1,15}(?:/[\w.\-]{1,15}){1,3}$")


@dataclass(frozen=True)
class Alternative:
    """Eine Lesart der Anfrage: Wörter (alle Pflicht) plus optional eine Pflichtphrase, mit Gewicht."""

    text: str
    phrase: str = ""
    boost: float = 1.0


@dataclass
class ParsedQuery:
    """Zerlegte Anfrage: Lesarten der Hauptklausel, optionale Grundwörter und Namensteile für die Prüfung."""

    original: str
    alternatives: list[Alternative] = field(default_factory=list)
    optional_suffixes: list[str] = field(default_factory=list)
    #: Namensteil eines Kompositums, der nur bei Seltenheit als eigene Lesart gilt (``hafen``)
    name_part: str = ""
    #: Lesart mit dem bloßen Namensteil (nur einsetzen, wenn ``name_part`` selten ist)
    name_part_alternative: Alternative | None = None
    #: Schreibweisen der Straße (Text, als Phrase?) für die Prüfung der Seltenheit
    street_forms: list[tuple[str, bool]] = field(default_factory=list)


def normalize(query: str) -> str:
    """Klein, Leerraum verdichtet, „Str.“ als „Straße“ (auch als Wortende: „Hafenstr.“ → „hafenstraße“)."""
    text = " ".join(query.lower().split())
    text = _STR_ABBREVIATION.sub("straße", text)
    return _STANDALONE_STR.sub("straße", text)


def is_reference(query: str) -> bool:
    """Sieht die Eingabe wie ein Aktenzeichen aus (``V/0624/2016``, ``A-R/0055/2026``, ``1234/2025``)?"""
    return bool(_REFERENCE.match(query.strip()))


def _split_compound(token: str) -> tuple[str, str] | None:
    for suffix in STREET_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_NAME_PART:
            prefix = token[: -len(suffix)]
            if prefix.isalpha():
                return prefix, suffix
    return None


def parse(query: str) -> ParsedQuery:
    """Zerlegt die Anfrage in Lesarten für die Hauptklausel (Straßen, Gleichbedeutungen)."""
    text = normalize(query)
    parsed = ParsedQuery(original=text)
    core: list[str] = []
    compound: tuple[int, str, str] | None = None
    for token in text.split():
        parts = token.split("-")
        if len(parts) > 1 and parts[-1] in STREET_SUFFIXES and any(parts[:-1]):
            # „von-witzleben-straße“ → Kern „von-witzleben“, Grundwort optional
            core.append("-".join(parts[:-1]))
            parsed.optional_suffixes.append(parts[-1])
        elif token in STREET_SUFFIXES and core:
            parsed.optional_suffixes.append(token)
        else:
            split = _split_compound(token) if compound is None else None
            if split:
                compound = (len(core), *split)
            core.append(token)
    if not core:  # nur ein Grundwort („Straße“): wörtlich suchen
        core, parsed.optional_suffixes = text.split(), []

    base = " ".join(core)
    parsed.alternatives.append(Alternative(base))
    if compound is not None:
        position, prefix, suffix = compound
        rest = " ".join(core[:position] + core[position + 1 :])
        parsed.street_forms = [(f"{prefix}{suffix}", False)]
        if suffix in ("straße", "strasse"):
            # „Hafenstr.“, „Von-Witzleben-Straße“ und „Von-Witzleben-Str.“
            abbreviated = [*core[:position], f"{prefix}str", *core[position + 1 :]]
            parsed.alternatives += [
                Alternative(" ".join(abbreviated)),
                Alternative(rest, phrase=f"{prefix} straße"),
                Alternative(rest, phrase=f"{prefix} str"),
            ]
            parsed.street_forms += [(f"{prefix}str", False), (f"{prefix} straße", True), (f"{prefix} str", True)]
        else:
            parsed.alternatives.append(Alternative(rest, phrase=f"{prefix} {suffix}"))
            parsed.street_forms.append((f"{prefix} {suffix}", True))
        parsed.name_part = prefix
        bare = [*core[:position], prefix, *core[position + 1 :]]
        parsed.name_part_alternative = Alternative(" ".join(bare))
    for position, token in enumerate(core):
        for synonym in CURATED_SYNONYMS.get(token, ()):
            replaced = [*core[:position], synonym, *core[position + 1 :]]
            parsed.alternatives.append(Alternative(" ".join(replaced), boost=SYNONYM_BOOST))
    return parsed


#: Felder, über die die Seltenheit des Namensteils gezählt wird (Vorgänge und Dateien, ``german_custom``)
NAME_PART_COUNT_FIELDS: Final = ("name", "paper_name", "text_content", "file_contents_preview")


def name_part_count_queries(parsed: ParsedQuery) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Zählabfragen für den bloßen Namensteil und für die Schreibweisen der Straße (oder ``None``).

    Der Namensteil („witzleben“) zählt als eigene Lesart, wenn er selten ist und überwiegend als Straße
    vorkommt; „hafen“ in Darmstadt (139 Dokumente, nur 9 mit „Hafenstraße“) brächte sonst Rauschen.
    """
    if not parsed.name_part:
        return None

    def lesart(query: str, phrase: bool) -> dict[str, Any]:
        kind = "phrase" if phrase else "best_fields"
        fields = list(NAME_PART_COUNT_FIELDS)
        return {"multi_match": {"query": query, "type": kind, "fields": fields, "analyzer": "german_custom"}}

    forms = [lesart(text, phrase) for text, phrase in parsed.street_forms]
    return lesart(parsed.name_part, False), {"bool": {"should": forms, "minimum_should_match": 1}}


def _fields(index_name: str, kind: str) -> list[str]:
    return list(FIELDS.get(index_name, {}).get(kind, ()))


def _alternative_clause(alternative: Alternative, index_name: str) -> dict[str, Any] | None:
    """Alle Wörter Pflicht (je Wort in irgendeinem Feld), getrennt nach Analyzer; optional Pflichtphrase."""
    must: list[dict[str, Any]] = []
    if alternative.text:
        per_analyzer: list[dict[str, Any]] = []
        german = _fields(index_name, "german")
        standard = _fields(index_name, "standard")
        if german:
            per_analyzer.append(
                {
                    "multi_match": {
                        "query": alternative.text,
                        "type": "cross_fields",
                        "operator": "and",
                        "fields": german,
                        "analyzer": "german_custom",
                    }
                }
            )
        if standard:
            per_analyzer.append(
                {
                    "multi_match": {
                        "query": alternative.text,
                        "type": "cross_fields",
                        "operator": "and",
                        "fields": standard,
                    }
                }
            )
        must.append({"dis_max": {"queries": per_analyzer}})
    if alternative.phrase:
        must.append(
            {
                "multi_match": {
                    "query": alternative.phrase,
                    "type": "phrase",
                    "fields": _fields(index_name, "phrase"),
                    "analyzer": "german_custom",
                }
            }
        )
    if not must:
        return None
    clause: dict[str, Any] = {"bool": {"must": must}}
    if alternative.boost != 1.0:
        clause["bool"]["boost"] = alternative.boost
    return clause


def text_query(query: str, index_name: str, *, name_part_is_rare: bool = False) -> dict[str, Any]:
    """Textteil der Abfrage v2 für einen Index (ohne Filter und ohne Aktualität)."""
    parsed = parse(query)
    alternatives = list(parsed.alternatives)
    if name_part_is_rare and parsed.name_part_alternative is not None:
        alternatives.append(parsed.name_part_alternative)
    main: list[dict[str, Any]] = [
        clause for clause in (_alternative_clause(a, index_name) for a in alternatives) if clause is not None
    ]
    if index_name == "papers" and is_reference(query):
        main.append(
            {
                "term": {
                    "reference.keyword": {
                        "value": query.strip(),
                        "boost": REFERENCE_BOOST,
                        "case_insensitive": True,
                    }
                }
            }
        )
    should: list[dict[str, Any]] = [
        {
            "multi_match": {
                "query": parsed.original,
                "type": "phrase",
                "fields": _fields(index_name, "phrase"),
                "analyzer": "german_custom",
                "boost": PHRASE_BOOST,
            }
        }
    ]
    if parsed.optional_suffixes and _fields(index_name, "german"):
        should.append(
            {
                "multi_match": {
                    "query": " ".join(parsed.optional_suffixes),
                    "fields": _fields(index_name, "german"),
                    "analyzer": "german_custom",
                    "boost": SUFFIX_BOOST,
                }
            }
        )
    return {"bool": {"must": [{"dis_max": {"queries": main}}], "should": should}}


def fuzzy_query(query: str, index_name: str) -> dict[str, Any]:
    """Rückfall bei (fast) keinen genauen Treffern: alle Wörter Pflicht, je Wort kleine Schreibfehler erlaubt."""
    common = {
        "query": normalize(query),
        "type": "best_fields",
        "operator": "and",
        "fuzziness": "AUTO:6,10",
        "prefix_length": 2,
        "max_expansions": 20,
    }
    queries: list[dict[str, Any]] = []
    if _fields(index_name, "german"):
        queries.append(
            {"multi_match": {**common, "fields": _fields(index_name, "german"), "analyzer": "german_custom"}}
        )
    if _fields(index_name, "standard"):
        queries.append({"multi_match": {**common, "fields": _fields(index_name, "standard")}})
    return {"dis_max": {"queries": queries}}


def with_recency(query: dict[str, Any], index_name: str, weight: float) -> dict[str, Any]:
    """Aktualitätsbonus: Faktor zwischen 1 und ``1 + weight``, nur für Dokumente mit Datum.

    ``gauss`` mit ``offset`` 180 Tage und ``scale`` drei Jahre: Was im letzten halben Jahr beraten wurde,
    bekommt den vollen Bonus, nach gut drei Jahren noch den halben. Ohne ``exists``-Filter gälte ein
    fehlendes Datum als „jetzt“ (Darmstadt: 91 % der Dateien ohne Sitzungsdatum).
    """
    date_field = DATE_FIELD_BY_INDEX.get(index_name)
    if not date_field or weight <= 0:
        return query
    return {
        "function_score": {
            "query": query,
            "functions": [
                {"weight": 1},
                {
                    "filter": {"exists": {"field": date_field}},
                    "gauss": {date_field: {"origin": "now", "offset": "180d", "scale": "1095d", "decay": 0.5}},
                    "weight": weight,
                },
            ],
            "score_mode": "sum",
            "boost_mode": "multiply",
        }
    }


# --- Bezug (Work, Issue #853) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationBoost:
    """Gewichtung nach Bezug einer Organisation: Faktor für Vorgänge, Sitzungen und Gremien mit eigenem Bezug.

    ``papers`` und ``meetings`` sind Paare aus Kennungen und Faktor (etwa eigene Anträge 1,5, bearbeitete Vorgänge
    1,3); trifft ein Dokument mehrere, multiplizieren sich die Faktoren. Dateien erben den Faktor ihres Vorgangs bzw.
    ihrer Sitzung. ``committees`` sind Namen von Gremien (wie ``organization_names`` im Index). Der Bezug ändert nur
    die Reihenfolge, nie die Treffermenge: Es sind Faktoren in einem ``function_score``, keine Filter.
    """

    papers: tuple[tuple[frozenset[str], float], ...] = ()
    meetings: tuple[tuple[frozenset[str], float], ...] = ()
    committees: tuple[str, ...] = ()
    committee_factor: float = 1.0

    def __bool__(self) -> bool:
        return any(ids for ids, _f in self.papers + self.meetings) or bool(self.committees)


def _relation_functions(index_name: str, boost: RelationBoost) -> list[dict[str, Any]]:
    """Faktoren je Index: Vorgänge und Sitzungen über ihre Kennung, Dateien über Vorgang bzw. Sitzung."""

    def clause(own_index: str, file_field: str, ids: frozenset[str]) -> dict[str, Any] | None:
        if index_name == own_index:
            return {"ids": {"values": sorted(ids)}}
        if index_name == "files":
            return {"terms": {file_field: sorted(ids)}}
        return None

    functions: list[dict[str, Any]] = []
    for own_index, file_field, groups in (
        ("papers", "paper_id", boost.papers),
        ("meetings", "meeting_id", boost.meetings),
    ):
        for ids, factor in groups:
            match = clause(own_index, file_field, ids) if ids and factor != 1.0 else None
            if match is not None:
                functions.append({"filter": match, "weight": factor})
    if boost.committees and boost.committee_factor != 1.0 and index_name in ("papers", "meetings", "files"):
        phrases = [{"match_phrase": {"organization_names": name}} for name in boost.committees if name]
        if phrases:
            functions.append(
                {"filter": {"bool": {"should": phrases, "minimum_should_match": 1}}, "weight": boost.committee_factor}
            )
    return functions


def has_relation(index_name: str, boost: RelationBoost | None) -> bool:
    """Wirkt der Bezug auf diesen Index? (Personen und Gremien haben keinen.)"""
    return boost is not None and bool(boost) and bool(_relation_functions(index_name, boost))


def with_relation(
    query: dict[str, Any], index_name: str, boost: RelationBoost | None, *, min_score: float = 0.0
) -> dict[str, Any]:
    """Bezug als Faktor: Dokumente mit eigenem Bezug zählen mehr, alle anderen unverändert (Faktor 1).

    Ohne passende Funktion liefert ``function_score`` den Faktor 1; so bleibt die Abfrage für Dokumente ohne Bezug
    gleich und Organisationen ohne eigene Daten bekommen dieselbe Reihenfolge wie das Bürgerportal.

    ``min_score`` ist die Mindestrelevanz (v2) und gilt für die Relevanz **ohne** Bezug: Sie filtert die innere
    Abfrage, erst danach wirken die Faktoren. Eine Schwelle auf die gewichtete Relevanz stiege mit dem besten
    gewichteten Treffer und nähme schwache Treffer ohne Bezug heraus, die das Bürgerportal zeigt.
    """
    if not boost:
        return query
    functions = _relation_functions(index_name, boost)
    if not functions:
        return query
    inner = {"function_score": {"query": query, "min_score": min_score}} if min_score > 0 else query
    return {
        "function_score": {"query": inner, "functions": functions, "score_mode": "multiply", "boost_mode": "multiply"}
    }
