# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Konto löschen, wenn die letzte Mitgliedschaft nach dem Auslauf gelöscht ist (Issue #1237).

Gelöscht wird nur, wenn nichts mehr am Konto hängt: keine weitere Mitgliedschaft (auch keine deaktivierte),
kein Session-Mandant, keine Gruppe, keine gültige Einladung, nie Mitarbeiter- oder Superuser-Konten. Verweise,
die beim Löschen nur geleert werden (Inhalte bleiben als „Ehemaliges Mitglied“), und reine Konto-Artefakte
binden das Konto nicht.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.orphaned_accounts import bindungen_nach_austritt, loesche_konto_nach_austritt
from apps.common.tests import factories
from apps.session.models import SessionTenant, SessionUser
from apps.tenants.models import UserInvitation

pytestmark = pytest.mark.django_db

#: Fabriken sind untypisiert (factory_boy)
fabrik: Any = factories


@pytest.fixture
def konto() -> Any:
    return fabrik.UserFactory(email="ehemalig@example.org")


def test_freies_konto_wird_geloescht(konto: Any) -> None:
    # Verweise, die nur geleert werden: angenommene Einladung, von der Person eingeladenes Mitglied
    org = fabrik.OrganizationFactory(name="Fraktion A", slug="fraktion-a")
    UserInvitation.objects.create(
        organization=org,
        email=konto.email,
        expires_at=timezone.now() - timedelta(days=1),
        accepted_at=timezone.now(),
        accepted_by=konto,
        invited_by=fabrik.UserFactory(email="verwaltung@example.org"),
    )
    fabrik.MembershipFactory(user=fabrik.UserFactory(email="kollegin@example.org"), organization=org, invited_by=konto)

    assert bindungen_nach_austritt(konto) == []
    assert loesche_konto_nach_austritt(konto) == []
    assert not User.objects.filter(pk=konto.pk).exists()


def test_weitere_mitgliedschaft_bindet_auch_wenn_deaktiviert(konto: Any) -> None:
    fabrik.MembershipFactory(user=konto, organization=fabrik.OrganizationFactory(name="B", slug="b"), is_active=False)

    assert loesche_konto_nach_austritt(konto) == ["memberships"]
    assert User.objects.filter(pk=konto.pk).exists()


def test_session_mandant_bindet(konto: Any) -> None:
    SessionUser.objects.create(user=konto, tenant=SessionTenant.objects.create(name="Stadt X", slug="stadt-x"))

    assert "session_memberships" in loesche_konto_nach_austritt(konto)
    assert User.objects.filter(pk=konto.pk).exists()


def test_eigentum_an_einer_organisation_bindet(konto: Any) -> None:
    fabrik.OrganizationFactory(name="C", slug="c", owner=konto)

    assert "owned_organizations" in loesche_konto_nach_austritt(konto)


def test_gueltige_einladung_bindet(konto: Any) -> None:
    UserInvitation.objects.create(
        organization=fabrik.OrganizationFactory(name="D", slug="d"),
        email=konto.email,
        expires_at=timezone.now() + timedelta(days=3),
        invited_by=fabrik.UserFactory(email="verwaltung@example.org"),
    )

    assert "einladungen" in loesche_konto_nach_austritt(konto)


@pytest.mark.parametrize("feld", ["is_staff", "is_superuser"])
def test_nie_mitarbeiter_oder_superuser(konto: Any, feld: str) -> None:
    User.objects.filter(pk=konto.pk).update(**{feld: True})
    konto.refresh_from_db()

    assert loesche_konto_nach_austritt(konto) == ["mitarbeiterkonto"]
    assert User.objects.filter(pk=konto.pk).exists()


def test_merkliste_bindet(konto: Any) -> None:
    """Insight-Merkliste: Das Konto bleibt, nur die Mitgliedschaft ist gelöscht (Prüfung PR #1241)."""
    from insight_core.models import Bookmark

    Bookmark.objects.create(user=konto, entity_type="paper", entity_id=uuid.uuid4())

    assert "bookmarks" in loesche_konto_nach_austritt(konto)
    assert User.objects.filter(pk=konto.pk).exists()


def test_profilbild_wird_mit_dem_konto_geloescht(
    konto: Any, settings: Any, tmp_path: Path, django_capture_on_commit_callbacks: Any
) -> None:
    settings.MEDIA_ROOT = str(tmp_path)
    konto.avatar = SimpleUploadedFile("bild.png", b"\x89PNG\r\n\x1a\n")
    konto.save()
    datei = Path(konto.avatar.path)
    assert datei.exists()

    with django_capture_on_commit_callbacks(execute=True):
        assert loesche_konto_nach_austritt(konto) == []

    assert not User.objects.filter(pk=konto.pk).exists()
    assert not datei.exists()
