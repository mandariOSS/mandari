# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung (Einstellungen und Felder) der Suchindizes und Namen der Schattenindizes.

Eine Quelle für ``manage.py setup_elasticsearch`` (Live-Index) und den Schattenindex des
Abonnements ``suchindex`` (``insight_search.abonnement``): Beide bekommen dieselbe Analyse und
dieselben Felder, damit der Vergleich (``manage.py suchindex_schatten vergleichen``) nur Inhalte
vergleicht.

Schattenindizes heißen ``schatten-<index>`` (``schatten-papers`` …). Kein Präfixmuster der Live-
Indizes (``papers*``) trifft sie, und die Suche fragt nur feste Namen ab.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Die Suchindizes in fester Reihenfolge
INDEXES: Final = ("papers", "meetings", "persons", "organizations", "files")
#: Präfix der Schattenindizes
SHADOW_PREFIX: Final = "schatten-"


def shadow_name(index: str) -> str:
    """Name des Schattenindex zu einem Live-Index (``papers`` → ``schatten-papers``)."""
    if index not in INDEXES:
        raise ValueError(f"Unbekannter Suchindex: {index}")
    return f"{SHADOW_PREFIX}{index}"


def analysis_settings(synonym_list: list[str]) -> dict[str, Any]:
    """Analyse für deutsche Suche: Stammformen, Stoppwörter, Synonyme nur beim Suchen."""
    analysis: dict[str, Any] = {
        "analyzer": {
            "german_custom": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": [
                    "lowercase",
                    "german_normalization",
                    "german_stop",
                    "german_stemmer",
                ],
            },
            "german_search": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": [
                    "lowercase",
                    "german_normalization",
                    "german_stop",
                    "german_stemmer",
                ],
            },
        },
        "filter": {
            "german_stop": {
                "type": "stop",
                "stopwords": "_german_",
            },
            "german_stemmer": {
                "type": "stemmer",
                "language": "light_german",
            },
        },
    }

    if synonym_list:
        analysis["filter"]["german_synonyms"] = {
            "type": "synonym",
            "synonyms": synonym_list,
            "lenient": True,
        }
        # Synonyme in den Search-Analyzer einbauen (nicht im Index-Analyzer!)
        analysis["analyzer"]["german_search"]["filter"].insert(1, "german_synonyms")

    return analysis


def index_configs(synonym_list: list[str]) -> dict[str, dict[str, Any]]:
    """Einstellungen und Felder je Suchindex."""
    analysis = analysis_settings(synonym_list)

    common_settings = {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": analysis,
    }

    return {
        "papers": {
            "settings": common_settings,
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "body_id": {"type": "keyword"},
                    "name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                    "reference": {
                        "type": "text",
                        "analyzer": "standard",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "paper_type": {"type": "keyword"},
                    "date": {
                        "type": "date",
                        "format": "strict_date_optional_time||yyyy-MM-dd",
                        "ignore_malformed": True,
                    },
                    "oparl_created": {"type": "date", "ignore_malformed": True},
                    "oparl_modified": {"type": "date", "ignore_malformed": True},
                    "file_contents_preview": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                    },
                    "file_names": {"type": "text"},
                    # Gremien der Beratungen — für Ausschuss-Filter
                    "organization_names": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                    },
                }
            },
        },
        "meetings": {
            "settings": common_settings,
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "body_id": {"type": "keyword"},
                    "name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                    "organization_names": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                    },
                    "location_name": {"type": "text"},
                    "start": {"type": "date", "ignore_malformed": True},
                    "end": {"type": "date", "ignore_malformed": True},
                    "cancelled": {"type": "boolean"},
                    "oparl_modified": {"type": "date", "ignore_malformed": True},
                }
            },
        },
        "persons": {
            "settings": common_settings,
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "body_id": {"type": "keyword"},
                    "name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                    "given_name": {"type": "text"},
                    "family_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "title": {"type": "text"},
                    "oparl_modified": {"type": "date", "ignore_malformed": True},
                }
            },
        },
        "organizations": {
            "settings": common_settings,
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "body_id": {"type": "keyword"},
                    "name": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "short_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "organization_type": {"type": "keyword"},
                    "classification": {"type": "keyword"},
                    "oparl_modified": {"type": "date", "ignore_malformed": True},
                }
            },
        },
        "files": {
            "settings": common_settings,
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "body_id": {"type": "keyword"},
                    "name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                    "file_name": {"type": "text"},
                    "mime_type": {"type": "keyword"},
                    "access_url": {"type": "keyword", "index": False},
                    "text_content": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                    },
                    "text_preview": {"type": "text", "index": False},
                    "paper_id": {"type": "keyword"},
                    "paper_name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                    "paper_reference": {"type": "text", "analyzer": "standard"},
                    "meeting_id": {"type": "keyword"},
                    "organization_names": {
                        "type": "text",
                        "analyzer": "german_custom",
                        "search_analyzer": "german_search",
                    },
                    "meeting_name": {"type": "text"},
                    "meeting_date": {"type": "date", "ignore_malformed": True},
                    "agenda_number": {"type": "keyword"},
                    "oparl_modified": {"type": "date", "ignore_malformed": True},
                }
            },
        },
    }


def synonyms() -> list[str]:
    """Synonymregeln für den Such-Analyzer."""
    from insight_search.synonyms import get_elasticsearch_synonyms

    return get_elasticsearch_synonyms()


def ensure_shadow_indices(client: Any, indexes: tuple[str, ...] = INDEXES) -> list[str]:
    """Legt fehlende Schattenindizes mit der Abbildung der Live-Indizes an; gibt die angelegten zurück.

    Ohne diesen Schritt legte Elasticsearch einen Index beim ersten Schreiben selbst an, mit erratenen
    Feldtypen. Ein schon vorhandener Index (auch von einem parallelen Prozess angelegt) bleibt unverändert.
    """
    configs = index_configs(synonyms())
    angelegt: list[str] = []
    for index in indexes:
        name = shadow_name(index)
        if client.indices.exists(index=name):
            continue
        config = copy.deepcopy(configs[index])
        try:
            client.indices.create(index=name, settings=config["settings"], mappings=config["mappings"])
        except Exception as exc:
            if _already_exists(exc):
                continue
            raise
        logger.info("Schattenindex %s angelegt", name)
        angelegt.append(name)
    return angelegt


def _already_exists(exc: Exception) -> bool:
    fehler = getattr(exc, "error", "")
    return getattr(exc, "status_code", None) == 400 and "resource_already_exists" in str(fehler)
