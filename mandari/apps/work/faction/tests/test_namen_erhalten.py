# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Namen in Protokollen, Anwesenheit und Teilnahmebestätigungen bleiben erhalten (Issue #591).

Seit #420 bleibt eine Fraktionssitzung beim Entfernen eines Mitglieds erhalten, zeigte aber
„Ehemaliges Mitglied“ statt des Namens – in der Anwesenheitsliste, bei Redebeiträgen und in der
Niederschrift. Protokolle belegen die Beschlussfassung; der Name muss bleiben, unabhängig davon,
ob das Protokoll schon genehmigt ist.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.common.formatting import FORMER_MEMBER
from apps.work.faction import certificates
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionAuditLog,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.faction.services import build_faction_protocol_pdf
from apps.work.organization import services as member_services

NAME = "Anna Beispiel"
MANAGE = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.view_public",
    "protocols.view_full",
]


@pytest.fixture
def chair(org: Any, make_member: Any) -> Any:
    return make_member(org, MANAGE, email="vorsitz@example.org", is_admin=True)


@pytest.fixture
def anna(org: Any, make_member: Any) -> Any:
    member = make_member(org, ["faction.view_public"], email="anna@example.org")
    member.user.first_name, member.user.last_name = "Anna", "Beispiel"
    member.user.save(update_fields=["first_name", "last_name"])
    return member


def _entry(meeting: Any, item: Any, entry_type: str, text: str, **people: Any) -> Any:
    entry: Any = FactionProtocolEntry(meeting=meeting, agenda_item=item, entry_type=entry_type, **people)
    entry.set_content_encrypted(text)
    entry.save()
    return entry


@pytest.fixture
def sitzung(org: Any, chair: Any, anna: Any) -> dict[str, Any]:
    """Abgeschlossene Sitzung: Anna war anwesend, hat geredet, übernimmt eine Aufgabe und bestätigte Teilnahmen."""
    meeting = FactionMeeting.objects.create(
        organization=org, title="Klausur", start=timezone.now() - timedelta(days=7), status="completed"
    )
    item = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt", visibility="public")
    teilnahme = FactionAttendance.objects.create(meeting=meeting, membership=anna, status="present")
    FactionAttendance.objects.create(meeting=meeting, membership=chair, status="present")
    rede = _entry(meeting, item, "speech", "Wir brauchen mehr Radwege.", speaker=anna)
    aufgabe = _entry(meeting, item, "action", "Stellungnahme entwerfen", action_assignee=anna)
    abstimmung = _entry(meeting, item, "vote", "Namentlich: Anna Beispiel stimmt mit Ja.", speaker=anna)

    # Anna bestätigt als Vorstand die Teilnahmen, so wie FactionActionView._confirm_attendance
    jetzt = timezone.now()
    for attendance in meeting.attendances.all():
        attendance.confirmed_final_at = jetzt
        attendance.confirmed_final_by = anna
        attendance.save(update_fields=["confirmed_final_at", "confirmed_final_by", "updated_at"])
    return {
        "meeting": meeting,
        "item": item,
        "teilnahme": teilnahme,
        "rede": rede,
        "aufgabe": aufgabe,
        "abstimmung": abstimmung,
    }


def _neu(obj: Any) -> Any:
    return type(obj).objects.get(pk=obj.pk)


def _entfernen(art: str, org: Any, chair: Any, anna: Any) -> None:
    if art == "mitglied_entfernen":
        member_services.remove_member(org, anna, chair.user)
    else:  # Konto gelöscht: die Mitgliedschaft entfällt per Kaskade am Benutzer
        anna.user.delete()


@pytest.mark.django_db
def test_name_wird_beim_erfassen_gesichert(sitzung: dict[str, Any]) -> None:
    teilnahme = _neu(sitzung["teilnahme"])
    assert teilnahme.member_name_snapshot == NAME
    assert teilnahme.confirmed_final_by_name_snapshot == NAME
    assert _neu(sitzung["rede"]).speaker_name_snapshot == NAME
    assert _neu(sitzung["aufgabe"]).action_assignee_name_snapshot == NAME
    assert _neu(sitzung["abstimmung"]).speaker_name_snapshot == NAME


@pytest.mark.django_db
@pytest.mark.parametrize("genehmigt", [False, True])
@pytest.mark.parametrize("art", ["mitglied_entfernen", "konto_loeschen"])
def test_namen_bleiben_nach_dem_entfernen(
    art: str, genehmigt: bool, org: Any, chair: Any, anna: Any, sitzung: dict[str, Any]
) -> None:
    if genehmigt:
        FactionMeeting.objects.filter(pk=sitzung["meeting"].pk).update(
            protocol_approved=True, protocol_status="approved", protocol_approved_at=timezone.now()
        )

    _entfernen(art, org, chair, anna)

    teilnahme = _neu(sitzung["teilnahme"])
    assert teilnahme.membership_id is None
    assert teilnahme.get_display_name() == NAME
    assert NAME in str(teilnahme)
    assert teilnahme.confirmed_final_by_name == NAME
    assert _neu(sitzung["rede"]).speaker_name == NAME
    assert _neu(sitzung["aufgabe"]).action_assignee_name == NAME
    assert _neu(sitzung["abstimmung"]).speaker_name == NAME


@pytest.mark.django_db
def test_niederschrift_zeigt_namen_nach_dem_entfernen(org: Any, chair: Any, anna: Any, sitzung: dict[str, Any]) -> None:
    member_services.remove_member(org, anna, chair.user)

    with mock.patch("apps.work.faction.services.html_to_pdf", side_effect=lambda html: html.encode()):
        html = build_faction_protocol_pdf(sitzung["meeting"], internal=True).decode()

    assert FORMER_MEMBER not in html
    teilnehmer = html.split("Teilnehmerverzeichnis", 1)[1].split("Verhandlung der Tagesordnung", 1)[0]
    assert NAME in teilnehmer
    assert f"Wortbeitrag</span> · {NAME}" in html
    assert f"Abstimmung</span> · {NAME}" in html
    assert f"zuständig: {NAME}" in html


@pytest.mark.django_db
def test_sitzungsseite_zeigt_namen_nach_dem_entfernen(
    org: Any, chair: Any, anna: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    member_services.remove_member(org, anna, chair.user)
    client = client_for(chair.user)
    meeting, item = sitzung["meeting"], sitzung["item"]

    detail = client.get(reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id}))
    assert detail.status_code == 200
    html = detail.content.decode()
    assert f"{NAME}:</span>" in html  # Redebeitrag
    assert f"zuständig: {NAME}" in html
    assert FORMER_MEMBER not in html

    panel = client.get(
        reverse("work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": meeting.id, "item_id": item.id})
    )
    assert panel.status_code == 200
    assert f"{NAME}:</span>" in panel.content.decode()


@pytest.mark.django_db
def test_entfernen_sichert_den_aktuellen_namen(org: Any, chair: Any, anna: Any, sitzung: dict[str, Any]) -> None:
    """Heißt die Person inzwischen anders, gilt der Name zum Zeitpunkt des Entfernens."""
    anna.user.last_name = "Neu"
    anna.user.save(update_fields=["last_name"])

    member_services.remove_member(org, anna, chair.user)

    assert _neu(sitzung["teilnahme"]).get_display_name() == "Anna Neu"
    assert _neu(sitzung["rede"]).speaker_name == "Anna Neu"


@pytest.mark.django_db
def test_speichern_nach_dem_entfernen_behaelt_den_namen(
    org: Any, chair: Any, anna: Any, sitzung: dict[str, Any]
) -> None:
    member_services.remove_member(org, anna, chair.user)

    teilnahme = _neu(sitzung["teilnahme"])
    teilnahme.status = "excused"
    teilnahme.save()
    rede = _neu(sitzung["rede"])
    rede.set_content_encrypted("Wir brauchen mehr sichere Radwege.")
    rede.save()

    assert _neu(teilnahme).get_display_name() == NAME
    assert _neu(rede).speaker_name == NAME


@pytest.mark.django_db
def test_geleerter_redner_leert_den_namen(anna: Any, sitzung: dict[str, Any]) -> None:
    rede = _neu(sitzung["rede"])
    rede.speaker = None
    rede.save()

    assert _neu(rede).speaker_name_snapshot == ""
    assert _neu(rede).speaker_name == ""


@pytest.mark.django_db
def test_geaenderter_redner_ersetzt_den_namen(org: Any, chair: Any, sitzung: dict[str, Any]) -> None:
    rede = _neu(sitzung["rede"])
    rede.speaker_id = chair.pk  # nur die ID, ohne geladenes Objekt
    rede.save()

    assert _neu(rede).speaker_name_snapshot == chair.user.get_display_name()

    # Die unveränderbare Historie hält den Wechsel als Verweis fest, nicht den Namen
    eintrag = FactionAuditLog.objects.filter(object_id=rede.pk, action="update").latest("created_at")
    assert "speaker" in eintrag.changes
    assert "speaker_name_snapshot" not in eintrag.changes


@pytest.mark.django_db
def test_sammel_export_enthaelt_ehemalige_mitglieder(org: Any, chair: Any, anna: Any, sitzung: dict[str, Any]) -> None:
    member_services.remove_member(org, anna, chair.user)
    heute = timezone.localdate()
    von, bis = heute - timedelta(days=30), heute

    attendances = list(certificates.bulk_confirmed_attendances(org, von, bis))
    assert sitzung["teilnahme"].pk in {a.pk for a in attendances}

    csv_text = certificates.build_bulk_export_csv(org, von, bis, attendances)
    zeile = next(z for z in csv_text.splitlines() if z.startswith(NAME))
    spalten = zeile.split(";")
    assert spalten[1] == ""  # keine Kontaktdaten ehemaliger Mitglieder
    assert spalten[5] == NAME  # bestätigt durch

    gruppen = certificates._group_by_member(attendances)
    assert NAME in [g["name"] for g in gruppen]


@pytest.mark.django_db
def test_sammel_export_ohne_gaeste(org: Any, chair: Any, sitzung: dict[str, Any]) -> None:
    FactionAttendance.objects.create(
        meeting=sitzung["meeting"],
        is_guest=True,
        guest_name="Gast Gustav",
        status="present",
        confirmed_final_at=timezone.now(),
    )
    heute = timezone.localdate()
    attendances = certificates.bulk_confirmed_attendances(org, heute - timedelta(days=30), heute)
    assert "Gast Gustav" not in [a.get_display_name() for a in attendances]


# ---------------------------------------------------------------------------
# Datenmigration work/0063
# ---------------------------------------------------------------------------

MIGRATION = importlib.import_module("apps.work.migrations.0063_namen_in_protokollen_sichern")
NACHHER = ("work", "0063_namen_in_protokollen_sichern")
VORHER = MIGRATION.Migration.dependencies[0]


@pytest.mark.django_db(transaction=True)
def test_migration_sichert_namen_im_bestand(org: Any, anna: Any, make_member: Any) -> None:
    ohne_namen = make_member(org, [], email="bernd.b@example.org")

    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Meeting = alt.get_model("work", "FactionMeeting")
        Attendance = alt.get_model("work", "FactionAttendance")
        Entry = alt.get_model("work", "FactionProtocolEntry")
        meeting = Meeting.objects.create(organization_id=org.pk, title="Alt", start=timezone.now())
        teilnahme = Attendance.objects.create(
            meeting=meeting, membership_id=anna.pk, status="present", confirmed_final_by_id=ohne_namen.pk
        )
        ehemalig = Attendance.objects.create(meeting=meeting, membership=None, status="present")
        gast = Attendance.objects.create(meeting=meeting, is_guest=True, guest_name="Gast", status="present")
        rede = Entry.objects.create(meeting=meeting, entry_type="speech", speaker_id=anna.pk)
        aufgabe = Entry.objects.create(meeting=meeting, entry_type="action", action_assignee_id=ohne_namen.pk)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        assert FactionAttendance.objects.get(pk=teilnahme.pk).member_name_snapshot == NAME
        assert FactionAttendance.objects.get(pk=teilnahme.pk).confirmed_final_by_name_snapshot == "bernd.b"
        assert FactionAttendance.objects.get(pk=ehemalig.pk).member_name_snapshot == ""
        assert FactionAttendance.objects.get(pk=ehemalig.pk).get_display_name() == FORMER_MEMBER
        assert FactionAttendance.objects.get(pk=gast.pk).member_name_snapshot == ""
        assert FactionProtocolEntry.objects.get(pk=rede.pk).speaker_name_snapshot == NAME
        assert FactionProtocolEntry.objects.get(pk=aufgabe.pk).action_assignee_name_snapshot == "bernd.b"

        # Wiederholbar: ein zweiter Lauf überschreibt gesicherte Namen nicht
        anna.user.first_name = "Hanna"
        anna.user.save(update_fields=["first_name"])
        MIGRATION.namen_sichern(executor.loader.project_state([NACHHER]).apps, None)
        assert FactionAttendance.objects.get(pk=teilnahme.pk).member_name_snapshot == NAME
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_migrationsname_passt_zur_anwendung() -> None:
    """Die Migration bildet den Anzeigenamen wie die Anwendung (Stand der Migration)."""
    from apps.common.formatting import MEMBER_NAME_MAX_LENGTH

    assert MIGRATION.NAME_MAX_LENGTH == MEMBER_NAME_MAX_LENGTH
    assert MIGRATION.display_name("Anna", "Beispiel", "a@example.org") == NAME
    assert MIGRATION.display_name("", "", "bernd.b@example.org") == "bernd.b"
