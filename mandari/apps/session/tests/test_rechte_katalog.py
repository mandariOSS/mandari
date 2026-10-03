# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtekatalog und Äquivalenz alt gegen neu (Issue #772, ADR „Rechte mit Geltungsbereich“).

Nachweis „keine Verhaltensänderung“: Die bisherigen Rechtenamen, die ``SessionPermissionChecker`` und ``visible_to()``
nutzen, entstehen jetzt über den Rechtekatalog (Rollen → Rechte nach Objektart und Aktion → Leitrecht je Häkchen).
Für jede Standardrolle, jedes einzelne Häkchen, Administrator-Rollen und zufällige Kombinationen muss das Ergebnis
gleich der bisherigen Auflösung sein. Die Referenz ist eine eingefrorene Kopie von ``role_permissions`` vor #772.
"""

from __future__ import annotations

import random
from typing import Any, cast

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.common.tests.factories import UserFactory
from apps.session.models import SessionRole, SessionTenant, SessionUser
from apps.session.permissions import (
    ALL_PERMISSIONS,
    AUDIT_PERMISSIONS,
    SessionPermissionChecker,
    grantable_permissions,
    role_permissions,
)
from apps.session.rechte import katalog

HAEKCHEN = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


def _referenz(session_user: Any) -> set[str]:
    """``role_permissions`` vor Issue #772 (eingefroren)."""
    if not session_user:
        return set()
    permissions: set[str] = set()
    for role in session_user.roles.all():
        if role.is_admin:
            permissions |= ALL_PERMISSIONS
        for attr in dir(role):
            if attr.startswith("can_") and getattr(role, attr, False):
                permissions.add(attr[4:])
    return permissions


class TestKatalog:
    def test_kennungen_sind_gueltig_und_eindeutig(self) -> None:
        kennungen = [recht.kennung for recht in katalog.RECHTE]
        assert len(kennungen) == len(set(kennungen))
        for kennung in kennungen:
            assert katalog.KENNUNG_MUSTER.match(kennung), kennung
            assert kennung.split(".")[0] in katalog.OBJEKTARTEN, kennung

    def test_rund_siebzig_rechte_und_jede_objektart_belegt(self) -> None:
        assert 60 <= len(katalog.RECHTE) <= 80
        assert {recht.objektart for recht in katalog.RECHTE} == set(katalog.OBJEKTARTEN)

    def test_jedes_haekchen_hat_genau_ein_leitrecht(self) -> None:
        namen = {feld[4:] for feld in HAEKCHEN}
        assert len(namen) == 29
        leit = [recht.herkunft for recht in katalog.RECHTE if recht.leit and recht.herkunft]
        assert sorted(leit) == sorted(namen)
        assert set(katalog.LEITRECHTE) == namen

    def test_herkunft_nur_aus_bekannten_haekchen(self) -> None:
        namen = {feld[4:] for feld in HAEKCHEN}
        for recht in katalog.RECHTE:
            assert recht.herkunft is None or recht.herkunft in namen, recht.kennung
            assert not recht.leit or recht.herkunft is not None, recht.kennung

    def test_administrator_vollmacht_ohne_kontrollrechte(self) -> None:
        kontrolle = {recht.herkunft for recht in katalog.RECHTE if recht.kontrolle}
        assert kontrolle == set(AUDIT_PERMISSIONS)
        assert katalog.haekchen_aus_rechten(katalog.ADMIN_RECHTE) == set(ALL_PERMISSIONS)
        assert not any(katalog.KATALOG[k].kontrolle for k in katalog.ADMIN_RECHTE)

    def test_beispiele_aus_dem_konzept(self) -> None:
        assert katalog.LEITRECHTE["approve_papers"] == "vorlage.freigeben"
        assert katalog.LEITRECHTE["view_non_public_papers"] == "vorlage.noe_sehen"
        assert {"sitzung.laden", "umlauf.anlegen", "vorlage.terminieren"} <= katalog.rechte_aus_haekchen(
            ["edit_meetings"]
        )
        assert katalog.KATALOG["vorlage.freigeben"].station
        assert katalog.rechte_aus_haekchen(["unbekannt"]) == frozenset()

    def test_neue_rechte_nur_mit_der_administrator_vollmacht(self) -> None:
        neu = {recht.kennung for recht in katalog.RECHTE if recht.herkunft is None}
        assert {"tagesordnung.benehmen_erklaeren", "konto.rechteauskunft", "ablauf.station_uebersteuern"} <= neu
        assert neu <= katalog.ADMIN_RECHTE
        alle_haekchen = katalog.rechte_aus_haekchen(feld[4:] for feld in HAEKCHEN)
        assert not neu & alle_haekchen


@pytest.mark.django_db
class TestAequivalenz:
    @pytest.fixture
    def tenant(self) -> SessionTenant:
        return SessionTenant.objects.create(name="Musterstadt", slug="musterstadt-rechte")

    def _konto(self, tenant: SessionTenant, *rollen: SessionRole) -> SessionUser:
        konto = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
        konto.roles.set(rollen)
        return SessionUser.objects.prefetch_related("roles").get(pk=konto.pk)

    def _rolle(self, tenant: SessionTenant, name: str, gesetzt: set[str], *, admin: bool = False) -> SessionRole:
        return SessionRole.objects.create(
            tenant=tenant, name=name, is_admin=admin, **{feld: feld in gesetzt for feld in HAEKCHEN}
        )

    def test_standardrollen(self, tenant: SessionTenant) -> None:
        rollen = SessionRole.create_default_roles(tenant)
        for schluessel, rolle in rollen.items():
            konto = self._konto(tenant, rolle)
            assert role_permissions(konto) == _referenz(konto), schluessel
        alle = self._konto(tenant, *rollen.values())
        assert role_permissions(alle) == _referenz(alle)

    @pytest.mark.parametrize("feld", HAEKCHEN)
    @pytest.mark.parametrize("admin", [False, True])
    def test_jedes_einzelne_haekchen(self, tenant: SessionTenant, feld: str, admin: bool) -> None:
        konto = self._konto(tenant, self._rolle(tenant, f"Nur {feld}", {feld}, admin=admin))
        assert role_permissions(konto) == _referenz(konto)
        # Vergaberecht (#221): Administratoren auch die Kontrollrechte, sonst nie
        erwartet = _referenz(konto) | AUDIT_PERMISSIONS if admin else _referenz(konto) - AUDIT_PERMISSIONS
        assert grantable_permissions(konto) == erwartet

    def test_ohne_rollen_und_ohne_konto(self, tenant: SessionTenant) -> None:
        konto = self._konto(tenant)
        assert role_permissions(konto) == _referenz(konto) == set()
        assert role_permissions(None) == set()

    def test_zufaellige_kombinationen(self, tenant: SessionTenant) -> None:
        zufall = random.Random(772)
        for i in range(60):
            rollen = [
                self._rolle(
                    tenant,
                    f"Kombination {i}/{j}",
                    {feld for feld in HAEKCHEN if zufall.random() < 0.3},
                    admin=zufall.random() < 0.15,
                )
                for j in range(zufall.randint(1, 3))
            ]
            konto = self._konto(tenant, *rollen)
            assert role_permissions(konto) == _referenz(konto), [r.name for r in rollen]
            assert SessionPermissionChecker(konto).permissions == _referenz(konto)

    def test_keine_zusaetzliche_abfrage_bei_vorgeladenen_rollen(self, tenant: SessionTenant) -> None:
        rollen = SessionRole.create_default_roles(tenant)
        konto = self._konto(tenant, *rollen.values())
        konto.roles.all()  # vorgeladen
        with CaptureQueriesContext(connection) as abfragen:
            role_permissions(konto)
        assert len(abfragen) == 0
