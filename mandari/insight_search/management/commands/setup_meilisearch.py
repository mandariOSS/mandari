"""
Django Management Command: setup_meilisearch

Konfiguriert die Meilisearch-Indizes mit optimalen Einstellungen:
- Typo-Toleranz (Fuzzy Search)
- Deutsche Kommunal-Synonyme
- Searchable/Filterable/Sortable Attributes
- Highlighting-Einstellungen
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Konfiguriert Meilisearch-Indizes für optimale Suche"

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
            import meilisearch
        except ImportError:
            raise CommandError("meilisearch nicht installiert. Bitte 'pip install meilisearch' ausführen.")

        url = getattr(settings, "MEILISEARCH_URL", "http://localhost:7700")
        key = getattr(settings, "MEILISEARCH_KEY", "")

        self.stdout.write(f"Verbinde mit Meilisearch: {url}")

        try:
            client = meilisearch.Client(url, key)
            health = client.health()
            if health.get("status") != "available":
                raise CommandError("Meilisearch nicht verfügbar")
        except Exception as e:
            raise CommandError(f"Meilisearch Verbindungsfehler: {e}")

        self.stdout.write(self.style.SUCCESS("Meilisearch verbunden"))

        # Enable vector store experimental feature only when semantic search is active
        semantic_ratio = getattr(settings, "MEILISEARCH_SEMANTIC_RATIO", 0.0)
        if semantic_ratio > 0:
            self.stdout.write("Aktiviere Vector Store Feature...")
            try:
                import httpx
                resp = httpx.patch(
                    f"{url}/experimental-features",
                    json={"vectorStore": True},
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    self.stdout.write(self.style.SUCCESS("  Vector Store aktiviert"))
                else:
                    self.stdout.write(self.style.WARNING(f"  Vector Store Aktivierung fehlgeschlagen: {resp.text[:200]}"))
            except ImportError:
                import json
                import urllib.request
                req = urllib.request.Request(
                    f"{url}/experimental-features",
                    data=json.dumps({"vectorStore": True}).encode(),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    method="PATCH",
                )
                try:
                    urllib.request.urlopen(req, timeout=10)
                    self.stdout.write(self.style.SUCCESS("  Vector Store aktiviert"))
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  Vector Store Aktivierung fehlgeschlagen: {e}"))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Vector Store Aktivierung fehlgeschlagen: {e}"))
        else:
            self.stdout.write("Semantic Search deaktiviert (MEILISEARCH_SEMANTIC_RATIO=0.0), Embedder werden übersprungen")

        # Index-Konfigurationen
        index_configs = self._get_index_configs()

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

            self._configure_index(client, index_name, config, options)

        self.stdout.write(self.style.SUCCESS("\nAlle Indizes konfiguriert!"))

    def _get_index_configs(self):
        """Gibt die Konfigurationen für alle Indizes zurück."""
        return {
            "papers": {
                "searchableAttributes": [
                    "name",
                    "reference",
                    "paper_type",
                    "file_contents_preview",
                    "file_names",
                ],
                "filterableAttributes": [
                    "body_id",
                    "paper_type",
                    "date",
                ],
                "sortableAttributes": [
                    "date",
                    "oparl_created",
                    "oparl_modified",
                ],
                "displayedAttributes": [
                    "id",
                    "name",
                    "reference",
                    "paper_type",
                    "date",
                    "body_id",
                    "file_contents_preview",
                    "file_names",
                ],
            },
            "meetings": {
                "searchableAttributes": [
                    "name",
                    "organization_names",
                    "location_name",
                ],
                "filterableAttributes": [
                    "body_id",
                    "cancelled",
                    "start",
                ],
                "sortableAttributes": [
                    "start",
                    "end",
                    "oparl_modified",
                ],
                "displayedAttributes": [
                    "id",
                    "name",
                    "organization_names",
                    "location_name",
                    "start",
                    "end",
                    "cancelled",
                    "body_id",
                ],
            },
            "persons": {
                "searchableAttributes": [
                    "name",
                    "given_name",
                    "family_name",
                    "title",
                ],
                "filterableAttributes": [
                    "body_id",
                ],
                "sortableAttributes": [
                    "family_name",
                    "given_name",
                    "oparl_modified",
                ],
                "displayedAttributes": [
                    "id",
                    "name",
                    "given_name",
                    "family_name",
                    "title",
                    "body_id",
                ],
            },
            "organizations": {
                "searchableAttributes": [
                    "name",
                    "short_name",
                    "organization_type",
                    "classification",
                ],
                "filterableAttributes": [
                    "body_id",
                    "organization_type",
                ],
                "sortableAttributes": [
                    "name",
                    "oparl_modified",
                ],
                "displayedAttributes": [
                    "id",
                    "name",
                    "short_name",
                    "organization_type",
                    "classification",
                    "body_id",
                ],
            },
            "files": {
                "searchableAttributes": [
                    "name",
                    "file_name",
                    "text_content",
                    "paper_name",
                    "paper_reference",
                ],
                "filterableAttributes": [
                    "body_id",
                    "paper_id",
                    "meeting_id",
                    "mime_type",
                ],
                "sortableAttributes": [
                    "oparl_modified",
                ],
                "displayedAttributes": [
                    "id",
                    "name",
                    "file_name",
                    "mime_type",
                    "text_preview",
                    "paper_id",
                    "paper_name",
                    "paper_reference",
                    "meeting_id",
                    "body_id",
                ],
            },
        }

    def _get_embedder_configs(self):
        """Returns embedder configurations per index for hybrid search."""
        model = getattr(settings, "MEILISEARCH_EMBEDDING_MODEL", "BAAI/bge-m3")
        return {
            "papers": {
                "default": {
                    "source": "huggingFace",
                    "model": model,
                    "documentTemplate": "{{ doc.name }} {{ doc.reference }} {{ doc.paper_type }} {{ doc.file_contents_preview }}",
                    "documentTemplateMaxBytes": 2048,
                }
            },
            "files": {
                "default": {
                    "source": "huggingFace",
                    "model": model,
                    "documentTemplate": "{{ doc.name }} {{ doc.file_name }} {{ doc.text_content }}",
                    "documentTemplateMaxBytes": 4096,
                }
            },
            "meetings": {
                "default": {
                    "source": "huggingFace",
                    "model": model,
                    "documentTemplate": "{{ doc.name }} {{ doc.organization_names }} {{ doc.location_name }}",
                    "documentTemplateMaxBytes": 400,
                }
            },
            "persons": {
                "default": {
                    "source": "huggingFace",
                    "model": model,
                    "documentTemplate": "{{ doc.name }} {{ doc.given_name }} {{ doc.family_name }}",
                    "documentTemplateMaxBytes": 400,
                }
            },
            "organizations": {
                "default": {
                    "source": "huggingFace",
                    "model": model,
                    "documentTemplate": "{{ doc.name }} {{ doc.short_name }} {{ doc.organization_type }} {{ doc.classification }}",
                    "documentTemplateMaxBytes": 400,
                }
            },
        }

    def _reset_index(self, client, index_name):
        """Löscht und erstellt einen Index neu."""
        self.stdout.write(f"  Lösche Index: {index_name}")
        try:
            client.index(index_name).delete()
        except Exception:
            pass  # Index existiert vielleicht nicht

        self.stdout.write(f"  Erstelle Index: {index_name}")
        client.create_index(index_name, {"primaryKey": "id"})

    def _configure_index(self, client, index_name, config, options):
        """Konfiguriert einen Index mit den angegebenen Einstellungen."""
        index = client.index(index_name)

        # Ensure index exists
        try:
            index.get_stats()
        except Exception:
            self.stdout.write(f"  Erstelle Index: {index_name}")
            client.create_index(index_name, {"primaryKey": "id"})
            index = client.index(index_name)

        # Searchable Attributes
        if "searchableAttributes" in config:
            self.stdout.write("  Setze searchable attributes...")
            index.update_searchable_attributes(config["searchableAttributes"])

        # Filterable Attributes
        if "filterableAttributes" in config:
            self.stdout.write("  Setze filterable attributes...")
            index.update_filterable_attributes(config["filterableAttributes"])

        # Sortable Attributes
        if "sortableAttributes" in config:
            self.stdout.write("  Setze sortable attributes...")
            index.update_sortable_attributes(config["sortableAttributes"])

        # Displayed Attributes
        if "displayedAttributes" in config:
            self.stdout.write("  Setze displayed attributes...")
            index.update_displayed_attributes(config["displayedAttributes"])

        # Typo Tolerance (Fuzzy Search)
        self.stdout.write("  Konfiguriere Typo-Toleranz...")
        index.update_typo_tolerance(
            {
                "enabled": True,
                "minWordSizeForTypos": {
                    "oneTypo": 4,  # Mindestens 4 Buchstaben für 1 Typo
                    "twoTypos": 8,  # Mindestens 8 Buchstaben für 2 Typos
                },
                "disableOnWords": [],
                "disableOnAttributes": [],
            }
        )

        # Synonyme
        if not options.get("no_synonyms"):
            self.stdout.write("  Konfiguriere Synonyme...")
            try:
                from insight_search.synonyms import get_meilisearch_synonyms

                synonyms = get_meilisearch_synonyms()
                index.update_synonyms(synonyms)
                self.stdout.write(f"    {len(synonyms)} Synonym-Gruppen gesetzt")
            except ImportError:
                self.stdout.write(self.style.WARNING("    Synonyme-Modul nicht gefunden"))

        # Ranking Rules (Standard + body_id Boost)
        self.stdout.write("  Konfiguriere Ranking...")
        index.update_ranking_rules(
            [
                "words",
                "typo",
                "proximity",
                "attribute",
                "sort",
                "exactness",
            ]
        )

        # Pagination (großzügige Limits)
        self.stdout.write("  Konfiguriere Pagination...")
        try:
            index.update_pagination({"maxTotalHits": 100000})
        except AttributeError:
            # Newer meilisearch-python versions use update_settings
            index.update_settings({"pagination": {"maxTotalHits": 100000}})

        # Embedder für Hybrid Search (Meilisearch v1.10+) — nur wenn aktiviert
        semantic_ratio = getattr(settings, "MEILISEARCH_SEMANTIC_RATIO", 0.0)
        if semantic_ratio > 0:
            embedder_configs = self._get_embedder_configs()
            if index_name in embedder_configs:
                self.stdout.write("  Konfiguriere Embedder für Hybrid Search...")
                try:
                    index.update_embedders(embedder_configs[index_name])
                    self.stdout.write(f"    Embedder 'default' gesetzt")
                except AttributeError:
                    try:
                        index.update_settings({"embedders": embedder_configs[index_name]})
                        self.stdout.write(f"    Embedder 'default' gesetzt (via update_settings)")
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"    Embedder-Konfiguration fehlgeschlagen: {e}"))
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"    Embedder-Konfiguration fehlgeschlagen: {e}"))

        self.stdout.write(self.style.SUCCESS(f"  {index_name} konfiguriert"))
