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

Mit ``SEARCH_INDEX_SUBSCRIPTION=aktiv`` (Issue #527) schreibt der Befehl nicht selbst: Er baut den Live-Index über
das Abonnement (``abonnement.vollbau``) mit der höchsten Folgenummer vor dem Lesen als externer Version. Danach
eintreffende Ereignisse gewinnen, ältere verlieren gegen den Neuaufbau. ``--clear`` gibt es dann nicht; einen
leeren Index legt ``setup_elasticsearch --reset`` an.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events.dispatch import head_seq
from hub.ris.selectors import public_body
from insight_core.services.search_projection import count_documents, iter_all_documents
from insight_search import abonnement
from insight_search.abonnement import BULK_BYTES
from insight_search.indices import INDEXES

BATCH_SIZE = 500


def bulk_error_summary(item: dict[str, Any]) -> str:
    """
    Fehler eines Dokuments aus ``streaming_bulk`` ohne Inhalt: Index, Kennung, Status und Fehlerart.

    Nie das Dokument selbst (``data``) und nie die Meldung von Elasticsearch: Beide können Ausschnitte des
    Textes enthalten.
    """
    aktion, info = next(iter(item.items()), ("?", {}))
    fehler = info.get("error")
    if isinstance(fehler, dict):
        art = str(fehler.get("type") or "unbekannt")
    elif info.get("exception") is not None:
        art = type(info["exception"]).__name__
    else:
        art = "unbekannt"
    return f"{aktion} {info.get('_index')}/{info.get('_id')}: Status {info.get('status')}, {art}"


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
        if settings.SEARCH_INDEX_SUBSCRIPTION == "aktiv":
            self._ueber_das_abonnement(options)
            return
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
            body = public_body(body_uuid)
            if body is None:
                self.stderr.write(self.style.ERROR(f"Body not found: {body_uuid}"))
                return
            body_name = body.name or body.short_name or str(body.id)
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
            # Abgelehnte Dokumente und fehlgeschlagene Anfragen (HTTP-Fehler) zählen als Fehler, der Neuaufbau
            # läuft weiter; nur ein nicht erreichbares Elasticsearch beendet diesen Index.
            try:
                for ok, item in streaming_bulk(
                    client,
                    actions,
                    chunk_size=BATCH_SIZE,
                    max_chunk_bytes=BULK_BYTES,
                    raise_on_error=False,
                    raise_on_exception=False,
                ):
                    if ok:
                        indexed += 1
                        continue
                    errors += 1
                    if errors <= 3:
                        self.stderr.write(self.style.WARNING(f"  Bulk error: {bulk_error_summary(item)}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Error indexing '{index_name}': {type(e).__name__}"))

            fehlerhaft = f", {errors} failed" if errors else ""
            self.stdout.write(self.style.SUCCESS(f"  {index_name}: {indexed}/{count} indexed{fehlerhaft}"))
            total_indexed += indexed

        elapsed = time.monotonic() - start_time
        self.stdout.write(self.style.SUCCESS(f"\nDone! {total_indexed} documents indexed in {elapsed:.1f}s"))

    def _ueber_das_abonnement(self, options: dict[str, Any]) -> None:
        """Neuaufbau mit ``SEARCH_INDEX_SUBSCRIPTION=aktiv``: über das Abonnement, Version = Folgenummer."""
        if options.get("clear"):
            raise CommandError(
                "--clear gibt es mit SEARCH_INDEX_SUBSCRIPTION=aktiv nicht: Der Neuaufbau überschreibt jedes Dokument, "
                "das Abonnement löscht, was aus dem Bestand fällt. Für einen leeren Index erst "
                "setup_elasticsearch --reset, dann reindex_elasticsearch."
            )
        kommunen: list[uuid.UUID] = []
        if options.get("body"):
            body = public_body(options["body"])
            if body is None:
                raise CommandError(f"Body not found: {options['body']}")
            kommunen = [body.pk]
        indizes = [options["index"]] if options.get("index") else list(INDEXES)
        es = abonnement.client()
        start_time = time.monotonic()
        # Vor dem Lesen: Was danach geschieht, hat eine höhere Folgenummer und gewinnt
        version = head_seq()
        self.stdout.write(
            f"SEARCH_INDEX_SUBSCRIPTION=aktiv: Neuaufbau über das Abonnement {abonnement.NAME}, "
            f"Version (Folgenummer) {version}"
        )
        gesamt = 0
        fehlerhaft = False
        for index in indizes:
            self.stdout.write(f"Indexing {count_documents(index, kommunen)} {index}...")
            tally = abonnement.vollbau(es, index, kommunen, version, shadow=False)
            werte = ", ".join(f"{name} {anzahl}" for name, anzahl in tally.as_dict().items() if anzahl) or "nichts"
            self.stdout.write(f"  {index}: {werte}")
            gesamt += tally.indexed
            fehlerhaft = fehlerhaft or bool(tally.rejected or tally.unavailable)
        if fehlerhaft:
            raise CommandError("Nicht alle Dokumente geschrieben (abgelehnt bzw. nicht verfügbar); Protokoll prüfen.")
        elapsed = time.monotonic() - start_time
        self.stdout.write(self.style.SUCCESS(f"\nDone! {gesamt} documents indexed in {elapsed:.1f}s"))
