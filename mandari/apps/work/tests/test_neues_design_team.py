# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Team und Mitgliedsprofil im neuen Erscheinungsbild (Issue #951, Teilpaket 4).

Mit eingeschaltetem Schalter rendern TeamDirectoryView und TeamMemberProfileView je eine neue Vorlage aus
work/team/neu/. Geprüft werden Gleichstand (apps.work.tests.gleichstand), Schalter und Rückweg, Rechte je Rolle und die
Bausteine statt Hand-Markup.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.organization.models import MemberAbsence
from apps.work.tests.gleichstand import vergleiche

pytestmark = pytest.mark.django_db

VORLAGEN = Path(settings.BASE_DIR) / "templates" / "work" / "team" / "neu"


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, ["dashboard.view"], email="vera@example.org")
    m.user.first_name, m.user.last_name = "Vera", "Beispiel"
    m.user.save()
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    org.owner = m.user
    org.save(update_fields=["owner"])
    return m


@pytest.fixture
def mitglied(org: Any, make_member: Any, vorsitz: Any) -> Any:
    m = make_member(org, ["dashboard.view"], email="mira@example.org")
    m.user.first_name, m.user.last_name = "Mira", "Mitglied"
    m.user.phone = "+49 251 123"
    m.user.settings = {
        "profile": {
            "bio": "Verkehr & Mobilität",
            "show_phone": True,
            "preferred_contact": "signal",
            "contact_signal": "@mira.01",
        }
    }
    m.user.save()
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org,
        membership=m,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=4),
        reason="Urlaub",
        deputy=vorsitz,
    )
    return m


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def test_gleichstand_verzeichnis(org: Any, vorsitz: Any, mitglied: Any, gast: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    neu, antwort = vergleiche(client, _url("team", org))
    html = antwort.content.decode()
    assert "3 aktive Mitglieder in Fraktion Test." in html
    assert "Eigentümer · vereidigt" in html and "Abwesend bis" in html and "Gast" in html
    assert f"data-href={_url('team_profile', org, member_id=mitglied.id)}" in neu.aktionen
    vergleiche(client, _url("team", org) + "?q=Mira")
    leer, antwort = vergleiche(client, _url("team", org) + "?q=Nie%26mand")
    assert "Keine Mitglieder zu „Nie&amp;mand“." in antwort.content.decode()  # einfach maskiert


def test_gleichstand_mitgliedsprofil(org: Any, vorsitz: Any, mitglied: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    neu, antwort = vergleiche(client, _url("team_profile", org, member_id=mitglied.id))
    html = antwort.content.decode()
    assert "Verkehr &amp; Mobilität" in html and "Signal: @mira.01" in html and "+49 251 123" in html
    assert "Stellvertretung:" in html and _url("team_profile", org, member_id=vorsitz.id) in neu.links
    assert "Profil bearbeiten" not in html

    _, eigenes = vergleiche(client, _url("team_profile", org, member_id=vorsitz.id))
    eigen = eigenes.content.decode()
    assert "Profil bearbeiten" in eigen and 'data-testid="team-profil-selbst"' in eigen
    assert "Eigentümer, vereidigt" in eigen


def test_schalter_aus_und_rueckweg(org: Any, vorsitz: Any, mitglied: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    for url in (_url("team", org), _url("team_profile", org, member_id=mitglied.id)):
        assert 'data-gestaltung="neu"' in client.get(url).content.decode()
        assert 'data-gestaltung="neu"' not in client.get(url, {"ansicht": "bisher"}).content.decode()
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    antwort = client.get(_url("team", org))
    assert "work/team/directory.html" in {t.name for t in antwort.templates}


def test_rechte_wie_bisher(org: Any, vorsitz: Any, mitglied: Any, gast: Any, make_member: Any, client_for: Any) -> None:
    ohne = make_member(org, [], email="ohne@example.org")
    for url in (_url("team", org), _url("team_profile", org, member_id=mitglied.id)):
        for person in (vorsitz, gast, ohne):
            client = client_for(person.user)
            neu, bisher = client.get(url), client.get(url, {"ansicht": "bisher"})
            assert neu.status_code == bisher.status_code, (url, person.user.email, neu.status_code, bisher.status_code)


def test_vorlagen_ohne_kaesten_und_farbchips() -> None:
    for datei in sorted(VORLAGEN.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert "rounded-xl" not in text and "shadow" not in text and ' style="' not in text, datei.name
        assert "rounded-full text-xs" not in text and "uppercase" not in text, datei.name
    for seite in ("verzeichnis.html", "profil.html"):
        text = (VORLAGEN / seite).read_text(encoding="utf-8")
        assert "<c-rahmen.kopfband" in text and " dicht" in text and "{% block gestaltung %}neu{% endblock %}" in text
    assert "<c-form.suche" in (VORLAGEN / "verzeichnis.html").read_text(encoding="utf-8")
    assert "<c-ui.angaben" in (VORLAGEN / "profil.html").read_text(encoding="utf-8")
