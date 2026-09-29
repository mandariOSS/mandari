# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nach PaperComment überführte TOP-Notizen tauchen nach dem Löschen nicht wieder auf (Issue #452).

Migration 0037 hat Notizen an TOPs mit Vorlage als PaperComment kopiert und die Originale markiert
behalten. Löschte die Autorin den Kommentar, setzte ``SET_NULL`` die Markierung zurück und das
Original stand wieder im Diskussions-Thread – der Löschwunsch wirkte nicht. Jetzt entfällt das
Original mit dem Kommentar, und Migration 0061 entfernt die markierten Originale im Bestand.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.work.meetings import selectors, services
from apps.work.meetings.models import AgendaItemNote, PaperComment
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)

MIGRATION = importlib.import_module("apps.work.migrations.0061_migrierte_notizen_entfernen")
NACHHER = ("work", "0061_migrierte_notizen_entfernen")
VORHER = MIGRATION.Migration.dependencies[0]


@pytest.fixture
def top_mit_vorlage(org: Any) -> tuple[OParlAgendaItem, OParlPaper]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now() + timedelta(days=3)
    )
    item = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Spielplatz", order=1
    )
    paper = OParlPaper.objects.create(
        external_id="https://ris.example.org/paper/1", body=body, name="Antrag Spielplatz", reference="V/2026/1"
    )
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/1",
        body=body,
        paper=paper,
        agenda_item_external_id=item.external_id,
        meeting_external_id=meeting.external_id,
    )
    return item, paper


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.prepare"], email="autorin@example.org")


def _ueberfuehrt(org: Any, item: OParlAgendaItem, paper: OParlPaper, autorin: Any) -> tuple[AgendaItemNote, Any]:
    """Zustand nach Migration 0037: Kommentar an der Vorlage, markiertes Original am TOP."""
    comment = PaperComment(paper=paper, organization=org, author=autorin, visibility="organization")
    comment.set_content_encrypted("Mein Diskussionsbeitrag")  # type: ignore[attr-defined]
    comment.save()
    note = AgendaItemNote(organization=org, agenda_item=item, author=autorin, migrated_to_paper_comment=comment)
    note.set_content_encrypted("Mein Diskussionsbeitrag")  # type: ignore[attr-defined]
    note.save()
    return note, comment


@pytest.mark.django_db
def test_geloeschter_kommentar_taucht_nicht_als_original_wieder_auf(
    org: Any, top_mit_vorlage: tuple[OParlAgendaItem, OParlPaper], autorin: Any
) -> None:
    item, paper = top_mit_vorlage
    note, comment = _ueberfuehrt(org, item, paper, autorin)
    assert selectors.thread_notes(org, item.id) == []

    assert services.delete_thread_note(autorin, comment.id) is True

    assert selectors.thread_notes(org, item.id) == [], "Das Original darf nach dem Löschen nicht wieder erscheinen"
    assert not AgendaItemNote.objects.filter(pk=note.pk).exists(), "Der Inhalt darf nicht doppelt zurückbleiben"
    assert not PaperComment.objects.filter(pk=comment.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_migration_entfernt_markierte_originale(
    org: Any, top_mit_vorlage: tuple[OParlAgendaItem, OParlPaper], autorin: Any
) -> None:
    item, paper = top_mit_vorlage
    markiert, comment = _ueberfuehrt(org, item, paper, autorin)
    eigenstaendig = AgendaItemNote(organization=org, agenda_item=item, author=autorin)
    eigenstaendig.set_content_encrypted("Notiz ohne Vorlage")  # type: ignore[attr-defined]
    eigenstaendig.save()

    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        assert not AgendaItemNote.objects.filter(pk=markiert.pk).exists()
        assert AgendaItemNote.objects.filter(pk=eigenstaendig.pk).exists(), "Unmarkierte Notizen bleiben"
        assert PaperComment.objects.filter(pk=comment.pk).exists(), "Der überführte Kommentar bleibt"

        # Wiederholbar: ein zweiter Lauf ändert nichts
        MIGRATION.markierte_originale_loeschen(executor.loader.project_state([NACHHER]).apps, None)
        assert AgendaItemNote.objects.filter(pk=eigenstaendig.pk).exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
