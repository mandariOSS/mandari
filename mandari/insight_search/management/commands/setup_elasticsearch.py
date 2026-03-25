"""
Django Management Command: setup_elasticsearch

Konfiguriert die Elasticsearch-Indizes mit optimalen Einstellungen:
- Deutsche Analyse (Stemming, Stoppwörter)
- Typo-Toleranz (Fuzzy Search)
- Deutsche Kommunal-Synonyme
- Index-Mappings mit optimalen Feldtypen
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Konfiguriert Elasticsearch-Indizes für optimale Suche"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Löscht alle Indizes und erstellt sie neu",
        )
        parser.add_argument(
            "--index",
            type=str,
            help="Nur einen spezifischen Index konfigurieren",
        )
        parser.add_argument(
            "--no-synonyms",
            action="store_true",
            help="Synonyme nicht konfigurieren",
        )

    def handle(self, *args, **options):
        try:
            from elasticsearch import Elasticsearch
        except ImportError:
            raise CommandError("elasticsearch nicht installiert. Bitte 'pip install elasticsearch' ausführen.")

        url = getattr(settings, "ELASTICSEARCH_URL", "http://localhost:9200")
        self.stdout.write(f"Verbinde mit Elasticsearch: {url}")

        try:
            client = Elasticsearch(url)
            if not client.ping():
                raise CommandError("Elasticsearch nicht verfügbar")
            info = client.info()
            self.stdout.write(self.style.SUCCESS(f"Elasticsearch {info['version']['number']} verbunden"))
        except Exception as e:
            raise CommandError(f"Elasticsearch Verbindungsfehler: {e}")

        # Synonyme laden
        synonym_list = []
        if not options.get("no_synonyms"):
            try:
                from insight_search.synonyms import get_elasticsearch_synonyms

                synonym_list = get_elasticsearch_synonyms()
                self.stdout.write(f"  {len(synonym_list)} Synonym-Regeln geladen")
            except ImportError:
                self.stdout.write(self.style.WARNING("  Synonyme-Modul nicht gefunden"))

        # Index-Konfigurationen
        index_configs = self._get_index_configs(synonym_list)

        # Optionaler Filter auf einen Index
        if options.get("index"):
            index_name = options["index"]
            if index_name not in index_configs:
                raise CommandError(f"Unbekannter Index: {index_name}. Verfügbar: {list(index_configs.keys())}")
            index_configs = {index_name: index_configs[index_name]}

        # Indizes verarbeiten
        for index_name, config in index_configs.items():
            self.stdout.write(f"\nKonfiguriere Index: {index_name}")

            if options.get("reset"):
                self._reset_index(client, index_name)

            self._configure_index(client, index_name, config)

        self.stdout.write(self.style.SUCCESS("\nAlle Indizes konfiguriert!"))

    def _get_analysis_settings(self, synonym_list: list[str]) -> dict:
        """Gibt die Analyse-Einstellungen für deutsche Suche zurück."""
        analysis = {
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

    def _get_index_configs(self, synonym_list: list[str]) -> dict:
        """Gibt die Konfigurationen für alle Indizes zurück."""
        analysis = self._get_analysis_settings(synonym_list)

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
                        "reference": {"type": "text", "analyzer": "standard", "fields": {"keyword": {"type": "keyword"}}},
                        "paper_type": {"type": "keyword"},
                        "date": {"type": "date", "format": "strict_date_optional_time||yyyy-MM-dd", "ignore_malformed": True},
                        "oparl_created": {"type": "date", "ignore_malformed": True},
                        "oparl_modified": {"type": "date", "ignore_malformed": True},
                        "file_contents_preview": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                        "file_names": {"type": "text"},
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
                        "organization_names": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
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
                        "name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search", "fields": {"keyword": {"type": "keyword"}}},
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
                        "text_content": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                        "text_preview": {"type": "text", "index": False},
                        "paper_id": {"type": "keyword"},
                        "paper_name": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                        "paper_reference": {"type": "text", "analyzer": "standard"},
                        "meeting_id": {"type": "keyword"},
                        "organization_names": {"type": "text", "analyzer": "german_custom", "search_analyzer": "german_search"},
                        "meeting_name": {"type": "text"},
                        "meeting_date": {"type": "date", "ignore_malformed": True},
                        "agenda_number": {"type": "keyword"},
                        "oparl_modified": {"type": "date", "ignore_malformed": True},
                    }
                },
            },
        }

    def _reset_index(self, client, index_name: str):
        """Löscht und erstellt einen Index neu."""
        self.stdout.write(f"  Lösche Index: {index_name}")
        try:
            client.indices.delete(index=index_name, ignore=[404])
        except Exception:
            pass

    def _configure_index(self, client, index_name: str, config: dict):
        """Erstellt oder aktualisiert einen Index."""
        try:
            if client.indices.exists(index=index_name):
                # Index existiert — dynamische Settings aktualisieren
                # Statische Settings (number_of_shards) können nicht geändert werden
                dynamic_settings = {
                    k: v for k, v in config["settings"].items()
                    if k not in ("number_of_shards",)
                }
                if "analysis" in dynamic_settings:
                    # Analysis-Settings erfordern close/open
                    self.stdout.write(f"  Index existiert, schließe für Settings-Update...")
                    client.indices.close(index=index_name)
                    try:
                        client.indices.put_settings(
                            index=index_name,
                            body=dynamic_settings,
                        )
                        self.stdout.write("  Settings aktualisiert")
                    finally:
                        client.indices.open(index=index_name)
                else:
                    self.stdout.write(f"  Index existiert, aktualisiere Settings...")
                    client.indices.put_settings(
                        index=index_name,
                        body=dynamic_settings,
                    )
                    self.stdout.write("  Settings aktualisiert")

                # Mappings können nur erweitert, nicht geändert werden
                client.indices.put_mapping(
                    index=index_name,
                    body=config["mappings"],
                )
                self.stdout.write("  Mappings aktualisiert")
            else:
                # Neuen Index erstellen
                self.stdout.write(f"  Erstelle Index: {index_name}")
                client.indices.create(
                    index=index_name,
                    body={
                        "settings": config["settings"],
                        "mappings": config["mappings"],
                    },
                )

            self.stdout.write(self.style.SUCCESS(f"  {index_name} konfiguriert"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Fehler bei {index_name}: {e}"))
