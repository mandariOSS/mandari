# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestehende Konten werden per Adresse nur übernommen, wenn sie zur Inhaberin des Postfachs gehören.

Die Selbstregistrierung legt ein Konto an, bevor die Adresse bestätigt ist. Solange sie
unbestätigt ist, darf ein solches Konto nicht allein anhand der Adresse Mitglied eines
Session-Mandanten, Administrator eines neu angelegten Mandanten oder Gast einer
Organisation werden. Stattdessen geht eine Einladung bzw. ein Link an das Postfach –
wer ihn einlöst, beweist die Kontrolle über die Adresse. Einladungs- und Passwort-Links
bestätigen die Adresse deshalb auch. Konten, die schon Zugang zu einer Organisation oder
einem Mandanten haben, bleiben wie bisher übernehmbar (``apps/accounts/adoption.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from typing import Any, cast

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionInvitation, SessionRole, SessionTenant, SessionUser
from apps.tenants.models import Membership
from apps.work.organization import services as organization_services

pytestmark = pytest.mark.django_db

FREMDES_PASSWORT = "Fremdes-Passwort-2026!"
EIGENES_PASSWORT = "Eigenes-Passwort-2026!"
ADRESSE = "sachbearbeitung@stadt-x.example"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def unbestaetigt(org: Any) -> User:
    """Konto aus der Selbstregistrierung einer fremden Person – Adresse nie bestätigt."""
    org.registration_enabled = True
    org.save()
    antwort = Client().post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {
            "email": ADRESSE,
            "first_name": "Fremd",
            "last_name": "Person",
            "password1": FREMDES_PASSWORT,
            "password2": FREMDES_PASSWORT,
        },
    )
    assert antwort.status_code == 200
    konto = cast(User, User.objects.get(email=ADRESSE))
    assert not konto.email_verified
    mail.outbox.clear()
    return konto


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt X", slug="stadt-x")


@pytest.fixture
def verwaltung(tenant: SessionTenant) -> Client:
    rolle = SessionRole.objects.create(tenant=tenant, name="Benutzerverwaltung", can_manage_users=True)
    user = cast(Any, UserFactory)(email="verwaltung@stadt-x.example", email_verified=True)
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(rolle)
    client = Client()
    client.force_login(user)
    return client


def _anmelden(email: str, passwort: str) -> Client:
    client = Client()
    client.post(reverse("accounts:login"), {"email": email, "password": passwort})
    return client


# ---------------------------------------------------------------- Session: Benutzer einladen


def test_session_einladung_uebernimmt_unbestaetigtes_konto_nicht(
    unbestaetigt: User, tenant: SessionTenant, verwaltung: Client
) -> None:
    verwaltung.post(reverse("session:user_invite", kwargs={"tenant_slug": tenant.slug}), {"email": ADRESSE})

    assert not SessionUser.objects.filter(user=unbestaetigt, tenant=tenant).exists()
    einladung = SessionInvitation.objects.get(tenant=tenant, email=ADRESSE)
    assert [m.to for m in mail.outbox] == [[ADRESSE]]
    assert einladung.token in mail.outbox[0].body
    # Mit dem Passwort aus der Registrierung gibt es keinen Zugang zum Mandanten
    fremd = _anmelden(ADRESSE, FREMDES_PASSWORT)
    assert fremd.get(f"/session/{tenant.slug}/").status_code in (302, 403, 404)


def test_eingeloest_einladung_bestaetigt_die_adresse(tenant: SessionTenant) -> None:
    konto = cast(Any, UserFactory)(email="inhaber@stadt-x.example")
    assert not konto.email_verified
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="inhaber@stadt-x.example")
    client = Client()
    client.force_login(konto)

    client.post(reverse("session:invitation_accept", kwargs={"token": einladung.token}))

    konto.refresh_from_db()
    assert konto.email_verified
    assert SessionUser.objects.filter(user=konto, tenant=tenant, is_active=True).exists()


def test_neues_konto_ueber_einladung_ist_bestaetigt(tenant: SessionTenant) -> None:
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="neu@stadt-x.example")

    Client().post(
        reverse("session:invitation_accept", kwargs={"token": einladung.token}),
        {"password": EIGENES_PASSWORT, "password_confirm": EIGENES_PASSWORT, "first_name": "N", "last_name": "N"},
    )

    assert User.objects.get(email="neu@stadt-x.example").email_verified


# ---------------------------------------------------------------- Session: Mandant anlegen


def _mandant_anlegen(email: str) -> str:
    ausgabe = StringIO()
    call_command(
        "session_create_tenant",
        "--profile",
        "nrw_stadt",
        "--name",
        "Stadt Musterstadt",
        "--slug",
        "musterstadt",
        "--admin-email",
        email,
        stdout=ausgabe,
    )
    return ausgabe.getvalue()


def test_mandant_anlegen_macht_unbestaetigtes_konto_nicht_zum_admin(unbestaetigt: User) -> None:
    _mandant_anlegen(ADRESSE)

    tenant = SessionTenant.objects.get(slug="musterstadt")
    assert not SessionUser.objects.filter(user=unbestaetigt, tenant=tenant).exists()
    einladung = SessionInvitation.objects.get(tenant=tenant, email=ADRESSE)
    assert einladung.roles.filter(is_admin=True).exists()
    assert [m.to for m in mail.outbox] == [[ADRESSE]]


def test_mandant_anlegen_nimmt_bestaetigtes_konto_auf() -> None:
    konto = cast(Any, UserFactory)(email="leitung@stadt-x.example", email_verified=True)

    _mandant_anlegen("leitung@stadt-x.example")

    tenant = SessionTenant.objects.get(slug="musterstadt")
    assert SessionUser.objects.get(user=konto, tenant=tenant).is_admin()


# ---------------------------------------------------------------- Work: Gast einladen


def test_gast_einladung_entzieht_unbestaetigtem_konto_das_fremde_passwort(
    org: Any, make_member: Any, unbestaetigt: User
) -> None:
    einladend = make_member(org, ["members.invite"], email="orga@example.org")
    andere = type(org).objects.create(name="Andere Fraktion", slug="andere-fraktion")

    organization_services.invite_guest(
        andere, einladend, email=ADRESSE, note="", share_level="view", document_ids=[], folder_ids=[]
    )

    unbestaetigt.refresh_from_db()
    assert Membership.objects.filter(user=unbestaetigt, organization=andere, is_guest=True).exists()
    # Das Passwort aus der Registrierung gilt nicht mehr; der Zugang entsteht über den Link im Postfach
    assert not unbestaetigt.has_usable_password()
    assert not unbestaetigt.check_password(FREMDES_PASSWORT)
    assert [m.to for m in mail.outbox] == [[ADRESSE]]
    assert "/accounts/reset/" in mail.outbox[0].body or "/accounts/password" in mail.outbox[0].body


def test_gast_einladung_laesst_konto_mit_bestehendem_zugang_unveraendert(
    org: Any, make_member: Any, client_for: Any
) -> None:
    """Mitglied in einer Organisation (ohne Bestätigungsvermerk) wird anderswo Gast – Anmeldung bleibt."""
    einladend = make_member(org, ["members.invite"], email="orga@example.org")
    andere = type(org).objects.create(name="Andere Fraktion", slug="andere-fraktion")
    mitglied = make_member(org, [], email="mitglied@example.org")
    mitglied.user.set_password(EIGENES_PASSWORT)
    mitglied.user.save()
    angemeldet = client_for(mitglied.user)

    organization_services.invite_guest(
        andere, einladend, email="mitglied@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )

    mitglied.user.refresh_from_db()
    assert mitglied.user.check_password(EIGENES_PASSWORT)
    assert "_auth_user_id" in angemeldet.session


def test_gast_einladung_laesst_bestaetigtes_konto_unveraendert(org: Any, make_member: Any) -> None:
    einladend = make_member(org, ["members.invite"], email="orga@example.org")
    andere = type(org).objects.create(name="Andere Fraktion", slug="andere-fraktion")
    konto = cast(Any, UserFactory)(email="gast@example.org", email_verified=True)
    konto.set_password(EIGENES_PASSWORT)
    konto.save()

    organization_services.invite_guest(
        andere, einladend, email="gast@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )

    konto.refresh_from_db()
    assert konto.check_password(EIGENES_PASSWORT)


# ---------------------------------------------------------------- Passwort-Link


def test_passwort_link_bestaetigt_die_adresse() -> None:
    konto = cast(Any, UserFactory)(email="reset@example.org")
    assert not konto.email_verified
    uid = urlsafe_base64_encode(force_bytes(konto.pk))
    token = default_token_generator.make_token(konto)
    client = Client()

    # Django leitet den Link auf eine Adresse ohne Token um und merkt sich das Token in der Sitzung
    antwort = client.get(reverse("accounts:password_reset_confirm", kwargs={"uidb64": uid, "token": token}))
    assert antwort.status_code == 302
    client.post(antwort["Location"], {"new_password1": EIGENES_PASSWORT, "new_password2": EIGENES_PASSWORT})

    konto.refresh_from_db()
    assert konto.check_password(EIGENES_PASSWORT)
    assert konto.email_verified
