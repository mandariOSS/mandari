# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaiste Konten (Issue #238): Fristen, Schutzkriterien, Probelauf und die
Ausgabe des Commands ohne personenbezogene Daten.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any, cast

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.orphaned_accounts import (
    ABGELEHNT_TAGE,
    ALTBESTAND_TAGE,
    UNBESTAETIGT_TAGE,
    loesche_verwaiste_konten,
    verwaiste_konten,
)
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import UserInvitation
from apps.work.organization import services

pytestmark = pytest.mark.django_db

JETZT = timezone.now()


def _konto(*, tage_alt: int, verifiziert: bool = True, abgelehnt_vor_tagen: int | None = None, **extra: Any) -> User:
    user = cast(User, cast(Any, UserFactory)(email_verified=verifiziert, **extra))
    user.date_joined = JETZT - timedelta(days=tage_alt)
    if abgelehnt_vor_tagen is not None:
        user.registration_rejected_at = JETZT - timedelta(days=abgelehnt_vor_tagen)
    user.save()
    return user


def _ids(konten: list[User]) -> set[Any]:
    return {u.pk for u in konten}


def test_unbestaetigte_konten_erst_nach_frist() -> None:
    alt = _konto(tage_alt=UNBESTAETIGT_TAGE + 1, verifiziert=False)
    frisch = _konto(tage_alt=UNBESTAETIGT_TAGE - 1, verifiziert=False)

    fund = verwaiste_konten(JETZT)

    assert _ids(fund.unbestaetigt) == {alt.pk}
    assert frisch.pk not in _ids(fund.alle)


def test_abgelehnte_konten_erst_nach_frist() -> None:
    alt = _konto(tage_alt=60, abgelehnt_vor_tagen=ABGELEHNT_TAGE + 1)
    frisch = _konto(tage_alt=60, abgelehnt_vor_tagen=ABGELEHNT_TAGE - 1)

    fund = verwaiste_konten(JETZT)

    assert _ids(fund.abgelehnt) == {alt.pk}
    assert frisch.pk not in _ids(fund.alle)


def test_altbestand_ohne_zuordnung_und_ohne_login() -> None:
    nie_angemeldet = _konto(tage_alt=ALTBESTAND_TAGE + 1)
    kuerzlich_angemeldet = _konto(tage_alt=ALTBESTAND_TAGE + 1, last_login=JETZT - timedelta(days=1))

    fund = verwaiste_konten(JETZT)

    assert _ids(fund.altbestand) == {nie_angemeldet.pk}
    assert kuerzlich_angemeldet.pk not in _ids(fund.alle)


def test_geschuetzt_mitgliedschaft_gruppe_staff_einladung() -> None:
    mitglied = _konto(tage_alt=365, verifiziert=False)
    cast(Any, MembershipFactory)(user=mitglied)
    in_gruppe = _konto(tage_alt=365, verifiziert=False)
    in_gruppe.groups.add(Group.objects.create(name="Redaktion"))
    staff = _konto(tage_alt=365, verifiziert=False, is_staff=True)
    einladende = _konto(tage_alt=365)  # hat created_invitations und ist damit selbst geschützt
    eingeladen = _konto(tage_alt=365, verifiziert=False)
    UserInvitation.objects.create(
        organization=cast(Any, OrganizationFactory)(),
        email=eingeladen.email.upper(),
        token="t" * 32,
        expires_at=JETZT + timedelta(days=3),
        invited_by=einladende,
    )
    abgelaufen_eingeladen = _konto(tage_alt=365, verifiziert=False)
    UserInvitation.objects.create(
        organization=cast(Any, OrganizationFactory)(),
        email=abgelaufen_eingeladen.email,
        token="u" * 32,
        expires_at=JETZT - timedelta(days=3),
        invited_by=einladende,
    )

    fund = verwaiste_konten(JETZT)

    assert _ids(fund.alle) == {abgelaufen_eingeladen.pk}
    assert {mitglied.pk, in_gruppe.pk, staff.pk, eingeladen.pk, einladende.pk}.isdisjoint(_ids(fund.alle))


def test_dry_run_loescht_nichts_und_echtlauf_loescht() -> None:
    alt = _konto(tage_alt=UNBESTAETIGT_TAGE + 1, verifiziert=False)
    behalten = _konto(tage_alt=1, verifiziert=False)

    fund, geloescht = loesche_verwaiste_konten(dry_run=True, jetzt=JETZT)
    assert fund.gesamt == 1 and geloescht == 0
    assert User.objects.filter(pk=alt.pk).exists()

    fund, geloescht = loesche_verwaiste_konten(jetzt=JETZT)
    assert fund.gesamt == 1 and geloescht == 1
    assert not User.objects.filter(pk=alt.pk).exists()
    assert User.objects.filter(pk=behalten.pk).exists()


def test_command_nennt_nur_zahlen() -> None:
    konto = _konto(tage_alt=UNBESTAETIGT_TAGE + 1, verifiziert=False)
    out = StringIO()

    call_command("cleanup_orphaned_accounts", "--dry-run", stdout=out)

    text = out.getvalue()
    assert "[DRY-RUN] Verwaiste Konten: 1" in text
    assert konto.email not in text and "@" not in text
    assert User.objects.filter(pk=konto.pk).exists()


def test_ablehnung_setzt_stempel_und_mail_nennt_frist(mailoutbox: list[Any]) -> None:
    organisation = cast(Any, OrganizationFactory)()
    mitgliedschaft = cast(Any, MembershipFactory)(organization=organisation, registration_requested_at=JETZT)
    user = mitgliedschaft.user
    assert user.registration_rejected_at is None

    services.reject_registration(mitgliedschaft, reason="Nicht aus unserer Fraktion")

    user.refresh_from_db()
    assert user.registration_rejected_at is not None
    assert not user.memberships.exists()
    assert mailoutbox, "Ablehnungsmail wurde nicht versendet"
    inhalt = mailoutbox[-1].alternatives[0][0] if mailoutbox[-1].alternatives else mailoutbox[-1].body
    assert f"{ABGELEHNT_TAGE} Tage" in inhalt
