# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration: AgendaItemNote -> PaperComment für TOPs mit Vorlage.

ARCHITEKTUR-ENTSCHEIDUNG (Sitzungsvorbereitungs-Umbau Etappe 1):
PaperComment ist DER Diskussions-Thread für TOPs mit Vorlage. Alle
bestehenden AgendaItemNotes, deren TOP eine Vorlage berät (Auflösung über
OParlConsultation.agenda_item_external_id), werden nach PaperComment
überführt:

- content 1:1 als Ciphertext-Kopie: AgendaItemNote und PaperComment
  verschlüsseln beide über get_encryption_organization() == organization.
  Da die Organisation identisch übernommen wird, ist der Tenant-Key
  derselbe und der Ciphertext direkt kopierbar (kein Ent-/Verschlüsseln
  nötig).
- visibility: organization -> organization, consulting -> consulting
- is_decision -> is_recommendation
- author und created_at bleiben erhalten (created_at per update(), da
  auto_now_add beim Anlegen überschreibt)

Die Original-Notizen werden NICHT gelöscht, sondern über
migrated_to_paper_comment markiert; UI-Queries schließen markierte aus
(keine Doppelanzeige). Die Migration ist idempotent: bereits markierte
Notizen werden übersprungen.
"""

from django.db import migrations


def migrate_notes_to_paper_comments(apps, schema_editor):
    AgendaItemNote = apps.get_model("work", "AgendaItemNote")
    PaperComment = apps.get_model("work", "PaperComment")
    OParlConsultation = apps.get_model("insight_core", "OParlConsultation")

    notes = list(AgendaItemNote.objects.filter(migrated_to_paper_comment__isnull=True).select_related("agenda_item"))
    if not notes:
        return

    # Vorlagen je TOP in einer Query auflösen (erste Consultation gewinnt)
    external_ids = {n.agenda_item.external_id for n in notes if n.agenda_item and n.agenda_item.external_id}
    paper_by_ext = {}
    for cons in (
        OParlConsultation.objects.filter(agenda_item_external_id__in=external_ids, paper__isnull=False)
        .order_by("created_at")
        .values_list("agenda_item_external_id", "paper_id")
    ):
        paper_by_ext.setdefault(cons[0], cons[1])

    for note in notes:
        external_id = note.agenda_item.external_id if note.agenda_item else None
        paper_id = paper_by_ext.get(external_id)
        if not paper_id:
            # TOP ohne Vorlage: Notiz bleibt org-lokal bestehen
            continue

        comment = PaperComment.objects.create(
            paper_id=paper_id,
            organization_id=note.organization_id,
            author_id=note.author_id,
            visibility="consulting" if note.visibility == "consulting" else "organization",
            # Gleiche Organisation -> gleicher Tenant-Key -> Ciphertext 1:1
            content_encrypted=bytes(note.content_encrypted) if note.content_encrypted else None,
            is_recommendation=note.is_decision,
        )
        # auto_now_add/auto_now nachträglich korrigieren
        PaperComment.objects.filter(pk=comment.pk).update(
            created_at=note.created_at,
            updated_at=note.updated_at,
        )
        note.migrated_to_paper_comment_id = comment.pk
        note.save(update_fields=["migrated_to_paper_comment"])


def revert_notes_from_paper_comments(apps, schema_editor):
    """Rückweg: erzeugte PaperComments löschen, Markierung zurücksetzen."""
    AgendaItemNote = apps.get_model("work", "AgendaItemNote")
    PaperComment = apps.get_model("work", "PaperComment")

    comment_ids = list(
        AgendaItemNote.objects.filter(migrated_to_paper_comment__isnull=False).values_list(
            "migrated_to_paper_comment_id", flat=True
        )
    )
    AgendaItemNote.objects.filter(migrated_to_paper_comment__isnull=False).update(migrated_to_paper_comment=None)
    PaperComment.objects.filter(pk__in=comment_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0036_sitzungsvorbereitung_umbau_etappe1"),
        ("insight_core", "0020_org_wide_meeting_prep_phase1"),
    ]

    operations = [
        migrations.RunPython(migrate_notes_to_paper_comments, revert_notes_from_paper_comments),
    ]
