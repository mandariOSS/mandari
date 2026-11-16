# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Persönlicher Datenexport: Inhalte aus Fraktionssitzungen nur, soweit die Person sie heute sehen darf (Teil von #1172).

Der Export nennt, woran die Person beteiligt war. Titel und Texte stehen darin nur, wenn sie die Stelle auch in der
Oberfläche öffnen dürfte; sonst bleiben Art, Rolle und Zeitpunkt, und ein Platzhalter ersetzt den Inhalt:

- Protokolleinträge, in denen sie als Rednerin oder Zuständige steht: nicht aus Sitzungen, die sie nicht sieht
  (Sitzungsarten), nicht aus nichtöffentlichen TOPs ohne Vereidigung; Einträge ohne TOP gelten als intern.
- Benachrichtigungen: Erwähnungen zu einem TOP, der für sie nichtöffentlich ist, wie in der Glocke.
- Aufgaben aus einem nichtöffentlichen TOP und die Vermerke daran: nur für Vereidigte.

Dazu der Text einer neuen Erwähnung: Bei nichtöffentlichen TOPs nennt er weder Nummer noch Titel.

Zu jedem Fall gehört der Normalfall: Wer die Stelle sehen darf, findet sie unverändert im Export.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.common.tests.factories import RoleFactory
from apps.tenants.models import Membership
from apps.work.faction import erwaehnungen
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingType,
    FactionProtocolEntry,
)
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization import export_service
from apps.work.tasks.models import Task, TaskActivity, TaskAttachment

pytestmark = pytest.mark.django_db

GEHEIM = "Grundstück Musterweg"
RECHTE = ["dashboard.view", "faction.view_public", "faction.view_non_public", "tasks.view", "tasks.create"]

hub = cast(Any, NotificationHub)


def _mitglied(org: Any, make_member: Any, name: str, *, vereidigt: bool) -> Any:
    member = make_member(org, RECHTE, email=f"{name}@example.org")
    if vereidigt:
        member.is_sworn_in = True
        member.save(update_fields=["is_sworn_in"])
    return member


def _entziehen(member: Any) -> Any:
    """Vereidigung entzogen; die Mitgliedschaft wie in einer neuen Anfrage."""
    Membership.objects.filter(pk=member.pk).update(is_sworn_in=False)
    return Membership.objects.get(pk=member.pk)


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "vorsitz", vereidigt=True)


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "person", vereidigt=True)


@pytest.fixture
def unvereidigt(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "unvereidigt", vereidigt=False)


@pytest.fixture
def sitzung(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung im November", start=timezone.now(), status="ongoing"
    )


@pytest.fixture
def top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public", order=1)


@pytest.fixture
def noe_top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(
        meeting=sitzung, number="N1", title=f"Ankauf {GEHEIM}", visibility="internal", order=2
    )


@pytest.fixture
def kernteam(org: Any) -> FactionMeetingType:
    """Sitzungsart, die nur ihre Teilnehmenden sehen (hier: eine Rolle, die niemand aus den Tests hat)."""
    rolle = cast(Any, RoleFactory)(organization=org, name="Kernteam", permissions=[])
    return FactionMeetingType.objects.create(
        organization=org,
        name="Vorstand",
        position=1,
        participants_mode="role",
        participants_role=rolle,
        visibility="participants",
    )


def _eintrag(meeting: FactionMeeting, inhalt: str, item: FactionAgendaItem | None = None, **felder: Any) -> Any:
    eintrag = cast(Any, FactionProtocolEntry)(meeting=meeting, agenda_item=item, **felder)
    eintrag.set_content_encrypted(inhalt)
    eintrag.save()
    return eintrag


def _eintraege(member: Any, org: Any) -> list[dict[str, Any]]:
    return cast(
        "list[dict[str, Any]]", export_service.DsgvoExportService()._collect_faction(member, org)["protocol_entries"]
    )


# --- Protokolleinträge ----------------------------------------------------------------------------------------------


def test_eintrag_aus_nicht_sichtbarer_sitzung_ohne_titel_und_inhalt(
    org: Any, unvereidigt: Any, kernteam: FactionMeetingType
) -> None:
    vorstand = FactionMeeting.objects.create(
        organization=org,
        meeting_type=kernteam,
        title=f"Vorstand {GEHEIM}",
        start=timezone.now() - timedelta(days=1),
        status="completed",
        meeting_number=1,
    )
    punkt = FactionAgendaItem.objects.create(meeting=vorstand, number="1", title="Lage", visibility="public", order=1)
    _eintrag(vorstand, f"Kaufpreis {GEHEIM} klären", punkt, entry_type="action", action_assignee=unvereidigt)

    gesperrt = _eintraege(unvereidigt, org)

    assert GEHEIM not in str(gesperrt)
    assert gesperrt[0]["meeting"] == gesperrt[0]["content"] == export_service.NO_ACCESS_MEETING
    assert gesperrt[0]["role"] == "Aufgabe" and gesperrt[0]["created_at"], "Art, Rolle und Zeitpunkt bleiben"

    # Normalfall: Wer zur Sitzung eingeladen ist, sieht sie und findet Titel und Text im Export
    FactionAttendance.objects.create(meeting=vorstand, membership=unvereidigt, status="present")
    sichtbar = _eintraege(unvereidigt, org)
    assert sichtbar[0]["meeting"] == f"Vorstand {GEHEIM}"
    assert sichtbar[0]["content"] == f"Kaufpreis {GEHEIM} klären"


def test_eintraege_aus_dem_nichtoeffentlichen_teil_und_ohne_top_nur_fuer_vereidigte(
    org: Any, person: Any, sitzung: FactionMeeting, top: FactionAgendaItem, noe_top: FactionAgendaItem
) -> None:
    _eintrag(sitzung, "Wortbeitrag zum Haushalt", top, entry_type="speech", speaker=person)
    _eintrag(sitzung, f"Wortbeitrag zum {GEHEIM}", noe_top, entry_type="speech", speaker=person)
    _eintrag(sitzung, f"Nachtrag zum {GEHEIM}", entry_type="note", speaker=person)

    # Normalfall: Vereidigte finden alle drei Einträge mit Inhalt
    assert {e["content"] for e in _eintraege(person, org)} == {
        "Wortbeitrag zum Haushalt",
        f"Wortbeitrag zum {GEHEIM}",
        f"Nachtrag zum {GEHEIM}",
    }

    nachher = _eintraege(_entziehen(person), org)

    assert GEHEIM not in str(nachher)
    assert sorted(e["content"] for e in nachher) == sorted(
        ["Wortbeitrag zum Haushalt", export_service.NO_ACCESS_ITEM, export_service.NON_PUBLIC_CONTENT]
    )
    assert {e["meeting"] for e in nachher} == {"Fraktionssitzung im November"}, "Die Sitzung selbst bleibt sichtbar"


def test_abschnitt_fraktion_prueft_sitzungen_nicht_je_zeile(
    org: Any, unvereidigt: Any, kernteam: FactionMeetingType
) -> None:
    """Mehr Einträge aus mehr Sitzungen lösen keine weiteren Abfragen aus."""

    def anlegen(nummer: int) -> None:
        meeting = FactionMeeting.objects.create(
            organization=org,
            meeting_type=kernteam,
            title=f"Vorstand {nummer}",
            start=timezone.now() - timedelta(days=nummer + 1),
            status="completed",
            meeting_number=nummer + 1,
        )
        _eintrag(meeting, "Vertraulich", entry_type="action", action_assignee=unvereidigt)
        FactionAgendaItem.objects.create(
            meeting=meeting, number="", title="Vorschlag", order=1, proposal_status="proposed", proposed_by=unvereidigt
        )

    def abfragen() -> int:
        member = Membership.objects.get(pk=unvereidigt.pk)
        member.has_permission("faction.view_public")  # Rechte sind in einer Anfrage schon geladen
        with CaptureQueriesContext(connection) as erfasst:
            export_service.DsgvoExportService()._collect_faction(member, org)
        return len(erfasst)

    for nummer in range(2):
        anlegen(nummer)
    wenige = abfragen()
    for nummer in range(2, 8):
        anlegen(nummer)

    assert abfragen() == wenige


# --- Benachrichtigungen und Text der Erwähnung ------------------------------------------------------------------------


def _erwaehnung_im_bestand(item: FactionAgendaItem, empfaenger: Any, von: Any) -> Notification:
    """Gespeicherte Erwähnung mit TOP-Titel im Text, wie sie im Bestand steht."""
    meeting = item.meeting
    angelegt = hub.send(
        recipient=empfaenger,
        notification_type=NotificationType.FACTION_MENTION,
        title="Erwähnung im Protokoll",
        message=f"Jemand hat Sie im Protokoll zu TOP {item.number} „{item.title}“ ({meeting.title}) erwähnt.",
        link=f"/work/{meeting.organization.slug}/faction/{meeting.pk}/?top={item.pk}",
        actor=von,
        metadata={"meeting_id": str(meeting.pk), "item_id": str(item.pk)},
        send_email=False,
        event_key=f"fs-erwaehnung:{item.pk}",
    )
    assert angelegt is not None
    return cast(Notification, angelegt)


def _nachrichten(member: Any) -> list[dict[str, Any]]:
    return cast(
        "list[dict[str, Any]]", export_service.DsgvoExportService()._collect_notifications(member)["notifications"]
    )


def test_export_der_benachrichtigungen_ohne_erwaehnung_zu_nichtoeffentlichem_top(
    org: Any, vorsitz: Any, person: Any, unvereidigt: Any, top: FactionAgendaItem, noe_top: FactionAgendaItem
) -> None:
    _erwaehnung_im_bestand(noe_top, person, vorsitz)
    _erwaehnung_im_bestand(top, person, vorsitz)
    _erwaehnung_im_bestand(top, unvereidigt, vorsitz)
    aufgabe = Task.objects.create(organization=org, title="Plakate kleben", created_by=vorsitz, assigned_to=person)
    hub.notify_task_assigned(aufgabe, person, vorsitz)

    # Normalfall: Vereidigte finden die Erwähnung mit ihrem Text; Erwähnungen zu öffentlichen TOPs bleiben für alle
    assert GEHEIM in str(_nachrichten(person))
    assert "Haushalt" in str(_nachrichten(unvereidigt))

    nachher = _nachrichten(_entziehen(person))

    assert GEHEIM not in str(nachher)
    gesperrt = [n for n in nachher if n["message"] == export_service.NO_ACCESS_ITEM]
    assert len(gesperrt) == 1 and gesperrt[0]["title"] == export_service.NO_ACCESS_ITEM
    assert gesperrt[0]["type"] == NotificationType.FACTION_MENTION and gesperrt[0]["created_at"]
    assert "Haushalt" in str(nachher) and "Plakate kleben" in str(nachher), "Alles andere steht weiter im Export"

    # Ein TOP, der nach der Erwähnung nichtöffentlich wird, verschwindet auch bei nie vereidigten Personen
    FactionAgendaItem.objects.filter(pk=top.pk).update(visibility="internal")
    assert "Haushalt" not in str(_nachrichten(Membership.objects.get(pk=unvereidigt.pk)))


def test_neue_erwaehnung_zu_nichtoeffentlichem_top_nennt_weder_nummer_noch_titel(
    vorsitz: Any, person: Any, top: FactionAgendaItem, noe_top: FactionAgendaItem
) -> None:
    html = f'<p><span class="erwaehnung" data-type="mention" data-id="{person.pk}">@Person</span> prüft das.</p>'

    intern = erwaehnungen.benachrichtigen(noe_top, html, von=vorsitz)
    oeffentlich = erwaehnungen.benachrichtigen(top, html, von=vorsitz)

    assert len(intern) == len(oeffentlich) == 1
    assert GEHEIM not in intern[0].message and "N1" not in intern[0].message
    assert "nichtöffentlichen TOP" in intern[0].message and "Fraktionssitzung im November" in intern[0].message
    assert f"?top={noe_top.pk}" in intern[0].link
    # Normalfall: Bei öffentlichen TOPs nennt die Nachricht Nummer und Titel wie bisher
    assert "TOP 1 „Haushalt“ (Fraktionssitzung im November)" in oeffentlich[0].message


# --- Aufgaben aus einem nichtöffentlichen TOP -----------------------------------------------------------------------


def _aufgabe(item: FactionAgendaItem, erstellt_von: Any, zugewiesen: Any, titel: str) -> Task:
    return Task.objects.create(
        organization=erstellt_von.organization,
        title=titel,
        description=f"Aus Fraktionssitzung: {item.meeting.title}\nTOP: {item.number} {item.title}",
        created_by=erstellt_von,
        assigned_to=zugewiesen,
        related_faction_meeting=item.meeting,
        related_faction_agenda_item=item,
    )


def test_aufgabe_aus_nichtoeffentlichem_top_steht_nur_fuer_vereidigte_im_export(
    org: Any, vorsitz: Any, person: Any, noe_top: FactionAgendaItem
) -> None:
    aufgabe = _aufgabe(noe_top, vorsitz, person, f"Gutachten {GEHEIM} einholen")
    TaskActivity.objects.create(task=aufgabe, actor=person, activity_type="comment", content="Mein Kommentar")
    TaskActivity.objects.create(task=aufgabe, actor=person, activity_type="status_changed")
    TaskAttachment.objects.create(
        task=aufgabe,
        file="tasks/gutachten.pdf",
        filename="gutachten.pdf",
        mime_type="application/pdf",
        uploaded_by=person,
    )
    dienst = export_service.DsgvoExportService()

    # Normalfall: Die vereidigte, zugewiesene Person findet Aufgabe und Vermerke mit Titel
    vorher = dienst._collect_tasks(person, org)
    assert vorher[0]["title"] == f"Gutachten {GEHEIM} einholen" and GEHEIM in vorher[0]["description"]
    assert GEHEIM in str([v for v in dienst._collect_vermerke(person, org) if v["area"] == "Aufgaben"])

    ohne = _entziehen(person)
    nachher = dienst._collect_tasks(ohne, org)
    vermerke = [v for v in dienst._collect_vermerke(ohne, org) if v["area"] == "Aufgaben"]

    assert GEHEIM not in str(nachher) and GEHEIM not in str(vermerke)
    assert nachher[0]["title"] == export_service.NO_ACCESS_TASK and nachher[0]["description"] == ""
    assert [c["content"] for c in nachher[0]["comments"]] == ["Mein Kommentar"], "Eigene Kommentare bleiben"
    assert len(vermerke) == 2 and all(v["object"] == export_service.NO_ACCESS_TASK for v in vermerke)


def test_aufgaben_im_export_ohne_abfrage_je_aufgabe(
    org: Any, vorsitz: Any, unvereidigt: Any, top: FactionAgendaItem, noe_top: FactionAgendaItem
) -> None:
    def abfragen() -> int:
        member = Membership.objects.get(pk=unvereidigt.pk)
        member.has_permission("tasks.view")  # Rechte sind in einer Anfrage schon geladen
        with CaptureQueriesContext(connection) as erfasst:
            eintraege = export_service.DsgvoExportService()._collect_tasks(member, org)
        assert {e["title"] for e in eintraege} == {export_service.NO_ACCESS_TASK, "Offen"}
        return len(erfasst)

    for item, titel in ((noe_top, "Gesperrt"), (top, "Offen")):
        _aufgabe(item, vorsitz, unvereidigt, titel)
    wenige = abfragen()
    for _nummer in range(5):
        _aufgabe(noe_top, vorsitz, unvereidigt, "Gesperrt")
        _aufgabe(top, vorsitz, unvereidigt, "Offen")

    assert abfragen() == wenige
