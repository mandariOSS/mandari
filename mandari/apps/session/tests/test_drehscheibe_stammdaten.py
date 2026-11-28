# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet ihre Stammdaten an die Datendrehscheibe (Issue #860).

Geprüft an den Fachfunktionen (Ansichten und Dienste, wie die Oberfläche sie aufruft):

- Gremien anlegen, bearbeiten, deaktivieren (``ris.organization.changed``),
- Personen anlegen und bearbeiten, ohne geschützte Daten (``ris.person.changed``),
- Besetzungen anlegen, bearbeiten, beenden, Nachrücken (``ris.membership.changed`` mit Person und Gremium),
- Wahlperioden anlegen, bearbeiten, löschen und der Periodenwechsel (``ris.legislativeterm.changed``,
  ``ris.object.depublished``),
- die Körperschaft: Lizenz der offenen Daten (Einstellungen) und Stammdaten im Django-Admin (``ris.body.changed``).

Jedes Ereignis prüft ``publish()`` gegen seinen Vertrag (``EVENTS_VALIDATE_CONTRACTS``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib import admin
from django.core.cache import cache
from django.db.models import Max
from django.test import Client, RequestFactory
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.common.tests.factories import UserFactory
from apps.events.models import Event
from apps.session.models import (
    SessionLegislativeTerm,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"
ALLE_RECHTE = {f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}
GREMIUM = {"organization_type": "committee", "invitation_period_days": "7", "allowance_amount": "0"}
#: Inhalte, die nie in einer Nutzlast stehen dürfen
GEHEIM = ("0221 12345", "Geheimweg 7", "karl@example.org", "Jugendbeirat", "Muster")


def kennung(kind: str, pk: Any) -> str:
    return str(canonical_id(f"{BASIS}{kind}/{pk}/"))


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    petra: SessionPerson
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def post(self, pfad: str, daten: dict[str, Any]) -> None:
        antwort = self.client.post(self.url(pfad), daten)
        assert antwort.status_code == 302, antwort.content[:500]


@pytest.fixture
def welt(settings: Any) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.SESSION_EVENTS = "aktiv"
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", oparl_public_since=timezone.now())
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat", organization_type="council")
    petra = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
    rolle = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", **dict.fromkeys(ALLE_RECHTE, True))
    user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    user.roles.add(rolle)
    client = Client()
    client.force_login(user.user)
    return Welt(tenant=tenant, rat=rat, petra=petra, client=client)


def _start() -> int:
    return int(Event.objects.aggregate(hoechste=Max("id"))["hoechste"] or 0)


def _neu(seit: int) -> list[Event]:
    events = list(Event.objects.filter(id__gt=seit).order_by("id"))
    for event in events:
        # Nie Inhalte, nie geschützte Personendaten: nur Kennungen, Codes und Feldnamen
        text = str(event.payload)
        assert not any(geheim in text for geheim in GEHEIM), event.type
        assert event.tenant_ref.startswith("session:")
    return events


def _kurz(events: list[Event]) -> list[tuple[str, str, str]]:
    return [(e.type, e.visibility, e.operation) for e in events]


# -- Gremien ---------------------------------------------------------------------------------------------


def test_gremium_anlegen_bearbeiten_deaktivieren(welt: Welt) -> None:
    seit = _start()
    welt.post("/organizations/create/", {**GREMIUM, "name": "Jugendbeirat", "short_name": "JB", "parent": welt.rat.pk})
    gremium = SessionOrganization.objects.get(name="Jugendbeirat")
    [angelegt] = _neu(seit)
    assert (angelegt.type, angelegt.visibility, angelegt.aggregate_type) == (
        "ris.organization.changed",
        "oeffentlich",
        "Organization",
    )
    assert angelegt.payload == {"organization": kennung("organization", gremium.pk), "change": "added"}
    assert str(angelegt.aggregate_id) == kennung("organization", gremium.pk)
    assert str(angelegt.body_id) == str(canonical_id(f"{BASIS}body/"))

    seit = _start()
    daten = {**GREMIUM, "name": "Jugendrat", "short_name": "JB", "parent": welt.rat.pk, "organization_type": "advisory"}
    welt.post(f"/organizations/{gremium.pk}/edit/", daten)
    [geaendert] = _neu(seit)
    assert geaendert.payload["changed"] == ["name", "classification"]

    # Aktiv kennt das kanonische Modell nicht, Speichern ohne Änderung meldet nichts
    seit = _start()
    welt.post(f"/organizations/{gremium.pk}/deactivate/", {})
    welt.post(f"/organizations/{gremium.pk}/edit/", daten)
    assert _neu(seit) == []


# -- Personen --------------------------------------------------------------------------------------------


def test_person_ohne_geschuetzte_daten(welt: Welt) -> None:
    seit = _start()
    welt.post("/persons/create/", {"given_name": "Karl", "family_name": "Neu", "is_active": "on"})
    karl = SessionPerson.objects.get(family_name="Neu")
    assert [(e.type, e.payload) for e in _neu(seit)] == [
        ("ris.person.changed", {"person": kennung("person", karl.pk), "change": "added"})
    ]

    # Telefon, Adresse und eine nicht veröffentlichte E-Mail: nichts, was die Schnittstelle zeigt
    seit = _start()
    geschuetzt = {
        "given_name": "Karl",
        "family_name": "Neu",
        "is_active": "on",
        "phone": GEHEIM[0],
        "address": GEHEIM[1],
        "email": GEHEIM[2],
    }
    welt.post(f"/persons/{karl.pk}/edit/", geschuetzt)
    assert _neu(seit) == []

    # Titel ändert auch den Anzeigenamen; die veröffentlichte E-Mail erscheint nur als Feldname
    seit = _start()
    welt.post(
        f"/persons/{karl.pk}/edit/",
        {
            **geschuetzt,
            "title": "Dr.",
            "contact_publish": "on",
            "contact_consent_date": "2026-01-05",
            "contact_consent_evidence": "Formular",
        },
    )
    [geaendert] = _neu(seit)
    assert (geaendert.visibility, geaendert.payload["changed"]) == ("oeffentlich", ["name", "title", "email"])

    seit = _start()
    welt.post(f"/persons/{karl.pk}/deactivate/", {})
    assert _neu(seit) == []


def test_vor_der_freischaltung_nur_nichtoeffentlich(welt: Welt) -> None:
    welt.tenant.oparl_public_since = None
    welt.tenant.save()
    seit = _start()
    welt.post("/persons/create/", {"given_name": "Karl", "family_name": "Neu", "is_active": "on"})
    assert _kurz(_neu(seit)) == [("ris.person.changed", "nichtoeffentlich", "upsert")]


# -- Besetzungen -----------------------------------------------------------------------------------------


def test_besetzung_anlegen_bearbeiten_beenden_nachruecken(welt: Welt) -> None:
    seit = _start()
    welt.post(
        f"/organizations/{welt.rat.pk}/memberships/add/",
        {"person": welt.petra.pk, "role": "member", "start_date": "2026-01-01", "has_voting_rights": "on"},
    )
    sitz = SessionOrganizationMembership.objects.get(person=welt.petra)
    [angelegt] = _neu(seit)
    assert angelegt.type == "ris.membership.changed"
    assert angelegt.payload == {
        "membership": kennung("membership", sitz.pk),
        "change": "added",
        "person": kennung("person", welt.petra.pk),
        "organization": kennung("organization", welt.rat.pk),
    }

    seit = _start()
    welt.post(
        f"/memberships/{sitz.pk}/update/",
        {"role": "deputy_chair", "start_date": "2026-01-01", "has_voting_rights": "on"},
    )
    [geaendert] = _neu(seit)
    assert geaendert.payload["changed"] == ["role"]

    seit = _start()
    welt.post(f"/memberships/{sitz.pk}/end/", {"end_date": "2030-10-31"})
    assert [e.payload["changed"] for e in _neu(seit)] == [["endDate"]]

    karl = SessionPerson.objects.create(tenant=welt.tenant, given_name="Karl", family_name="Neu")
    seit = _start()
    welt.post(f"/memberships/{sitz.pk}/succession/", {"successor": karl.pk, "change_date": "2027-03-01"})
    nachfolge = SessionOrganizationMembership.objects.get(person=karl)
    events = _neu(seit)
    assert sorted((e.payload["membership"], e.payload["change"]) for e in events) == sorted(
        [(kennung("membership", sitz.pk), "changed"), (kennung("membership", nachfolge.pk), "added")]
    )


# -- Wahlperioden ----------------------------------------------------------------------------------------


def test_wahlperiode_anlegen_bearbeiten_loeschen(welt: Welt) -> None:
    seit = _start()
    welt.post("/settings/terms/save/", {"name": "2025–2030", "start_date": "2025-11-01"})
    periode = SessionLegislativeTerm.objects.get(tenant=welt.tenant)
    [angelegt] = _neu(seit)
    assert angelegt.payload == {"legislative_term": kennung("legislativeterm", periode.pk), "change": "added"}

    seit = _start()
    welt.post(
        "/settings/terms/save/",
        {"term_id": periode.pk, "name": "WP 2025–2030", "start_date": "2025-11-01", "number": "11"},
    )
    # Die Nummer kennt das kanonische Modell nicht
    assert [e.payload["changed"] for e in _neu(seit)] == [["name"]]

    leer = SessionLegislativeTerm.objects.create(tenant=welt.tenant, name="Alt", start_date=None)
    seit = _start()
    welt.post(f"/settings/terms/{leer.pk}/delete/", {})
    [weg] = _neu(seit)
    assert (weg.type, weg.operation, weg.payload) == (
        "ris.object.depublished",
        "delete",
        {"object_type": "LegislativeTerm", "object": kennung("legislativeterm", leer.pk), "reason": "quelle_geloescht"},
    )


def test_periodenwechsel_meldet_perioden_und_besetzungen(welt: Welt) -> None:
    beginn = timezone.localdate()
    periode = SessionLegislativeTerm.objects.create(tenant=welt.tenant, name="2025–2030", start_date=beginn)
    sitz = SessionOrganizationMembership.objects.create(
        organization=welt.rat, person=welt.petra, start_date=beginn, legislative_term=periode
    )
    stichtag = beginn + timedelta(days=5 * 365)
    seit = _start()
    welt.post("/settings/terms/change/", {"name": "2030–2035", "start_date": stichtag.isoformat(), "mode": "carry"})
    neue_periode = SessionLegislativeTerm.objects.get(name="2030–2035")
    uebernommen = SessionOrganizationMembership.objects.exclude(pk=sitz.pk).get(person=welt.petra)
    events = _neu(seit)
    # Erst die Wahlperioden, dann die Besetzungen
    assert [e.aggregate_type for e in events] == ["LegislativeTerm", "LegislativeTerm", "Membership", "Membership"]
    assert {(str(e.aggregate_id), e.payload["change"], tuple(e.payload.get("changed", ()))) for e in events} == {
        (kennung("legislativeterm", periode.pk), "changed", ("endDate",)),
        (kennung("legislativeterm", neue_periode.pk), "added", ()),
        (kennung("membership", sitz.pk), "changed", ("endDate",)),
        (kennung("membership", uebernommen.pk), "added", ()),
    }


# -- Körperschaft ----------------------------------------------------------------------------------------


def test_lizenz_der_offenen_daten_meldet_die_koerperschaft(welt: Welt) -> None:
    seit = _start()
    welt.post("/settings/oparl-lizenz/", {"license": "https://www.govdata.de/dl-de/zero-2-0"})
    [geaendert] = _neu(seit)
    koerperschaft = str(canonical_id(f"{BASIS}body/"))
    assert (geaendert.type, geaendert.aggregate_type, str(geaendert.aggregate_id)) == (
        "ris.body.changed",
        "Body",
        koerperschaft,
    )
    assert geaendert.payload == {
        "body": koerperschaft,
        "change": "changed",
        "changed": ["license", "licenseValidSince"],
    }
    # Unverändert: nichts
    seit = _start()
    welt.post("/settings/oparl-lizenz/", {"license": "https://www.govdata.de/dl-de/zero-2-0"})
    assert _neu(seit) == []


def test_stammdaten_der_koerperschaft_im_admin(welt: Welt) -> None:
    from apps.session.admin import SessionTenantAdmin

    anfrage = RequestFactory().post("/admin/")
    anfrage.user = cast(Any, UserFactory)(is_staff=True, is_superuser=True)
    verwaltung: Any = SessionTenantAdmin(SessionTenant, admin.site)
    mandant = SessionTenant.objects.get(pk=welt.tenant.pk)
    mandant.short_name = "Nord"
    mandant.website = "https://nord.example"
    seit = _start()
    verwaltung.save_model(anfrage, mandant, form=None, change=True)
    [geaendert] = _neu(seit)
    assert (geaendert.type, geaendert.visibility, geaendert.payload["changed"]) == (
        "ris.body.changed",
        "oeffentlich",
        ["shortName", "website"],
    )
    # Deaktivieren meldet wie die Rücknahme der Freischaltung nichts
    mandant.is_active = False
    seit = _start()
    verwaltung.save_model(anfrage, mandant, form=None, change=True)
    assert _neu(seit) == []
