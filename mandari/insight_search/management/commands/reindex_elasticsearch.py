# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Bulk-Reindex aller OParl-Entitäten in Elasticsearch.

Usage:
    python manage.py reindex_elasticsearch              # Alles
    python manage.py reindex_elasticsearch --index files # Nur Files
    python manage.py reindex_elasticsearch --body <uuid> # Nur eine Kommune
    python manage.py reindex_elasticsearch --clear       # Index leeren + rebuild

Dateitexte kommen in kleinen Abschnitten und schon in der Datenbank gekürzt (``INDEX_TEXT_MAX_CHARS``), von den
Dateien eines Vorgangs nur der Anfang (``paper_to_doc``). Die Bulk-Anfragen sind nach Anzahl (``BATCH_SIZE``) und
Bytes (``BULK_MAX_BYTES``) begrenzt, sodass der Speicherbedarf nicht von der Größe einzelner Texte abhängt.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import QuerySet
from django.db.models.functions import Substr

from insight_core.models import (
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services.search_documents import (
    INDEX_TEXT_MAX_CHARS,
    file_to_doc,
    meeting_to_doc,
    organization_to_doc,
    paper_to_doc,
    person_to_doc,
)

BATCH_SIZE = 500
#: Dateien samt Text lädt der Neuaufbau in Abschnitten von so vielen Dateien
FILE_CHUNK_SIZE = 10
#: Höchstens so viele Bytes je Bulk-Anfrage (wie im Ingestor, ``BULK_MAX_BYTES``); ein Dokument, das nicht mehr
#: passt, beginnt eine neue Anfrage
BULK_MAX_BYTES = 8 * 1024 * 1024

INDEX_CONFIG = {
    "papers": {
        "model": OParlPaper,
        # paper_to_doc lädt die Dateien je Vorgang selbst, vom Text nur den Anfang für die Vorschau
        "builder": paper_to_doc,
        "queryset_filter": None,
    },
    "meetings": {
        "model": OParlMeeting,
        "builder": meeting_to_doc,
        "queryset_filter": None,
    },
    "persons": {
        "model": OParlPerson,
        "builder": person_to_doc,
        "queryset_filter": None,
    },
    "organizations": {
        "model": OParlOrganization,
        "builder": organization_to_doc,
        "queryset_filter": None,
    },
    "files": {
        "model": OParlFile,
        "builder": file_to_doc,
        "queryset_filter": lambda qs: qs.filter(
            text_content__isnull=False,
            text_extraction_status="completed",
        ).select_related("paper"),
    },
}


def _files_with_index_text(qs: QuerySet[Any], ids: Sequence[Any]) -> Iterator[Any]:
    """Dateien zu ``ids`` in Abschnitten von ``FILE_CHUNK_SIZE``, ihr Text schon in der Datenbank gekürzt."""
    for start in range(0, len(ids), FILE_CHUNK_SIZE):
        chunk = (
            qs.filter(pk__in=ids[start : start + FILE_CHUNK_SIZE])
            .defer("text_content")
            .annotate(suchindex_text=Substr("text_content", 1, INDEX_TEXT_MAX_CHARS))
        )
        for obj in chunk:
            # Den gekürzten Text als text_content setzen (sonst lüde Django den vollen Text nach)
            obj.text_content = obj.suchindex_text
            yield obj


class Command(BaseCommand):
    help = "Bulk-reindex OParl entities into Elasticsearch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--index",
            type=str,
            choices=list(INDEX_CONFIG.keys()),
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
            from elasticsearch.helpers import bulk
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

        indexes_to_process = {index_filter: INDEX_CONFIG[index_filter]} if index_filter else INDEX_CONFIG

        body_name = ""
        if body_uuid:
            try:
                body = OParlBody.objects.get(id=body_uuid)
                body_name = body.name or body.short_name or str(body.id)
            except OParlBody.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Body not found: {body_uuid}"))
                return

        start_time = time.monotonic()
        total_indexed = 0

        for index_name, config in indexes_to_process.items():
            model = config["model"]
            builder = config["builder"]
            qs_filter = config["queryset_filter"]

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

            # Build queryset (Tombstones bleiben draußen — von der Quelle
            # gelöschte Objekte gehören nicht in den Suchindex)
            qs = model.objects.filter(deleted=False)
            if body_uuid:
                qs = qs.filter(body_id=body_uuid)
            if qs_filter:
                qs = qs_filter(qs)

            count = qs.count()
            self.stdout.write(f"Indexing {count} {index_name}" + (f" for {body_name}" if body_name else "") + "...")

            indexed = 0

            # Process in batches; Dateien mit Text in kleinen Abschnitten, Anfragen nach Anzahl und Bytes begrenzt
            for offset in range(0, count, BATCH_SIZE):
                if index_name == "files":
                    ids = list(qs.order_by("pk").values_list("pk", flat=True)[offset : offset + BATCH_SIZE])
                    batch: Any = _files_with_index_text(qs, ids)
                else:
                    batch = qs[offset : offset + BATCH_SIZE]
                try:
                    success, errors = bulk(
                        client,
                        self._actions(index_name, builder, batch),
                        chunk_size=BATCH_SIZE,
                        max_chunk_bytes=BULK_MAX_BYTES,
                        raise_on_error=False,
                    )
                    indexed += success
                    if errors:
                        for err in errors[:3]:
                            self.stderr.write(self.style.WARNING(f"  Bulk error: {err}"))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  Error indexing batch: {e}"))

            self.stdout.write(self.style.SUCCESS(f"  {index_name}: {indexed}/{count} indexed"))
            total_indexed += indexed

        elapsed = time.monotonic() - start_time
        self.stdout.write(self.style.SUCCESS(f"\nDone! {total_indexed} documents indexed in {elapsed:.1f}s"))

    def _actions(self, index_name: str, builder: Any, objs: Any) -> Iterator[dict[str, Any]]:
        """Bulk-Aktionen zu ``objs``, eine nach der anderen (die Bulk-Hilfe liest sie abschnittsweise)."""
        for obj in objs:
            try:
                doc = builder(obj)
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"  Error building doc for {obj.id}: {e}"))
                continue
            yield {"_index": index_name, "_id": doc["id"], "_source": doc}
