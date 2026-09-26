# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangstoken stehen nur als Hash in der Datenbank (apps/common/tokens.py).

Je Token: Nach dem Erzeugen steht das Klartext-Token nicht in der Datenbank, der Link aus der Mail
bzw. der Oberfläche funktioniert, ein falsches Token wird abgelehnt – auch der Hash selbst, wie er
nach einem Datenbankabfluss bekannt wäre. Die Datenmigrationen hashen den Bestand so, dass bereits
verschickte Links und abonnierte Kalender-Feeds weiter funktionieren.
"""

from __future__ import annotations

import importlib
import re
import secrets
import uuid
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.models import EmailVerificationToken, TrustedDevice
from apps.common.tests.factories import UserFactory
from apps.common.tokens import hash_token, new_token
from apps.session.models import SessionInvitation, SessionTenant
from apps.tenants.models import UserInvitation
from apps.work.faction.models import CalendarFeedToken
from apps.work.organization import selectors as org_selectors
from apps.work.organization import services as org_services
from insight_core.models import OParlBody, OParlPerson, OParlSource, PublicQuestion
from insight_core.services import question_service


def _gespeichert(model: Any, pk: Any, field: str = "token") -> str:
    """Wert direkt aus der Datenbank, am Modell vorbei."""
    return str(model.objects.filter(pk=pk).values_list(field, flat=True).get())


def _link_token(pfad: str, text: object) -> str:
    treffer = re.search(re.escape(pfad) + r"([^/\s\"<]+)/", str(text))
    assert treffer is not None, f"kein Link {pfad}… gefunden"
    return treffer.group(1)


def _konto(email: str) -> Any:
    return cast(Any, UserFactory)(email=email)


@pytest.fixture
def frage(db: Any) -> PublicQuestion:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    person = OParlPerson.objects.create(
        external_id="https://ris.example.org/person/1", body=body, name="Anna Rat", family_name="Rat"
    )
    frage = PublicQuestion(
        body=body,
        recipient=person,
        questioner_name="Frieda",
        questioner_email="frieda@example.org",
        subject="Radweg",
        question_text="Wann kommt der Radweg?",
    )
    frage.issue_token()
    frage.save()
    return frage


# ---------------------------------------------------------------------------
# Einladung in eine Organisation (Work)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEinladungWork:
    def test_link_aus_der_mail_funktioniert_gespeichert_ist_nur_der_hash(
        self, org: Any, make_member: Any, client_for: Any
    ) -> None:
        admin = make_member(org, ["members.invite"], email="admin@example.org", is_admin=True)
        org_services.invite_member(org, admin.user, "neu@example.org", [], "")

        token = _link_token("/work/invitation/", mail.outbox[-1].body)
        einladung = UserInvitation.objects.get(organization=org, email="neu@example.org")
        assert _gespeichert(UserInvitation, einladung.pk) == hash_token(token) != token
        assert not UserInvitation.objects.filter(token=token).exists()

        for falsch in (new_token(), einladung.token, token[:-1]):
            assert org_selectors.find_invitation_by_token(falsch) is None

        neu = _konto("neu@example.org")
        client_for(neu).post(reverse("work:accept_invitation", kwargs={"token": token}))
        assert org.memberships.filter(user=neu, is_active=True).exists()

    def test_erneut_senden_macht_den_alten_link_ungueltig(self, org: Any, make_member: Any) -> None:
        admin = make_member(org, ["members.invite"], email="admin@example.org", is_admin=True)
        einladung = UserInvitation.create_for_organization(
            organization=org, email="neu@example.org", invited_by=admin.user
        )
        alt = einladung.plain_token
        mail.outbox.clear()

        org_services.resend_invitation(org, UserInvitation.objects.get(pk=einladung.pk))

        neu = _link_token("/work/invitation/", mail.outbox[-1].body)
        assert neu != alt
        assert org_selectors.find_invitation_by_token(alt) is None
        assert org_selectors.find_invitation_by_token(neu) == einladung


# ---------------------------------------------------------------------------
# Einladung in den Sitzungsdienst (Session)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_einladung_nur_mit_dem_token_aus_der_mail() -> None:
    tenant = SessionTenant.objects.create(name="Stadt Test", slug="stadt-test")
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="x@example.org")
    token = cast(str, einladung.plain_token)
    assert _gespeichert(SessionInvitation, einladung.pk) == hash_token(token) != token

    client = Client()
    assert client.get(reverse("session:invitation_accept", kwargs={"token": token})).status_code == 200
    for falsch in (new_token(), einladung.token):
        assert client.get(reverse("session:invitation_accept", kwargs={"token": falsch})).status_code == 404


# ---------------------------------------------------------------------------
# Bestätigungslink der Selbstregistrierung
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bestaetigungslink_der_selbstregistrierung(org: Any) -> None:
    org.registration_enabled = True
    org.save(update_fields=["registration_enabled"])
    person = _konto("selbst@example.org")

    assert org_services.start_self_registration(org, person)

    token = _link_token(f"/accounts/register/{org.slug}/bestaetigen/", mail.outbox[-1].body)
    bestaetigung = EmailVerificationToken.objects.get(user=person)
    assert _gespeichert(EmailVerificationToken, bestaetigung.pk) == hash_token(token) != token
    assert org_selectors.find_registration_token(token) == bestaetigung
    for falsch in (new_token(), bestaetigung.token):
        assert org_selectors.find_registration_token(falsch) is None

    antwort = Client().post(reverse("accounts:self_register_confirm", kwargs={"org_slug": org.slug, "token": token}))
    assert antwort.status_code in (200, 302)
    bestaetigung.refresh_from_db()
    assert bestaetigung.verified_at is not None


# ---------------------------------------------------------------------------
# Persönlicher Kalender-Feed
# ---------------------------------------------------------------------------


def _feed_url_auf_der_seite(html: str) -> str | None:
    treffer = re.search(r'value="[^"]*/kalender/feed/([^"/]+)\.ics"', html)
    return treffer.group(1) if treffer else None


@pytest.mark.django_db
class TestKalenderFeed:
    def test_url_erscheint_genau_einmal_und_funktioniert(self, org: Any, make_member: Any, client_for: Any) -> None:
        mitglied = make_member(org, ["dashboard.view"], email="kalender@example.org")
        client = client_for(mitglied.user)
        profil = reverse("work:profile", kwargs={"org_slug": org.slug})

        assert "noch keinen Kalender-Feed" in client.get(profil).content.decode()
        client.post(profil, {"action": "regenerate_calendar_feed"})

        token = _feed_url_auf_der_seite(client.get(profil).content.decode())
        assert token is not None
        feed = CalendarFeedToken.objects.get(user=mitglied.user)
        assert _gespeichert(CalendarFeedToken, feed.pk) == hash_token(token) != token

        spaeter = client.get(profil).content.decode()
        assert _feed_url_auf_der_seite(spaeter) is None and token not in spaeter
        assert "Feed-URL neu erzeugen" in spaeter

        antwort = Client().get(reverse("personal_calendar_feed", kwargs={"token": token}))
        assert antwort.status_code == 200 and antwort["Content-Type"].startswith("text/calendar")
        for falsch in (new_token(), feed.token):
            assert Client().get(reverse("personal_calendar_feed", kwargs={"token": falsch})).status_code == 404

    def test_neu_erzeugen_macht_die_alte_url_ungueltig(self, org: Any, make_member: Any, client_for: Any) -> None:
        mitglied = make_member(org, ["dashboard.view"], email="kalender@example.org")
        client = client_for(mitglied.user)
        profil = reverse("work:profile", kwargs={"org_slug": org.slug})
        client.post(profil, {"action": "regenerate_calendar_feed"})
        alt = _feed_url_auf_der_seite(client.get(profil).content.decode())

        client.post(profil, {"action": "regenerate_calendar_feed"})
        neu = _feed_url_auf_der_seite(client.get(profil).content.decode())

        assert alt and neu and alt != neu
        assert Client().get(reverse("personal_calendar_feed", kwargs={"token": alt})).status_code == 404
        assert Client().get(reverse("personal_calendar_feed", kwargs={"token": neu})).status_code == 200
        assert CalendarFeedToken.objects.filter(user=mitglied.user).count() == 1


# ---------------------------------------------------------------------------
# Bestätigungslink einer öffentlichen Frage
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bestaetigungslink_einer_frage(frage: PublicQuestion) -> None:
    mail.outbox.clear()
    question_service.send_verification_email(frage)

    token = _link_token("/insight/fragen/verifizieren/", mail.outbox[-1].body)
    assert token == frage.plain_token
    assert _gespeichert(PublicQuestion, frage.pk, "verification_token") == hash_token(token) != token

    for falsch in (new_token(), frage.verification_token, str(uuid.uuid4())):
        assert Client().post(f"/insight/fragen/verifizieren/{falsch}/").status_code == 404
    assert Client().post(f"/insight/fragen/verifizieren/{token}/").status_code == 200
    frage.refresh_from_db()
    assert frage.status == "pending"


# ---------------------------------------------------------------------------
# Vertrauenswürdiges Gerät
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_geraete_token_nur_als_hash() -> None:
    person = _konto("geraet@example.org")
    geraet = TrustedDevice.create_for_user(person, RequestFactory().get("/", HTTP_USER_AGENT="Firefox auf Linux"))
    token = cast(str, geraet.plain_token)

    assert _gespeichert(TrustedDevice, geraet.pk, "device_token") == hash_token(token) != token
    assert TrustedDevice.find_valid(person, token) == geraet
    assert TrustedDevice.find_valid(person, geraet.device_token) is None
    assert TrustedDevice.find_valid(_konto("fremd@example.org"), token) is None


# ---------------------------------------------------------------------------
# Datenmigrationen: Bestand im Klartext wird gehasht, Links funktionieren weiter
# ---------------------------------------------------------------------------


def _migration(app: str, name: str) -> Any:
    return importlib.import_module(f"{app}.migrations.{name}")


@pytest.mark.django_db
class TestBestandNachMigration:
    def test_einladung_work(self, org: Any, make_member: Any) -> None:
        admin = make_member(org, [], email="admin@example.org", is_admin=True)
        einladung = UserInvitation.create_for_organization(
            organization=org, email="alt@example.org", invited_by=admin.user
        )
        alt = secrets.token_urlsafe(48)  # so erzeugte die bisherige Version das Token
        UserInvitation.objects.filter(pk=einladung.pk).update(token=alt)

        migration = _migration("apps.tenants", "0023_zugangstoken_als_hash")
        migration.hash_tokens(django_apps, None)

        assert not UserInvitation.objects.filter(token=alt).exists()
        assert org_selectors.find_invitation_by_token(alt) == einladung
        # Wiederholbar: Ein zweiter Lauf hasht den Hash nicht noch einmal
        migration.hash_tokens(django_apps, None)
        assert org_selectors.find_invitation_by_token(alt) == einladung

    def test_einladung_session(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="stadt-test")
        einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="alt@example.org")
        alt = secrets.token_urlsafe(48)
        SessionInvitation.objects.filter(pk=einladung.pk).update(token=alt)

        migration = _migration("apps.session", "0039_zugangstoken_als_hash")
        migration.hash_tokens(django_apps, None)
        migration.hash_tokens(django_apps, None)

        assert not SessionInvitation.objects.filter(token=alt).exists()
        assert Client().get(reverse("session:invitation_accept", kwargs={"token": alt})).status_code == 200

    def test_bestaetigung_und_geraet(self) -> None:
        person = _konto("alt@example.org")
        bestaetigung = EmailVerificationToken.create_for_user(person)
        geraet = TrustedDevice.create_for_user(person, RequestFactory().get("/"))
        alt_bestaetigung, alt_geraet = secrets.token_urlsafe(48), secrets.token_hex(32)
        EmailVerificationToken.objects.filter(pk=bestaetigung.pk).update(token=alt_bestaetigung)
        TrustedDevice.objects.filter(pk=geraet.pk).update(device_token=alt_geraet)

        _migration("apps.accounts", "0005_zugangstoken_als_hash").hash_tokens(django_apps, None)

        assert not EmailVerificationToken.objects.filter(token=alt_bestaetigung).exists()
        assert not TrustedDevice.objects.filter(device_token=alt_geraet).exists()
        assert org_selectors.find_registration_token(alt_bestaetigung) == bestaetigung
        assert TrustedDevice.find_valid(person, alt_geraet) == geraet

    def test_abonnierter_kalender_feed_laeuft_weiter(self) -> None:
        person = _konto("abo@example.org")
        feed = CalendarFeedToken.issue_for_user(person)
        alt = secrets.token_urlsafe(24)  # bisherige Feed-Tokens: 32 Zeichen
        CalendarFeedToken.objects.filter(pk=feed.pk).update(token=alt)

        migration = _migration("apps.work", "0058_zugangstoken_als_hash")
        migration.hash_tokens(django_apps, None)
        migration.hash_tokens(django_apps, None)

        assert not CalendarFeedToken.objects.filter(token=alt).exists()
        assert Client().get(reverse("personal_calendar_feed", kwargs={"token": alt})).status_code == 200


VORHER = ("insight_core", "0036_zusammenfassungen_nach_ruecknahme")
NACHHER = ("insight_core", "0037_zugangstoken_als_hash")


@pytest.mark.django_db(transaction=True)
def test_migration_bestaetigungslink_uuid_im_schema(frage: PublicQuestion) -> None:
    """Echter Lauf: Spalte vorher UUID, nachher Hash; der bisher verschickte UUID-Link gilt weiter."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = uuid.uuid4()
        historisch = executor.loader.project_state([VORHER]).apps.get_model("insight_core", "PublicQuestion")
        historisch.objects.filter(pk=frage.pk).update(verification_token=alt)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        assert _gespeichert(PublicQuestion, frage.pk, "verification_token") == hash_token(str(alt))
        assert Client().get(f"/insight/fragen/verifizieren/{alt}/").status_code == 200
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
