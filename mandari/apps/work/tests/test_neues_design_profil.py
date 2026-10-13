# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mein Profil im neuen Erscheinungsbild (Issue #951, Teilpaket 4): neun Seiten unter sechs Reitern.

Reiter: Profil (mit Sichtbarkeit und Kontakt) · Gremien · Benachrichtigungen (mit Abwesenheit) · Sicherheit · Anträge ·
Daten und Aktivität. Alle Adressen bleiben; die zweite Seite eines Reiters erreicht man über die Textlinks unter den
Reitern. Geprüft werden Gleichstand je Seite (apps.work.tests.gleichstand), die Erreichbarkeit aller neun Seiten, Schalter
und Rückweg, Rechte (auch Gäste, die Profil und Sicherheit sehen dürfen) und die Bausteine.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import UserSession
from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Topic
from apps.work.organization.models import DataExport, MemberAbsence, MemberChangeRequest
from apps.work.tests.gleichstand import funktionen, vergleiche

pytestmark = pytest.mark.django_db

VORLAGEN = Path(settings.BASE_DIR) / "templates" / "work" / "profile" / "neu"
SEITEN = {
    "profile": "profil.html",
    "profile_visibility": "sichtbarkeit.html",
    "profile_committees": "gremien.html",
    "profile_notifications": "benachrichtigungen.html",
    "profile_absence": "abwesenheit.html",
    "security": "sicherheit.html",
    "profile_requests": "antraege.html",
    "profile_data": "daten.html",
    "profile_activity": "aktivitaet.html",
}
#: Zweite Seite eines Reiters → Seite, auf der der Link dorthin steht
UNTERSEITEN = {
    "profile_visibility": "profile",
    "profile_absence": "profile_notifications",
    "profile_activity": "profile_data",
}


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    m = make_member(org, ["dashboard.view", "members.edit"], email="vera@example.org")
    m.user.first_name, m.user.last_name, m.user.phone = "Vera", "Beispiel", "+49 251 1"
    m.user.settings = {"profile": {"bio": "Haushalt & Finanzen", "show_phone": True, "preferred_contact": "signal"}}
    m.user.save()
    return m


@pytest.fixture
def bestand(org: Any, person: Any, make_member: Any) -> dict[str, Any]:
    kollegin = make_member(org, ["dashboard.view"], email="mira@example.org")
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org,
        membership=person,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=3),
        reason="Urlaub",
        deputy=kollegin,
    )
    MemberAbsence.objects.create(
        organization=org,
        membership=person,
        start_date=heute - timedelta(days=40),
        end_date=heute - timedelta(days=30),
        reason="Reise",
    )
    MemberAbsence.objects.create(
        organization=org,
        membership=kollegin,
        start_date=heute + timedelta(days=5),
        end_date=heute + timedelta(days=9),
        deputy=person,
    )
    MemberChangeRequest.objects.create(
        organization=org, requester=person, request_type="role_change", status="pending", reason="Mehr Rechte"
    )
    MemberChangeRequest.objects.create(
        organization=org,
        requester=person,
        request_type="committee_change",
        status="rejected",
        reason="Ausschuss",
        decision_comment="Nicht jetzt",
    )
    offen = MemberChangeRequest.objects.create(
        organization=org, requester=kollegin, request_type="permission_request", status="pending", reason="Presse"
    )
    Topic.objects.create(organization=org, name="Verkehr", color="blue")
    DataExport.objects.create(
        organization=org,
        membership=person,
        status="completed",
        export_format="json",
        file_size=2048,
        completed_at=timezone.now(),
    )
    UserSession.objects.create(
        user=person.user,
        session_key="fremd-123",
        device_name="iPhone",
        ip_address="10.0.0.1",
        expires_at=timezone.now() + timedelta(days=1),
    )
    return {"kollegin": kollegin, "offen": offen}


def _url(name: str, org: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug})


def _ausnahmen(org: Any, name: str) -> set[str]:
    """
    Die neun Reiter verlinkten jede Seite direkt; die sechs Reiter führen zur ersten Seite ihres Reiters, die zweite
    erreicht man dort über die Textlinks (test_alle_neun_seiten_erreichbar), auf ihr selbst steht sie als Text. Die zwei Passwortfelder der Dialoge trugen
    beide die Kennung id_password (doppelt); die neuen tragen eigene.
    """
    ausnahmen = {f"Link {_url(seite, org)}" for seite in UNTERSEITEN}
    if name == "security":
        ausnahmen.add("Kennung id_password")
    return ausnahmen


@pytest.mark.parametrize("name", list(SEITEN))
def test_gleichstand(name: str, org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    neu, antwort = vergleiche(client_for(person.user), _url(name, org), _ausnahmen(org, name))
    html = antwort.content.decode()
    assert html.count('<a href="/work/fraktion-test/profile/') >= 6  # sechs Reiter
    assert f"work/profile/neu/{SEITEN[name]}" in {t.name for t in antwort.templates}


def test_alle_neun_seiten_erreichbar(org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    client = client_for(person.user)
    erreichbar: set[str] = set()
    for name in SEITEN:
        erreichbar |= funktionen(client.get(_url(name, org)).content.decode(), _url(name, org)).links
    assert {_url(name, org) for name in SEITEN} <= erreichbar
    for unterseite, seite in UNTERSEITEN.items():
        assert _url(unterseite, org) in funktionen(client.get(_url(seite, org)).content.decode(), "").links


def test_inhalte(org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    client = client_for(person.user)
    html = client.get(_url("profile_absence", org)).content.decode()
    assert "Sie sind abwesend vom" in html and "Stornieren" in html and "Sie vertreten" not in html
    html = client.get(_url("profile_requests", org)).content.decode()
    assert "1 Antrag wartet auf Ihre Prüfung." in html and 'data-testid="antraege-pruefen"' in html
    assert "Antwort: Nicht jetzt" in html and "Abgelehnt" in html
    html = client.get(_url("profile_visibility", org)).content.decode()
    assert "Haushalt &amp; Finanzen" in html and 'name="preferred_contact" id="sichtbarkeit-kontakt-signal"' in html
    html = client.get(_url("security", org)).content.decode()
    assert "iPhone" in html and "Alle anderen beenden" not in html  # nur eine fremde Sitzung, keine eigene
    html = client.get(_url("profile_committees", org)).content.decode()
    assert "Verkehr" in html and "Fachgebiete speichern" in html


def test_formulare_wirken_wie_bisher(org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    client = client_for(person.user)
    antwort = client.post(_url("profile", org), {"action": "update_profile", "first_name": "Vera", "last_name": "Neu"})
    assert antwort.status_code == 302
    person.user.refresh_from_db()
    assert person.user.last_name == "Neu"
    eigener = MemberChangeRequest.objects.get(requester=person, status="pending")
    antwort = client.post(_url("profile_requests", org), {"action": "withdraw_request", "request_id": str(eigener.id)})
    assert antwort.status_code == 302
    eigener.refresh_from_db()
    assert eigener.status != "pending"


def test_schalter_rueckweg_und_rechte(
    org: Any, person: Any, bestand: dict[str, Any], make_member: Any, client_for: Any
) -> None:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    ohne = make_member(org, [], email="ohne@example.org")
    for mitglied in (person, gast, ohne):
        client = client_for(mitglied.user)
        for name in SEITEN:
            neu, bisher = client.get(_url(name, org)), client.get(_url(name, org), {"ansicht": "bisher"})
            assert neu.status_code == bisher.status_code, (name, mitglied.user.email, neu.status_code)
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    client = client_for(person.user)
    for name in SEITEN:
        assert 'data-gestaltung="neu"' not in client.get(_url(name, org)).content.decode(), name


def test_vorlagen_ohne_kaesten_pillen_und_overlays() -> None:
    for datei in sorted(VORLAGEN.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert "rounded-xl" not in text and "shadow" not in text and ' style="' not in text, datei.name
        assert "fixed inset-0" not in text and "<script" not in text, datei.name
        assert not re.search(r'class="[^"]*uppercase', text), datei.name
        assert "rounded-full text-xs" not in text and "peer-checked:translate-x" not in text, datei.name
        assert len(text.splitlines()) <= 300, datei.name
    for seite in SEITEN.values():
        text = (VORLAGEN / seite).read_text(encoding="utf-8")
        assert "<c-rahmen.kopfband" in text and '{% include "work/profile/neu/_reiter.html"' in text, seite
        assert "{% block gestaltung %}neu{% endblock %}" in text, seite
    reiter = (VORLAGEN / "_reiter.html").read_text(encoding="utf-8")
    assert reiter.count("<c-rahmen.reiter-link") == 6
    assert (VORLAGEN / "_sicherheit_dialoge.html").read_text(encoding="utf-8").count("<c-ui.alpine-modal") == 3
