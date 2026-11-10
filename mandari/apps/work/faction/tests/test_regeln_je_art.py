# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Regeln je Sitzungsart (Issue #1240, Epic #1226).

- Wirksame Einstellungen: Standardwerte, darüber die Organisation, darüber die Art, feldweise; leere Felder der Art
  gelten „wie die Organisation“. Einladung, Erinnerungen, Protokollversand, Erster TOP und Beschlussfähigkeit lesen
  sie je Sitzung bzw. Art; ein Dienst speichert die Übersteuerung einer Art.
- Standard-Tagesordnung je Art; „ohne feste Tagesordnung“ (``free``) bekommt keine Standardpunkte.
- Art ohne nichtöffentlichen Teil: keine neuen NÖ-TOPs (Oberfläche und Server), bestehende bleiben geschützt.
- Art ohne Abstimmungen: keine Abstimmung und keine Beschlussfähigkeit (Oberfläche und Server), erfasste bleiben lesbar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction import agenda, internal_documents, sitzung
from apps.work.faction.generation import create_occurrence
from apps.work.faction.invitations import get_invitation_settings, invitation_dispatch_at
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionMeetingType,
    FactionStandardAgendaItem,
)
from apps.work.faction.protocol_dispatch import run_faction_protocol_pass
from apps.work.faction.quorum import get_quorum_rule, quorum_der_sitzung
from apps.work.faction.regeln import (
    AbstimmungenAusError,
    NichtOeffentlichAusError,
    einstellungen,
    wirksame_einstellungen,
)
from apps.work.faction.services import meeting_reminder_hours, record_decision
from apps.work.organization import selectors as org_selectors
from apps.work.organization import services as org_services
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.create",
    "faction.manage",
    "faction.start",
    "agenda.create",
    "agenda.propose",
    "agenda.approve",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
    "motions.view",
]
LESEN = ["faction.view_public", "protocols.view_public"]

START = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
EINGESCHALTET = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
SITZUNG = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)


@pytest.fixture
def standardart(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.get(organization=org, is_default=True)


@pytest.fixture
def ag(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.create(organization=org, name="Arbeitsgruppe Umwelt", position=1)


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


def _org_einstellungen(org: Any, **werte: Any) -> None:
    org.settings = {**(org.settings or {}), "faction": werte}
    org.save(update_fields=["settings"])


def _sitzung(org: Any, art: FactionMeetingType, **felder: Any) -> FactionMeeting:
    werte: dict[str, Any] = {"title": f"Sitzung {art.name}", "start": START, "status": "planned", **felder}
    return FactionMeeting.objects.create(organization=org, meeting_type=art, **werte)


def _ohne(art: FactionMeetingType, **werte: Any) -> FactionMeetingType:
    """Schalter oder Tagesordnungsart der Art ändern (wie später die Oberfläche der Sitzungsarten)."""
    for feld, wert in werte.items():
        setattr(art, feld, wert)
    art.save(update_fields=list(werte))
    return art


def _neue_sicht(org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


def _aktion(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def _sitzungsaktion(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def _panel(org: Any, meeting: FactionMeeting, item: FactionAgendaItem) -> str:
    return reverse(
        "work:faction_item_panel_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk, "item_id": item.pk}
    )


def _seite(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def _top(meeting: FactionMeeting, nummer: str, titel: str, teil: str, order: int) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=meeting, number=nummer, title=titel, visibility=teil, order=order)


# =============================================================================
# Wirksame Einstellungen
# =============================================================================


def test_uebersteuerung_wirkt_nur_in_ihrer_art_und_leere_felder_erben(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType
) -> None:
    _org_einstellungen(org, invitation_lead_hours=48, invitation_mode="opt_out", quorum_rule="majority")
    ag.settings = {
        "invitation_lead_hours": 12,
        "invitation_mode": None,
        "meeting_reminder_enabled": False,
        "first_agenda_description": "Arbeitsgruppe",
        "quorum_rule": "zwei_drittel",
        # Gilt für die ganze Organisation, eine Art übersteuert es nicht
        "publish_protocols": True,
    }
    ag.save(update_fields=["settings"])
    fraktion = _sitzung(org, standardart)
    umwelt = _sitzung(org, ag)

    assert get_invitation_settings(fraktion)["invitation_lead_hours"] == 48
    assert get_invitation_settings(umwelt)["invitation_lead_hours"] == 12
    assert invitation_dispatch_at(umwelt) == START - timedelta(hours=12)
    assert invitation_dispatch_at(fraktion) == START - timedelta(hours=48)
    # Ohne Wert (None) und ohne Feld: wie die Organisation
    assert get_invitation_settings(umwelt)["invitation_mode"] == "opt_out"
    assert meeting_reminder_hours(fraktion) == 48
    assert meeting_reminder_hours(umwelt) is None
    assert get_quorum_rule(fraktion) == "majority"
    assert get_quorum_rule(umwelt) == "zwei_drittel"
    assert umwelt.get_faction_settings()["first_agenda_description"] == "Arbeitsgruppe"
    assert "publish_protocols" not in einstellungen(ag)

    # Standardwerte, Organisation, Art – und mit der Organisation allein ihre eigenen Werte
    assert wirksame_einstellungen(ag)["invitation_lead_hours"] == 12
    assert wirksame_einstellungen(ag)["require_protocol_approval"] is True
    assert org_services.faction_settings_with_defaults(org)["invitation_lead_hours"] == 48
    assert org_services.faction_settings_with_defaults(org, ag)["invitation_lead_hours"] == 12
    assert org_services.faction_settings_with_defaults(org, standardart)["first_agenda_description"] == ""


def test_standardart_ohne_uebersteuerung_liest_wie_bisher(org: Any, standardart: FactionMeetingType) -> None:
    _org_einstellungen(
        org, invitation_lead_hours=36, protocol_dispatch="after_approval", auto_create_approval_item=False
    )
    meeting = _sitzung(org, standardart)

    assert standardart.settings == {}
    assert wirksame_einstellungen(meeting) == org_services.faction_settings_with_defaults(org)
    assert meeting.get_faction_settings() == org.settings["faction"]
    assert get_invitation_settings(meeting) == get_invitation_settings(org)


def test_dienst_speichert_uebersteuerung_feldweise_und_streng(org: Any, ag: FactionMeetingType) -> None:
    _org_einstellungen(org, invitation_lead_hours=48)
    vorher = dict(org.settings)

    org_services.save_meeting_type_settings(
        ag, {"invitation_lead_hours": "24", "first_agenda_title_no_previous": " Begrüßung "}, ["invitation_lead_hours"]
    )
    org_services.save_meeting_type_settings(ag, {}, ["auto_create_approval_item"])
    ag.refresh_from_db()
    assert ag.settings == {"invitation_lead_hours": 24, "auto_create_approval_item": False}

    for form, felder, meldung in (
        ({"invitation_lead_hours": "0"}, ["invitation_lead_hours"], "Einladungsvorlauf"),
        ({"invitation_mode": "irgendwie"}, ["invitation_mode"], "Ungültige Auswahl"),
        ({"publish_protocols": "on"}, ["publish_protocols"], "Unbekannte Einstellung"),
        ({}, ["gibt_es_nicht"], "Unbekannte Einstellung"),
    ):
        with pytest.raises(org_services.ServiceError, match=meldung):
            org_services.save_meeting_type_settings(ag, form, felder)
    with pytest.raises(org_services.ServiceError):
        org_services.save_meeting_type_settings(
            ag, {"invitation_lead_hours": "5"}, ["invitation_lead_hours"], inherit=["invitation_lead_hours"]
        )

    org_services.save_meeting_type_settings(ag, {}, inherit=["invitation_lead_hours"])
    ag.refresh_from_db()
    assert ag.settings == {"auto_create_approval_item": False}
    assert wirksame_einstellungen(ag)["invitation_lead_hours"] == 48
    org.refresh_from_db()
    assert org.settings == vorher


def test_genehmigungs_top_nach_regeln_der_art(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType
) -> None:
    ag.settings = {"auto_create_approval_item": False}
    ag.save(update_fields=["settings"])
    standardart.settings = {"first_agenda_title_no_previous": "Begrüßung und Tagesordnung"}
    standardart.save(update_fields=["settings"])
    reihe_fraktion = FactionMeetingSchedule.objects.create(
        organization=org, meeting_type=standardart, name="Fraktion", weekday=0, time=time(18, 0)
    )
    reihe_ag = FactionMeetingSchedule.objects.create(
        organization=org, meeting_type=ag, name="AG Umwelt", weekday=1, time=time(18, 0)
    )

    fraktion = create_occurrence(reihe_fraktion, date(2026, 11, 23))  # type: ignore[no-untyped-call]
    umwelt = create_occurrence(reihe_ag, date(2026, 11, 24))  # type: ignore[no-untyped-call]

    erster = fraktion.agenda_items.get(is_approval_item=True)
    assert erster.title == "Begrüßung und Tagesordnung"
    assert not umwelt.agenda_items.filter(is_approval_item=True).exists()


# =============================================================================
# Einladung, Erinnerung und Protokollversand je Art
# =============================================================================


def test_protokollversand_je_art_eingeschaltet_und_ausgeschaltet(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_member(org, LESEN, email="mitglied@example.org")
    with monkeypatch.context() as m:
        m.setattr("django.utils.timezone.now", lambda: EINGESCHALTET)
        org_services.save_meeting_type_settings(
            ag,
            {"protocol_dispatch": "after_completion", "protocol_dispatch_delay_hours": "0"},
            ["protocol_dispatch", "protocol_dispatch_delay_hours"],
        )
    ag.refresh_from_db()
    assert ag.settings["protocol_dispatch_since"] == EINGESCHALTET.isoformat()
    ende = SITZUNG + timedelta(hours=2)
    fraktion = _sitzung(org, standardart, start=SITZUNG, end=ende, status="completed")
    umwelt = _sitzung(org, ag, start=SITZUNG, end=ende, status="completed")
    mail.outbox = []

    # Nur in der Art eingeschaltet: nur ihre Sitzung
    assert run_faction_protocol_pass(now=ende + timedelta(hours=1)) == {"meetings": 1, "sent": 1}
    fraktion.refresh_from_db()
    umwelt.refresh_from_db()
    assert umwelt.protocol_sent_at is not None and fraktion.protocol_sent_at is None

    # In der Organisation eingeschaltet, in der Art aus: nur die Standardart
    vorsitz = make_member(org, ["faction.manage"], email="vorsitz@example.org")
    with monkeypatch.context() as m:
        m.setattr("django.utils.timezone.now", lambda: EINGESCHALTET)
        org_services.save_faction_settings(
            org, vorsitz, {"protocol_dispatch": "after_completion", "protocol_dispatch_delay_hours": "0"}
        )
    org_services.save_meeting_type_settings(ag, {"protocol_dispatch": "off"}, ["protocol_dispatch"])
    umwelt_zwei = _sitzung(org, ag, start=SITZUNG, end=ende, status="completed", title="Zweite AG")
    assert run_faction_protocol_pass(now=ende + timedelta(hours=1))["meetings"] == 1
    fraktion.refresh_from_db()
    umwelt_zwei.refresh_from_db()
    assert fraktion.protocol_sent_at is not None and umwelt_zwei.protocol_sent_at is None

    # Wieder „wie die Organisation“: auch ab ihrem Einschaltzeitpunkt
    org_services.save_meeting_type_settings(ag, {}, inherit=["protocol_dispatch"])
    ag.refresh_from_db()
    assert "protocol_dispatch" not in ag.settings and "protocol_dispatch_since" not in ag.settings


# =============================================================================
# Standard-Tagesordnung je Art
# =============================================================================


def test_standard_tagesordnung_je_art_und_ohne_feste_tagesordnung(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType
) -> None:
    FactionStandardAgendaItem.objects.create(organization=org, title="Beschlüsse", order=1)
    FactionStandardAgendaItem.objects.create(organization=org, meeting_type=ag, title="Umweltthemen", order=1)
    FactionStandardAgendaItem.objects.create(
        organization=org, meeting_type=ag, title="Personal", visibility="internal", order=2
    )

    fraktion = _sitzung(org, standardart)
    umwelt = _sitzung(org, ag)
    assert agenda.apply_standard_agenda(fraktion) == 1
    assert agenda.apply_standard_agenda(umwelt) == 2
    assert list(fraktion.agenda_items.values_list("title", flat=True)) == ["Beschlüsse"]
    assert list(umwelt.agenda_items.order_by("order").values_list("title", "number")) == [
        ("Umweltthemen", "1"),
        ("Personal", "NÖ 1"),
    ]
    # Ohne Angabe die Standardart (Einstellungsseite, Dialog „Neue Sitzung“)
    assert [i.title for i in agenda.standard_items(org)] == ["Beschlüsse"]
    vorschau = agenda.standard_agenda_preview(org, may_view_internal=True, meeting_type=ag)
    assert [i.title for i in vorschau["public"]] == ["Umweltthemen"] and vorschau["count"] == 2
    kontext = org_selectors.standard_agenda_context(org, may_view_internal=True, approval_item=True, meeting_type=ag)
    assert [item.title for _nr, item in kontext["standard_agenda_rows"]] == ["Umweltthemen", "Personal"]

    # Ohne nichtöffentlichen Teil nur die öffentlichen Punkte
    _ohne(ag, non_public_enabled=False)
    zweite = _sitzung(org, ag, title="Zweite")
    assert agenda.apply_standard_agenda(zweite) == 1
    assert list(zweite.agenda_items.values_list("title", flat=True)) == ["Umweltthemen"]
    assert (
        org_selectors.standard_agenda_context(org, may_view_internal=True, approval_item=True, meeting_type=ag)[
            "standard_agenda_may_internal"
        ]
        is False
    )

    # Ohne feste Tagesordnung keine Standardpunkte; der erste TOP folgt seiner eigenen Einstellung
    _ohne(ag, agenda_mode="free")
    frei = _sitzung(org, ag, title="Frei")
    assert agenda.apply_standard_agenda(frei) == 0
    vorschau = agenda.standard_agenda_preview(org, may_view_internal=True, meeting_type=ag)
    assert vorschau["count"] == 0 and vorschau["approval_item"] is True


def test_vorsitzung_der_vorschau_aus_derselben_art(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType
) -> None:
    _sitzung(org, standardart, start=START - timedelta(days=7), status="completed")

    assert agenda.new_meeting_has_previous(org) is True
    assert agenda.new_meeting_has_previous(org, ag) is False
    fraktion = agenda.standard_agenda_preview(org, may_view_internal=True)
    umwelt = agenda.standard_agenda_preview(org, may_view_internal=True, meeting_type=ag)
    assert fraktion["approval_title"] == "Tagesordnung festlegen und letztes Protokoll genehmigen"
    assert umwelt["approval_title"] == "Tagesordnung festlegen"


def test_standard_tagesordnung_pflegen_je_art(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType
) -> None:
    FactionStandardAgendaItem.objects.create(organization=org, title="Beschlüsse", order=1)

    org_services.add_standard_agenda_item(org, {"title": "Termine"}, may_view_internal=True)
    assert org_services.add_standard_agenda_suggestion(org, ag) == len(org_services.STANDARD_AGENDA_SUGGESTION)
    org_services.add_standard_agenda_item(
        org, {"title": "Personal", "visibility": "internal"}, may_view_internal=True, meeting_type=ag
    )
    assert [i.title for i in agenda.standard_items(org, standardart)] == ["Beschlüsse", "Termine"]
    assert [i.title for i in agenda.standard_items(org, ag)][-1] == "Personal"

    # Verschieben bleibt in der Art des Punkts
    termine = FactionStandardAgendaItem.objects.get(organization=org, meeting_type=standardart, title="Termine")
    org_services.move_standard_agenda_item(org, {"item_id": str(termine.pk), "direction": "up"}, may_view_internal=True)
    assert [i.title for i in agenda.standard_items(org)] == ["Termine", "Beschlüsse"]
    assert [i.title for i in agenda.standard_items(org, ag)][0] == org_services.STANDARD_AGENDA_SUGGESTION[0]

    _ohne(ag, non_public_enabled=False)
    with pytest.raises(org_services.ServiceError, match="keinen nichtöffentlichen Teil"):
        org_services.add_standard_agenda_item(
            org, {"title": "Noch eins", "visibility": "internal"}, may_view_internal=True, meeting_type=ag
        )
    andere = FactionMeetingType.default_for(cast(Any, OrganizationFactory)(name="Andere Fraktion"))
    with pytest.raises(org_services.ServiceError, match="Sitzungsart nicht gefunden"):
        org_services.add_standard_agenda_item(org, {"title": "X"}, may_view_internal=True, meeting_type=andere)


# =============================================================================
# Nichtöffentlicher Teil je Art
# =============================================================================


def test_art_ohne_noe_lehnt_neue_noe_tops_im_server_ab(org: Any, ag: FactionMeetingType, vorsitz: Any) -> None:
    meeting = _sitzung(org, ag, status="ongoing")
    oeffentlich = _top(meeting, "1", "Haushalt", "public", 1)
    bestehend = _top(meeting, "NÖ 1", "Grundstück", "internal", 2)
    _ohne(ag, non_public_enabled=False)
    meeting = FactionMeeting.objects.get(pk=meeting.pk)

    with pytest.raises(NichtOeffentlichAusError):
        agenda.add_item(meeting, title="Personal", visibility="internal")
    with pytest.raises(NichtOeffentlichAusError):
        agenda.add_item(meeting, title="Unterpunkt", visibility="public", parent=bestehend)
    with pytest.raises(NichtOeffentlichAusError):
        agenda.move_to_part(meeting, oeffentlich, "internal")
    with pytest.raises(NichtOeffentlichAusError):
        FactionAgendaItem.objects.create(meeting=meeting, title="Direkt", visibility="internal")
    assert not internal_documents.can_import(vorsitz, meeting)

    # Bestehende NÖ-TOPs bleiben geschützt und lassen sich weiter bearbeiten; öffentlich nur auf ausdrücklichen Wunsch
    bestehend.title = "Grundstück Flurstück 12"
    bestehend.save()
    assert FactionAgendaItem.objects.get(pk=bestehend.pk).visibility == "internal"
    neu = agenda.add_item(meeting, title="Presse", visibility="public")
    assert (neu.number, neu.visibility) == ("2", "public")
    agenda.move_to_part(meeting, bestehend, "public")
    assert FactionAgendaItem.objects.get(pk=bestehend.pk).visibility == "public"


def test_art_ohne_noe_in_oberflaeche_und_aktionen(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType, vorsitz: Any, client_for: Any
) -> None:
    _neue_sicht(org)
    meeting = _sitzung(org, ag, created_by=vorsitz)
    oeffentlich = _top(meeting, "1", "Haushalt", "public", 1)
    bestehend = _top(meeting, "NÖ 1", "Grundstück Hafen", "internal", 2)
    _ohne(ag, non_public_enabled=False)
    client = client_for(vorsitz.user)
    htmx = {"HTTP_HX_REQUEST": "true"}
    anzahl = FactionAgendaItem.objects.filter(meeting=meeting).count()

    antwort = client.post(
        _aktion(org, meeting), {"action": "add_item", "title": "Personal", "visibility": "internal"}, **htmx
    )
    assert antwort.status_code == 400 and "keinen nichtöffentlichen Teil" in antwort.content.decode()
    antwort = client.post(
        _aktion(org, meeting), {"action": "add_item", "title": "Unter NÖ", "parent_id": str(bestehend.pk)}, **htmx
    )
    assert antwort.status_code == 400
    client.post(_aktion(org, meeting), {"action": "propose", "title": "Vorschlag", "visibility": "internal"})
    antwort = client.post(
        _aktion(org, meeting),
        {"action": "move_item", "item_id": str(oeffentlich.pk), "direction": "to", "teil": "internal", "position": "0"},
        **htmx,
    )
    assert antwort.status_code == 400
    # Auch nicht als Unterpunkt eines bestehenden NÖ-TOPs (Issue #1285)
    antwort = client.post(
        _aktion(org, meeting),
        {"action": "move_item", "item_id": str(oeffentlich.pk), "direction": "to", "parent_id": str(bestehend.pk)},
        **htmx,
    )
    assert antwort.status_code == 400
    with pytest.raises(NichtOeffentlichAusError):
        agenda.place_item(meeting, oeffentlich, parent=bestehend, position=None)
    antwort = client.post(_panel(org, meeting, oeffentlich), {"action": "update", "visibility": "internal"}, **htmx)
    assert antwort.status_code == 400
    assert FactionAgendaItem.objects.filter(meeting=meeting).count() == anzahl
    assert FactionAgendaItem.objects.get(pk=oeffentlich.pk).visibility == "public"

    # Vorbereitung: keine Auswahl „nichtöffentlich“, kein Hinzufügen im NÖ-Teil; der bestehende NÖ-TOP bleibt sichtbar
    html = client.get(_seite(org, meeting)).content.decode()
    assert 'data-testid="fs-teil-intern"' in html and "Grundstück Hafen" in html
    assert 'value="internal">nichtöffentlich' not in html and 'id="neu-noe"' not in html
    assert 'data-teil="internal" data-ziehbar' not in html
    # Gegenprobe: die Standardart bietet den nichtöffentlichen Teil weiter an
    fraktion = _sitzung(org, standardart, created_by=vorsitz)
    html = client.get(_seite(org, fraktion)).content.decode()
    assert 'id="neu-noe"' in html and 'data-teil="internal" data-ziehbar' in html

    # Laufende Sitzung: „TOP hinzufügen“ ohne Auswahl des Teils, der Server lehnt NÖ ab
    meeting.status = "ongoing"
    meeting.save(update_fields=["status"])
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    html = client.get(_seite(org, meeting), {"top": str(oeffentlich.pk)}).content.decode()
    assert 'id="fs-top-neu-teil"' not in html
    antwort = client.post(
        _sitzungsaktion(org, meeting), {"action": "top_neu", "title": "Personal", "visibility": "internal"}, **htmx
    )
    assert antwort.status_code == 400
    assert not FactionAgendaItem.objects.filter(meeting=meeting, title="Personal").exists()


def test_art_ohne_noe_uebernimmt_keine_nichtoeffentlichen_ratspunkte(
    org: Any, ag: FactionMeetingType, vorsitz: Any, client_for: Any
) -> None:
    _neue_sicht(org)
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt A")
    org.body = body
    org.save(update_fields=["body"])
    meeting = _sitzung(org, ag, created_by=vorsitz)
    _ohne(ag, non_public_enabled=False)
    rat = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=START + timedelta(days=2)
    )
    punkte = [
        OParlAgendaItem.objects.create(
            external_id=f"https://ris.example.org/item/{nr}",
            meeting=rat,
            number=nr,
            order=int(nr),
            name=name,
            public=oe,
        )
        for nr, name, oe in (("1", "Jugendbeirat", True), ("2", "Grundstücksverkauf Hafen", False))
    ]
    client = client_for(vorsitz.user)
    kopf = {"HTTP_HX_REQUEST": "true", "HTTP_X_WORK_GESTALTUNG": "neu"}

    auswahl = client.post(_aktion(org, meeting), {"action": "ris_auswahl", "ris_meeting_id": str(rat.pk)}, **kopf)
    inhalt = auswahl.content.decode()
    assert "Grundstücksverkauf Hafen" not in inhalt and "keinen nichtöffentlichen Teil" in inhalt
    antwort = client.post(
        _aktion(org, meeting),
        {"action": "ris_uebernehmen", "ris_meeting_id": str(rat.pk), "punkt": [str(p.pk) for p in punkte]},
        **kopf,
    )
    assert antwort.status_code == 400
    assert not FactionAgendaItem.objects.filter(meeting=meeting).exists()


# =============================================================================
# Abstimmungen und Beschlussfähigkeit je Art
# =============================================================================


def test_art_ohne_abstimmungen(
    org: Any, standardart: FactionMeetingType, ag: FactionMeetingType, vorsitz: Any, client_for: Any
) -> None:
    _neue_sicht(org)
    meeting = _sitzung(org, ag, status="ongoing", start=timezone.now() - timedelta(minutes=10), created_by=vorsitz)
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    item = _top(meeting, "1", "Haushalt", "public", 1)
    zweiter = _top(meeting, "2", "Sonstiges", "public", 2)
    FactionDecision.objects.create(agenda_item=item, result="accepted", votes_yes=4, decision_text="Wie vorgelegt")
    _ohne(ag, voting_enabled=False)
    meeting = FactionMeeting.objects.get(pk=meeting.pk)
    client = client_for(vorsitz.user)
    htmx = {"HTTP_HX_REQUEST": "true"}

    with pytest.raises(AbstimmungenAusError):
        record_decision(zweiter, vorsitz, votes_yes=1, votes_no=0, votes_abstain=0, result="accepted", decision_text="")
    stimmen = {"votes_yes": "3", "votes_no": "1", "votes_abstain": "0", "result": "rejected"}
    for url, daten in (
        (_sitzungsaktion(org, meeting), {"action": "beschluss", "item_id": str(item.pk), **stimmen}),
        (_sitzungsaktion(org, meeting), {"action": "beschluss_aendern", "item_id": str(item.pk)}),
        (_aktion(org, meeting), {"action": "record_decision", "agenda_item_id": str(item.pk), **stimmen}),
        (_panel(org, meeting, item), {"action": "record_decision", **stimmen}),
        (_panel(org, meeting, item), {"action": "clear_decision"}),
    ):
        antwort = client.post(url, daten, **htmx)
        assert antwort.status_code == 400, daten["action"]
    # Die erfasste Abstimmung bleibt unverändert und lesbar
    decision = FactionDecision.objects.get(agenda_item=item)
    assert (decision.result, decision.votes_yes) == ("accepted", 4)
    assert not FactionDecision.objects.filter(agenda_item=zweiter).exists()

    assert quorum_der_sitzung(meeting) is None
    assert [g.schluessel for g in sitzung.anwesenheit(meeting).gruppen] == ["mitglieder"]
    html = client.get(_seite(org, meeting), {"top": str(item.pk)}).content.decode()
    assert "Angenommen" in html and "Wie vorgelegt" in html
    assert 'value="beschluss"' not in html and "beschluss_aendern" not in html
    assert "beschlussfähig" not in html
    html = client.get(_seite(org, meeting), {"top": str(zweiter.pk)}).content.decode()
    assert "Noch kein Beschluss" not in html and 'value="beschluss"' not in html

    # Gegenprobe: in der Standardart Abstimmung und Beschlussfähigkeit wie bisher
    fraktion = _sitzung(org, standardart, status="ongoing", start=timezone.now() - timedelta(minutes=5))
    FactionAttendance.objects.create(meeting=fraktion, membership=vorsitz, status="present")
    assert quorum_der_sitzung(fraktion) is not None
    assert [g.schluessel for g in sitzung.anwesenheit(fraktion).gruppen] == ["stimmberechtigt"]
