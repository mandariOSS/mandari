# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen im neuen Erscheinungsbild, Teil 3b (Issue #951): Mitglieder, Mitglied, Mitglied einladen, Gast
einladen, Rollen, Rolle anlegen und bearbeiten, Einladung annehmen.

Gleichstand je Seite und Rolle (gleichstand.py) mit allen Aktionen der Mitgliedsseite – Rollen, Rechte, Vereidigung,
Gremien, Fachgebiete, Gast-Freigaben, deaktivieren, reaktivieren, entfernen, Eigentum übertragen –, Rückfragen und
Sperren; dieselben Antwortcodes je Rolle in beiden Vorlagen.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Role, Topic, UserInvitation
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionShare

from .einstellungen_hilfen import (
    fixtures_uebernehmen,
    html,
    rechte_wie_bisher,
    unterpunkte_anderer_reiter,
    url,
    vergleich,
    vorlagen,
)
from .gleichstand import fehlend, funktionen

pytestmark = pytest.mark.django_db
fixtures_uebernehmen(globals())

SEITEN = {
    "members": "work/organization/neu/mitglieder.html",
    "member_invite": "work/organization/neu/einladen.html",
    "guest_invite": "work/organization/neu/gast_einladen.html",
    "roles": "work/organization/neu/rollen.html",
    "role_create": "work/organization/neu/rolle.html",
}
#: Unterpunkt der Einstellungen je Seite (für die Reiter-Ausnahmen, siehe unterpunkte_anderer_reiter)
UNTERPUNKT = {
    "members": "mitglieder",
    "member_invite": "mitglieder",
    "guest_invite": "mitglieder",
    "roles": "rollen",
    "role_create": "rollen",
}


@pytest.fixture
def bestand(org: Any, admin: Any, make_member: Any) -> dict[str, Any]:
    """Alles, was die Mitgliederseiten zeigen können: Einladung, Registrierung, Gast mit Freigaben, Deaktivierte."""
    rolle = Role.objects.filter(organization=org, is_admin=False).first()
    assert rolle is not None
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org", first_name="Mia", last_name="Muster")
    mitglied.roles.add(rolle)
    mitglied.is_sworn_in = True
    mitglied.save(update_fields=["is_sworn_in"])
    Topic.objects.create(organization=org, name="Verkehr", color="blue")
    einladung = UserInvitation.objects.create(
        organization=org, email="neu@example.org", invited_by=admin.user, expires_at=timezone.now() + timedelta(days=7)
    )
    einladung.roles.add(rolle)
    anfrage = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="anfrage@example.org"),
        organization=org,
        is_active=False,
        registration_requested_at=timezone.now(),
    )
    weg = make_member(org, ["dashboard.view"], email="weg@example.org")
    weg.is_active = False
    weg.save(update_fields=["is_active"])
    gast = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="gast@example.org"), organization=org, is_guest=True
    )
    ordner = DocumentFolder.objects.create(organization=org, name="Haushalt", created_by=admin)
    FolderGuestShare.objects.create(
        folder=ordner, user=gast.user, level="view", allow_download=True, created_by=admin.user
    )
    antrag = Motion.objects.create(organization=org, author=admin, title="Antrag Radweg", visibility="organization")
    MotionShare.objects.create(motion=antrag, scope="user", user=gast.user, level="view", created_by=admin.user)
    return {"mitglied": mitglied, "einladung": einladung, "anfrage": anfrage, "weg": weg, "gast": gast, "rolle": rolle}


@pytest.mark.parametrize(("name", "vorlage"), SEITEN.items())
def test_schalter_waehlt_die_neue_vorlage(org: Any, admin: Any, client_for: Any, name: str, vorlage: str) -> None:
    client = client_for(admin.user)
    assert vorlage in vorlagen(client.get(url(name, org)))
    assert vorlage not in vorlagen(client.get(url(name, org) + "?ansicht=bisher"))
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert vorlage not in vorlagen(client.get(url(name, org)))


@pytest.mark.parametrize("name", SEITEN)
def test_gleichstand(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any], name: str) -> None:
    alt, neu = vergleich(client_for(admin.user), url(name, org))
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, UNTERPUNKT[name])) == []


@pytest.mark.parametrize("wer", ["mitglied", "gast", "weg", "admin"])
def test_gleichstand_mitglied(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any], wer: str) -> None:
    """Alle Aktionen der Mitgliedsseite für ein Mitglied, einen Gast, ein deaktiviertes Mitglied und sich selbst; die
    Eigentümerin sieht zusätzlich „Eigentum übertragen“."""
    org.owner = admin.user
    org.save(update_fields=["owner"])
    ziel = admin if wer == "admin" else bestand[wer]
    client = client_for(admin.user)
    pfad = url("member_detail", org, member_id=ziel.id)
    assert "work/organization/neu/mitglied.html" in vorlagen(client.get(pfad))
    alt, neu = vergleich(client, pfad)
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "mitglieder")) == []
    if wer == "mitglied":
        for aktion in (
            "update_roles",
            "update_permissions",
            "update_sworn_in",
            "update_expertise",
            "deactivate",
            "remove",
            "transfer_ownership",
        ):
            assert any(f"action={aktion}" in k for k in neu if k.startswith("formular:")), aktion


def test_gleichstand_mitglied_nur_lesend(org: Any, leser: Any, client_for: Any, bestand: dict[str, Any]) -> None:
    client = client_for(leser.user)
    pfad = url("member_detail", org, member_id=bestand["mitglied"].id)
    alt, neu = vergleich(client, pfad)
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "mitglieder")) == []
    seite = html(client.get(pfad))
    assert 'value="update_roles"' not in seite
    assert 'value="remove"' not in seite
    # Ohne Bearbeitungsrecht bleiben die Rechte-Checkboxen gesperrt
    assert any(k.startswith("gesperrt:") and "individual_permissions" in k for k in neu)


def test_gleichstand_rollen_bearbeiten(org: Any, admin: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    for rolle in Role.objects.filter(organization=org):
        pfad = url("role_edit", org, role_id=rolle.id)
        assert "work/organization/neu/rolle.html" in vorlagen(client.get(pfad))
        alt, neu = vergleich(client, pfad)
        assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "rollen")) == [], rolle.name


def test_gleichstand_gast_einladen_am_limit(org: Any, admin: Any, client_for: Any, bestand: dict[str, Any]) -> None:
    org.guest_limit = 1
    org.save(update_fields=["guest_limit"])
    alt, neu = vergleich(client_for(admin.user), url("guest_invite", org))
    assert any(k.startswith("gesperrt:") for k in alt)
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "mitglieder")) == []


#: Antwortcodes je Rolle (einstellungen_hilfen.ROLLEN): unverändert gegenüber der bisherigen Vorlage
CODES = {
    "members": {"admin": 200, "leser": 200, "mitgliederverwaltung": 200, "mitglied": 403},
    "member_invite": {"admin": 200, "leser": 403, "mitgliederverwaltung": 200, "rollenverwaltung": 403},
    "guest_invite": {"admin": 200, "leser": 403, "mitgliederverwaltung": 200, "bearbeitung": 403},
    "roles": {"admin": 200, "leser": 403, "mitgliederverwaltung": 403, "rollenverwaltung": 200},
    "role_create": {"admin": 200, "leser": 403, "rollenverwaltung": 200},
    "member_detail": {"admin": 200, "leser": 200, "mitgliederverwaltung": 200, "mitglied": 403},
    "role_edit": {"admin": 200, "mitgliederverwaltung": 403, "rollenverwaltung": 200},
}


@pytest.mark.parametrize("name", CODES)
def test_rechte_und_gleichstand_je_rolle(
    org: Any, rollen: dict[str, Any], client_for: Any, bestand: dict[str, Any], name: str
) -> None:
    """Je Rolle derselbe Antwortcode und, wo die Seite sich öffnet, alle Funktionen der bisherigen Vorlage."""
    kwargs: dict[str, Any] = {}
    if name == "member_detail":
        kwargs = {"member_id": bestand["mitglied"].id}
    elif name == "role_edit":
        kwargs = {"role_id": bestand["rolle"].id}
    unterpunkt = "rollen" if name.startswith("role") else "mitglieder"
    codes = rechte_wie_bisher(client_for, rollen, url(name, org, **kwargs), unterpunkte_anderer_reiter(org, unterpunkt))
    assert {rolle: codes[rolle] for rolle in CODES[name]} == CODES[name]


def test_einladung_annehmen(org: Any, admin: Any) -> None:
    """Öffentliche Annahmeseite im Layout der Anmeldung; der Schalter der einladenden Organisation entscheidet."""
    from django.test import Client

    einladung = UserInvitation(
        organization=org,
        email="neu@example.org",
        invited_by=admin.user,
        expires_at=timezone.now() + timedelta(days=7),
        message="Willkommen in der Fraktion",
    )
    token = einladung.issue_token()
    einladung.save()
    client = Client()
    client.force_login(cast(Any, UserFactory)(email="neu@example.org"))
    pfad = reverse("work:accept_invitation", kwargs={"token": token})
    antwort = client.get(pfad)
    assert "work/organization/neu/einladung_annehmen.html" in vorlagen(antwort)
    assert "Willkommen in der Fraktion" in html(antwort)
    alt = funktionen(html(client.get(pfad + "?ansicht=bisher")), pfad)
    assert fehlend(alt, funktionen(html(antwort), pfad)) == []
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert "work/organization/accept_invitation.html" in vorlagen(client.get(pfad))
