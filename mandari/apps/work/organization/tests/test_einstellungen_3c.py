# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen im neuen Erscheinungsbild, Teil 3c (Issue #951): Fraktionssitzungen, Dokumente (Übersicht,
Dokumenteinstellungen, Typen, Themen, Vorlagen, Briefköpfe samt Formularen) und Verwaltung (Einreichung,
Ratsfraktionen).

Gleichstand je Seite (gleichstand.py) mit allen POST-Wegen (section=…/action=…), versteckten Werten, Rückfragen,
Sperren und den Alpine-Komponenten factionTitlePreview, deleteConfirmation, letterheadForm und partyManager; dieselben
Antwortcodes je Rolle in beiden Vorlagen; die Dokument-Einstellungen gehören im Rahmen zu „Einstellungen“.
"""

from __future__ import annotations

import re
from datetime import date, time, timedelta
from typing import Any

import pytest

from apps.tenants.models import AdministrationContact, CouncilParty, Topic
from apps.work.faction.models import FactionMeetingException, FactionMeetingSchedule, FactionStandardAgendaItem
from apps.work.motions.models import MotionTemplate, MotionType, OrganizationLetterhead
from apps.work.rahmen import einordnen

from .einstellungen_hilfen import (
    fixtures_uebernehmen,
    html,
    rechte_wie_bisher,
    unterpunkte_anderer_reiter,
    url,
    vergleich,
    vorlagen,
)
from .gleichstand import fehlend

pytestmark = pytest.mark.django_db
fixtures_uebernehmen(globals())


@pytest.fixture
def bestand(org: Any, admin: Any) -> dict[str, Any]:
    """Was die Seiten zeigen können: Sitzungsreihen mit Ausnahme, Standard-TOPs, Dokumenttypen, Vorlagen,
    Briefköpfe, Themen, Ratsfraktionen mit Koalition und Verwaltungskontakte."""
    aktiv = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0), rsvp_enabled=True
    )
    FactionMeetingSchedule.objects.create(
        organization=org, name="Klausur", weekday=5, time=time(10, 0), is_active=False
    )
    FactionMeetingException.objects.create(
        schedule=aktiv, original_date=date.today() + timedelta(days=30), exception_type="cancelled", reason="Ferien"
    )
    FactionStandardAgendaItem.objects.create(organization=org, title="Termine", visibility="public", order=1)
    FactionStandardAgendaItem.objects.create(organization=org, title="Personalien", visibility="internal", order=2)
    typ = MotionType.objects.create(
        organization=org, name="Antrag", slug="antrag", is_default=True, requires_approval=True
    )
    MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage", icon="help-circle", color="green")
    kopf = OrganizationLetterhead.objects.create(organization=org, name="Offiziell", is_default=True)
    vorlage = MotionTemplate.objects.create(organization=org, name="Standardantrag", motion_type=typ, letterhead=kopf)
    Topic.objects.create(organization=org, name="Verkehr", color="blue")
    CouncilParty.objects.create(
        organization=org, name="Grüne", short_name="GRÜNE", color="#16a34a", is_coalition_member=True
    )
    CouncilParty.objects.create(
        organization=org, name="Alte Liste", short_name="AL", is_active=False, email="al@example.org"
    )
    AdministrationContact.objects.create(organization=org, label="Ratsbüro", email="ratsbuero@example.org")
    return {"reihe": aktiv, "typ": typ, "kopf": kopf, "vorlage": vorlage}


#: Adressname → (neue Vorlage, Unterpunkt der Einstellungen)
SEITEN = {
    "organization_faction_settings": ("work/organization/neu/fraktion.html", "fraktion"),
    "organization_documents": ("work/organization/neu/dokumente.html", "uebersicht"),
    "document_settings": ("work/motions/settings/neu/index.html", "uebersicht"),
    "document_type_list": ("work/motions/settings/neu/typen.html", "typen"),
    "document_type_create": ("work/motions/settings/neu/typ.html", "typen"),
    "document_topic_list": ("work/motions/settings/neu/themen.html", "themen"),
    "document_template_list": ("work/motions/settings/neu/vorlagen.html", "vorlagen"),
    "document_template_create": ("work/motions/settings/neu/vorlage.html", "vorlagen"),
    "document_letterhead_list": ("work/motions/settings/neu/briefkoepfe.html", "briefkoepfe"),
    "document_letterhead_create": ("work/motions/settings/neu/briefkopf.html", "briefkoepfe"),
    "organization_ris_settings": ("work/organization/neu/einreichung.html", "einreichung"),
    "council_parties": ("work/organization/neu/ratsfraktionen.html", "ratsfraktionen"),
}
#: Bearbeitungsseiten mit dem Schlüssel ihres Objekts im Bestand
BEARBEITEN = {
    "document_type_edit": ("type_id", "typ", "work/motions/settings/neu/typ.html", "typen"),
    "document_template_edit": ("template_id", "vorlage", "work/motions/settings/neu/vorlage.html", "vorlagen"),
    "document_letterhead_edit": ("letterhead_id", "kopf", "work/motions/settings/neu/briefkopf.html", "briefkoepfe"),
}


@pytest.mark.parametrize("name", SEITEN)
def test_schalter_waehlt_die_neue_vorlage(org: Any, admin: Any, client_for: Any, name: str) -> None:
    vorlage = SEITEN[name][0]
    client = client_for(admin.user)
    assert vorlage in vorlagen(client.get(url(name, org)))
    assert vorlage not in vorlagen(client.get(url(name, org) + "?ansicht=bisher"))
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert vorlage not in vorlagen(client.get(url(name, org)))


@pytest.mark.parametrize("name", SEITEN)
def test_gleichstand(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any], name: str) -> None:
    alt, neu = vergleich(client_for(admin.user), url(name, org))
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, SEITEN[name][1])) == []


@pytest.mark.parametrize("name", BEARBEITEN)
def test_gleichstand_bearbeiten(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any], name: str) -> None:
    schluessel, objekt, vorlage, seite = BEARBEITEN[name]
    client = client_for(admin.user)
    pfad = url(name, org, **{schluessel: bestand[objekt].id})
    assert vorlage in vorlagen(client.get(pfad))
    alt, neu = vergleich(client, pfad)
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, seite)) == []


def test_gleichstand_leere_listen(org: Any, admin: Any, client_for: Any) -> None:
    """Ohne Bestand: Leerzustände mit ihren Wegen („Ersten Typ erstellen“ …) bleiben erhalten."""
    client = client_for(admin.user)
    for name, (_vorlage, seite) in SEITEN.items():
        alt, neu = vergleich(client, url(name, org))
        assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, seite)) == [], name


def test_fraktionssitzungen_mit_allen_wegen(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any]) -> None:
    _alt, neu = vergleich(client_for(admin.user), url("organization_faction_settings", org))
    for weg in (
        "add_schedule",
        "update_schedule_automation",
        "toggle_schedule",
        "delete_schedule",
        "add_exception",
        "delete_exception",
        "add_standard_item",
        "move_standard_item",
        "delete_standard_item",
    ):
        assert any(f"section={weg}" in k for k in neu if k.startswith("formular:")), weg
    assert "komponente:factionTitlePreview" in neu
    assert "komponente:deleteConfirmation" in neu


def test_veroeffentlichen_ohne_recht_gesperrt(org: Any, make_member: Any, client_for: Any) -> None:
    """publish_protocols bleibt ohne protocols.publish gesperrt (wie bisher)."""
    leitung = make_member(org, ["dashboard.view", "organization.view", "faction.manage"], email="leitung@example.org")
    alt, neu = vergleich(client_for(leitung.user), url("organization_faction_settings", org))
    gesperrt = {k for k in alt if k.startswith("gesperrt:") and "publish_protocols" in k}
    assert gesperrt and gesperrt <= neu
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "fraktion")) == []


#: Antwortcodes je Rolle (einstellungen_hilfen.ROLLEN) für die Seiten mit eigenem Recht, unverändert gegenüber bisher
CODES = {
    "organization_faction_settings": {"admin": 200, "leser": 403, "bearbeitung": 403, "fraktionsleitung": 200},
    "organization_documents": {"admin": 200, "leser": 200, "bearbeitung": 200, "mitglied": 403},
    "document_settings": {"admin": 200, "leser": 403, "bearbeitung": 200, "fraktionsleitung": 403},
    "organization_ris_settings": {"admin": 200, "bearbeitung": 403, "fraktionsleitung": 200},
    "council_parties": {"admin": 200, "leser": 403, "bearbeitung": 200, "fraktionsleitung": 403},
}


@pytest.mark.parametrize("name", [*SEITEN, *BEARBEITEN])
def test_rechte_und_gleichstand_je_rolle(
    org: Any, rollen: dict[str, Any], client_for: Any, bestand: dict[str, Any], name: str
) -> None:
    """Je Rolle derselbe Antwortcode und, wo die Seite sich öffnet, alle Funktionen der bisherigen Vorlage."""
    if name in BEARBEITEN:
        schluessel, objekt, _vorlage, seite = BEARBEITEN[name]
        pfad = url(name, org, **{schluessel: bestand[objekt].id})
    else:
        seite = SEITEN[name][1]
        pfad = url(name, org)
    codes = rechte_wie_bisher(client_for, rollen, pfad, unterpunkte_anderer_reiter(org, seite))
    erwartet = CODES.get(name, {"admin": 200, "leser": 403, "bearbeitung": 200, "fraktionsleitung": 403})
    assert {rolle: codes[rolle] for rolle in erwartet} == erwartet


def test_fraktion_vorschau_ohne_inline_skript(org: Any, admin: Any, client_for: Any) -> None:
    """Wie test_faction_settings_page für die neue Vorlage: Vorschau über factionTitlePreview, kein Inline-Skript."""
    antwort = client_for(admin.user).get(url("organization_faction_settings", org))
    seite = html(antwort)
    assert 'x-data="factionTitlePreview"' in seite
    assert 'x-ref="titleInput"' in seite
    assert 'id="preview-title" x-text="preview"' in seite
    for feld in ("first_agenda_title_with_previous", "invitation_mode", "publish_protocols"):
        assert f'name="{feld}"' in seite, feld
    assert "Noch keine Sitzungsreihe angelegt." in seite
    assert "DOMContentLoaded" not in seite
    assert not re.search(r"<c-[a-z]", seite)
    for vorlage in antwort.templates:
        if vorlage.name and vorlage.name.startswith("work/organization/neu/"):
            assert "<script" not in vorlage.source, vorlage.name
            assert not re.search(r"\son[a-z]+=\"", vorlage.source), vorlage.name


@pytest.mark.parametrize(
    "name",
    [
        "document_settings",
        "document_type_list",
        "document_topic_list",
        "document_template_edit",
        "document_letterhead_create",
    ],
)
def test_dokument_einstellungen_gehoeren_zu_den_einstellungen(name: str) -> None:
    assert einordnen(name)[0] == "einstellungen"
    assert einordnen("document_create")[0] == "dokumente"


def test_ratsfraktionen_ohne_inline_stil(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any]) -> None:
    seite = html(client_for(admin.user).get(url("council_parties", org)))
    teil = seite.split('id="inhalt"', 1)[1]
    assert ' style="' not in teil
    assert 'fill="#16a34a"' in teil
