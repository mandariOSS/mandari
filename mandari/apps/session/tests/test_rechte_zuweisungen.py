# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollenzuweisungen mit Geltungsbereich und Zeitraum (Issue #772, ADR „Rechte mit Geltungsbereich“).

- Spiegel: Jede Rolle aus ``SessionUser.roles`` hat genau eine aktive mandantenweite, unbefristete Zuweisung –
  nachgeführt bei jeder Änderung und beim Abgleich nach ``migrate``.
- Befristete Zuweisungen und Zuweisungen mit Geltungsbereich wirken nur bei eingeschaltetem Schalter und nur im
  Zugriffskontext, nie über die bisherigen Rechtenamen (keine Verhaltensänderung).
- Bäume: Körperschaft → Gremien (keine andere Körperschaft), Amt → Ämter darunter, Gremium nur sich selbst.
- Migration: Bestand wird gespiegelt, idempotent, umkehrbar; in PostgreSQL löschen die Fremdschlüssel selbst mit.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionBody,
    SessionOrganization,
    SessionRole,
    SessionRoleAssignment,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import role_permissions
from apps.session.rechte import kern, zuweisungen
from apps.session.rechte.aufloesung import zugriffskontext
from apps.session.rechte.bereiche import AMT, GREMIUM, KOERPERSCHAFT, bereich, bereichsbaum

HEUTE = timezone.localdate()


def _konto(tenant: SessionTenant, *rollen: SessionRole) -> SessionUser:
    konto = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    if rollen:
        konto.roles.add(*rollen)
    return konto


def _frisch(konto: SessionUser) -> SessionUser:
    return SessionUser.objects.select_related("tenant").prefetch_related("roles").get(pk=konto.pk)


def _spiegel(konto: SessionUser) -> set[Any]:
    return set(
        SessionRoleAssignment.objects.filter(zuweisungen.spiegel_q(), user=konto).values_list("role_id", flat=True)
    )


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Samtgemeinde Muster", slug="sg-muster-rechte")


@pytest.fixture
def rollen(tenant: SessionTenant) -> dict[str, SessionRole]:
    return SessionRole.create_default_roles(tenant)


class TestKern:
    def test_teilbaum_mit_zyklus_und_unbekannt(self) -> None:
        a, b, c = (kern.Bereich("amt", x) for x in "abc")
        baum = kern.Baum({a: [b], b: [c], c: [a]})
        assert baum.teilbaum(b) == {a, b, c}
        assert baum.teilbaum(kern.Bereich("amt", "x")) == frozenset()

    def test_zeitraum_einschliesslich(self) -> None:
        z = kern.Zuweisung(rechte=frozenset({"r"}), gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 1, 31))
        assert not z.wirkt_am(date(2025, 12, 31))
        assert z.wirkt_am(date(2026, 1, 1)) and z.wirkt_am(date(2026, 1, 31))
        assert not z.wirkt_am(date(2026, 2, 1))

    def test_kontext_mandantenweit_vor_bereich(self) -> None:
        amt = kern.Bereich("amt", "1")
        baum = kern.Baum({amt: []})
        kontext = kern.zugriffskontext(
            [
                kern.Zuweisung(rechte=frozenset({"a", "b"}), bereich=amt),
                kern.Zuweisung(rechte=frozenset({"a"})),
            ],
            HEUTE,
            baum,
        )
        assert kontext.gilt_mandantenweit("a") and not kontext.bereiche_fuer("a")
        assert kontext.kennungen("b", "amt") == {"1"}
        assert kontext.gilt_in("b", [amt]) and not kontext.gilt_in("b", [kern.Bereich("amt", "2")])
        assert not kontext.gilt_in("c", [amt])


@pytest.mark.django_db
class TestSpiegel:
    def test_hinzufuegen_entfernen_setzen_leeren(self, tenant: SessionTenant, rollen: dict[str, SessionRole]) -> None:
        konto = _konto(tenant, rollen["clerk"])
        assert _spiegel(konto) == {rollen["clerk"].pk}
        konto.roles.add(rollen["clerk"])  # zweimal: weiter genau eine
        assert SessionRoleAssignment.objects.filter(user=konto).count() == 1
        zuweisung = SessionRoleAssignment.objects.get(user=konto)
        assert (zuweisung.source, zuweisung.tenant_id, zuweisung.is_mirror) == ("manuell", tenant.pk, True)

        konto.roles.set([rollen["viewer"], rollen["recorder"]])
        assert _spiegel(konto) == {rollen["viewer"].pk, rollen["recorder"].pk}
        # Aufgehobene bleiben als Nachweis
        alt = SessionRoleAssignment.objects.get(user=konto, role=rollen["clerk"])
        assert alt.revoked_at is not None

        konto.roles.remove(rollen["viewer"])
        assert _spiegel(konto) == {rollen["recorder"].pk}
        konto.roles.clear()
        assert _spiegel(konto) == set()
        assert SessionRoleAssignment.objects.filter(user=konto).count() == 3

    def test_von_der_rolle_aus(self, tenant: SessionTenant, rollen: dict[str, SessionRole]) -> None:
        eins, zwei = _konto(tenant), _konto(tenant)
        rollen["viewer"].users.add(eins, zwei)
        assert _spiegel(eins) == _spiegel(zwei) == {rollen["viewer"].pk}
        rollen["viewer"].users.remove(eins)
        assert _spiegel(eins) == set()
        rollen["viewer"].users.clear()
        assert _spiegel(zwei) == set()

    def test_abgleich_holt_aenderungen_ohne_signal_nach(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole]
    ) -> None:
        konto = _konto(tenant)
        through = SessionUser.roles.through
        # Wie ein älteres Image oder eine Massenänderung: ohne m2m_changed
        through.objects.bulk_create([through(sessionuser_id=konto.pk, sessionrole_id=rollen["clerk"].pk)])
        bleibt = _konto(tenant, rollen["viewer"])
        geht = _konto(tenant, rollen["recorder"])
        through.objects.filter(sessionuser_id=geht.pk).delete()

        assert zuweisungen.abgleichen() == {"angelegt": 1, "aufgehoben": 1}
        assert _spiegel(konto) == {rollen["clerk"].pk}
        assert SessionRoleAssignment.objects.get(user=konto).source == "migration"
        assert _spiegel(bleibt) == {rollen["viewer"].pk}
        assert _spiegel(geht) == set()
        assert zuweisungen.abgleichen() == {"angelegt": 0, "aufgehoben": 0}

    def test_abgleich_ohne_modell_im_migrationsstand(self) -> None:
        class Ohne:
            def get_model(self, app_label: str, model_name: str) -> Any:
                raise LookupError(model_name)

        assert zuweisungen.abgleichen(Ohne()) == {}

    def test_post_migrate_ist_verbunden(self) -> None:
        from django.db.models.signals import post_migrate

        assert any("session_rollen_post_migrate" in str(eintrag[0]) for eintrag in post_migrate.receivers)


@pytest.fixture
def aufbau(tenant: SessionTenant) -> dict[str, Any]:
    """Samtgemeinde mit Mitgliedsgemeinde, Gremien beider und einem Ämterbaum."""
    sg = SessionBody.objects.get(tenant=tenant, is_default=True)
    mg = SessionBody.objects.create(tenant=tenant, name="Gemeinde Mitglied", slug="mitglied", parent=sg)
    org = SessionOrganization.objects
    return {
        "sg": sg,
        "mg": mg,
        "sg_rat": org.create(tenant=tenant, body=sg, name="Samtgemeinderat", organization_type="council"),
        "sg_ausschuss": org.create(tenant=tenant, body=sg, name="Bauausschuss", organization_type="committee"),
        "mg_rat": org.create(tenant=tenant, body=mg, name="Gemeinderat", organization_type="council"),
        "fachbereich": (fb := org.create(tenant=tenant, name="Fachbereich 2", organization_type="department")),
        "bauamt": (bau := org.create(tenant=tenant, name="Bauamt", organization_type="department", parent=fb)),
        "sachgebiet": org.create(tenant=tenant, name="Sachgebiet Hochbau", organization_type="department", parent=bau),
        "hauptamt": org.create(tenant=tenant, name="Hauptamt", organization_type="department"),
    }


@pytest.mark.django_db
class TestBereiche:
    def test_koerperschaft_umfasst_nur_ihre_gremien(self, tenant: SessionTenant, aufbau: dict[str, Any]) -> None:
        baum = bereichsbaum(tenant)
        sg = baum.teilbaum(bereich(KOERPERSCHAFT, aufbau["sg"].pk))
        assert sg == {
            bereich(KOERPERSCHAFT, aufbau["sg"].pk),
            bereich(GREMIUM, aufbau["sg_rat"].pk),
            bereich(GREMIUM, aufbau["sg_ausschuss"].pk),
        }
        # Nie die Mitgliedsgemeinde, nie die Ämter der Verwaltung
        assert bereich(KOERPERSCHAFT, aufbau["mg"].pk) not in sg
        assert baum.teilbaum(bereich(KOERPERSCHAFT, aufbau["mg"].pk)) == {
            bereich(KOERPERSCHAFT, aufbau["mg"].pk),
            bereich(GREMIUM, aufbau["mg_rat"].pk),
        }

    def test_amt_umfasst_die_aemter_darunter(self, tenant: SessionTenant, aufbau: dict[str, Any]) -> None:
        baum = bereichsbaum(tenant)
        assert baum.teilbaum(bereich(AMT, aufbau["bauamt"].pk)) == {
            bereich(AMT, aufbau["bauamt"].pk),
            bereich(AMT, aufbau["sachgebiet"].pk),
        }
        assert bereich(AMT, aufbau["hauptamt"].pk) not in baum.teilbaum(bereich(AMT, aufbau["fachbereich"].pk))
        assert baum.teilbaum(bereich(GREMIUM, aufbau["sg_ausschuss"].pk)) == {
            bereich(GREMIUM, aufbau["sg_ausschuss"].pk)
        }

    def test_fremde_und_geloeschte_bereiche_wirken_nicht(self, tenant: SessionTenant, aufbau: dict[str, Any]) -> None:
        fremd = SessionTenant.objects.create(name="Andere", slug="andere-rechte")
        fremdes_amt = SessionOrganization.objects.create(tenant=fremd, name="Amt", organization_type="department")
        baum = bereichsbaum(tenant)
        assert baum.teilbaum(bereich(AMT, fremdes_amt.pk)) == frozenset()
        assert baum.teilbaum(bereich(GREMIUM, aufbau["bauamt"].pk)) == frozenset()  # falsche Art


@pytest.mark.django_db
class TestZuweisen:
    def test_mandantenweit_unbefristet_ist_der_spiegel(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole]
    ) -> None:
        admin = _konto(tenant, rollen["admin"])
        konto = _konto(tenant)
        zuweisung = zuweisungen.zuweisen(konto, rollen["clerk"], vermerk="Einarbeitung", von=admin)
        assert list(konto.roles.all()) == [rollen["clerk"]]
        assert SessionRoleAssignment.objects.filter(user=konto).count() == 1
        assert (zuweisung.created_by, zuweisung.note, zuweisung.is_mirror) == (admin, "Einarbeitung", True)
        assert zuweisungen.zuweisen(konto, rollen["clerk"]) == zuweisung

        zuweisungen.aufheben(zuweisung, von=admin)
        zuweisung.refresh_from_db()
        assert zuweisung.revoked_by == admin and zuweisung.revoked_at is not None
        assert list(konto.roles.all()) == []

    def test_begrenzt_und_befristet_ohne_wirkung_auf_die_bisherigen_rechte(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole], aufbau: dict[str, Any]
    ) -> None:
        tenant.scoped_permissions_enabled = True
        tenant.save(update_fields=["scoped_permissions_enabled"])
        konto = _konto(tenant, rollen["viewer"])
        vorher = role_permissions(_frisch(konto))
        zuweisungen.zuweisen(konto, rollen["clerk"], bereich_art=AMT, bereich_kennung=aufbau["bauamt"].pk)
        zuweisungen.zuweisen(konto, rollen["recorder"], gueltig_bis=HEUTE + timedelta(days=5))
        assert list(konto.roles.all()) == [rollen["viewer"]]
        assert role_permissions(_frisch(konto)) == vorher

    @pytest.mark.parametrize(
        ("art", "kennung", "admin", "bis_vor_von"),
        [
            ("amt", None, False, False),
            ("unbekannt", "x", False, False),
            ("mandant", "x", False, False),
            ("mandant", None, True, True),
        ],
    )
    def test_ungueltige_zuweisungen(
        self,
        tenant: SessionTenant,
        rollen: dict[str, SessionRole],
        art: str,
        kennung: Any,
        admin: bool,
        bis_vor_von: bool,
    ) -> None:
        konto = _konto(tenant)
        with pytest.raises(ValueError):
            zuweisungen.zuweisen(
                konto,
                rollen["admin" if admin else "clerk"],
                bereich_art=art,
                bereich_kennung=kennung,
                gueltig_von=HEUTE if bis_vor_von else None,
                gueltig_bis=HEUTE - timedelta(days=1) if bis_vor_von else None,
            )

    def test_administrator_nur_mandantenweit_unbefristet(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole], aufbau: dict[str, Any]
    ) -> None:
        konto = _konto(tenant)
        with pytest.raises(ValueError):
            zuweisungen.zuweisen(konto, rollen["admin"], bereich_art=AMT, bereich_kennung=aufbau["bauamt"].pk)
        with pytest.raises(ValueError):
            zuweisungen.zuweisen(konto, rollen["admin"], gueltig_bis=HEUTE)

    def test_rolle_und_bereich_aus_demselben_mandanten(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole]
    ) -> None:
        fremd = SessionTenant.objects.create(name="Andere", slug="andere-zuweisen")
        fremde_rolle = SessionRole.objects.create(tenant=fremd, name="Fremd")
        fremdes_amt = SessionOrganization.objects.create(tenant=fremd, name="Amt", organization_type="department")
        konto = _konto(tenant)
        with pytest.raises(ValueError):
            zuweisungen.zuweisen(konto, fremde_rolle)
        with pytest.raises(ValueError):
            zuweisungen.zuweisen(konto, rollen["clerk"], bereich_art=AMT, bereich_kennung=fremdes_amt.pk)


@pytest.mark.django_db
class TestZugriffskontext:
    def _zuweisungen(self, konto: SessionUser, rollen: dict[str, SessionRole], aufbau: dict[str, Any]) -> None:
        zuweisungen.zuweisen(konto, rollen["clerk"], bereich_art=AMT, bereich_kennung=aufbau["bauamt"].pk)
        zuweisungen.zuweisen(konto, rollen["clerk"], bereich_art=KOERPERSCHAFT, bereich_kennung=aufbau["mg"].pk)
        zuweisungen.zuweisen(konto, rollen["recorder"], gueltig_von=HEUTE, gueltig_bis=HEUTE + timedelta(days=1))
        zuweisungen.zuweisen(konto, rollen["clerk"], gueltig_von=HEUTE + timedelta(days=3))

    def test_ohne_schalter_genau_die_rollen(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole], aufbau: dict[str, Any]
    ) -> None:
        konto = _konto(tenant, rollen["viewer"])
        self._zuweisungen(konto, rollen, aufbau)
        konto = _frisch(konto)
        with CaptureQueriesContext(connection) as abfragen:
            kontext = zugriffskontext(konto)
        assert len(abfragen) == 0
        assert not kontext.bereiche
        assert "vorlage.sehen" in kontext.mandantenweit
        assert "vorlage.noe_sehen" not in kontext.mandantenweit

    def test_mit_schalter(self, tenant: SessionTenant, rollen: dict[str, SessionRole], aufbau: dict[str, Any]) -> None:
        tenant.scoped_permissions_enabled = True
        tenant.save(update_fields=["scoped_permissions_enabled"])
        konto = _konto(tenant, rollen["viewer"])
        self._zuweisungen(konto, rollen, aufbau)
        kontext = zugriffskontext(_frisch(konto))

        # Befristet und heute wirksam: Protokollant mandantenweit; die künftige Zuweisung wirkt noch nicht
        assert "niederschrift.bearbeiten" in kontext.mandantenweit
        assert "vorlage.anlegen" not in kontext.mandantenweit
        # Sachbearbeitung für das Bauamt (mit Sachgebiet), nicht das Hauptamt
        assert kontext.kennungen("vorlage.bearbeiten", AMT) == {str(aufbau["bauamt"].pk), str(aufbau["sachgebiet"].pk)}
        # … und für die Mitgliedsgemeinde mit ihren Gremien, nicht die Samtgemeinde
        assert kontext.kennungen("sitzung.bearbeiten", KOERPERSCHAFT) == {str(aufbau["mg"].pk)}
        assert kontext.kennungen("sitzung.bearbeiten", GREMIUM) == {str(aufbau["mg_rat"].pk)}
        assert kontext.gilt_in("vorlage.bearbeiten", [bereich(AMT, aufbau["sachgebiet"].pk)])
        assert not kontext.gilt_in("vorlage.bearbeiten", [bereich(AMT, aufbau["hauptamt"].pk)])
        # Mandantenweit Gültiges steht nicht zusätzlich in den Bereichen
        assert not kontext.bereiche_fuer("vorlage.sehen")
        # Die bisherigen Rechtenamen bleiben unverändert (nur die Rollen des Kontos)
        assert role_permissions(_frisch(konto)) == role_permissions(_frisch(_konto(tenant, rollen["viewer"])))

    def test_aufgehobene_und_fremde_zuweisungen_wirken_nicht(
        self, tenant: SessionTenant, rollen: dict[str, SessionRole], aufbau: dict[str, Any]
    ) -> None:
        tenant.scoped_permissions_enabled = True
        tenant.save(update_fields=["scoped_permissions_enabled"])
        konto = _konto(tenant)
        zuweisung = zuweisungen.zuweisen(konto, rollen["clerk"], bereich_art=AMT, bereich_kennung=aufbau["bauamt"].pk)
        zuweisungen.aufheben(zuweisung)
        # Direkt eingetragen (z. B. Admin): Administrator-Rolle mit Bereich und gelöschtes Gremium wirken nicht
        SessionRoleAssignment.objects.create(
            tenant=tenant, user=konto, role=rollen["admin"], scope_type=AMT, scope_id=aufbau["hauptamt"].pk
        )
        weg = SessionOrganization.objects.create(tenant=tenant, name="Aufgelöst", organization_type="committee")
        SessionRoleAssignment.objects.create(
            tenant=tenant, user=konto, role=rollen["clerk"], scope_type=GREMIUM, scope_id=weg.pk
        )
        weg.delete()
        kontext = zugriffskontext(_frisch(konto))
        assert kontext == kern.Zugriffskontext()


@pytest.mark.django_db
def test_fremdschluessel_loeschen_in_postgresql_selbst_mit(
    tenant: SessionTenant, rollen: dict[str, SessionRole]
) -> None:
    """Ein älteres Image kennt die Tabelle nicht: Löschen von Konto und Rolle darf nicht an ihr scheitern."""
    if connection.vendor != "postgresql":
        pytest.skip("Datenbankregeln für das Mitlöschen nur in PostgreSQL")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.attname, c.confdeltype FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.conrelid = 'session_role_assignments'::regclass AND c.contype = 'f'"
        )
        regeln = dict(cursor.fetchall())
    assert regeln == {
        "tenant_id": "c",
        "user_id": "c",
        "role_id": "c",
        "created_by_id": "n",
        "revoked_by_id": "n",
    }
    konto = _konto(tenant, rollen["clerk"])
    zuweisungen.zuweisen(_konto(tenant), rollen["viewer"], von=konto)
    with connection.cursor() as cursor:
        # Wie ein älteres Image: erst die bekannten Verknüpfungen, dann das Konto – ohne die Zuweisungen
        cursor.execute("DELETE FROM session_users_roles WHERE sessionuser_id = %s", [konto.pk])
        cursor.execute("DELETE FROM session_users WHERE id = %s", [konto.pk])
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
    assert not SessionRoleAssignment.objects.filter(user_id=konto.pk).exists()
    assert SessionRoleAssignment.objects.get(role=rollen["viewer"]).created_by_id is None


def _migrationen() -> tuple[tuple[str, str], tuple[str, str]]:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    namen = sorted(name for app, name in loader.disk_migrations if app == "session")
    schema = next(name for name in namen if name.endswith("_rollenzuweisungen"))
    daten = next(name for name in namen if name.endswith("_rollenzuweisungen_spiegeln"))
    vorher = next(dep for dep in loader.disk_migrations[("session", schema)].dependencies if dep[0] == "session")
    return (vorher[0], vorher[1]), ("session", daten)


@pytest.mark.django_db(transaction=True)
def test_migration_spiegelt_den_bestand() -> None:
    ausgang, ziel = _migrationen()
    executor = MigrationExecutor(connection)
    executor.migrate([ausgang])
    try:
        alt = executor.loader.project_state([ausgang]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Role = alt.get_model("session", "SessionRole")
        Konto = alt.get_model("session", "SessionUser")
        User = alt.get_model("accounts", "User")

        stadt = Tenant.objects.create(name="Stadt Alt", slug="stadt-alt-rechte")
        admin = Role.objects.create(tenant=stadt, name="Administrator", is_admin=True)
        lesen = Role.objects.create(tenant=stadt, name="Lesezugriff")
        eins = Konto.objects.create(user=User.objects.create(email="eins@example.org"), tenant=stadt)
        zwei = Konto.objects.create(user=User.objects.create(email="zwei@example.org"), tenant=stadt)
        Konto.objects.create(user=User.objects.create(email="ohne@example.org"), tenant=stadt)
        eins.roles.set([admin, lesen])
        zwei.roles.set([lesen])

        executor = MigrationExecutor(connection)
        executor.migrate([ziel])
        neu = MigrationExecutor(connection).loader.project_state([ziel]).apps
        Zuweisung = neu.get_model("session", "SessionRoleAssignment")
        Tenant = neu.get_model("session", "SessionTenant")

        paare = set(Zuweisung.objects.values_list("user_id", "role_id", "scope_type", "source", "tenant_id"))
        assert paare == {
            (eins.pk, admin.pk, "mandant", "migration", stadt.pk),
            (eins.pk, lesen.pk, "mandant", "migration", stadt.pk),
            (zwei.pk, lesen.pk, "mandant", "migration", stadt.pk),
        }
        assert not Zuweisung.objects.filter(revoked_at__isnull=False).exists()
        assert not Zuweisung.objects.exclude(valid_from=None, valid_until=None).exists()
        assert Tenant.objects.get(pk=stadt.pk).scoped_permissions_enabled is False

        # Idempotent: ein zweiter Lauf legt nichts an
        schritt = importlib.import_module(f"apps.session.migrations.{ziel[1]}")
        schritt.spiegeln(neu, None)
        assert Zuweisung.objects.count() == 3

        # Umkehrbar bis zum Entfernen der Häkchen
        executor = MigrationExecutor(connection)
        executor.migrate([ausgang])
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
