"""
Management Command: Bulk-Reindex aller OParl-Entitäten in Meilisearch.

Usage:
    python manage.py reindex_meilisearch              # Alles
    python manage.py reindex_meilisearch --index files # Nur Files
    python manage.py reindex_meilisearch --body <uuid> # Nur eine Kommune
    python manage.py reindex_meilisearch --clear       # Index leeren + rebuild
"""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from insight_core.models import (
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services.search_documents import (
    file_to_doc,
    meeting_to_doc,
    organization_to_doc,
    paper_to_doc,
    person_to_doc,
)

BATCH_SIZE = 500

INDEX_CONFIG = {
    "papers": {
        "model": OParlPaper,
        "builder": paper_to_doc,
        "queryset_filter": lambda qs: qs.prefetch_related("files"),
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


class Command(BaseCommand):
    help = "Bulk-reindex OParl entities into Meilisearch."

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
            import meilisearch
        except ImportError:
            self.stderr.write(self.style.ERROR(
                "meilisearch package not installed. Run: pip install meilisearch"
            ))
            return

        url = getattr(settings, "MEILISEARCH_URL", "http://localhost:7700")
        key = getattr(settings, "MEILISEARCH_KEY", "")
        client = meilisearch.Client(url, key)

        # Health check
        try:
            health = client.health()
            if health.get("status") != "available":
                self.stderr.write(self.style.ERROR("Meilisearch is not healthy"))
                return
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Cannot reach Meilisearch: {e}"))
            return

        index_filter = options.get("index")
        body_uuid = options.get("body")
        clear = options.get("clear", False)

        indexes_to_process = (
            {index_filter: INDEX_CONFIG[index_filter]}
            if index_filter
            else INDEX_CONFIG
        )

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
                    index = client.index(index_name)
                    task = index.delete_all_documents()
                    client.wait_for_task(task.task_uid, timeout_in_ms=30000)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f"  Could not clear '{index_name}': {e}"
                    ))

            # Build queryset
            qs = model.objects.all()
            if body_uuid:
                qs = qs.filter(body_id=body_uuid)
            if qs_filter:
                qs = qs_filter(qs)

            count = qs.count()
            self.stdout.write(
                f"Indexing {count} {index_name}"
                + (f" for {body_name}" if body_name else "")
                + "..."
            )

            indexed = 0
            index = client.index(index_name)

            # Process in batches
            for offset in range(0, count, BATCH_SIZE):
                batch = list(qs[offset:offset + BATCH_SIZE])
                docs = []
                for obj in batch:
                    try:
                        if index_name == "papers":
                            files = obj.files.filter(
                                text_content__isnull=False,
                                text_extraction_status="completed",
                            )
                            docs.append(builder(obj, files=files))
                        else:
                            docs.append(builder(obj))
                    except Exception as e:
                        self.stderr.write(self.style.WARNING(
                            f"  Error building doc for {obj.id}: {e}"
                        ))

                if docs:
                    try:
                        index.add_documents(docs, primary_key="id")
                        indexed += len(docs)
                    except Exception as e:
                        self.stderr.write(self.style.ERROR(
                            f"  Error indexing batch: {e}"
                        ))

            self.stdout.write(self.style.SUCCESS(
                f"  {index_name}: {indexed}/{count} indexed"
            ))
            total_indexed += indexed

        elapsed = time.monotonic() - start_time
        self.stdout.write(self.style.SUCCESS(
            f"\nDone! {total_indexed} documents indexed in {elapsed:.1f}s"
        ))
