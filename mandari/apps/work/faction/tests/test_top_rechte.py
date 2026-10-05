# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechte für TOPs je Standardrolle (Issue #872).

- Sachkundige (und Bezirksvertretung) schlagen TOPs nur vor; der Vorschlag steht erst nach der Genehmigung
  auf der Tagesordnung.
- Ratsmitglieder (Fraktionsmitglied) tragen TOPs direkt ein, auch ohne die Sitzung zu verwalten.
- Vorsitz, stellvertretender Vorsitz und Geschäftsführung genehmigen oder lehnen ab (``agenda.approve``); sie
  werden über neue Vorschläge benachrichtigt, die vorschlagende Person über die Entscheidung.
- Alles über das bestehende Rechtesystem: Rollenrechte, ausdrücklich verweigerte Rechte.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import DEFAULT_ROLES
from apps.common.tests.factories import PermissionFactory
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.notifications.models import Notification, NotificationType


def _rolle(key: str) -> list[str]:
    return list(cast(list[str], DEFAULT_ROLES[key]["permissions"]))


@pytest.fixture
def rollen(org: Any, make_member: Any) -> dict[str, Any]:
    """Je Standardrolle ein vereidigtes Mitglied mit den Rechten der Rolle aus ``DEFAULT_ROLES``."""
    members = {}
    for key in ("faction_chair", "faction_vice_chair", "managing_director", "faction_member", "expert_citizen"):
        member = make_member(org, _rolle(key), email=f"{key}@example.org")
        member.is_sworn_in = True
        member.save(update_fields=["is_sworn_in"])
        members[key] = member
    return members


@pytest.fixture
def sitzung(org: Any, rollen: dict[str, Any]) -> FactionMeeting:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=rollen["faction_chair"],
    )
    FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Tagesordnung", is_approval_item=True, order=0)
    FactionAgendaItem.objects.create(meeting=meeting, number="2", title="Haushalt", order=1)
    return meeting


def _aktion(client_for: Any, member: Any, meeting: FactionMeeting, **data: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.id})
    return client_for(member.user).post(url, data)


def _vorschlag(meeting: FactionMeeting, von: Any, titel: str = "Radweg Südring") -> FactionAgendaItem:
    from apps.work.faction.services import AgendaProposalService

    item: FactionAgendaItem = AgendaProposalService.create_proposal(
        meeting=meeting, title=titel, description="Bitte beraten", proposed_by=von, visibility="public"
    )
    return item


def test_standardrollen_tragen_die_vorgesehenen_top_rechte() -> None:
    for key in ("faction_chair", "faction_vice_chair", "managing_director"):
        assert "agenda.approve" in _rolle(key), key
    for key in ("faction_member", "expert_citizen", "district_representative", "faction_staff"):
        assert "agenda.approve" not in _rolle(key), key
    for key in ("expert_citizen", "district_representative"):
        assert "agenda.propose" in _rolle(key), key
        assert "agenda.create" not in _rolle(key), key
    assert "agenda.create" in _rolle("faction_member")


@pytest.mark.django_db
def test_sachkundige_schlagen_nur_vor(rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any) -> None:
    sachkundig = rollen["expert_citizen"]

    direkt = _aktion(client_for, sachkundig, sitzung, action="add_item", title="Direkt", visibility="public")
    _aktion(client_for, sachkundig, sitzung, action="propose", title="Radweg Südring", description="Bitte")

    assert direkt.status_code == 403
    assert not sitzung.agenda_items.filter(title="Direkt").exists()
    vorschlag = sitzung.agenda_items.get(title="Radweg Südring")
    assert vorschlag.proposal_status == "proposed"
    assert vorschlag.proposed_by == sachkundig


@pytest.mark.django_db
def test_vorschlag_benachrichtigt_vorsitz_stellvertretung_und_geschaeftsfuehrung(
    rollen: dict[str, Any], sitzung: FactionMeeting
) -> None:
    _vorschlag(sitzung, rollen["expert_citizen"])

    empfaenger = set(
        Notification.objects.filter(notification_type=NotificationType.FACTION_MEETING_UPDATED).values_list(
            "recipient_id", flat=True
        )
    )
    assert empfaenger == {
        rollen["faction_chair"].id,
        rollen["faction_vice_chair"].id,
        rollen["managing_director"].id,
    }


@pytest.mark.django_db
def test_offener_vorschlag_steht_nicht_auf_der_tagesordnung(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    _vorschlag(sitzung, rollen["expert_citizen"])
    url = reverse("work:faction_detail", kwargs={"org_slug": sitzung.organization.slug, "meeting_id": sitzung.id})

    mitglied = client_for(rollen["faction_member"].user).get(url)
    vorsitz = client_for(rollen["faction_chair"].user).get(url).content.decode()
    sachkundig = client_for(rollen["expert_citizen"].user).get(url).content.decode()

    assert [i.title for i in mitglied.context["public_agenda_items"]] == ["Tagesordnung", "Haushalt"]
    assert "Radweg Südring" not in mitglied.content.decode()
    assert "Offene Vorschläge (1)" in vorsitz
    assert "Wartet auf Genehmigung" in sachkundig


@pytest.mark.django_db
def test_ratsmitglieder_tragen_direkt_ein(rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any) -> None:
    mitglied = rollen["faction_member"]
    _vorschlag(sitzung, rollen["expert_citizen"])
    url = reverse("work:faction_detail", kwargs={"org_slug": sitzung.organization.slug, "meeting_id": sitzung.id})

    seite = client_for(mitglied.user).get(url)
    antwort = _aktion(client_for, mitglied, sitzung, action="add_item", title="Schulbau", visibility="public")

    assert seite.context["can_add_items"]
    assert "Öffentlicher TOP" in seite.content.decode()
    assert antwort.status_code == 302
    eintrag = sitzung.agenda_items.get(title="Schulbau")
    assert eintrag.proposal_status == "active"
    # Der offene Vorschlag zählt bei der Nummer nicht mit
    assert eintrag.number == "3"


@pytest.mark.django_db
@pytest.mark.parametrize("rolle", ["faction_chair", "faction_vice_chair", "managing_director"])
def test_vorsitz_stellvertretung_und_geschaeftsfuehrung_genehmigen(
    rolle: str, rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    sachkundig = rollen["expert_citizen"]
    vorschlag = _vorschlag(sitzung, sachkundig)
    zweiter = _vorschlag(sitzung, sachkundig, "Spielplatz")

    _aktion(client_for, rollen[rolle], sitzung, action="accept_proposal", item_id=str(vorschlag.id))
    _aktion(
        client_for, rollen[rolle], sitzung, action="reject_proposal", item_id=str(zweiter.id), reason="Nächste Sitzung"
    )

    vorschlag.refresh_from_db()
    zweiter.refresh_from_db()
    assert vorschlag.proposal_status == "active"
    assert vorschlag.reviewed_by == rollen[rolle]
    assert (vorschlag.number, vorschlag.order) == ("3", 2)
    assert zweiter.proposal_status == "rejected"
    assert zweiter.rejection_reason == "Nächste Sitzung"
    entscheidungen = Notification.objects.filter(
        recipient=sachkundig, notification_type=NotificationType.FACTION_PROPOSAL_DECIDED
    )
    assert sorted(entscheidungen.values_list("title", flat=True)) == [
        "TOP-Vorschlag abgelehnt",
        "TOP-Vorschlag angenommen",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("rolle", ["faction_member", "expert_citizen"])
def test_ohne_genehmigungsrecht_keine_entscheidung(
    rolle: str, rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    vorschlag = _vorschlag(sitzung, rollen["expert_citizen"])

    _aktion(client_for, rollen[rolle], sitzung, action="accept_proposal", item_id=str(vorschlag.id))
    _aktion(client_for, rollen[rolle], sitzung, action="reject_proposal", item_id=str(vorschlag.id))

    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "proposed"


@pytest.mark.django_db
def test_verweigertes_genehmigungsrecht_gilt_auch_fuer_den_vorsitz(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    vorsitz = rollen["faction_chair"]
    vorsitz.denied_permissions.add(cast(Any, PermissionFactory)(codename="agenda.approve"))
    vorschlag = _vorschlag(sitzung, rollen["expert_citizen"])

    _aktion(client_for, vorsitz, sitzung, action="accept_proposal", item_id=str(vorschlag.id))

    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "proposed"
    assert not Notification.objects.filter(recipient=vorsitz).exists()


@pytest.mark.django_db
def test_verschieben_ueberspringt_offene_vorschlaege(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    vorsitz = rollen["faction_chair"]
    _vorschlag(sitzung, rollen["expert_citizen"])
    _aktion(client_for, vorsitz, sitzung, action="add_item", title="Schulbau", visibility="public")
    schulbau = sitzung.agenda_items.get(title="Schulbau")

    _aktion(client_for, vorsitz, sitzung, action="move_item", item_id=str(schulbau.id), direction="up")

    nummern = dict(sitzung.agenda_items.values_list("title", "number"))
    assert nummern["Schulbau"] == "2"
    assert nummern["Haushalt"] == "3"
    assert nummern["Radweg Südring"] == ""


# -- Nicht-öffentliche Vorschläge, Entscheidungen, Nummern und Sitzungsstand ----------------------------------


def _abwesend_mit_vertretung(member: Any, vertretung: Any) -> None:
    from apps.work.organization.models import MemberAbsence

    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=member.organization,
        membership=member,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=7),
        deputy=vertretung,
        notify_deputy=True,
    )


def _texte_aller_benachrichtigungen() -> str:
    from django.core import mail

    texte = [f"{titel} {text}" for titel, text in Notification.objects.values_list("title", "message")]
    texte += [f"{m.subject} {m.body}" for m in mail.outbox]
    return "\n".join(texte)


@pytest.mark.django_db
def test_noe_vorschlag_erreicht_vertretung_ohne_titel(
    rollen: dict[str, Any], sitzung: FactionMeeting, make_member: Any, client_for: Any
) -> None:
    """NÖ strikt (#64): Die nicht vereidigte Vertretung der abwesenden Vorsitzenden erfährt den Titel nicht."""
    vertretung = make_member(sitzung.organization, _rolle("faction_member"), email="vertretung@example.org")
    _abwesend_mit_vertretung(rollen["faction_chair"], vertretung)

    _aktion(
        client_for,
        rollen["expert_citizen"],
        sitzung,
        action="propose",
        title="GEHEIMTITEL",
        description="GEHEIMINHALT",
        visibility="internal",
    )

    assert sitzung.agenda_items.filter(title="GEHEIMTITEL", proposal_status="proposed").exists()
    weitergeleitet = Notification.objects.get(recipient=vertretung)
    assert weitergeleitet.title == "[Vertretung] Neuer nicht-öffentlicher TOP-Vorschlag"
    texte = _texte_aller_benachrichtigungen()
    assert "GEHEIMTITEL" not in texte
    assert "GEHEIMINHALT" not in texte


@pytest.mark.django_db
def test_noe_entscheidung_erreicht_vertretung_ohne_titel_und_begruendung(
    rollen: dict[str, Any], sitzung: FactionMeeting, make_member: Any, client_for: Any
) -> None:
    from apps.work.faction.services import AgendaProposalService

    sachkundig = rollen["expert_citizen"]
    vertretung = make_member(sitzung.organization, _rolle("faction_member"), email="vertretung@example.org")
    abgelehnt = AgendaProposalService.create_proposal(
        meeting=sitzung, title="GEHEIMTITEL", description="", proposed_by=sachkundig, visibility="internal"
    )
    angenommen = AgendaProposalService.create_proposal(
        meeting=sitzung, title="GEHEIMSACHE", description="", proposed_by=sachkundig, visibility="internal"
    )
    _abwesend_mit_vertretung(sachkundig, vertretung)

    vorsitz = rollen["faction_chair"]
    _aktion(client_for, vorsitz, sitzung, action="reject_proposal", item_id=str(abgelehnt.id), reason="GEHEIMGRUND")
    _aktion(client_for, vorsitz, sitzung, action="accept_proposal", item_id=str(angenommen.id))

    an_vertretung = Notification.objects.filter(
        recipient=vertretung, notification_type=NotificationType.FACTION_PROPOSAL_DECIDED
    )
    assert an_vertretung.count() == 2
    texte = _texte_aller_benachrichtigungen()
    for geheim in ("GEHEIMTITEL", "GEHEIMSACHE", "GEHEIMGRUND"):
        assert geheim not in texte
    # Die Begründung sieht die (vereidigte) vorschlagende Person in der Sitzung bei ihren Vorschlägen
    url = reverse("work:faction_detail", kwargs={"org_slug": sitzung.organization.slug, "meeting_id": sitzung.id})
    assert "Begründung: GEHEIMGRUND" in client_for(sachkundig.user).get(url).content.decode()


@pytest.mark.django_db
def test_entscheidungen_in_der_sie_form(rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any) -> None:
    sachkundig = rollen["expert_citizen"]
    vorschlag = _vorschlag(sitzung, sachkundig)
    zweiter = _vorschlag(sitzung, sachkundig, "Spielplatz")

    _aktion(client_for, rollen["faction_chair"], sitzung, action="accept_proposal", item_id=str(vorschlag.id))
    _aktion(
        client_for, rollen["faction_chair"], sitzung, action="reject_proposal", item_id=str(zweiter.id), reason="Später"
    )

    texte = set(Notification.objects.filter(recipient=sachkundig).values_list("message", flat=True))
    assert texte == {
        'Ihr Vorschlag "Radweg Südring" wurde angenommen.',
        'Ihr Vorschlag "Spielplatz" wurde nicht angenommen. Grund: Später',
    }


@pytest.mark.django_db
def test_doppelte_annahme_entscheidet_und_benachrichtigt_einmal(
    rollen: dict[str, Any], sitzung: FactionMeeting
) -> None:
    from apps.work.faction.services import AgendaProposalService

    sachkundig = rollen["expert_citizen"]
    vorschlag = _vorschlag(sitzung, sachkundig)
    # Zwei Anfragen, die den Vorschlag beide noch offen geladen haben (Doppelklick)
    erste = FactionAgendaItem.objects.get(pk=vorschlag.pk)
    zweite = FactionAgendaItem.objects.get(pk=vorschlag.pk)

    assert AgendaProposalService.accept_proposal(erste, rollen["faction_chair"]) is True
    assert AgendaProposalService.accept_proposal(zweite, rollen["faction_vice_chair"]) is False
    assert AgendaProposalService.reject_proposal(zweite, rollen["faction_vice_chair"]) is False

    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "active"
    assert vorschlag.reviewed_by == rollen["faction_chair"]
    entscheidungen = Notification.objects.filter(
        recipient=sachkundig, notification_type=NotificationType.FACTION_PROPOSAL_DECIDED
    )
    assert entscheidungen.count() == 1


def _altbestand(sitzung: FactionMeeting, sachkundig: Any) -> None:
    """Stand vor #872: Ein abgelehnter Vorschlag zählte mit, der danach eingetragene TOP erhielt die „4“."""
    FactionAgendaItem.objects.create(
        meeting=sitzung,
        number="",
        title="Alter Vorschlag",
        order=9999,
        proposal_status="rejected",
        proposed_by=sachkundig,
    )
    FactionAgendaItem.objects.create(meeting=sitzung, number="4", title="Schulbau", order=2)


@pytest.mark.django_db
def test_eintragen_im_altbestand_vergibt_keine_nummer_doppelt(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    _altbestand(sitzung, rollen["expert_citizen"])

    _aktion(client_for, rollen["faction_member"], sitzung, action="add_item", title="Radweg", visibility="public")

    nummern = dict(sitzung.agenda_items.filter(proposal_status="active").values_list("title", "number"))
    assert nummern == {"Tagesordnung": "1", "Haushalt": "2", "Schulbau": "3", "Radweg": "4"}


@pytest.mark.django_db
def test_annahme_im_altbestand_vergibt_keine_nummer_doppelt(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    _altbestand(sitzung, rollen["expert_citizen"])
    vorschlag = _vorschlag(sitzung, rollen["expert_citizen"])

    _aktion(client_for, rollen["faction_chair"], sitzung, action="accept_proposal", item_id=str(vorschlag.id))

    nummern = dict(sitzung.agenda_items.filter(proposal_status="active").values_list("title", "number"))
    assert nummern == {"Tagesordnung": "1", "Haushalt": "2", "Schulbau": "3", "Radweg Südring": "4"}


@pytest.mark.django_db
def test_ratsmitglieder_tragen_in_abgeschlossene_sitzungen_nichts_ein(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    sitzung.status = "completed"
    sitzung.protocol_approved = True
    sitzung.save(update_fields=["status", "protocol_approved"])
    url = reverse("work:faction_detail", kwargs={"org_slug": sitzung.organization.slug, "meeting_id": sitzung.id})

    seite = client_for(rollen["faction_member"].user).get(url)
    antwort = _aktion(client_for, rollen["faction_member"], sitzung, action="add_item", title="Nachtrag")

    assert not seite.context["can_add_items"]
    assert antwort.status_code == 403
    assert not sitzung.agenda_items.filter(title="Nachtrag").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["ongoing", "completed"])
def test_vorschlaege_nur_vor_sitzungsbeginn(
    status: str, rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    sitzung.status = status
    sitzung.save(update_fields=["status"])

    _aktion(client_for, rollen["expert_citizen"], sitzung, action="propose", title="Zu spät", description="")

    assert not sitzung.agenda_items.filter(title="Zu spät").exists()
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_eigene_noe_vorschlaege_nur_mit_vereidigung(
    rollen: dict[str, Any], sitzung: FactionMeeting, client_for: Any
) -> None:
    from apps.work.faction.services import AgendaProposalService

    sachkundig = rollen["expert_citizen"]
    AgendaProposalService.create_proposal(
        meeting=sitzung, title="GEHEIMTITEL", description="", proposed_by=sachkundig, visibility="internal"
    )
    _vorschlag(sitzung, sachkundig)
    sachkundig.is_sworn_in = False
    sachkundig.save(update_fields=["is_sworn_in"])
    url = reverse("work:faction_detail", kwargs={"org_slug": sitzung.organization.slug, "meeting_id": sitzung.id})

    seite = client_for(sachkundig.user).get(url)

    assert [item.title for item in seite.context["my_proposals"]] == ["Radweg Südring"]
    assert "GEHEIMTITEL" not in seite.content.decode()
