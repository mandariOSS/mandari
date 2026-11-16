# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben und Protokolleinträge aus dem nichtöffentlichen Teil einer Fraktionssitzung (Teil von #1172).

- Eine Aufgabe aus einem nichtöffentlichen TOP gehört zum nichtöffentlichen Teil: Board, Panel, Anhänge, Export und
  alle Aktionen stehen nur Vereidigten offen, auch der erstellenden und der zugewiesenen Person, und zwar am
  heutigen Stand von TOP und Mitgliedschaft. Unterpunkte erben den Stand vom übergeordneten TOP.
- Zuständig für eine solche Aufgabe wird im Panel nur, wer sie öffnen darf.
- Die Übernahme aus Protokollen bietet Einträge ohne TOP nur Vereidigten an, wie Protokollseite und Niederschrift.

Zu jedem Fall gehört der Normalfall: Vereidigte sehen alles weiter, und Aufgaben ohne nichtöffentlichen TOP bleiben
für erstellende und zugewiesene Personen, wie sie waren.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Membership
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionProtocolEntry
from apps.work.tasks import selectors, services
from apps.work.tasks.models import Task, TaskAttachment, TaskShare

pytestmark = pytest.mark.django_db

TITEL = "Angebot der Firma Müller prüfen"
RECHTE = [
    "dashboard.view",
    "tasks.view",
    "tasks.create",
    "faction.view_public",
    "faction.view_non_public",
    "protocols.view_public",
    "protocols.view_full",
]


def _mitglied(org: Any, make_member: Any, name: str, *, vereidigt: bool, rechte: list[str] | None = None) -> Any:
    member = make_member(org, RECHTE if rechte is None else rechte, email=f"{name}@example.org")
    if vereidigt:
        member.is_sworn_in = True
        member.save(update_fields=["is_sworn_in"])
    return member


def _frisch(member: Any) -> Any:
    """Mitgliedschaft wie in einer neuen Anfrage (ohne zwischengespeicherte Rechte)."""
    return Membership.objects.get(pk=member.pk)


def _entziehen(member: Any) -> Any:
    Membership.objects.filter(pk=member.pk).update(is_sworn_in=False)
    return _frisch(member)


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
        organization=org, title="Fraktionssitzung", start=timezone.now(), status="ongoing", created_by=vorsitz
    )


@pytest.fixture
def noe_top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(
        meeting=sitzung, number="N1", title="Vergabe Schulsanierung", visibility="internal", order=2
    )


@pytest.fixture
def top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public", order=1)


def _aufgabe(item: FactionAgendaItem | None, erstellt_von: Any, zugewiesen: Any, **felder: Any) -> Task:
    """Aufgabe, wie sie die Sitzungsansicht aus einem TOP anlegt (``faction.sitzung.aufgaben_anlegen``)."""
    werte: dict[str, Any] = {"title": TITEL, "created_by": erstellt_von, "assigned_to": zugewiesen, **felder}
    if item is not None:
        werte["related_faction_meeting"] = item.meeting
        werte["related_faction_agenda_item"] = item
        werte["description"] = f"Aus Fraktionssitzung: {item.meeting.title}\nTOP: {item.number} {item.title}"
    return Task.objects.create(organization=erstellt_von.organization, **werte)


def _in_listen(org: Any, member: Any, aufgabe: Task) -> tuple[bool, bool]:
    basis = selectors.card_queryset(org)
    return (
        aufgabe in selectors.own_tasks(member, base=basis),
        aufgabe in selectors.visible_tasks(org, member, base=basis),
    )


def _panel(client: Any, org: Any, aufgabe: Task) -> Any:
    return client.get(reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": aufgabe.id}))


def _aktion(client: Any, org: Any, aufgabe: Task, **daten: str) -> Any:
    url = reverse("work:task_panel_action", kwargs={"org_slug": org.slug, "task_id": aufgabe.id})
    return client.post(url, daten)


# --- Aufgabe aus einem nichtöffentlichen TOP ------------------------------------------------------------------------


@pytest.mark.parametrize("rolle", ["zugewiesen", "erstellt"])
def test_aufgabe_aus_nichtoeffentlichem_top_schliesst_mit_dem_entzug_der_vereidigung(
    org: Any, vorsitz: Any, person: Any, noe_top: FactionAgendaItem, client_for: Any, rolle: str
) -> None:
    aufgabe = _aufgabe(noe_top, vorsitz, person) if rolle == "zugewiesen" else _aufgabe(noe_top, person, vorsitz)
    anhang = TaskAttachment.objects.create(
        task=aufgabe,
        file=SimpleUploadedFile("angebot.pdf", b"%PDF-1.4 Angebot"),
        filename="angebot.pdf",
        mime_type="application/pdf",
        uploaded_by=vorsitz,
    )
    client = client_for(person.user)
    kwargs = {"org_slug": org.slug}
    download = reverse(
        "work:task_attachment_download", kwargs={**kwargs, "task_id": aufgabe.id, "attachment_id": anhang.id}
    )

    # Normalfall: Vereidigte öffnen die Aufgabe wie bisher, als zugewiesene wie als erstellende Person
    assert aufgabe.can_access(person) and services.can_edit_task(aufgabe, person)
    assert _in_listen(org, person, aufgabe) == (True, True)
    assert _panel(client, org, aufgabe).status_code == 200
    assert client.get(download).status_code == 200
    assert TITEL in client.get(reverse("work:tasks", kwargs=kwargs)).content.decode()

    ohne = _entziehen(person)
    aufgabe = Task.objects.get(pk=aufgabe.pk)

    assert not aufgabe.can_access(ohne) and not services.can_edit_task(aufgabe, ohne)
    assert _in_listen(org, ohne, aufgabe) == (False, False)
    assert _panel(client, org, aufgabe).status_code == 403
    assert client.get(download).status_code == 403
    assert _aktion(client, org, aufgabe, action="add_comment", content="Nachfrage").status_code == 403
    for ansicht in ("my", "all"):
        assert TITEL not in client.get(reverse("work:tasks", kwargs=kwargs), {"view": ansicht}).content.decode()
    export = client.get(reverse("work:tasks_export", kwargs=kwargs), {"format": "json"}).content.decode()
    assert TITEL not in export and "Schulsanierung" not in export
    abhaken = client.post(
        reverse("work:tasks_api", kwargs=kwargs), {"action": "toggle_complete", "task_id": str(aufgabe.id)}
    )
    assert abhaken.status_code == 403
    assert not Task.objects.get(pk=aufgabe.pk).is_completed
    assert not aufgabe.activities.filter(activity_type="comment").exists()

    # Die andere, weiter vereidigte Person behält alles
    assert aufgabe.can_access(vorsitz) and _in_listen(org, vorsitz, aufgabe) == (True, True)
    assert _panel(client_for(vorsitz.user), org, aufgabe).status_code == 200


def test_aufgaben_ohne_nichtoeffentlichen_top_bleiben_wie_sie_waren(
    org: Any, vorsitz: Any, unvereidigt: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    """Normalfall: Nicht-Vereidigte behalten eigene und zugewiesene Aufgaben ohne TOP und aus öffentlichen TOPs."""
    aus_top = _aufgabe(top, vorsitz, unvereidigt)
    ohne_top = _aufgabe(None, unvereidigt, unvereidigt, title="Plakate kleben")
    client = client_for(unvereidigt.user)

    for aufgabe in (aus_top, ohne_top):
        assert aufgabe.can_access(unvereidigt) and services.can_edit_task(aufgabe, unvereidigt)
        assert _in_listen(org, unvereidigt, aufgabe) == (True, True)
        assert _panel(client, org, aufgabe).status_code == 200
    services.toggle_completion(aus_top, unvereidigt)
    assert Task.objects.get(pk=aus_top.pk).is_completed


def test_top_nachtraeglich_nichtoeffentlich_schliesst_die_aufgabe_fuer_nicht_vereidigte(
    org: Any, vorsitz: Any, unvereidigt: Any, top: FactionAgendaItem, sitzung: FactionMeeting, client_for: Any
) -> None:
    unterpunkt = FactionAgendaItem.objects.create(
        meeting=sitzung, parent=top, number="1.1", title="Angebote", visibility="public", order=1
    )
    aufgabe = _aufgabe(unterpunkt, vorsitz, unvereidigt)
    assert aufgabe.can_access(unvereidigt) and _in_listen(org, unvereidigt, aufgabe) == (True, True)

    # Der übergeordnete TOP wird nichtöffentlich: Der Unterpunkt erbt den Stand, auch wenn an ihm „öffentlich“ steht
    FactionAgendaItem.objects.filter(pk=top.pk).update(visibility="internal")
    aufgabe = Task.objects.get(pk=aufgabe.pk)

    assert not aufgabe.can_access(unvereidigt) and not services.can_edit_task(aufgabe, unvereidigt)
    assert _in_listen(org, unvereidigt, aufgabe) == (False, False)
    assert _panel(client_for(unvereidigt.user), org, aufgabe).status_code == 403
    with pytest.raises(PermissionDenied):
        services.toggle_completion(aufgabe, unvereidigt)
    assert aufgabe.can_access(vorsitz) and _in_listen(org, vorsitz, aufgabe) == (True, True)


def test_organisationsweite_und_geteilte_aufgabe_aus_nichtoeffentlichem_top_nur_fuer_vereidigte(
    org: Any, make_member: Any, vorsitz: Any, person: Any, unvereidigt: Any, noe_top: FactionAgendaItem, client_for: Any
) -> None:
    leitung = _mitglied(org, make_member, "leitung", vereidigt=False, rechte=["tasks.view", "tasks.manage"])
    fuer_alle = _aufgabe(noe_top, vorsitz, vorsitz, visibility="organization")
    geteilt = _aufgabe(noe_top, vorsitz, vorsitz, visibility="shared", title="Rückfrage an das Rechtsamt")
    for empfaenger in (person, unvereidigt):
        TaskShare.objects.create(task=geteilt, membership=empfaenger, shared_by=vorsitz)

    for aufgabe in (fuer_alle, geteilt):
        assert aufgabe.can_access(person), "Vereidigte sehen die Aufgabe wie bisher"
        assert aufgabe in selectors.visible_tasks(org, person)
        for ohne in (unvereidigt, leitung):
            assert not aufgabe.can_access(ohne)
            assert aufgabe not in selectors.visible_tasks(org, ohne)
            assert not services.can_edit_task(aufgabe, ohne), "auch nicht mit dem Recht, Aufgaben zu verwalten"
        assert _panel(client_for(unvereidigt.user), org, aufgabe).status_code == 403
    assert geteilt in selectors.own_tasks(person, base=selectors.card_queryset(org))
    assert geteilt not in selectors.own_tasks(unvereidigt, base=selectors.card_queryset(org))


def test_listen_pruefen_den_top_in_der_abfrage(
    org: Any,
    vorsitz: Any,
    unvereidigt: Any,
    noe_top: FactionAgendaItem,
    top: FactionAgendaItem,
    django_assert_num_queries: Any,
) -> None:
    """Die Grenze steht in der Abfrage: Mehr Aufgaben lösen keine weiteren Abfragen aus."""
    for nummer in range(6):
        _aufgabe(noe_top, vorsitz, unvereidigt, title=f"Gesperrt {nummer}")
        _aufgabe(top, vorsitz, unvereidigt, title=f"Offen {nummer}")
    mitglied = _frisch(unvereidigt)
    mitglied.has_permission("tasks.view")  # Rechte sind in einer Anfrage schon geladen

    with django_assert_num_queries(1):
        titel = {a.title for a in selectors.own_tasks(mitglied, base=Task.objects.filter(organization=org))}
    with django_assert_num_queries(1):
        alle = {a.title for a in selectors.visible_tasks(org, mitglied)}

    assert titel == alle == {f"Offen {nummer}" for nummer in range(6)}


def test_startseite_und_aktivitaet_nennen_die_aufgabe_nur_vereidigten(
    org: Any, vorsitz: Any, person: Any, noe_top: FactionAgendaItem, client_for: Any
) -> None:
    """Auch die Listen außerhalb des Boards ziehen die Grenze: Startseite (bisher und neu) und Aktivitätsübersicht."""
    from apps.work.dashboard import selectors as start
    from apps.work.organization import selectors as profil

    _aufgabe(noe_top, vorsitz, person)
    _aufgabe(None, vorsitz, person, title="Plakate kleben")
    url = reverse("work:dashboard", kwargs={"org_slug": org.slug})

    def ausgaben(member: Any) -> str:
        seite = client_for(member.user).get(url)
        assert seite.status_code == 200
        return " ".join(
            [seite.content.decode(), str(start.fuer_sie(org, member)), str(profil.activity_timeline(org, member))]
        )

    assert ausgaben(person).count(TITEL) >= 3, "Vereidigte finden die Aufgabe in allen drei Listen"

    nachher = ausgaben(_entziehen(person))

    assert TITEL not in nachher
    assert nachher.count("Plakate kleben") >= 3, "Andere Aufgaben bleiben"


# --- Zuständigkeit im Panel -----------------------------------------------------------------------------------------


def _speichern(client: Any, org: Any, aufgabe: Task, zustaendig: Any) -> Any:
    return _aktion(
        client,
        org,
        aufgabe,
        action="update",
        title=aufgabe.title,
        description=aufgabe.description,
        status=aufgabe.status,
        priority=aufgabe.priority,
        visibility=aufgabe.visibility,
        assigned_to=str(zustaendig.pk),
    )


def test_panel_weist_aufgaben_aus_nichtoeffentlichem_top_nur_vereidigten_zu(
    org: Any,
    vorsitz: Any,
    person: Any,
    unvereidigt: Any,
    noe_top: FactionAgendaItem,
    top: FactionAgendaItem,
    client_for: Any,
) -> None:
    aufgabe = _aufgabe(noe_top, vorsitz, vorsitz)
    client = client_for(vorsitz.user)

    assert _speichern(client, org, aufgabe, unvereidigt).status_code == 422
    assert Task.objects.get(pk=aufgabe.pk).assigned_to_id == vorsitz.pk
    assert str(unvereidigt.pk) not in _panel(client, org, aufgabe).content.decode()

    # Normalfall: Vereidigte lassen sich weiter zuweisen, und bei anderen Aufgaben stehen alle Mitglieder zur Wahl
    assert _speichern(client, org, aufgabe, person).status_code == 200
    assert Task.objects.get(pk=aufgabe.pk).assigned_to_id == person.pk
    offen = _aufgabe(top, vorsitz, vorsitz)
    assert _speichern(client, org, offen, unvereidigt).status_code == 200
    assert Task.objects.get(pk=offen.pk).assigned_to_id == unvereidigt.pk


def test_panel_bleibt_speicherbar_wenn_die_zustaendige_person_nicht_mehr_vereidigt_ist(
    org: Any, vorsitz: Any, person: Any, noe_top: FactionAgendaItem, client_for: Any
) -> None:
    aufgabe = _aufgabe(noe_top, vorsitz, person)
    _entziehen(person)

    antwort = _speichern(client_for(vorsitz.user), org, Task.objects.get(pk=aufgabe.pk), person)

    assert antwort.status_code == 200, "Die bisher zuständige Person bleibt wählbar, die übrigen Felder speicherbar"


# --- Übernahme aus Protokollen: Einträge ohne TOP -------------------------------------------------------------------


def _eintrag(sitzung: FactionMeeting, inhalt: str, item: FactionAgendaItem | None = None, **felder: Any) -> Any:
    eintrag = cast(Any, FactionProtocolEntry)(meeting=sitzung, agenda_item=item, **felder)
    eintrag.set_content_encrypted(inhalt)
    eintrag.save()
    return eintrag


def test_uebernahme_bietet_eintraege_ohne_top_nur_vereidigten_an(
    org: Any, vorsitz: Any, unvereidigt: Any, sitzung: FactionMeeting, top: FactionAgendaItem, client_for: Any
) -> None:
    ohne_top = _eintrag(sitzung, "Vertrauliche Rückfrage beim Rechtsamt", entry_type="action")
    nachtrag = _eintrag(sitzung, "Vertraulicher Nachtrag zur Vergabe", entry_type="note")
    am_top = _eintrag(sitzung, "Stellungnahme zum Haushalt", top, entry_type="action")
    kwargs = {"org_slug": org.slug}
    client = client_for(unvereidigt.user)

    def angeboten(wer: Any) -> list[str]:
        liste = client_for(wer.user).get(reverse("work:tasks_import", kwargs=kwargs)).json()
        return [cast(str, zeile["id"]) for zeile in liste["items"]]

    def vorbelegt(wer: Any, eintrag: Any) -> str:
        seite = client_for(wer.user).get(reverse("work:task_create", kwargs=kwargs), {"from_protocol": str(eintrag.id)})
        return cast(str, seite.content.decode())

    assert angeboten(unvereidigt) == [str(am_top.id)], "Einträge an sichtbaren TOPs bleiben im Angebot"
    assert "Vertrauliche" not in vorbelegt(unvereidigt, ohne_top)
    assert "Vertraulicher" not in vorbelegt(unvereidigt, nachtrag)
    assert "Stellungnahme zum Haushalt" in vorbelegt(unvereidigt, am_top)
    antwort = client.post(reverse("work:tasks_import", kwargs=kwargs), {"entry_ids[]": [str(ohne_top.id)]})
    assert antwort.json()["created"] == 0
    assert not Task.objects.filter(related_protocol_entry=ohne_top).exists()

    # Normalfall: Vereidigte sehen und übernehmen Einträge ohne TOP wie bisher
    assert set(angeboten(vorsitz)) == {str(ohne_top.id), str(am_top.id)}
    assert "Vertraulicher Nachtrag zur Vergabe" in vorbelegt(vorsitz, nachtrag)
    assert services.import_protocol_entries(org, vorsitz, [str(ohne_top.id)]) == 1
    assert Task.objects.get(related_protocol_entry=ohne_top).title == "Vertrauliche Rückfrage beim Rechtsamt"
