# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Fixtures und Helfer der Tests zu den Einstellungen im neuen Erscheinungsbild (Issue #951, Teilpaket 3).

Die Testmodule test_einstellungen_3a/3b/3c.py übernehmen die Fixtures per Import. ``vergleich`` rendert eine Seite
in der bisherigen (``?ansicht=bisher``) und der neuen Vorlage und liefert die Funktionen beider (gleichstand.py).
``rechte_wie_bisher`` prüft eine Seite für jede Rolle aus ``ROLLEN``: derselbe Antwortcode in beiden Vorlagen und, wo
die Seite sich öffnet, derselbe Funktionsumfang.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.tenants.models import PartyGroup
from apps.work.organization import einstellungen

from .gleichstand import fehlend, funktionen

#: Rollen für den Vergleich je Recht (neben ``admin`` und ``leser``): Einzelrechte, wie Fraktionen sie verteilen
ROLLEN: dict[str, list[str]] = {
    "bearbeitung": ["dashboard.view", "organization.view", "organization.edit", "members.view"],
    "fraktionsleitung": ["dashboard.view", "organization.view", "faction.manage", "protocols.publish", "members.view"],
    "mitgliederverwaltung": [
        "dashboard.view",
        "organization.view",
        "members.view",
        "members.view_details",
        "members.invite",
        "members.edit",
        "members.remove",
        "members.manage_roles",
        "guests.invite",
        "guests.manage",
    ],
    "rollenverwaltung": ["dashboard.view", "organization.view", "organization.manage_roles", "members.view"],
    "mitglied": ["dashboard.view", "motions.view"],
}


@pytest.fixture
def org(org: Any) -> Any:
    """Organisation mit eingeschaltetem neuen Erscheinungsbild (wie in Produktion seit 06.10.2026)."""
    org.work_new_design = True
    org.description = "Fraktion im Rat"
    org.contact_email = "info@fraktion.example.org"
    org.address = "Rathausplatz 1\n12345 Stadt"
    partei = PartyGroup.objects.create(name="Musterpartei", slug="musterpartei", abbreviation="MP")
    org.party_group = partei
    org.save()
    PartyGroup.objects.create(name="Andere Partei", slug="andere-partei", abbreviation="AP")
    return org


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, [], email="admin@example.org", is_admin=True)


@pytest.fixture
def leser(org: Any, make_member: Any) -> Any:
    """Sieht die Einstellungen, darf aber nichts ändern."""
    return make_member(org, ["dashboard.view", "organization.view", "members.view"], email="leser@example.org")


@pytest.fixture
def rollen(org: Any, admin: Any, leser: Any, make_member: Any) -> dict[str, Any]:
    """Administration, Leserin und je eine Person mit den Rechten aus ``ROLLEN``."""
    weitere = {name: make_member(org, rechte, email=f"rolle-{name}@example.org") for name, rechte in ROLLEN.items()}
    return {"admin": admin, "leser": leser, **weitere}


def url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def html(antwort: Any) -> str:
    assert antwort.status_code == 200, antwort.status_code
    return str(antwort.content.decode())


def vorlagen(antwort: Any) -> set[str]:
    return {t.name for t in antwort.templates if t.name}


def unterpunkte_anderer_reiter(org: Any, seite: str) -> set[str]:
    """
    Bisherige Reiter, die jetzt Unterpunkt eines anderen Reiters sind (E-Mail, Registrierung, API, Rollen,
    Ratsfraktionen …): Die alte Leiste verlinkte sie auf jeder Seite, die neue über ihren Reiter. Dass jeder dieser
    Unterpunkte dort steht, prüft test_jede_bisherige_reiterseite_bleibt_erreichbar. Dazu die Dokumenteinstellungen:
    Sie leiten im neuen Erscheinungsbild auf die eine Übersicht weiter (Issue #1230, test_dokumenteinstellungen_*).
    """
    eigener = next(r for r in einstellungen.REITER if any(s.key == seite for s in r.seiten))
    return {
        f"link:{reverse(s.url_name, kwargs={'org_slug': org.slug})}"
        for r in einstellungen.REITER
        if r is not eigener
        for s in r.seiten[1:]
    } | {f"link:{url('document_settings', org)}"}


def ohne_zugriff(org: Any, membership: Any) -> set[str]:
    """
    Links der bisherigen Leiste auf Seiten, die die Person nicht öffnen darf: Seit Issue #1230 zeigt die neue
    Navigation sie nicht mehr (bisher endeten sie mit 403). Dass sie fehlen, prüft test_reiter_nur_mit_zugriff.
    """
    return {
        f"link:{reverse(s.url_name, kwargs={'org_slug': org.slug})}"
        for r in einstellungen.REITER
        for s in r.seiten
        if not einstellungen.seite_sichtbar(s.key, membership)
    }


#: Aktionen der Verwaltungskontakte, die seit Issue #1230 auf ihrer eigenen Unterseite stehen
KONTAKT_AKTIONEN = ("action=add_admin_contact", "action=delete_admin_contact")


def kontakte(funktionen_der_seite: set[str]) -> set[str]:
    """Kennungen der Formulare für Verwaltungskontakte (bisher auf der Seite der Ratsfraktionen)."""
    return {k for k in funktionen_der_seite if any(aktion in k for aktion in KONTAKT_AKTIONEN)}


def vergleich(client: Any, pfad: str, *weitere: str) -> tuple[set[str], set[str]]:
    """Funktionen der bisherigen Vorlage (``?ansicht=bisher``) und der neuen (samt weiterer Ansichten derselben Seite)."""
    trenner = "&" if "?" in pfad else "?"
    alt = funktionen(html(client.get(f"{pfad}{trenner}ansicht=bisher")), pfad)
    neu: set[str] = set()
    for ansicht in (pfad, *weitere):
        neu |= funktionen(html(client.get(ansicht)), ansicht)
    return alt, neu


def rechte_wie_bisher(
    client_for: Any,
    rollen: dict[str, Any],
    pfad: str,
    ausnahmen: set[str] | frozenset[str] = frozenset(),
    weitere: tuple[str, ...] = (),
) -> dict[str, int]:
    """
    Für jede Rolle: derselbe Antwortcode in der neuen und der bisherigen Vorlage, und wo die Seite sich öffnet, fehlt
    der neuen keine Funktion der bisherigen (was die bisherige je Recht zeigt, zeigt die neue genauso; ausgenommen
    Links auf Seiten ohne Zugriff, die die Navigation seit Issue #1230 nicht mehr zeigt, ``ohne_zugriff``). ``weitere``
    sind Ansichten derselben Seite in der neuen Vorlage (etwa die Anmeldesicherheit), die zusammen den Umfang der
    bisherigen Seite tragen. Liefert die Antwortcodes je Rolle, damit der Test sie zusätzlich festhalten kann.
    """
    trenner = "&" if "?" in pfad else "?"
    codes: dict[str, int] = {}
    for name, person in rollen.items():
        client = client_for(person.user)
        ausnahmen_person = set(ausnahmen) | ohne_zugriff(person.organization, person)
        antworten = [(ansicht, client.get(ansicht)) for ansicht in (pfad, *weitere)]
        alt = client.get(f"{pfad}{trenner}ansicht=bisher")
        assert [a.status_code for _, a in antworten] == [alt.status_code] * len(antworten), (name, alt.status_code)
        if alt.status_code == 200:
            neu: set[str] = set()
            for ansicht, antwort in antworten:
                neu |= funktionen(html(antwort), ansicht)
            luecken = fehlend(funktionen(html(alt), pfad), neu, ausnahmen_person)
            assert luecken == [], (name, luecken)
        codes[name] = alt.status_code
    return codes


def fixtures_uebernehmen(ziel: dict[str, Any]) -> None:
    """Fixtures ``org`` (Schalter an), ``admin``, ``leser`` und ``rollen`` in den Namensraum eines Testmoduls übernehmen."""
    ziel.update({"org": org, "admin": admin, "leser": leser, "rollen": rollen})
