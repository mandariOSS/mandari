# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Bestätigungslink der Selbstregistrierung bestätigt nur das Konto der angemeldeten Person.

Der Link beweist die Kontrolle über das Postfach, das Passwort des Kontos hat aber die Person
gesetzt, die das Formular ausgefüllt hat. Bestätigt wird deshalb erst, wenn beides
zusammenkommt: Wer den Link einlöst, ist mit genau diesem Konto angemeldet. Die Inhaberin einer
fremd registrierten Adresse übernimmt das Konto über „Passwort vergessen“; das ersetzt das
fremde Passwort und beendet dessen Sitzungen.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import quote

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import EmailVerificationToken, User
from apps.common.tests.factories import UserFactory
from apps.tenants.models import Membership

pytestmark = pytest.mark.django_db

ADRESSE = "rita.rat@example.org"
FREMDES_PASSWORT = "Fremdes-Passwort-2026!"
EIGENES_PASSWORT = "Eigenes-Passwort-2026!"
BESTAETIGEN_RE = re.compile(r"/accounts/register/[\w-]+/bestaetigen/[\w-]+/")


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def offen(org: Any) -> Any:
    """Organisation mit Selbstregistrierung und sofortiger Freischaltung."""
    org.registration_enabled = True
    org.registration_auto_approve = True
    org.save()
    return org


def _registrieren(org: Any, passwort: str) -> str:
    """Registrierung mit ``ADRESSE`` und ``passwort``; liefert den Pfad des Bestätigungslinks."""
    antwort = Client().post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {
            "email": ADRESSE,
            "first_name": "Rita",
            "last_name": "Rat",
            "password1": passwort,
            "password2": passwort,
        },
    )
    assert antwort.status_code == 200
    [nachricht] = [m for m in mail.outbox if ADRESSE in m.to]
    mail.outbox.clear()
    treffer = BESTAETIGEN_RE.search(str(nachricht.body))
    assert treffer, nachricht.body
    return treffer.group(0)


def _anmelden(passwort: str, next_url: str = "") -> tuple[Client, Any]:
    client = Client()
    daten = {"email": ADRESSE, "password": passwort}
    if next_url:
        daten["next"] = next_url
    antwort = client.post(reverse("accounts:login"), daten)
    return client, antwort


def _angemeldet(client: Client) -> bool:
    # Abmeldeseite: angemeldet 200, sonst Umleitung zur Anmeldung
    return client.get(reverse("accounts:logout")).status_code == 200


def _konto() -> User:
    return cast(User, User.objects.get(email=ADRESSE))


def test_bestaetigung_ohne_anmeldung_bestaetigt_nichts(offen: Any) -> None:
    pfad = _registrieren(offen, FREMDES_PASSWORT)
    registrierend, _ = _anmelden(FREMDES_PASSWORT)
    assert _angemeldet(registrierend)

    # Die Inhaberin des Postfachs öffnet den Link, ohne das Passwort zu kennen
    inhaberin = Client()
    assert inhaberin.get(pfad).status_code == 200
    antwort = inhaberin.post(pfad)

    assert antwort.status_code == 302
    assert antwort["Location"] == f"{reverse('accounts:login')}?next={quote(pfad)}"
    konto = _konto()
    assert not konto.email_verified
    assert not Membership.objects.filter(user=konto, organization=offen).exists()
    # Der Link bleibt einlösbar, bis das Konto bestätigt
    assert EmailVerificationToken.objects.get(user=konto).verified_at is None


def test_bestaetigung_mit_anderem_konto_bestaetigt_nichts(offen: Any, client_for: Any) -> None:
    pfad = _registrieren(offen, FREMDES_PASSWORT)
    anderes_konto = cast(Any, UserFactory)(email="jemand@example.org", email_verified=True)

    antwort = client_for(anderes_konto).post(pfad)

    assert antwort.status_code == 403
    konto = _konto()
    assert not konto.email_verified
    assert not Membership.objects.filter(organization=offen, user__in=[konto, anderes_konto]).exists()


def test_registrierende_person_bestaetigt_nach_der_anmeldung(offen: Any) -> None:
    pfad = _registrieren(offen, EIGENES_PASSWORT)

    # Ohne Anmeldung zeigt die Seite den Weg zur Anmeldung, die danach hierher zurückführt
    seite = Client().get(pfad)
    assert f"{reverse('accounts:login')}?next={quote(pfad)}" in seite.content.decode()

    client, antwort = _anmelden(EIGENES_PASSWORT, next_url=pfad)
    assert antwort["Location"] == pfad
    antwort = client.post(pfad)

    assert antwort.status_code == 302
    assert antwort["Location"] == reverse("work:dashboard", kwargs={"org_slug": offen.slug})
    konto = _konto()
    assert konto.email_verified
    assert Membership.objects.get(user=konto, organization=offen).is_active


def test_nach_der_bestaetigung_gilt_die_2fa_pflicht_der_organisation_sofort(offen: Any, settings: Any) -> None:
    """Die Anmeldung kommt jetzt vor dem Beitritt; die zwischengespeicherte 2FA-Richtlinie darf nicht nachhängen."""
    settings.TWO_FACTOR_ENFORCEMENT = True
    offen.require_2fa = True
    offen.save()
    pfad = _registrieren(offen, EIGENES_PASSWORT)
    client, _ = _anmelden(EIGENES_PASSWORT)
    client.get("/")  # Richtlinie ohne Mitgliedschaft geprüft und zwischengespeichert

    assert client.post(pfad).status_code == 302
    antwort = client.get(reverse("work:dashboard", kwargs={"org_slug": offen.slug}))

    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("accounts:two_factor_enroll"))


def test_inhaberin_uebernimmt_fremd_registrierte_adresse_ueber_passwort_link(offen: Any) -> None:
    pfad = _registrieren(offen, FREMDES_PASSWORT)
    registrierend, _ = _anmelden(FREMDES_PASSWORT)

    # „Passwort vergessen“: der Link geht an das Postfach und ersetzt das fremde Passwort
    konto = _konto()
    uid = urlsafe_base64_encode(force_bytes(konto.pk))
    inhaberin = Client()
    antwort = inhaberin.get(
        reverse(
            "accounts:password_reset_confirm",
            kwargs={"uidb64": uid, "token": default_token_generator.make_token(konto)},
        )
    )
    inhaberin.post(antwort["Location"], {"new_password1": EIGENES_PASSWORT, "new_password2": EIGENES_PASSWORT})

    inhaberin, _ = _anmelden(EIGENES_PASSWORT)
    assert inhaberin.post(pfad).status_code == 302

    konto.refresh_from_db()
    assert konto.email_verified
    assert not konto.check_password(FREMDES_PASSWORT)
    assert Membership.objects.get(user=konto, organization=offen).is_active
    # Die Sitzung der registrierenden Person endet mit dem Passwortwechsel
    assert not _angemeldet(registrierend)
