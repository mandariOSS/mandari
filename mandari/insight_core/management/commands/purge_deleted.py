# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Physische Löschung von Tombstone-Objekten.

Objekte, die im Quellsystem gelöscht wurden, werden beim Sync nur MARKIERT
(``deleted=True``) — nie automatisch entfernt. Dieses Command löscht markierte
Objekte endgültig, ausschließlich auf explizite Aufforderung einer Kommune.

Usage:
    # Dry-Run (Standard): zeigt nur, was gelöscht würde
    python manage.py purge_deleted --body muenster
    python manage.py purge_deleted --body muenster --older-than 30
    python manage.py purge_deleted --ids <uuid> <uuid> ...

    # Tatsächlich löschen (inkl. Elasticsearch-Dokumente über die
    # post_delete-Signale und lokale Dateikopien bei Files):
    python manage.py purge_deleted --body muenster --yes

Hinweise:
- Es werden NUR als gelöscht markierte Objekte entfernt. FK-Kaskaden können
  abhängige (unmarkierte) Kind-Objekte mitentfernen (z. B. TOPs einer
  gelöschten Sitzung) — das Command löscht Kind-Tabellen zuerst, damit die
  Kaskade klein bleibt, und weist im Dry-Run darauf hin.
- Objekte, auf die Daten anderer Module verweisen (direkt oder über die
  Kaskade, z. B. Sitzungsvorbereitungen, Notizen, Positionen aus Work),
  werden NICHT gelöscht. Dry-Run und Ergebnis listen sie mit der Anzahl der
  verweisenden Datensätze je Typ (Issue #421).
- Elasticsearch wird über die bestehenden post_delete-Signale aufgeräumt
  (ELASTICSEARCH_AUTO_INDEX). Lokale Dateikopien (OParlFile.local_path)
  werden vor dem Löschen der DB-Zeile vom Datenträger entfernt.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services.external_references import external_references, split_by_references

BATCH_SIZE = 1000
# So viele Objekte je Entität werden im Bericht namentlich aufgeführt
LIST_LIMIT = 10

# Kind-zuerst-Reihenfolge (kleine Rest-Kaskaden), analog zu
# services/body_deletion.py. Der Filter-Pfad ordnet jede Entität einem Body zu.
ENTITY_STEPS = [
    ("files", OParlFile, "body"),
    ("consultations", OParlConsultation, "body"),
    ("agenda_items", OParlAgendaItem, "meeting__body"),
    ("memberships", OParlMembership, "organization__body"),
    ("meetings", OParlMeeting, "body"),
    ("papers", OParlPaper, "body"),
    ("persons", OParlPerson, "body"),
    ("organizations", OParlOrganization, "body"),
    ("locations", OParlLocation, "body"),
    ("legislative_terms", OParlLegislativeTerm, "body"),
]


def _chunked_delete(queryset, batch_size: int = BATCH_SIZE) -> int:
    """Löscht ein Queryset in Batches (Signale/Kaskaden bleiben intakt)."""
    model = queryset.model
    total = 0
    while True:
        pks = list(queryset.values_list("pk", flat=True)[:batch_size])
        if not pks:
            return total
        deleted, _ = model.objects.filter(pk__in=pks).delete()
        total += deleted


def _remove_local_files(file_qs) -> int:
    """Entfernt lokale Dateikopien (local_path) vom Datenträger.

    Ablage nach SHA-256 (#788): Referenzen werden freigegeben; ein Inhalt verschwindet erst, wenn ihn keine
    andere Datei mehr braucht (``dokumentablage --aufraeumen``).
    """
    from insight_core.services.file_store import blob_root, release_queryset

    removed = release_queryset(file_qs.filter(blob__isnull=False))
    for local_path in (
        file_qs.exclude(local_path__isnull=True)
        .exclude(local_path="")
        .exclude(local_path__startswith=str(blob_root()))
        .values_list("local_path", flat=True)
    ):
        try:
            path = Path(local_path)
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            # Fehlende/gesperrte Datei blockiert die Löschung nicht
            continue
    return removed


class Command(BaseCommand):
    help = (
        "Löscht als gelöscht markierte OParl-Objekte (Tombstones) endgültig — "
        "nur auf explizite Aufforderung einer Kommune. Ohne --yes: Dry-Run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--body",
            type=str,
            help="Slug der Kommune, deren markierte Objekte gelöscht werden sollen.",
        )
        parser.add_argument(
            "--older-than",
            type=int,
            metavar="DAYS",
            help="Nur Objekte löschen, deren Markierung älter als DAYS Tage ist.",
        )
        parser.add_argument(
            "--ids",
            nargs="+",
            metavar="UUID",
            help="Nur diese Objekt-UUIDs löschen (müssen als gelöscht markiert sein).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Löschung tatsächlich ausführen (ohne dieses Flag nur Dry-Run).",
        )

    def handle(self, *args, **options):
        body_slug = options.get("body")
        older_than = options.get("older_than")
        ids = options.get("ids")
        execute = options.get("yes", False)

        if not body_slug and not ids:
            raise CommandError("Bitte --body <slug> und/oder --ids <uuid ...> angeben.")

        body = None
        if body_slug:
            try:
                body = OParlBody.objects.get(slug=body_slug)
            except OParlBody.DoesNotExist:
                raise CommandError(f"Kommune mit Slug '{body_slug}' nicht gefunden.") from None

        id_set = None
        if ids:
            id_set = set()
            for raw in ids:
                try:
                    id_set.add(uuid_module.UUID(raw))
                except ValueError:
                    raise CommandError(f"'{raw}' ist keine gültige UUID.") from None

        cutoff = None
        if older_than is not None:
            cutoff = timezone.now() - timedelta(days=older_than)

        # Querysets je Entität aufbauen (NUR markierte Objekte), getrennt in löschbar und durch
        # Verweise anderer Module geschützt. Die Querysets bleiben lazy: Beim Löschen wird die Prüfung
        # je Batch neu ausgewertet und berücksichtigt damit auch bereits gelöschte Kind-Objekte.
        steps = []
        for name, model, body_path in ENTITY_STEPS:
            qs = model.objects.filter(deleted=True)
            if body is not None:
                qs = qs.filter(**{body_path: body})
            if id_set is not None:
                qs = qs.filter(pk__in=id_set)
            if cutoff is not None:
                qs = qs.filter(deleted_at__lte=cutoff)
            deletable, protected = split_by_references(qs)
            steps.append((name, qs, deletable, protected))

        total = sum(qs.count() for _, qs, _, _ in steps)
        protected_total = 0
        mode = "LÖSCHUNG" if execute else "DRY-RUN (nichts wird gelöscht, --yes zum Ausführen)"
        scope = f"Kommune '{body_slug}'" if body_slug else "ID-Auswahl"
        if id_set is not None and body_slug:
            scope += " + ID-Auswahl"
        self.stdout.write(self.style.MIGRATE_HEADING(f"purge_deleted — {mode}"))
        self.stdout.write(f"Umfang: {scope}" + (f", Markierung älter als {older_than} Tage" if older_than else ""))
        self.stdout.write(f"Markierte Objekte gesamt: {total}\n")

        for name, qs, _deletable, protected in steps:
            count = qs.count()
            if not count:
                continue
            self.stdout.write(f"  {name}: {count}")
            for obj in qs[:LIST_LIMIT]:
                marked = obj.deleted_at.strftime("%d.%m.%Y") if obj.deleted_at else "?"
                self.stdout.write(f"    - {obj.pk} ({obj}) — markiert am {marked}")
            if count > LIST_LIMIT:
                self.stdout.write(f"    ... und {count - LIST_LIMIT} weitere")
            protected_count = protected.count()
            if protected_count:
                protected_total += protected_count
                self._write_protected(protected, protected_count)

        if id_set is not None:
            found = set()
            for _, qs, _, _ in steps:
                found.update(qs.values_list("pk", flat=True))
            missing = id_set - found
            for pk in sorted(missing, key=str):
                self.stdout.write(
                    self.style.WARNING(
                        f"  Hinweis: {pk} wird übersprungen (nicht gefunden, nicht als "
                        "gelöscht markiert oder außerhalb des Filters)."
                    )
                )

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nichts zu löschen."))
            return

        deletable_total = total - protected_total
        if protected_total:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{protected_total} markierte Objekte bleiben erhalten, weil Daten anderer Module auf sie "
                    f"verweisen. Löschbar: {deletable_total}."
                )
            )
        if deletable_total <= 0:
            self.stdout.write(self.style.SUCCESS("Nichts zu löschen."))
            return

        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry-Run beendet. Zum endgültigen Löschen --yes anhängen. "
                    "FK-Kaskaden können abhängige Kind-Objekte mitentfernen."
                )
            )
            return

        # Tatsächliche Löschung: lokale Dateikopien zuerst (nur löschbare Dateien), dann Kind-zuerst
        # über das ORM (post_delete-Signale räumen Elasticsearch auf).
        removed_files = _remove_local_files(steps[0][2])
        if removed_files:
            self.stdout.write(f"Lokale Dateikopien entfernt: {removed_files}")

        deleted_total = 0
        for name, _qs, deletable, _protected in steps:
            deleted_rows = _chunked_delete(deletable)
            if deleted_rows:
                self.stdout.write(f"  {name}: {deleted_rows} Zeilen gelöscht (inkl. Kaskaden)")
            deleted_total += deleted_rows

        kept_total = sum(protected.count() for _, _, _, protected in steps)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nEndgültig gelöscht: {deleted_total} Zeilen. "
                "Elasticsearch-Dokumente wurden über die post_delete-Signale entfernt."
            )
        )
        if kept_total:
            self.stdout.write(
                self.style.WARNING(
                    f"Wegen Verweisen anderer Module nicht gelöscht: {kept_total} markierte Objekte (siehe Liste oben)."
                )
            )

    def _write_protected(self, protected, protected_count: int) -> None:
        """Listet geschützte Objekte und die verweisenden Datensätze je Typ."""
        self.stdout.write(
            self.style.WARNING(
                f"    davon {protected_count} mit Verweisen aus anderen Modulen – werden NICHT gelöscht:"
            )
        )
        for reference in external_references(protected):
            self.stdout.write(self.style.WARNING(f"      {reference}"))
        for obj in protected[:LIST_LIMIT]:
            self.stdout.write(f"      ! {obj.pk} ({obj})")
        if protected_count > LIST_LIMIT:
            self.stdout.write(f"      ... und {protected_count - LIST_LIMIT} weitere")
