# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Persönliche Nachrichten in Einladungen (Mitglied und Gast) sind serverseitig begrenzt, bevor sie gespeichert und in
die Einladungsmail gerendert werden, wie beim erneuten Senden eines Gastzugangs (500 Zeichen).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.tenants.models import UserInvitation
from apps.work.organization import services
from apps.work.organization.services import ServiceError
from apps.work.organization.views.invitations import MAX_INVITATION_MESSAGE_LENGTH

pytestmark = pytest.mark.django_db

NACHRICHT = "  Hallo,\r\nwillkommen im Team! " + "x" * 5000


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.invite", "guests.invite"], email="admin@example.org", is_admin=True)


def test_grenze() -> None:
    assert MAX_INVITATION_MESSAGE_LENGTH == 2000


def test_mitglied_einladen_kuerzt_die_nachricht(org: Any, admin: Any, client_for: Any) -> None:
    client_for(admin.user).post(
        reverse("work:member_invite", kwargs={"org_slug": org.slug}),
        {"email": "neu@example.org", "message": NACHRICHT},
    )

    einladung = UserInvitation.objects.get(email="neu@example.org")
    assert einladung.message == NACHRICHT.strip()[:MAX_INVITATION_MESSAGE_LENGTH]


def test_mitglied_einladen_laesst_kurze_nachricht_unveraendert(org: Any, admin: Any, client_for: Any) -> None:
    client_for(admin.user).post(
        reverse("work:member_invite", kwargs={"org_slug": org.slug}),
        {"email": "kurz@example.org", "message": "  Willkommen!  "},
    )

    assert UserInvitation.objects.get(email="kurz@example.org").message == "Willkommen!"


def test_gast_einladen_kuerzt_die_nachricht(
    org: Any, admin: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    uebergeben: dict[str, Any] = {}

    def invite_guest(*args: Any, **kwargs: Any) -> Any:
        uebergeben.update(kwargs)
        raise ServiceError("Nach der Übergabe beendet")

    monkeypatch.setattr(services, "invite_guest", invite_guest)

    client_for(admin.user).post(
        reverse("work:guest_invite", kwargs={"org_slug": org.slug}),
        {"email": "gast@example.org", "message": NACHRICHT},
    )

    assert uebergeben["note"] == NACHRICHT.strip()[:MAX_INVITATION_MESSAGE_LENGTH]
