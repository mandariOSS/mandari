# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erinnerung an die Löschung einer gesperrten Demo (Issue #1575).

Der Zeitplan ``demo_ablauf`` schreibt den Administratoren 7 Tage und 24 Stunden vor der Löschung, jede Stufe höchstens
einmal, einzeln an jede Adresse und mit einer Kopie an ``DEMO_KONTAKT_EMAIL``. Ein verspäteter Lauf schickt nur die
jüngste fällige Stufe; eine neue Sperre beginnt wieder ohne Erinnerung.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from apps.common.tests import factories
from apps.tenants import demo
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db

#: Fabriken sind untypisiert (factory_boy)
fabrik: Any = factories

STUNDE = timedelta(hours=1)
TAG = timedelta(days=1)
FRIST = timedelta(days=demo.LOESCHFRIST_TAGE)
KONTAKT = "hallo@example.org"
BUCHEN = "https://kunden.example.org/buchen/"


@pytest.fixture(autouse=True)
def _cache_und_post(settings: Any) -> Iterator[None]:
    settings.DEMO_KONTAKT_EMAIL = KONTAKT
    settings.DEMO_VERLAENGERN_URL = BUCHEN
    cache.clear()
    mail.outbox.clear()
    yield
    cache.clear()


def _demo(slug: str, *, loeschung_in: timedelta, aktiv: bool = False) -> Organization:
    """Gesperrte Demo, deren Löschung in ``loeschung_in`` fällig ist."""
    org: Organization = fabrik.OrganizationFactory(name=f"Demo {slug}", slug=slug)
    org.demo_laufzeit_tage = 14
    org.demo_endet_am = timezone.now() - FRIST + loeschung_in
    org.is_active = aktiv
    org.save(update_fields=["demo_laufzeit_tage", "demo_endet_am", "is_active"])
    return org


def _lauf() -> str:
    ausgabe = StringIO()
    call_command("demo_ablauf", stdout=ausgabe)
    return ausgabe.getvalue()


def _an(adresse: str) -> list[Any]:
    return [nachricht for nachricht in mail.outbox if adresse in nachricht.to]


def test_sieben_tage_vorher_an_administratoren_mit_kopie(make_member: Any) -> None:
    org = _demo("demo-sieben", loeschung_in=3 * TAG)
    admin = make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)
    make_member(org, ["dashboard.view"], email="mitglied@demo.example")
    org.owner = admin.user
    org.save(update_fields=["owner"])
    loeschung = demo.loeschung_am(org)
    assert loeschung is not None
    datum = timezone.localtime(loeschung).strftime("%d.%m.%Y")

    ausgabe = _lauf()

    assert "erinnert: demo-sieben (168 Stunden vor der Löschung, 1 Empfänger)" in ausgabe
    [nachricht] = _an("admin@demo.example")
    assert nachricht.to == ["admin@demo.example"]
    assert nachricht.subject == f"Eure Demo „Demo demo-sieben“ wird am {datum} gelöscht"
    assert datum in nachricht.body
    assert "kostenpflichtige Verlängerung" in nachricht.body
    assert BUCHEN in nachricht.body
    assert KONTAKT in nachricht.body
    assert _an("mitglied@demo.example") == []
    [kopie] = _an(KONTAKT)
    assert kopie.subject.startswith("Kopie (1 Empfänger): ")
    org.refresh_from_db()
    assert org.demo_erinnert_stunden == 168
    assert Organization.objects.filter(pk=org.pk).exists()


def test_jede_stufe_geht_nur_einmal_raus(make_member: Any) -> None:
    org = _demo("demo-einmal", loeschung_in=3 * TAG)
    make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)

    _lauf()
    mail.outbox.clear()
    _lauf()

    assert mail.outbox == []


def test_24_stunden_vorher_letzte_erinnerung(make_member: Any) -> None:
    org = _demo("demo-letzte", loeschung_in=12 * STUNDE)
    org.demo_erinnert_stunden = 168
    org.save(update_fields=["demo_erinnert_stunden"])
    make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)

    _lauf()

    [nachricht] = _an("admin@demo.example")
    assert nachricht.subject.startswith("Letzte Erinnerung: Eure Demo")
    org.refresh_from_db()
    assert org.demo_erinnert_stunden == 24


def test_verspaeteter_lauf_schickt_nur_die_juengste_stufe(make_member: Any) -> None:
    org = _demo("demo-spaet", loeschung_in=12 * STUNDE)
    make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)

    _lauf()

    [nachricht] = _an("admin@demo.example")
    assert nachricht.subject.startswith("Letzte Erinnerung:")
    org.refresh_from_db()
    assert org.demo_erinnert_stunden == 24


@pytest.mark.parametrize("fall", ["zu_frueh", "aktiv", "gebucht", "keine_demo"])
def test_keine_erinnerung(fall: str, make_member: Any) -> None:
    org = _demo(f"demo-{fall}".replace("_", "-"), loeschung_in=10 * TAG if fall == "zu_frueh" else 3 * TAG)
    make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)
    if fall == "aktiv":
        # Verlängert: wieder aktiv und das Ende in die Zukunft verschoben
        org.is_active = True
        org.demo_endet_am = timezone.now() + 14 * TAG
        org.save(update_fields=["is_active", "demo_endet_am"])
    elif fall == "gebucht":
        org.provisioning_ref = "BS-00042"
        org.save(update_fields=["provisioning_ref"])
    elif fall == "keine_demo":
        org.demo_laufzeit_tage = None
        org.save(update_fields=["demo_laufzeit_tage"])

    _lauf()

    assert mail.outbox == []


def test_neue_sperre_beginnt_ohne_erinnerung() -> None:
    """Verlängert, wieder abgelaufen: Beide Stufen gehen erneut raus."""
    org = _demo("demo-wieder", loeschung_in=FRIST - TAG, aktiv=True)
    org.demo_erinnert_stunden = 24
    org.save(update_fields=["demo_erinnert_stunden"])

    demo.abgelaufene_sperren()

    org.refresh_from_db()
    assert org.is_active is False
    assert org.demo_erinnert_stunden is None


def test_ohne_eigene_kontaktadresse_keine_kopie(settings: Any, make_member: Any) -> None:
    settings.DEMO_KONTAKT_EMAIL = ""
    org = _demo("demo-ohne-kopie", loeschung_in=3 * TAG)
    make_member(org, ["dashboard.view"], email="admin@demo.example", is_admin=True)

    _lauf()

    assert [nachricht.to for nachricht in mail.outbox] == [["admin@demo.example"]]
