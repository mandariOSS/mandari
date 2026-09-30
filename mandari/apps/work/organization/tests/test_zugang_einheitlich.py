# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugänge beginnen und enden auf allen Wegen gleich (Issue #480).

- Wird ein Zugang aktiv, benachrichtigt ein gemeinsamer Weg: Freischalten, Reaktivieren im
  Mitglieder-Detail und „Mitglied einladen“ lösen für dasselbe Ereignis dieselben Nachrichten aus.
- Verlässt jemand die Organisation – deaktiviert, entfernt oder durch Löschen des eigenen Kontos –,
  verfallen offene Einladungen an die Person und von ihr.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import DEFAULT_PASSWORD, MembershipFactory, UserFactory
from apps.tenants.models import Membership, UserInvitation
from apps.work.notifications.models import Notification, NotificationType
from apps.work.organization import emails, services

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.invite", "members.edit"], email="admin@example.org", is_admin=True)


@pytest.fixture
def freigabe(org: Any, make_member: Any) -> Any:
    """Weitere Person, die Zugänge freischalten darf (Empfängerin der In-App-Nachricht)."""
    return make_member(org, ["members.invite"], email="freigabe@example.org")


def _anfrage(org: Any, email: str = "anfrage@example.org") -> Membership:
    user = cast(Any, UserFactory)(email=email)
    return cast(
        Membership,
        cast(Any, MembershipFactory)(
            user=user, organization=org, is_active=False, registration_requested_at=timezone.now()
        ),
    )


def _deaktiviert(org: Any, email: str = "ehemalig@example.org") -> Membership:
    user = cast(Any, UserFactory)(email=email)
    return cast(Membership, cast(Any, MembershipFactory)(user=user, organization=org, is_active=False))


def _betreffe(adresse: str) -> list[str]:
    return [str(message.subject) for message in mail.outbox if adresse in message.to]


def _beitrittsnachrichten(empfaenger: Membership) -> int:
    return Notification.objects.filter(recipient=empfaenger, notification_type=NotificationType.MEMBER_JOINED).count()


# ---------------------------------------------------------------------------
# Zugang aktiv: ein Weg für die Benachrichtigung
# ---------------------------------------------------------------------------


def _freischalten(org: Any, admin: Any, anfrage: Membership) -> None:
    services.approve_registration(anfrage, actor=admin)


def _reaktivieren(org: Any, admin: Any, anfrage: Membership) -> None:
    services.reactivate_member(org, anfrage, admin)


def _einladen(org: Any, admin: Any, anfrage: Membership) -> None:
    services.invite_member(org, admin.user, anfrage.user.email, [], "")


@pytest.mark.parametrize(
    "weg", [_freischalten, _reaktivieren, _einladen], ids=["freischalten", "reaktivieren", "einladen"]
)
def test_freischalten_benachrichtigt_auf_jedem_weg_gleich(org: Any, admin: Any, freigabe: Any, weg: Any) -> None:
    anfrage = _anfrage(org)

    weg(org, admin, anfrage)

    anfrage.refresh_from_db()
    assert anfrage.is_active is True
    assert anfrage.registration_requested_at is None
    assert _betreffe("anfrage@example.org") == [f"Dein Zugang zu {org.name} ist freigeschaltet"]
    # Die übrigen Freigebenden erfahren vom neuen Mitglied, die handelnde Person nicht
    assert _beitrittsnachrichten(freigabe) == 1
    assert _beitrittsnachrichten(admin) == 0


@pytest.mark.parametrize("weg", [_reaktivieren, _einladen], ids=["reaktivieren", "einladen"])
def test_reaktivieren_meldet_keinen_neuen_beitritt(org: Any, admin: Any, freigabe: Any, weg: Any) -> None:
    ehemalig = _deaktiviert(org)

    weg(org, admin, ehemalig)

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is True
    assert _betreffe("ehemalig@example.org") == [f"Dein Zugang zu {org.name} ist wieder aktiv"]
    assert _beitrittsnachrichten(freigabe) == 0


def test_einladen_meldet_freischaltung_statt_reaktivierung(org: Any, admin: Any) -> None:
    _anfrage(org)

    meldung = services.invite_member(org, admin.user, "anfrage@example.org", [], "")

    assert meldung == "anfrage@example.org wurde freigeschaltet und per E-Mail informiert."


def test_reaktivieren_meldet_fehlgeschlagenen_versand(
    org: Any, admin: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ehemalig = _deaktiviert(org)
    monkeypatch.setattr(emails, "send_organization_mail", lambda *args, **kwargs: False)

    response = client_for(admin.user).post(
        reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": ehemalig.id}),
        {"action": "reactivate"},
    )

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is True
    meldungen = [str(message) for message in get_messages(response.wsgi_request)]
    assert any("nicht versendet" in meldung for meldung in meldungen), meldungen


# ---------------------------------------------------------------------------
# Zugang endet: offene Einladungen verfallen
# ---------------------------------------------------------------------------


def _einladungen(org: Any, admin: Any, person: Membership) -> tuple[UserInvitation, UserInvitation, UserInvitation]:
    an_person = UserInvitation.create_for_organization(organization=org, email=person.user.email, invited_by=admin.user)
    von_person = UserInvitation.create_for_organization(
        organization=org, email="eingeladen@example.org", invited_by=person.user
    )
    andere = UserInvitation.create_for_organization(organization=org, email="andere@example.org", invited_by=admin.user)
    return an_person, von_person, andere


def _offen(org: Any) -> set[Any]:
    return set(UserInvitation.objects.filter(organization=org, accepted_at__isnull=True).values_list("id", flat=True))


def test_konto_loeschen_widerruft_offene_einladungen(org: Any, admin: Any, make_member: Any) -> None:
    person = make_member(org, ["dashboard.view"], email="Person@Example.org")
    # Einladungen speichern die Adresse kleingeschrieben; der Abgleich ignoriert die Schreibweise
    _an_person, _von_person, andere = _einladungen(org, admin, person)

    services.request_account_deletion(org, person, DEFAULT_PASSWORD)

    person.refresh_from_db()
    assert person.is_active is False
    assert _offen(org) == {andere.id}


def test_konto_loeschen_mit_falschem_passwort_laesst_einladungen_bestehen(
    org: Any, admin: Any, make_member: Any
) -> None:
    person = make_member(org, ["dashboard.view"], email="person@example.org")
    einladungen = _einladungen(org, admin, person)

    with pytest.raises(services.ServiceError):
        services.request_account_deletion(org, person, "falsch")

    assert _offen(org) == {einladung.id for einladung in einladungen}


def test_konto_loeschen_ueber_die_profilseite(org: Any, admin: Any, make_member: Any, client_for: Any) -> None:
    person = make_member(org, ["dashboard.view"], email="person@example.org")
    _an_person, _von_person, andere = _einladungen(org, admin, person)

    client_for(person.user).post(
        reverse("work:profile_data", kwargs={"org_slug": org.slug}),
        {"action": "request_deletion", "password": DEFAULT_PASSWORD},
    )

    assert _offen(org) == {andere.id}


def test_entfernen_widerruft_offene_einladungen(org: Any, admin: Any, make_member: Any) -> None:
    person = make_member(org, ["dashboard.view"], email="person@example.org")
    _an_person, _von_person, andere = _einladungen(org, admin, person)

    services.remove_member(org, person, admin.user)

    assert _offen(org) == {andere.id}
