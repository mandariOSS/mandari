# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliches gilt über die Elternkette, nicht nur über das gespeicherte Feld (Issue #64).

Ein Unterpunkt eines nichtöffentlichen TOPs ist nichtöffentlich, auch wenn an ihm selbst „öffentlich“ gespeichert ist
(``visibility.is_item_internal``). Diese Regel gilt hier für die Wege, die bisher nur das Feld lasen:

- Änderungshistorie: Einträge zu solchen Unterpunkten und zu Protokolleinträgen, Beschlüssen und Anlagen eines TOPs,
  der inzwischen nichtöffentlich ist, sehen Nicht-Vereidigte nur als „Gesperrte Information“.
- Verschieben in den nichtöffentlichen Teil nimmt alle Ebenen von Unterpunkten mit.
- Löschen: Ein TOP mit nichtöffentlichen Unterpunkten lässt sich nur löschen, wenn man diese sehen darf.
- Aufgaben: Zuständig für Aufgaben aus nichtöffentlichen TOPs (auch geerbt) ist nur, wer sie sehen darf.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import agenda, deletion
from apps.work.faction import services as faction_services
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAgendaItemAttachment,
    FactionAuditLog,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.faction.visibility import LOCKED_PLACEHOLDER
from apps.work.tasks.models import Task

HTMX = {"HTTP_HX_REQUEST": "true"}
VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.view_audit",
    "protocols.create",
    "protocols.edit",
    "protocols.view_public",
    "protocols.view_full",
    "tasks.view",
    "tasks.create",
]
DATEI = "angebot-firma-mueller.pdf"
BESCHLUSS = "Vergabe an die Firma Müller"


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def vereidigt(org: Any, make_member: Any) -> Any:
    m = make_member(org, ["faction.view_public", "faction.view_non_public", "tasks.view"], email="rat@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def pruefer(org: Any, make_member: Any) -> Any:
    """Liest die Änderungshistorie und verwaltet Tagesordnungen, ist aber nicht vereidigt."""
    rechte = ["faction.view_public", "faction.view_audit", "faction.manage", "tasks.view"]
    return make_member(org, rechte, email="pruefer@example.org")


@pytest.fixture
def meeting(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now(), status="ongoing", created_by=vorsitz
    )


def _top(meeting: FactionMeeting, nummer: str, titel: str, **felder: Any) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=meeting, number=nummer, title=titel, **felder)


def _historie(client_for: Any, member: Any) -> str:
    url = reverse("work:faction_audit", kwargs={"org_slug": member.organization.slug})
    return cast(str, client_for(member.user).get(url).content.decode())


def _aktion(client_for: Any, member: Any, meeting: FactionMeeting, **daten: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.pk})
    return client_for(member.user).post(url, daten, **HTMX)


def _panel_url(top: FactionAgendaItem, name: str = "work:faction_item_panel_action") -> str:
    return reverse(
        name, kwargs={"org_slug": top.meeting.organization.slug, "meeting_id": top.meeting_id, "item_id": top.pk}
    )


def _panel(client_for: Any, member: Any, top: FactionAgendaItem, **daten: str) -> Any:
    return client_for(member.user).post(_panel_url(top), daten, **HTMX)


# ---- Änderungshistorie ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_eintraege_zu_unterpunkten_nichtoeffentlicher_tops_sind_gesperrt(
    meeting: FactionMeeting, vorsitz: Any, pruefer: Any, client_for: Any
) -> None:
    eltern = _top(meeting, "NÖ 1", "Vertragssache", visibility="internal")
    kind = _top(meeting, "NÖ 1.1", "Kaufpreis Hafenstraße", visibility="public", parent=eltern)

    kind.title = "Kaufpreis Hafenstraße 12"
    kind.save()

    eintraege = FactionAuditLog.objects.filter(model_name="FactionAgendaItem", object_id=kind.pk)
    assert eintraege.count() == 2 and all(e.is_internal for e in eintraege)
    assert "Hafenstraße" not in _historie(client_for, pruefer)
    assert "Hafenstraße" in _historie(client_for, vorsitz)


@pytest.mark.django_db
def test_historie_sperrt_nach_dem_heutigen_stand_der_elternkette(
    meeting: FactionMeeting, vorsitz: Any, pruefer: Any, client_for: Any
) -> None:
    """Bestand: Der TOP darüber ist nichtöffentlich, am Unterpunkt und in seinen Einträgen steht noch „öffentlich“."""
    eltern = _top(meeting, "2", "Liegenschaften", visibility="public")
    kind = _top(meeting, "2.1", "Kaufpreis Hafenstraße", visibility="public", parent=eltern)
    assert "Hafenstraße" in _historie(client_for, pruefer)

    # Ohne Speichern: wie ein Unterpunkt, den eine frühere Fassung beim Verschieben nicht mitgenommen hat
    FactionAgendaItem.objects.filter(pk=eltern.pk).update(visibility="internal")

    assert not FactionAuditLog.objects.filter(object_id=kind.pk, is_internal=True).exists()
    html = _historie(client_for, pruefer)
    assert "Hafenstraße" not in html and LOCKED_PLACEHOLDER in html
    assert "Hafenstraße" in _historie(client_for, vorsitz)


@pytest.mark.django_db
def test_anlage_und_beschluss_bleiben_nicht_lesbar_wenn_der_top_nichtoeffentlich_wird(
    meeting: FactionMeeting, vorsitz: Any, pruefer: Any, client_for: Any
) -> None:
    top = _top(meeting, "2", "Vergabe Winterdienst", visibility="public", order=1)
    anlage = FactionAgendaItemAttachment.objects.create(
        agenda_item=top,
        file=SimpleUploadedFile(DATEI, b"%PDF-1.4"),
        filename=DATEI,
        mime_type="application/pdf",
        file_size=8,
        uploaded_by=vorsitz,
    )
    stimmen = {"votes_yes": 5, "votes_no": 0, "votes_abstain": 0, "result": "accepted"}
    cast(Any, faction_services).record_decision(top, vorsitz, decision_text="Vergabe offen", **stimmen)
    cast(Any, faction_services).record_decision(top, vorsitz, decision_text=BESCHLUSS, **stimmen)
    vorher = _historie(client_for, pruefer)
    assert DATEI in vorher and BESCHLUSS in vorher

    agenda.move_to_part(meeting, top, "internal")

    # Die Einträge der Anlage und des Beschlusses tragen weiter den Stand ihres Schreibens
    assert not FactionAuditLog.objects.filter(object_id=anlage.pk, is_internal=True).exists()
    html = _historie(client_for, pruefer)
    assert DATEI not in html and BESCHLUSS not in html and "Winterdienst" not in html
    sicht_vorsitz = _historie(client_for, vorsitz)
    assert DATEI in sicht_vorsitz and BESCHLUSS in sicht_vorsitz


@pytest.mark.django_db
def test_nichtoeffentlicher_teil_nimmt_alle_ebenen_von_unterpunkten_mit(meeting: FactionMeeting) -> None:
    top = _top(meeting, "2", "Liegenschaften", visibility="public", order=1)
    kind = _top(meeting, "2.1", "Hafenstraße", visibility="public", parent=top, order=1)
    enkel = _top(meeting, "2.1.1", "Kaufpreis", visibility="public", parent=kind, order=1)

    agenda.move_to_part(meeting, top, "internal")

    for item in (top, kind, enkel):
        item.refresh_from_db()
        assert item.visibility == "internal", item.title
    assert FactionAuditLog.objects.filter(object_id=enkel.pk, is_internal=True).exists()


@pytest.mark.django_db
def test_mitgeloeschte_unterpunkte_tragen_den_stand_ihres_tops(meeting: FactionMeeting, vorsitz: Any) -> None:
    """Beim Löschen ist der übergeordnete TOP schon entfernt: Seine Historie sagt, ob er nichtöffentlich war."""
    offen = _top(meeting, "2", "Verkehr", visibility="public", order=1)
    offen_kind = _top(meeting, "2.1", "Radweg", visibility="public", parent=offen, order=1)
    geheim = _top(meeting, "NÖ 1", "Vertragssache", visibility="internal", order=2)
    geheim_kind = _top(meeting, "NÖ 1.1", "Kaufpreis", visibility="public", parent=geheim, order=1)

    deletion.delete_agenda_item(offen, "Verkehr", vorsitz)
    deletion.delete_agenda_item(geheim, "Vertragssache", vorsitz)

    def geloescht(pk: Any) -> FactionAuditLog:
        return FactionAuditLog.objects.get(object_id=pk, action="delete")

    assert geloescht(offen_kind.pk).is_internal is False
    assert geloescht(geheim_kind.pk).is_internal is True


# ---- Löschen -------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_top_mit_nichtoeffentlichem_unterpunkt_loescht_nur_wer_ihn_sehen_darf(
    meeting: FactionMeeting, vorsitz: Any, pruefer: Any, client_for: Any
) -> None:
    top = _top(meeting, "2", "Verkehr", visibility="public", order=1)
    kind = _top(meeting, "2.1", "Vertrauliche Trassenfrage", visibility="internal", parent=top, order=1)
    eintrag: Any = FactionProtocolEntry(meeting=meeting, agenda_item=kind, entry_type="note", created_by=vorsitz)
    eintrag.set_content_encrypted("Vertraulicher Eintrag")
    eintrag.save()

    vorschau = _aktion(client_for, pruefer, meeting, action="delete_item_preview", item_id=str(top.pk))
    antwort = _aktion(client_for, pruefer, meeting, action="delete_item", item_id=str(top.pk), confirmation="Verkehr")

    assert vorschau.status_code == 403 and antwort.status_code == 403
    assert antwort.content.decode() == deletion.HIDDEN_SUBITEMS
    assert FactionAgendaItem.objects.filter(pk__in=[top.pk, kind.pk]).count() == 2
    assert FactionProtocolEntry.objects.filter(pk=eintrag.pk).exists()
    with pytest.raises(deletion.DeletionNotAllowedError):
        deletion.delete_agenda_item(top, "Verkehr", pruefer)

    # Wer den nichtöffentlichen Teil sieht, löscht wie bisher mit der ausdrücklichen Eingabe
    antwort = _aktion(client_for, vorsitz, meeting, action="delete_item", item_id=str(top.pk), confirmation="Verkehr")
    assert antwort.status_code == 200
    assert not FactionAgendaItem.objects.filter(pk__in=[top.pk, kind.pk]).exists()


@pytest.mark.django_db
def test_top_mit_oeffentlichen_unterpunkten_loescht_die_verwaltung_wie_bisher(
    meeting: FactionMeeting, pruefer: Any, client_for: Any
) -> None:
    top = _top(meeting, "2", "Verkehr", visibility="public", order=1)
    _top(meeting, "2.1", "Radweg", visibility="public", parent=top, order=1)

    antwort = _aktion(client_for, pruefer, meeting, action="delete_item", item_id=str(top.pk), confirmation="Verkehr")

    assert antwort.status_code == 200
    assert not meeting.agenda_items.exists()


# ---- Aufgaben aus nichtöffentlichen TOPs ---------------------------------------------------------------------


@pytest.fixture
def tops(meeting: FactionMeeting) -> dict[str, FactionAgendaItem]:
    geheim = _top(meeting, "NÖ 1", "Vertragssache", visibility="internal", order=2)
    return {
        "offen": _top(meeting, "2", "Verkehr", visibility="public", order=1),
        "geheim": geheim,
        "erbt": _top(meeting, "NÖ 1.1", "Kaufpreis", visibility="public", parent=geheim, order=1),
    }


@pytest.mark.django_db
def test_einladung_nennt_geerbt_nichtoeffentliche_unterpunkte_nur_vereidigten(
    meeting: FactionMeeting, tops: dict[str, FactionAgendaItem]
) -> None:
    """Die Einladungsmail teilt wie das PDF nach der Elternkette, nicht nach dem Feld des Unterpunkts."""
    liste = cast(Any, faction_services).invitation_agenda

    oeffentlich, intern = liste(meeting, include_internal=False)
    assert [t.title for t in oeffentlich] == ["Verkehr"] and intern == []

    oeffentlich, intern = liste(meeting, include_internal=True)
    assert [t.title for t in oeffentlich] == ["Verkehr"]
    assert [t.title for t in intern] == ["Vertragssache", "Kaufpreis"]


@pytest.mark.django_db
@pytest.mark.parametrize("welcher", ["geheim", "erbt"])
def test_aufgabe_aus_nichtoeffentlichem_top_nur_an_vereidigte(
    tops: dict[str, FactionAgendaItem], vorsitz: Any, vereidigt: Any, pruefer: Any, client_for: Any, welcher: str
) -> None:
    top = tops[welcher]

    abgelehnt = _panel(
        client_for, vorsitz, top, action="create_task", title="Angebot prüfen", assigned_to=str(pruefer.pk)
    )

    assert abgelehnt.status_code == 400
    assert abgelehnt.content.decode() == faction_services.ZUSTAENDIG_NUR_VEREIDIGTE
    assert not Task.objects.exists()

    erlaubt = _panel(
        client_for, vorsitz, top, action="create_task", title="Angebot prüfen", assigned_to=str(vereidigt.pk)
    )
    assert erlaubt.status_code == 200
    assert Task.objects.get().assigned_to_id == vereidigt.pk
    # Zur Auswahl stehen nur Personen, die den TOP sehen dürfen
    auswahl = {
        m.pk
        for m in client_for(vorsitz.user).get(_panel_url(top, "work:faction_item_panel")).context["available_members"]
    }
    assert auswahl == {vorsitz.pk, vereidigt.pk}


@pytest.mark.django_db
@pytest.mark.parametrize("welcher", ["geheim", "erbt"])
def test_aufgaben_eintrag_am_nichtoeffentlichen_top_nur_fuer_vereidigte(
    meeting: FactionMeeting,
    tops: dict[str, FactionAgendaItem],
    vorsitz: Any,
    vereidigt: Any,
    pruefer: Any,
    client_for: Any,
    welcher: str,
) -> None:
    top = tops[welcher]
    eintrag = {"action": "add_entry", "entry_type": "action", "content": "Angebot einholen"}

    am_panel = _panel(client_for, vorsitz, top, action_assignee=str(pruefer.pk), **eintrag)
    in_der_sitzung = _aktion(
        client_for, vorsitz, meeting, agenda_item_id=str(top.pk), action_assignee=str(pruefer.pk), **eintrag
    )

    assert am_panel.status_code == 400 and in_der_sitzung.status_code == 400
    assert not FactionProtocolEntry.objects.exists()

    erlaubt = _panel(client_for, vorsitz, top, action_assignee=str(vereidigt.pk), **eintrag)
    assert erlaubt.status_code == 200
    assert FactionProtocolEntry.objects.get().action_assignee_id == vereidigt.pk


@pytest.mark.django_db
def test_aufgabe_aus_altem_eintrag_prueft_die_zustaendige_person_erneut(
    meeting: FactionMeeting,
    tops: dict[str, FactionAgendaItem],
    vorsitz: Any,
    vereidigt: Any,
    pruefer: Any,
    client_for: Any,
) -> None:
    """Der Eintrag ist älter als die Regel, oder sein TOP ging erst später in den nichtöffentlichen Teil."""

    def eintrag(top: FactionAgendaItem | None, person: Any) -> Any:
        neu: Any = FactionProtocolEntry(
            meeting=meeting, agenda_item=top, entry_type="action", created_by=vorsitz, action_assignee=person
        )
        neu.set_content_encrypted("Vertrauliches Angebot einholen")
        neu.save()
        return neu

    for top in (tops["erbt"], None):
        alt = eintrag(top, pruefer)
        antwort = _aktion(client_for, vorsitz, meeting, action="create_task", entry_id=str(alt.pk))
        assert antwort.status_code == 400, top
        assert not Task.objects.exists()

    gut = eintrag(tops["erbt"], vereidigt)
    _aktion(client_for, vorsitz, meeting, action="create_task", entry_id=str(gut.pk))
    assert Task.objects.get().assigned_to_id == vereidigt.pk


@pytest.mark.django_db
def test_oeffentlicher_top_weist_aufgaben_wie_bisher_zu(
    tops: dict[str, FactionAgendaItem], vorsitz: Any, pruefer: Any, client_for: Any
) -> None:
    antwort = _panel(
        client_for, vorsitz, tops["offen"], action="create_task", title="Radweg prüfen", assigned_to=str(pruefer.pk)
    )

    assert antwort.status_code == 200
    assert Task.objects.get().assigned_to_id == pruefer.pk
