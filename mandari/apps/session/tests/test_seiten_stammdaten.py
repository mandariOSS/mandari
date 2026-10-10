# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitentest Stammdaten (Issue #708): alle Seiten des Bereichs mit allen Rollen, ohne Serverfehler.

Bereich: Gremien, Personen, Besetzungen, Wahlperioden und Archiv, Rollen und Rechte, Benutzer und
Einladungen, Einstellungen, Session-API, Endgeräte, Datenschutz, Audit-Log.

Abgeleitet aus dem automatischen Durchlauf der Fehlersuche, aber klein gehalten: ein Bestand je Modul
(zwei Mandanten), je Rolle ein Test. Geprüft wird

- jede Seite des Bereichs, auch mit kaputten Abfrageparametern (``?term=abc``, ``?page=x`` …) und als
  HTMX-Anfrage: kein Serverfehler, keine Ausnahme;
- Formulare des Bereichs mit leeren und ungültigen Kennungen (POST): Meldung statt Serverfehler;
- die Links des Bereichs in der Seitenleiste: Jede Rolle sieht nur Links, die sie auch öffnen darf;
- Mandantentrennung: Objekte des anderen Mandanten unter dem eigenen Kürzel ergeben 404.

Fällt hier etwas auf, steht in der Meldung jede betroffene Adresse mit Status bzw. Ausnahme.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.common.tests.festgeschriebene_testdaten import sicherheitsprotokoll_zuruecksetzen
from apps.session.models import (
    SessionAPIToken,
    SessionDevice,
    SessionDeviceGrant,
    SessionInvitation,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

ROLLEN = [
    "verwaltung",  # Standardrollen Administrator und Revision (wie in der Demo)
    "sachbearbeitung",
    "protokoll",
    "lesezugriff",
    "revision",
    "datenschutz",
    "benutzerverwaltung",  # eigene Rolle nur mit „Benutzer verwalten“
    "ohne_mandant",  # angemeldet, ohne Zugang zum Mandanten
    "anonym",
]
STANDARDROLLE = {
    "verwaltung": "admin",
    "sachbearbeitung": "clerk",
    "protokoll": "recorder",
    "lesezugriff": "viewer",
    "revision": "revision",
    "datenschutz": "privacy",
}

#: GET-Seiten des Bereichs, relativ zu /session/<kürzel>; Platzhalter aus World.ids
SEITEN = [
    "/organizations/",
    "/organizations/?term={term}",
    "/organizations/?term=abc",
    "/organizations/?type=council&active=1&page=x",
    "/organizations/create/",
    "/organizations/{org}/",
    "/organizations/{org}/?term={term}",
    "/organizations/{org}/?term=abc",
    "/organizations/{org}/?term=2026-02-30",
    "/organizations/{org}/edit/",
    "/organizations/{org}/sitzungen.ics",
    "/persons/",
    "/persons/?q=Anna+Amberg&active=0",
    "/persons/?q=M%C3%BCller%26Co&page=x",
    "/persons/create/",
    "/persons/{person}/",
    "/persons/{person}/edit/",
    "/persons/{person}/auskunft.json",
    "/archive/",
    "/settings/",
    "/settings/terms/",
    "/settings/roles/",
    "/settings/roles/?edit={role}",
    "/settings/roles/?edit=abc",
    "/settings/users/",
    "/settings/users/?page=x",
    "/settings/users/invite/",
    "/settings/delegations/",
    "/settings/four-eyes/",
    "/settings/cosign/",
    "/settings/numbering/",
    "/settings/textblocks/",
    "/settings/meeting-formats/",
    "/settings/api-tokens/",
    "/settings/buergerportal-beenden/",
    "/settings/privacy/",
    "/devices/",
    "/devices/{device}/protokoll.pdf",
    "/audit/",
    "/audit/?action=abc&page=x&user=abc&date_from=2026-02-30",
    "/api/",
    "/api/session/meetings/",
    "/api/session/papers/",
    "/api/session/applications/",
]

#: Seiten außerhalb des Mandantenpfads
SEITEN_OHNE_KUERZEL = [
    "/api/v1/session/{slug}/",
    "/api/v1/session/{slug}/meetings/",
    "/api/v1/session/{slug}/papers/",
    "/api/v1/session/{slug}/applications/",
    "/session/{slug}/datenschutz/",
    "/session/invite/kein-gueltiges-token/",
]

#: Formulare des Bereichs mit leeren bzw. ungültigen Kennungen (nur als Administrator)
FORMULARE: list[tuple[str, dict[str, str]]] = [
    ("/settings/terms/save/", {}),
    ("/settings/terms/save/", {"name": "X", "term_id": "abc", "start_date": "2026-02-30"}),
    ("/settings/terms/change/", {"name": "X", "start_date": "abc", "mode": "abc"}),
    ("/organizations/{org}/memberships/add/", {"person": "abc"}),
    ("/organizations/{org}/memberships/add/", {"person": "", "substitute_for": "abc"}),
    ("/organizations/{org}/memberships/add/", {"person": "{person_b}"}),
    ("/memberships/{membership}/update/", {"substitute_for": "abc", "start_date": "x", "end_date": "1990-01-01"}),
    ("/memberships/{membership}/end/", {"end_date": "1990-01-01"}),
    ("/memberships/{membership}/succession/", {"successor": "abc"}),
    ("/memberships/{membership}/succession/", {"successor": "{person}", "change_date": "1990-01-01"}),
    ("/settings/roles/save/", {}),
    ("/settings/roles/save/", {"role_id": "abc", "name": "Rolle X"}),
    ("/settings/roles/delete/", {"role_id": "abc"}),
    ("/settings/users/invite/", {"email": "neu@example.org", "roles": "abc"}),
    ("/settings/users/{session_user}/roles/", {"roles": "abc"}),
    ("/settings/delegations/create/", {"delegate": "abc", "principal": "abc", "start_date": "x"}),
    ("/device-grants/add/", {"person": "abc", "amount": "1e3"}),
    ("/devices/{device}/unbekannt/", {}),
    ("/devices/{device}/issue/", {"person": "abc"}),
    ("/settings/api-tokens/create/", {"name": "X", "expires_at": "2026-02-30"}),
    ("/settings/privacy/", {"persons_years": "abc", "audit_years": "-1"}),
    ("/settings/reminders/", {"rsvp_days": "abc"}),
    ("/audit/export/", {"format": "abc", "date_from": "2026-02-30", "user": "abc"}),
    ("/audit/pruefen/", {"seq": "abc"}),
]

#: Objekte des Mandanten B unter dem Kürzel von A: 404, nie Daten von B
FREMDE_SEITEN = [
    "/organizations/{org_b}/",
    "/organizations/{org_b}/edit/",
    "/persons/{person_b}/",
    "/persons/{person_b}/edit/",
    "/persons/{person_b}/auskunft.json",
    "/devices/{device_b}/protokoll.pdf",
]

#: Links des Bereichs in der Seitenleiste (Präfixe relativ zum Mandanten)
BEREICH_IN_DER_SEITENLEISTE = ("/organizations/", "/persons/", "/settings/", "/devices/", "/audit/")

NAV_RE = re.compile(r'<nav class="flex-1 py-4[^"]*">(.*?)</nav>', re.S)
HREF_RE = re.compile(r'href="([^"]+)"')


@dataclass
class World:
    tenant: SessionTenant
    other: SessionTenant
    ids: dict[str, str]
    clients: dict[str, Client]
    users: list[User] = field(default_factory=list)

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}" + pfad.format(**self.ids)


def _user(world: World, email: str) -> User:
    user = cast(User, cast(Any, UserFactory)(email=email))
    world.users.append(user)
    return user


def _client(user: User | None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _bestand(tenant: SessionTenant, kennung: str) -> dict[str, Any]:
    """Gremium mit Untergremium, Personen, Besetzungen, Periode, Sitzung, Vorlage, Geräte, Zuschuss."""
    heute = timezone.localdate()
    term = SessionLegislativeTerm.objects.create(tenant=tenant, name=f"WP {kennung}", start_date=date(2024, 6, 9))
    org = SessionOrganization.objects.create(tenant=tenant, name=f"Rat {kennung}", organization_type="council")
    SessionOrganization.objects.create(tenant=tenant, name=f"Unterausschuss {kennung}", parent=org)
    anna = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Amberg", email="a@example.org")
    cast(Any, anna).set_bank_iban_encrypted("DE02120300000000202051")
    anna.save()
    bernd = SessionPerson.objects.create(tenant=tenant, given_name="Bernd", family_name="Berg", is_active=False)
    SessionPerson.objects.create(tenant=tenant, given_name="Zoë", family_name="Łukasiewicz")
    membership = SessionOrganizationMembership.objects.create(
        organization=org, person=anna, start_date=date(2024, 7, 1), legislative_term=term, substitute_for=bernd
    )
    SessionOrganizationMembership.objects.create(
        organization=org, person=bernd, start_date=date(2024, 7, 1), end_date=date(2025, 1, 31), legislative_term=term
    )
    SessionMeeting.objects.create(
        tenant=tenant, organization=org, name=f"Sitzung {kennung}", start=timezone.now() + timedelta(days=7)
    )
    SessionPaper.objects.create(tenant=tenant, reference=f"V/{kennung}", name="Vorlage", is_public=True)
    device = SessionDevice.objects.create(
        tenant=tenant, label="iPad", status="issued", issued_to=anna, issued_at=timezone.now()
    )
    SessionDevice.objects.create(tenant=tenant, label="iPad defekt", status="defect")
    SessionDeviceGrant.objects.create(tenant=tenant, person=anna, amount=Decimal("300.00"))
    SessionAPIToken.create_token(tenant, f"Fraktion {kennung}")
    del heute
    return {"term": term, "org": org, "person": anna, "membership": membership, "device": device}


def _build() -> World:
    tenant = SessionTenant.objects.create(name="Stadt Seitentest", slug="seitentest")
    other = SessionTenant.objects.create(name="Stadt Fremd", slug="seitentest-fremd")
    eigen = _bestand(tenant, "A")
    fremd = _bestand(other, "B")
    rollen = SessionRole.create_default_roles(tenant)
    nur_benutzer = SessionRole.objects.create(
        tenant=tenant,
        name="Nur Benutzerverwaltung",
        **{
            f.name: f.name in ("can_view_dashboard", "can_manage_users")
            for f in SessionRole._meta.concrete_fields
            if f.name.startswith("can_")
        },
    )
    world = World(tenant=tenant, other=other, ids={}, clients={})
    session_users: dict[str, SessionUser] = {}
    for name in ROLLEN:
        if name == "anonym":
            world.clients[name] = _client(None)
            continue
        user = _user(world, f"seitentest-{name}@example.org")
        world.clients[name] = _client(user)
        if name == "ohne_mandant":
            continue
        session_user = SessionUser.objects.create(user=user, tenant=tenant)
        session_user.roles.add(nur_benutzer if name == "benutzerverwaltung" else rollen[STANDARDROLLE[name]])
        if name == "verwaltung":
            session_user.roles.add(rollen["revision"])
        session_users[name] = session_user
    invitation = SessionInvitation.create_for_tenant(tenant, "eingeladen@example.org", roles=[rollen["viewer"]])
    world.ids = {
        "slug": tenant.slug,
        "term": str(eigen["term"].pk),
        "org": str(eigen["org"].pk),
        "person": str(eigen["person"].pk),
        "membership": str(eigen["membership"].pk),
        "device": str(eigen["device"].pk),
        "role": str(rollen["clerk"].pk),
        "session_user": str(session_users["lesezugriff"].pk),
        "invitation": str(invitation.pk),
        "org_b": str(fremd["org"].pk),
        "person_b": str(fremd["person"].pk),
        "device_b": str(fremd["device"].pk),
    }
    return world


@pytest.fixture(scope="module")
def world(django_db_setup: None, django_db_blocker: Any) -> Iterator[World]:
    with django_db_blocker.unblock(), sicherheitsprotokoll_zuruecksetzen():
        built = _build()
        yield built
        SessionTenant.objects.filter(pk__in=[built.tenant.pk, built.other.pk]).delete()
        User.objects.filter(pk__in=[user.pk for user in built.users]).delete()


def _abrufen(client: Client, methode: str, url: str, **kwargs: Any) -> tuple[int | None, str]:
    """Status und – bei einer Ausnahme – deren Art; Ausnahmen werden gesammelt, nicht sofort geworfen."""
    try:
        response = getattr(client, methode)(url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - jede Ausnahme ist ein Befund dieses Tests
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"
    return response.status_code, ""


def _befund(status: int | None, fehler: str) -> bool:
    return status is None or status >= 500


@pytest.mark.parametrize("rolle", ROLLEN)
def test_seiten_ohne_serverfehler(world: World, rolle: str) -> None:
    client = world.clients[rolle]
    befunde = []
    adressen = [world.url(pfad) for pfad in SEITEN] + [pfad.format(**world.ids) for pfad in SEITEN_OHNE_KUERZEL]
    for url in adressen:
        for kopf in ({}, {"HTTP_HX_REQUEST": "true"}):
            status, fehler = _abrufen(client, "get", url, **kopf)
            if _befund(status, fehler):
                befunde.append(f"GET {url} {'(HTMX) ' if kopf else ''}→ {status} {fehler}")
    assert not befunde, "\n".join(befunde)


def test_formulare_mit_ungueltigen_kennungen(world: World) -> None:
    client = world.clients["verwaltung"]
    befunde = []
    for pfad, daten in FORMULARE:
        url = world.url(pfad)
        status, fehler = _abrufen(client, "post", url, data={k: v.format(**world.ids) for k, v in daten.items()})
        if _befund(status, fehler):
            befunde.append(f"POST {url} {daten} → {status} {fehler}")
    assert not befunde, "\n".join(befunde)


@pytest.mark.parametrize("rolle", [r for r in ROLLEN if r not in ("anonym", "ohne_mandant")])
def test_seitenleiste_zeigt_nur_erreichbare_links(world: World, rolle: str) -> None:
    client = world.clients[rolle]
    # Erste Seite, die die Rolle öffnen darf: Dashboard, sonst Einstellungen oder Audit-Log
    for start in ("/dashboard/", "/settings/", "/audit/"):
        response = client.get(world.url(start))
        if response.status_code == 200:
            break
    else:
        pytest.fail(f"{rolle}: keine Startseite erreichbar")
    treffer = NAV_RE.search(response.content.decode())
    assert treffer, "Seitenleiste nicht gefunden"
    basis = world.url("")
    links = {
        href
        for href in HREF_RE.findall(treffer.group(1))
        if href.startswith(basis) and href[len(basis) :].startswith(BEREICH_IN_DER_SEITENLEISTE)
    }
    tot = [f"{href} → {client.get(href).status_code}" for href in sorted(links) if client.get(href).status_code != 200]
    assert not tot, f"{rolle}: Links in der Seitenleiste ohne Zugriff:\n" + "\n".join(tot)


@pytest.mark.parametrize("pfad", FREMDE_SEITEN)
def test_fremde_objekte_unter_eigenem_kuerzel(world: World, pfad: str) -> None:
    status, fehler = _abrufen(world.clients["verwaltung"], "get", world.url(pfad))
    assert status == 404, f"{pfad} → {status} {fehler}"
