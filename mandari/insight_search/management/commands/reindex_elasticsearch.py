# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Bulk-Reindex aller OParl-Entitäten in Elasticsearch.

Usage:
    python manage.py reindex_elasticsearch              # Alles
    python manage.py reindex_elasticsearch --index files # Nur Files
    python manage.py reindex_elasticsearch --body <uuid> # Nur eine Kommune
    python manage.py reindex_elasticsearch --clear       # Index leeren + rebuild

Die Dokumente kommen aus derselben Projektion wie beim Abonnement ``suchindex`` und dem Vollbau des
Schattenindex (``insight_core.services.search_projection``): Dateitexte in kleinen Abschnitten und schon in
der Datenbank gekürzt, von den Dateien eines Vorgangs nur der Anfang. Die Bulk-Anfragen sind nach Anzahl
(``BATCH_SIZE``) und Bytes (``abonnement.BULK_BYTES``) begrenzt, sodass der Speicherbedarf nicht von der
Größe einzelner Texte abhängt.
"""

from __future__ import annotations

import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand

from insight_core.models import OParlBody
from insight_core.services.search_projection import count_documents, iter_all_documents
from insight_search.abonnement import BULK_BYTES
from insight_search.indices import INDEXES

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Bulk-reindex OParl entities into Elasticsearch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--index",
            type=str,
            choices=list(INDEXES),
            help="Only reindex a specific index (papers, meetings, etc.)",
        )
        parser.add_argument(
            "--body",
            type=str,
            help="Only reindex entities for a specific body UUID",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear indexes before reindexing",
        )

    def handle(self, *args, **options):
        try:
            from elasticsearch import Elasticsearch
            from elasticsearch.helpers import streaming_bulk
        except ImportError:
            self.stderr.write(self.style.ERROR("elasticsearch package not installed. Run: pip install elasticsearch"))
            return

        url = getattr(settings, "ELASTICSEARCH_URL", "http://localhost:9200")
        client = Elasticsearch(url)

        # Health check
        try:
            if not client.ping():
                self.stderr.write(self.style.ERROR("Elasticsearch is not healthy"))
                return
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Cannot reach Elasticsearch: {e}"))
            return

        index_filter = options.get("index")
        body_uuid = options.get("body")
        clear = options.get("clear", False)

        indexes_to_process = [index_filter] if index_filter else list(INDEXES)

        body_name = ""
        kommunen: list[uuid.UUID] = []
        if body_uuid:
            try:
                body = OParlBody.objects.get(id=body_uuid)
                body_name = body.name or body.short_name or str(body.id)
            except OParlBody.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Body not found: {body_uuid}"))
                return
            kommunen = [body.pk]

        start_time = time.monotonic()
        total_indexed = 0

        for index_name in indexes_to_process:
            # Clear index if requested
            if clear:
                self.stdout.write(f"  Clearing index '{index_name}'...")
                try:
                    if client.indices.exists(index=index_name):
                        client.delete_by_query(
                            index=index_name,
                            body={"query": {"match_all": {}}},
                            refresh=True,
                        )
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  Could not clear '{index_name}': {e}"))

            # Bestand wie im Abonnement: Tombstones und gesperrte Dateien (Löschabgleich, #787) bleiben draußen
            count = count_documents(index_name, kommunen)
            self.stdout.write(f"Indexing {count} {index_name}" + (f" for {body_name}" if body_name else "") + "...")

            indexed = 0
            errors = 0
            actions = (
                {"_index": index_name, "_id": doc["id"], "_source": doc}
                for doc in iter_all_documents(index_name, kommunen)
            )
            try:
                for ok, item in streaming_bulk(
                    client, actions, chunk_size=BATCH_SIZE, max_chunk_bytes=BULK_BYTES, raise_on_error=False
                ):
                    if ok:
                        indexed += 1
                        continue
                    errors += 1
                    if errors <= 3:
                        self.stderr.write(self.style.WARNING(f"  Bulk error: {item}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Error indexing '{index_name}': {e}"))

            self.stdout.write(self.style.SUCCESS(f"  {index_name}: {indexed}/{count} indexed"))
            total_indexed += indexed

        elapsed = time.monotonic() - start_time
        self.stdout.write(self.style.SUCCESS(f"\nDone! {total_indexed} documents indexed in {elapsed:.1f}s"))
