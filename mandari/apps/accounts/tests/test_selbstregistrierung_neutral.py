# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Selbstregistrierung: Die Antwort ist für jede Adresse dieselbe.

Ob zu einer Adresse schon ein Konto besteht (aktiv oder deaktiviert), zeigt die Registrierungsseite nicht: gleicher
Status, gleiche Seite, je eine Mail. Die Drossel zählt jede Anfrage, bevor die Adresse nachgeschlagen wird. Dass es
das Konto gibt, steht nur in der Mail an die Adresse; ein bestehendes Konto wird nie übernommen.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.views import SELF_REGISTER_MAILS_PER_IP_PER_HOUR
from apps.tenants.models import Membership

pytestmark = pytest.mark.django_db

PASSWORT = "Sicheres-Passwort-2026!"
FREMDES_PASSWORT = "Anderes-Passwort-2026!"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def offen(org: Any) -> Any:
    org.registration_enabled = True
    org.registration_auto_approve = True
    org.save()
    return org


def _registrieren(org: Any, email: str, ip: str = "198.51.100.7") -> Any:
    return Client().post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {
            "email": email,
            "first_name": "Eva",
            "last_name": "Muster",
            "password1": FREMDES_PASSWORT,
            "password2": FREMDES_PASSWORT,
        },
        REMOTE_ADDR=ip,
    )


def _konto(email: str, **felder: Any) -> Any:
    return User.objects.create_user(email=email, password=PASSWORT, **felder)  # type: ignore[no-untyped-call]


def _vorlagen(antwort: Any) -> list[str]:
    """Vorlagen der Seite; die der Mail gehören nicht zur Antwort."""
    namen = {str(vorlage.name) for vorlage in antwort.templates if vorlage.name}
    return sorted(name for name in namen if "email" not in name)


def _ohne_adresse(antwort: Any, email: str) -> str:
    """Seite ohne das, was je Anfrage wechselt: die eingegebene Adresse und die Einmalwerte der Seite."""
    seite = str(antwort.content.decode().replace(email, "ADRESSE"))
    return re.sub(r'nonce="[^"]*"', 'nonce=""', seite)


def test_bestehende_deaktivierte_und_neue_adresse_bekommen_dieselbe_antwort(offen: Any) -> None:
    _konto("aktiv@example.org")
    _konto("inaktiv@example.org", is_active=False)

    antworten = {
        email: _registrieren(offen, email, ip=f"198.51.100.{nummer}")
        for nummer, email in enumerate(("aktiv@example.org", "inaktiv@example.org", "neu@example.org"), start=1)
    }

    assert {antwort.status_code for antwort in antworten.values()} == {200}
    assert len({tuple(_vorlagen(antwort)) for antwort in antworten.values()}) == 1
    assert len({_ohne_adresse(antwort, email) for email, antwort in antworten.items()}) == 1
    # Je Adresse genau eine Mail, auf demselben Weg
    for email in antworten:
        assert len([nachricht for nachricht in mail.outbox if nachricht.to == [email]]) == 1, email


def test_schreibweise_der_adresse_aendert_die_antwort_nicht(offen: Any) -> None:
    _konto("aktiv@example.org")

    bestand = _registrieren(offen, "Aktiv@Example.org", ip="198.51.100.1")
    neu = _registrieren(offen, "Neu@Example.org", ip="198.51.100.2")

    assert bestand.status_code == neu.status_code == 200
    assert _ohne_adresse(bestand, "aktiv@example.org") == _ohne_adresse(neu, "neu@example.org")


def test_bestehendes_konto_bleibt_unberuehrt_und_erfaehrt_es_per_mail(offen: Any) -> None:
    konto = _konto("aktiv@example.org")

    _registrieren(offen, "aktiv@example.org")

    konto.refresh_from_db()
    assert konto.check_password(PASSWORT), "das Passwort aus der Registrierung gilt nicht"
    assert User.objects.filter(email__iexact="aktiv@example.org").count() == 1
    assert not Membership.objects.filter(user=konto, organization=offen).exists()
    [nachricht] = mail.outbox
    assert nachricht.to == ["aktiv@example.org"]
    assert reverse("accounts:login") in nachricht.body
    assert reverse("accounts:password_reset") in nachricht.body
    assert "bestaetigen" not in nachricht.body, "kein Bestätigungslink für ein fremdes Konto"


def test_drossel_zaehlt_vor_dem_nachschlagen_der_adresse(offen: Any) -> None:
    """Ist das Kontingent einer Adresse verbraucht, sehen bestehende und neue Adressen dieselbe Abweisung."""
    _konto("aktiv@example.org")
    for nummer in range(SELF_REGISTER_MAILS_PER_IP_PER_HOUR):
        _registrieren(offen, f"wegwerf-{nummer}@example.org", ip="203.0.113.9")
    mail.outbox.clear()

    bestand = _registrieren(offen, "aktiv@example.org", ip="203.0.113.9")
    neu = _registrieren(offen, "neu@example.org", ip="203.0.113.9")

    assert bestand.status_code == neu.status_code == 200
    assert _vorlagen(bestand) == _vorlagen(neu)
    assert "zu viele Registrierungen" in bestand.content.decode()
    assert "zu viele Registrierungen" in neu.content.decode()
    assert mail.outbox == []
    assert not User.objects.filter(email="neu@example.org").exists()


def test_hinweis_geht_nie_ueber_das_smtp_der_organisation(offen: Any) -> None:
    offen.mail_sender_mode = "smtp"
    offen.smtp_host = "smtp.fraktion.example"
    offen.smtp_from_email = "fraktion@fraktion.example"
    offen.save()
    _konto("aktiv@example.org")

    with mock.patch("apps.common.mail.config.organization_backend") as verbindung:
        _registrieren(offen, "aktiv@example.org")

    verbindung.assert_not_called()
    assert len(mail.outbox) == 1


def test_gescheiterter_versand_sieht_fuer_beide_gleich_aus(offen: Any) -> None:
    _konto("aktiv@example.org")

    with mock.patch("apps.common.mail.delivery.deliver", side_effect=OSError("kein Mailserver")):
        bestand = _registrieren(offen, "aktiv@example.org", ip="198.51.100.1")
        neu = _registrieren(offen, "neu@example.org", ip="198.51.100.2")

    assert bestand.status_code == neu.status_code == 200
    assert _ohne_adresse(bestand, "aktiv@example.org") == _ohne_adresse(neu, "neu@example.org")
    assert "konnte gerade nicht versendet werden" in bestand.content.decode()
