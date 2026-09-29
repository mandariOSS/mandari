# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ende-TOPs der Standard-Tagesordnung (z. B. „Verschiedenes“, „Schluss der Sitzung“).

Später ergänzte TOPs – von Hand angelegt oder aus der Beratungsfolge terminiert – kommen vor die
Ende-TOPs ihres Teils, auch nach mehreren Ergänzungen (die Neunummerierung setzt die Reihenfolge
auf 1..n, die Kennzeichnung ``is_end_item`` bleibt). Unterpunkte kommen ans Ende ihres TOPs. Die
Migration kennzeichnet Ende-TOPs bestehender Tagesordnungen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionStandardAgendaItem,
    SessionTenant,
    SessionUser,
)
from apps.session.services import textblock_service

VORHER = ("session", "0040_export_referenz_zaehler")
NACHHER = ("session", "0041_ende_top")


@dataclass
class Welt:
    tenant: SessionTenant
    gremium: SessionOrganization
    sitzung: SessionMeeting
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def ergaenzen(self, name: str, *, oeffentlich: bool = True, parent: SessionAgendaItem | None = None) -> None:
        daten = {"name": name}
        if oeffentlich:
            daten["is_public"] = "on"
        if parent is not None:
            daten["parent"] = str(parent.id)
        antwort = self.client.post(self.url(f"/meetings/{self.sitzung.id}/agenda/add/"), daten)
        assert antwort.status_code == 302, antwort.status_code

    def tagesordnung(self) -> list[tuple[str, str]]:
        return [
            (item.number, item.name)
            for item in SessionAgendaItem.objects.filter(meeting=self.sitzung).order_by("order", "created_at")
        ]


def _welt(*, noe_ende: bool = False) -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Ende", slug=f"ende-{uuid.uuid4().hex[:6]}")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    SessionStandardAgendaItem.objects.create(tenant=tenant, name="Eröffnung", placement="start", order=1)
    SessionStandardAgendaItem.objects.create(tenant=tenant, name="Verschiedenes", placement="end", order=1)
    SessionStandardAgendaItem.objects.create(tenant=tenant, name="Schluss der Sitzung", placement="end", order=2)
    if noe_ende:
        SessionStandardAgendaItem.objects.create(
            tenant=tenant, name="Verschiedenes (nichtöffentlich)", placement="end", order=3, is_public=False
        )
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=gremium, start=timezone.now() + timedelta(days=10)
    )
    textblock_service.apply_standard_items(sitzung)

    rolle = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_view_papers=True,
        can_edit_papers=True,
    )
    session_user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    session_user.roles.add(rolle)
    client = Client()
    client.force_login(session_user.user)
    return Welt(tenant=tenant, gremium=gremium, sitzung=sitzung, client=client)


@pytest.mark.django_db
class TestErgaenzen:
    def test_mehrere_ergaenzte_tops_vor_den_ende_tops(self) -> None:
        w = _welt()
        for name in ("Haushalt", "Radweg", "Schulbau"):
            w.ergaenzen(name)

        assert w.tagesordnung() == [
            ("1", "Eröffnung"),
            ("2", "Haushalt"),
            ("3", "Radweg"),
            ("4", "Schulbau"),
            ("5", "Verschiedenes"),
            ("6", "Schluss der Sitzung"),
        ]

    def test_terminierung_aus_der_beratungsfolge_vor_den_ende_tops(self) -> None:
        w = _welt()
        w.ergaenzen("Haushalt")
        vorlage = SessionPaper.objects.create(
            tenant=w.tenant, reference="V/7", name="Radweg", is_public=True, status="approved"
        )
        station = SessionConsultation.objects.create(paper=vorlage, organization=w.gremium, meeting=w.sitzung, order=1)

        antwort = w.client.post(w.url(f"/consultations/{station.id}/schedule/"))

        assert antwort.status_code == 302
        assert [name for _, name in w.tagesordnung()] == [
            "Eröffnung",
            "Haushalt",
            "V/7: Radweg",
            "Verschiedenes",
            "Schluss der Sitzung",
        ]

    def test_nichtoeffentlicher_teil_hat_eigene_ende_tops(self) -> None:
        w = _welt(noe_ende=True)
        w.ergaenzen("Haushalt")
        w.ergaenzen("Personalie", oeffentlich=False)
        w.ergaenzen("Grundstück", oeffentlich=False)

        assert w.tagesordnung() == [
            ("1", "Eröffnung"),
            ("2", "Haushalt"),
            ("3", "Verschiedenes"),
            ("4", "Schluss der Sitzung"),
            ("N1", "Personalie"),
            ("N2", "Grundstück"),
            ("N3", "Verschiedenes (nichtöffentlich)"),
        ]

    def test_unterpunkt_kommt_ans_ende_seines_tops(self) -> None:
        w = _welt()
        w.ergaenzen("Haushalt")
        haushalt = SessionAgendaItem.objects.get(meeting=w.sitzung, name="Haushalt")
        w.ergaenzen("Haushalt – Teil A", parent=haushalt)
        w.ergaenzen("Haushalt – Teil B", parent=haushalt)
        w.ergaenzen("Radweg")

        assert w.tagesordnung() == [
            ("1", "Eröffnung"),
            ("2", "Haushalt"),
            ("2.1", "Haushalt – Teil A"),
            ("2.2", "Haushalt – Teil B"),
            ("3", "Radweg"),
            ("4", "Verschiedenes"),
            ("5", "Schluss der Sitzung"),
        ]

    def test_verschobener_ende_top_zaehlt_nur_am_schluss(self) -> None:
        """Wer „Verschiedenes“ nach oben zieht, bekommt neue TOPs dahinter, aber vor „Schluss“."""
        w = _welt()
        w.ergaenzen("Haushalt")
        verschiedenes = SessionAgendaItem.objects.get(meeting=w.sitzung, name="Verschiedenes")
        w.client.post(w.url(f"/agenda/{verschiedenes.id}/move/"), {"direction": "up"})
        w.ergaenzen("Radweg")

        assert [name for _, name in w.tagesordnung()] == [
            "Eröffnung",
            "Verschiedenes",
            "Haushalt",
            "Radweg",
            "Schluss der Sitzung",
        ]

    def test_ohne_standard_tops_ans_ende(self) -> None:
        w = _welt()
        SessionAgendaItem.objects.filter(meeting=w.sitzung).delete()
        w.ergaenzen("Haushalt")
        w.ergaenzen("Radweg")

        assert w.tagesordnung() == [("1", "Haushalt"), ("2", "Radweg")]


@pytest.mark.django_db(transaction=True)
def test_migration_kennzeichnet_bestehende_ende_tops() -> None:
    """Ende-TOPs bestehender Tagesordnungen werden gekennzeichnet – auch nach einer Neunummerierung."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        historisch = executor.loader.project_state([VORHER]).apps
        Tenant = historisch.get_model("session", "SessionTenant")
        Organization = historisch.get_model("session", "SessionOrganization")
        Meeting = historisch.get_model("session", "SessionMeeting")
        Item = historisch.get_model("session", "SessionAgendaItem")
        Standard = historisch.get_model("session", "SessionStandardAgendaItem")

        tenant = Tenant.objects.create(name="Stadt Alt", slug="alt")
        rat = Organization.objects.create(tenant=tenant, name="Rat")
        bau = Organization.objects.create(tenant=tenant, name="Bauausschuss")
        Standard.objects.create(tenant=tenant, name="Verschiedenes", placement="end")
        Standard.objects.create(tenant=tenant, organization=rat, name="Schluss", placement="end")
        Standard.objects.create(tenant=tenant, name="Eröffnung", placement="start")
        kuenftig = timezone.now() + timedelta(days=5)

        def top(sitzung: Any, name: str, order: int, **felder: Any) -> Any:
            return Item.objects.create(meeting=sitzung, name=name, number=str(order), order=order, **felder)

        # Noch nicht neu nummeriert: hohe Order-Werte
        frisch = Meeting.objects.create(tenant=tenant, name="Frisch", organization=bau, start=kuenftig)
        frisch_ende = top(frisch, "Abschluss", 900000)
        frisch_start = top(frisch, "Eröffnung", 100)
        # Neu nummeriert, anstehend: nach dem Betreff der Standard-TOPs (Gremium beachtet)
        rat_sitzung = Meeting.objects.create(tenant=tenant, name="Rat", organization=rat, start=kuenftig)
        rat_verschiedenes = top(rat_sitzung, "Verschiedenes", 3)
        rat_schluss = top(rat_sitzung, "Schluss", 4)
        rat_start = top(rat_sitzung, "Eröffnung", 1)
        bau_sitzung = Meeting.objects.create(tenant=tenant, name="Bau", organization=bau, start=kuenftig)
        bau_schluss = top(bau_sitzung, "Schluss", 2)
        # Vergangene Sitzung bleibt unberührt
        alt = Meeting.objects.create(
            tenant=tenant, name="Alt", organization=rat, start=timezone.now() - timedelta(days=30)
        )
        alt_verschiedenes = top(alt, "Verschiedenes", 2)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        neu = (
            MigrationExecutor(connection).loader.project_state([NACHHER]).apps.get_model("session", "SessionAgendaItem")
        )
        gekennzeichnet = set(neu.objects.filter(is_end_item=True).values_list("pk", flat=True))
        assert gekennzeichnet == {frisch_ende.pk, rat_verschiedenes.pk, rat_schluss.pk}
        for pk in (frisch_start.pk, rat_start.pk, bau_schluss.pk, alt_verschiedenes.pk):
            assert pk not in gekennzeichnet
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
