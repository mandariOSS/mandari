# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endgültiges Löschen in den Fraktionssitzungen nur mit ausdrücklicher Freigabe (Issue #897).

Sven (06.10.2026): „Löschen darf man nur mit expliziter Freigabe. Es sollte aber möglich sein.“

- Sitzungen und Sitzungsreihen löscht nur, wer ``faction.delete`` hat – ohne Recht kein Knopf und Ablehnung
  durch den Server (je Standardrolle geprüft).
- Der Dialog nennt die Folgen mit Zahlen und bietet das Absagen bzw. Pausieren an.
- Ohne passende Eingabe (Datum oder Titel der Sitzung, Name der Reihe, Nummer oder Titel des TOPs) löscht der
  Server nichts; ein leerer TOP geht wie bisher ohne Eingabe.
- Der Audit-Eintrag des gelöschten Objekts hält die Zahlen fest.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import DEFAULT_ROLES
from apps.work.faction import deletion
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAgendaItemAttachment,
    FactionAttendance,
    FactionAuditLog,
    FactionDecision,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
    FactionProtocolEntry,
)
from apps.work.tasks.models import Task

HTMX = {"HTTP_HX_REQUEST": "true"}
START = datetime(2026, 11, 3, 18, 0)
DATUM = "03.11.2026"


def _rolle(key: str) -> list[str]:
    return list(cast(list[str], DEFAULT_ROLES[key]["permissions"]))


def _darf_loeschen(key: str) -> bool:
    return bool(DEFAULT_ROLES[key].get("is_admin")) or "faction.delete" in _rolle(key)


def _mitglied(org: Any, make_member: Any, key: str, email: str | None = None) -> Any:
    member = make_member(
        org, _rolle(key), email=email or f"{key}@example.org", is_admin=bool(DEFAULT_ROLES[key].get("is_admin"))
    )
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


def _eintrag(meeting: FactionMeeting, item: FactionAgendaItem, text: str) -> FactionProtocolEntry:
    entry: Any = FactionProtocolEntry(meeting=meeting, agenda_item=item, entry_type="note")
    entry.set_content_encrypted(text)
    entry.save()
    return cast(FactionProtocolEntry, entry)


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "faction_chair", "vorsitz@example.org")


@pytest.fixture
def stellvertretung(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "faction_vice_chair", "stellv@example.org")


@pytest.fixture
def sitzung(org: Any, vorsitz: Any, stellvertretung: Any, settings: Any, tmp_path: Any) -> FactionMeeting:
    """Sitzung mit Inhalt: 3 TOPs (einer davon Unterpunkt), 2 Anwesenheiten, 2 Protokolleinträge, 1 Beschluss,
    1 Anhang und eine Aufgabe mit Bezug."""
    settings.MEDIA_ROOT = str(tmp_path)
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung November",
        start=timezone.make_aware(START),
        status="planned",
        created_by=stellvertretung,
    )
    haushalt = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt", order=0)
    unterpunkt = FactionAgendaItem.objects.create(
        meeting=meeting, number="1.1", title="Schulen", parent=haushalt, order=0
    )
    verkehr = FactionAgendaItem.objects.create(meeting=meeting, number="2", title="Verkehr", order=1)
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="confirmed")
    FactionAttendance.objects.create(meeting=meeting, membership=stellvertretung, status="invited")
    _eintrag(meeting, haushalt, "Kämmerei berichtet")
    _eintrag(meeting, unterpunkt, "Sanierung vorziehen")
    FactionDecision.objects.create(agenda_item=haushalt, votes_yes=5, result="accepted")
    FactionAgendaItemAttachment.objects.create(
        agenda_item=verkehr,
        file=SimpleUploadedFile("plan.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
        filename="plan.pdf",
        mime_type="application/pdf",
        file_size=13,
        uploaded_by=vorsitz,
    )
    Task.objects.create(organization=org, title="Flyer", created_by=vorsitz, related_faction_meeting=meeting)
    return meeting


def _aktion(client: Any, meeting: FactionMeeting, htmx: bool = True, **data: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.id})
    return client.post(url, data, **(HTMX if htmx else {}))


def _detail(client: Any, meeting: FactionMeeting) -> str:
    url = reverse("work:faction_detail", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.id})
    response = client.get(url)
    assert response.status_code == 200
    return cast(str, response.content.decode())


def _zeilen(html: str) -> dict[str, int]:
    """Bezeichnung → Anzahl aus der Folgenliste des Dialogs."""
    paare = re.findall(r"<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>(\d+)</dd>", html)
    return {label.strip(): int(anzahl) for label, anzahl in paare}


# -- Rechte je Rolle ----------------------------------------------------------------------------------------


def test_nur_admin_und_vorsitz_haben_das_loeschrecht() -> None:
    erlaubt = {key for key in DEFAULT_ROLES if _darf_loeschen(key)}
    assert erlaubt == {"admin", "faction_chair"}


@pytest.mark.django_db
@pytest.mark.parametrize("rolle", list(DEFAULT_ROLES))
def test_sitzung_loeschen_je_rolle(org: Any, make_member: Any, client_for: Any, rolle: str) -> None:
    """Ohne faction.delete kein Knopf und Ablehnung, auch für die Person, die die Sitzung angelegt hat."""
    member = _mitglied(org, make_member, rolle)
    meeting = FactionMeeting.objects.create(
        organization=org, title="Klausur", start=timezone.make_aware(START), status="planned", created_by=member
    )
    client = client_for(member.user)

    if "faction.view_public" in _rolle(rolle) or DEFAULT_ROLES[rolle].get("is_admin"):
        html = _detail(client, meeting)
        assert ("$dispatch('open-delete-meeting')" in html) is _darf_loeschen(rolle)
        assert ('id="delete-meeting-modal"' in html) is _darf_loeschen(rolle)

    response = _aktion(client, meeting, action="delete", confirmation=DATUM)

    if _darf_loeschen(rolle):
        assert response.status_code == 200
        assert response["HX-Redirect"] == reverse("work:faction", kwargs={"org_slug": org.slug})
        assert not FactionMeeting.objects.filter(pk=meeting.pk).exists()
    else:
        assert response.status_code == 403
        assert FactionMeeting.objects.filter(pk=meeting.pk).exists()


@pytest.mark.django_db
def test_stellvertretung_mit_verwaltungsrecht_loescht_nicht_mehr(
    sitzung: FactionMeeting, stellvertretung: Any, client_for: Any
) -> None:
    """Früher genügten faction.manage oder das Anlegen der Sitzung; jetzt braucht es faction.delete."""
    client = client_for(stellvertretung.user)

    vorschau = _aktion(client, sitzung, action="delete_preview")
    ohne_htmx = _aktion(client, sitzung, htmx=False, action="delete", confirmation=DATUM)

    assert vorschau.status_code == 403
    assert ohne_htmx.status_code == 302
    assert FactionMeeting.objects.filter(pk=sitzung.pk).exists()
    assert sitzung.attendances.count() == 2


@pytest.mark.django_db
def test_einzeln_vergebenes_loeschrecht_genuegt(org: Any, make_member: Any, client_for: Any) -> None:
    """Das Recht lässt sich ausdrücklich vergeben – auch ohne Verwaltungsrecht erscheint dann der Knopf."""
    member = make_member(org, ["faction.view_public", "faction.delete"], email="loeschen@example.org")
    meeting = FactionMeeting.objects.create(organization=org, title="Doppelt", start=timezone.make_aware(START))
    client = client_for(member.user)

    assert "$dispatch('open-delete-meeting')" in _detail(client, meeting)
    response = _aktion(client, meeting, action="delete", confirmation="doppelt")

    assert response.status_code == 200
    assert not FactionMeeting.objects.filter(pk=meeting.pk).exists()


# -- Ausdrückliche Eingabe ------------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("eingabe", ["", "   ", "ja", "04.11.2026", "Fraktionssitzung"])
def test_ohne_passende_eingabe_loescht_der_server_nichts(
    sitzung: FactionMeeting, vorsitz: Any, client_for: Any, eingabe: str
) -> None:
    client = client_for(vorsitz.user)

    htmx = _aktion(client, sitzung, action="delete", confirmation=eingabe)
    ohne_feld = _aktion(client, sitzung, action="delete")
    formular = _aktion(client, sitzung, htmx=False, action="delete", confirmation=eingabe)

    assert htmx.status_code == 400
    assert htmx.content.decode() == deletion.NOT_CONFIRMED_MEETING
    assert ohne_feld.status_code == 400
    assert formular.status_code == 302
    assert formular["Location"].endswith(f"/faction/{sitzung.id}/")
    assert FactionMeeting.objects.filter(pk=sitzung.pk).exists()
    assert sitzung.attendances.count() == 2
    assert sitzung.agenda_items.count() == 3


@pytest.mark.django_db
@pytest.mark.parametrize("eingabe", [DATUM, "3.11.2026", " 03.11.2026 ", "fraktionssitzung   NOVEMBER"])
def test_datum_oder_titel_gibt_das_loeschen_frei(
    sitzung: FactionMeeting, vorsitz: Any, client_for: Any, eingabe: str
) -> None:
    response = _aktion(client_for(vorsitz.user), sitzung, action="delete", confirmation=eingabe)

    assert response.status_code == 200
    assert response["HX-Redirect"].endswith("/faction/")
    assert not FactionMeeting.objects.filter(pk=sitzung.pk).exists()


@pytest.mark.django_db
def test_loeschen_ohne_htmx_leitet_auf_die_liste(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    response = _aktion(client_for(vorsitz.user), sitzung, htmx=False, action="delete", confirmation=DATUM)

    assert response.status_code == 302
    assert response["Location"] == reverse("work:faction", kwargs={"org_slug": sitzung.organization.slug})


# -- Folgen im Dialog, Alternative, Audit ---------------------------------------------------------------------


@pytest.mark.django_db
def test_dialog_nennt_die_folgen_und_bietet_das_absagen_an(
    sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    response = _aktion(client_for(vorsitz.user), sitzung, action="delete_preview")
    html = response.content.decode()

    assert response.status_code == 200
    assert _zeilen(html) == {
        "Tagesordnungspunkte": 3,
        "Anwesenheiten und Zusagen": 2,
        "Protokolleinträge": 2,
        "Beschlüsse": 1,
        "Unterlagen (Anhänge an TOPs)": 1,
        "Aufgaben": 1,
    }
    assert "Bleibt erhalten, verliert den Bezug" in html
    # Absagen als schonende Alternative, Löschen erst nach Eingabe des Datums
    assert '{"action": "cancel"}' in html
    assert "Sitzung absagen" in html
    assert 'x-data="deleteConfirmation"' in html
    assert 'name="confirmation"' in html
    assert 'x-bind:disabled="!matches"' in html
    match = re.search(r'data-phrases="([^"]*)"', html)
    assert match
    phrasen = json.loads(match.group(1).replace("&quot;", '"'))
    assert DATUM in phrasen
    assert "fraktionssitzung november" in phrasen
    assert DATUM in html


@pytest.mark.django_db
def test_abgesagte_sitzung_bietet_kein_erneutes_absagen(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    sitzung.status = "cancelled"
    sitzung.save(update_fields=["status"])

    html = _aktion(client_for(vorsitz.user), sitzung, action="delete_preview").content.decode()

    assert '{"action": "cancel"}' not in html
    assert 'name="confirmation"' in html


@pytest.mark.django_db
def test_absagen_aus_dem_dialog_behaelt_alle_daten(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    _aktion(client_for(vorsitz.user), sitzung, action="cancel")

    sitzung.refresh_from_db()
    assert sitzung.status == "cancelled"
    assert sitzung.attendances.count() == 2
    assert sitzung.agenda_items.count() == 3


@pytest.mark.django_db
def test_audit_eintrag_haelt_die_zahlen_fest(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    meeting_id = sitzung.pk
    _aktion(client_for(vorsitz.user), sitzung, action="delete", confirmation=DATUM)

    eintrag = FactionAuditLog.objects.get(model_name="FactionMeeting", action="delete", object_id=meeting_id)
    assert eintrag.membership == vorsitz
    assert eintrag.changes == {
        "Tagesordnungspunkte (mitgelöscht)": {"alt": 3, "neu": 0},
        "Anwesenheiten und Zusagen (mitgelöscht)": {"alt": 2, "neu": 0},
        "Protokolleinträge (mitgelöscht)": {"alt": 2, "neu": 0},
        "Beschlüsse (mitgelöscht)": {"alt": 1, "neu": 0},
        "Unterlagen (Anhänge an TOPs) (mitgelöscht)": {"alt": 1, "neu": 0},
        "Aufgaben (Bezug entfernt)": {"alt": 1, "neu": 0},
    }


@pytest.mark.django_db
def test_aufgaben_bleiben_beim_loeschen_erhalten(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    aufgabe = Task.objects.get(title="Flyer")
    _aktion(client_for(vorsitz.user), sitzung, action="delete", confirmation=DATUM)

    aufgabe.refresh_from_db()
    assert aufgabe.related_faction_meeting is None


@pytest.mark.django_db
@pytest.mark.parametrize("neuer_rahmen", [False, True])
def test_beide_rahmen_zeigen_knopf_und_dialog(
    sitzung: FactionMeeting, vorsitz: Any, client_for: Any, neuer_rahmen: bool
) -> None:
    org = sitzung.organization
    org.work_new_design = neuer_rahmen
    org.save(update_fields=["work_new_design"])

    html = _detail(client_for(vorsitz.user), sitzung)

    assert ('data-rahmen="neu"' in html) is neuer_rahmen
    assert "$dispatch('open-delete-meeting')" in html
    assert 'id="delete-meeting-modal"' in html
    assert 'id="delete-item-modal"' in html
    assert "Sitzung unwiderruflich löschen?" not in html
    config = re.search(r'<script[^>]*id="faction-detail-config"[^>]*>(.*?)</script>', html, re.S)
    assert config
    assert json.loads(config.group(1))["actionUrl"].endswith(f"/faction/{sitzung.id}/action/")


# -- TOPs ------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_top_mit_inhalt_nur_mit_nummer_oder_titel(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    haushalt = sitzung.agenda_items.get(number="1")

    ohne = _aktion(client, sitzung, action="delete_item", item_id=str(haushalt.id))
    falsch = _aktion(client, sitzung, action="delete_item", item_id=str(haushalt.id), confirmation="2")

    assert ohne.status_code == 400
    assert falsch.status_code == 400
    assert FactionAgendaItem.objects.filter(pk=haushalt.pk).exists()

    richtig = _aktion(client, sitzung, action="delete_item", item_id=str(haushalt.id), confirmation="1")

    assert richtig.status_code == 200
    assert not FactionAgendaItem.objects.filter(pk=haushalt.pk).exists()
    assert sitzung.agenda_items.count() == 1
    eintrag = FactionAuditLog.objects.get(model_name="FactionAgendaItem", action="delete", object_id=haushalt.pk)
    assert eintrag.changes["Unterpunkte (mitgelöscht)"] == {"alt": 1, "neu": 0}
    assert eintrag.changes["Protokolleinträge (mitgelöscht)"] == {"alt": 2, "neu": 0}
    assert eintrag.changes["Beschlüsse (mitgelöscht)"] == {"alt": 1, "neu": 0}


@pytest.mark.django_db
def test_top_dialog_nennt_folgen_und_verlangt_die_nummer(
    sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    haushalt = sitzung.agenda_items.get(number="1")

    html = _aktion(client_for(vorsitz.user), sitzung, action="delete_item_preview", item_id=str(haushalt.id)).content
    html = html.decode()

    assert _zeilen(html) == {"Unterpunkte": 1, "Protokolleinträge": 2, "Beschlüsse": 1}
    assert 'name="confirmation"' in html
    assert 'data-after-request="close-dialog"' in html
    assert "Zur Bestätigung die Nummer des TOPs eingeben:" in html


@pytest.mark.django_db
def test_leerer_top_geht_wie_bisher_ohne_eingabe(sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    leer = FactionAgendaItem.objects.create(meeting=sitzung, number="3", title="Sonstiges", order=2)

    vorschau = _aktion(client, sitzung, action="delete_item_preview", item_id=str(leer.id)).content.decode()
    response = _aktion(client, sitzung, action="delete_item", item_id=str(leer.id))

    assert 'name="confirmation"' not in vorschau
    assert 'data-phrases="[]"' in vorschau
    assert response.status_code == 200
    assert not FactionAgendaItem.objects.filter(pk=leer.pk).exists()


@pytest.mark.django_db
def test_top_vorschau_ohne_agendarecht_und_fremder_top(
    org: Any, make_member: Any, sitzung: FactionMeeting, stellvertretung: Any, client_for: Any
) -> None:
    mitglied = _mitglied(org, make_member, "faction_member", "mitglied@example.org")
    fremd = FactionMeeting.objects.create(organization=org, title="Andere", start=timezone.make_aware(START))
    fremder_top = FactionAgendaItem.objects.create(meeting=fremd, number="1", title="Fremd")
    haushalt = sitzung.agenda_items.get(number="1")

    ohne_recht = _aktion(client_for(mitglied.user), sitzung, action="delete_item_preview", item_id=str(haushalt.id))
    falscher_top = _aktion(
        client_for(stellvertretung.user), sitzung, action="delete_item_preview", item_id=str(fremder_top.id)
    )
    kaputte_kennung = _aktion(
        client_for(stellvertretung.user), sitzung, action="delete_item_preview", item_id="kein-uuid"
    )

    assert ohne_recht.status_code == 403
    assert falscher_top.status_code == 404
    assert kaputte_kennung.status_code == 404


# -- Sitzungsreihen ----------------------------------------------------------------------------------------


@pytest.fixture
def reihe(org: Any, stellvertretung: Any) -> FactionMeetingSchedule:
    schedule = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time="18:00"
    )
    FactionMeetingException.objects.create(
        schedule=schedule, original_date="2026-12-21", exception_type="cancelled", reason="Weihnachten"
    )
    for tag in (3, 10):
        FactionMeeting.objects.create(
            organization=org,
            title=f"Termin {tag}",
            start=timezone.make_aware(datetime(2030, 11, tag, 18, 0)),
            schedule=schedule,
            created_by=stellvertretung,
        )
    return schedule


def _einstellungen(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


@pytest.mark.django_db
def test_reihe_ohne_loeschrecht_kein_knopf_und_ablehnung(
    org: Any, reihe: FactionMeetingSchedule, stellvertretung: Any, client_for: Any
) -> None:
    client = client_for(stellvertretung.user)

    html = client.get(_einstellungen(org)).content.decode()
    response = client.post(
        _einstellungen(org),
        {"section": "delete_schedule", "schedule_id": str(reihe.id), "confirmation": reihe.name},
    )

    assert "Wöchentliche Fraktionssitzung" in html
    assert f'id="delete-schedule-{reihe.id}"' not in html
    assert 'value="delete_schedule"' not in html
    assert response.status_code == 403
    assert FactionMeetingSchedule.objects.filter(pk=reihe.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("neuer_rahmen", [False, True])
def test_reihe_dialog_nennt_folgen_und_pausieren(
    org: Any, reihe: FactionMeetingSchedule, vorsitz: Any, client_for: Any, neuer_rahmen: bool
) -> None:
    org.work_new_design = neuer_rahmen
    org.save(update_fields=["work_new_design"])

    html = client_for(vorsitz.user).get(_einstellungen(org)).content.decode()
    dialog = html[html.index(f'id="delete-schedule-{reihe.id}"') :]
    dialog = dialog[: dialog.index("</dialog>")]

    assert _zeilen(dialog) == {"Ausnahmezeiträume": 1, "Termine der Reihe": 2}
    assert "auch 2 kommende" in dialog
    assert 'value="toggle_schedule"' in dialog
    assert "Pausieren" in dialog
    assert 'value="delete_schedule"' in dialog
    assert 'x-data="deleteConfirmation"' in dialog
    assert "confirmAction(" not in html


@pytest.mark.django_db
def test_reihe_nur_mit_ihrem_namen_loeschen_termine_bleiben(
    org: Any, reihe: FactionMeetingSchedule, vorsitz: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    daten = {"section": "delete_schedule", "schedule_id": str(reihe.id)}

    client.post(_einstellungen(org), {**daten, "confirmation": "Reihe"})
    assert FactionMeetingSchedule.objects.filter(pk=reihe.pk).exists()

    client.post(_einstellungen(org), {**daten, "confirmation": "wöchentliche fraktionssitzung"})

    assert not FactionMeetingSchedule.objects.filter(pk=reihe.pk).exists()
    assert FactionMeeting.objects.filter(organization=org, title__startswith="Termin", schedule=None).count() == 2
    eintrag = FactionAuditLog.objects.get(model_name="FactionMeetingSchedule", action="delete", object_id=reihe.pk)
    assert eintrag.changes == {
        "Ausnahmezeiträume (mitgelöscht)": {"alt": 1, "neu": 0},
        "Termine der Reihe (Bezug entfernt)": {"alt": 2, "neu": 0},
    }


@pytest.mark.django_db
def test_reihe_einer_anderen_organisation_bleibt(
    org: Any, reihe: FactionMeetingSchedule, client_for: Any, make_member: Any
) -> None:
    from apps.common.tests.factories import OrganizationFactory

    andere: Any = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")  # type: ignore[no-untyped-call]
    admin = make_member(andere, [], email="admin@andere.example.org", is_admin=True)

    client_for(admin.user).post(
        _einstellungen(andere),
        {"section": "delete_schedule", "schedule_id": str(reihe.id), "confirmation": reihe.name},
    )

    assert FactionMeetingSchedule.objects.filter(pk=reihe.pk).exists()
