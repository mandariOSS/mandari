# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ö/NÖ im Durchstich: Ein TOP, eine Vorlage oder eine Anlage, die in Session nicht-öffentlich
wird, verschwindet im selben Moment aus dem Bürgerportal – ohne auf den nächsten Sync des
Spiegels zu warten – und zeigt dort keinen Inhalt mehr. Dazu die Regeln der Tagesordnung:
keine NÖ-Unterpunkte unter Ö-TOPs, keine NÖ-Vorlagen auf Ö-TOPs.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.oparl_publication import retract_from_portal
from apps.session.services import agenda_service
from insight_core.models import OParlAgendaItem, OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db
HOST = "https://mandari.example"


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Sitzung HA", organization=gremium, start=timezone.now(), is_public=True
    )
    vorlage = SessionPaper.objects.create(tenant=tenant, name="Radweg Hauptstraße", is_public=True, status="approved")
    top = SessionAgendaItem.objects.create(meeting=sitzung, name="Radweg", number="1", order=1, paper=vorlage)
    # Spiegel im Bürgerportal, wie ihn der Ingestor aus der Session-OParl-API anlegt
    basis = f"{HOST}/session/nord/api/oparl/"
    source = OParlSource.objects.create(name="Bezirk Nord (Session)", url=basis + "system/")
    body = OParlBody.objects.create(
        external_id=basis + "body/1/", source=source, name="Bezirk Nord", slug="bezirk-nord"
    )
    m = OParlMeeting.objects.create(external_id=f"{basis}meeting/{sitzung.id}/", body=body, name="Sitzung HA")
    t = OParlAgendaItem.objects.create(external_id=f"{basis}agendaitem/{top.id}/", meeting=m, name="Radweg", number="1")
    p = OParlPaper.objects.create(external_id=f"{basis}paper/{vorlage.id}/", body=body, name="Radweg Hauptstraße")
    f = OParlFile.objects.create(external_id=f"{basis}file/abc/", body=body, paper=p, name="Anlage")
    return {"tenant": tenant, "sitzung": sitzung, "vorlage": vorlage, "top": top, "m": m, "t": t, "p": p, "f": f}


class TestSofortigeRuecknahme:
    def test_top_auf_noe_verschwindet_sofort(
        self, welt: dict[str, Any], django_capture_on_commit_callbacks: Any
    ) -> None:
        client = Client()
        seite = client.get(f"/insight/termine/{welt['m'].id}/").content.decode()
        assert "Radweg" in seite
        with django_capture_on_commit_callbacks(execute=True):
            welt["top"].is_public = False
            welt["top"].save()
        welt["t"].refresh_from_db()
        assert welt["t"].deleted is True
        seite = client.get(f"/insight/termine/{welt['m'].id}/").content.decode()
        assert "Radweg" not in seite

    def test_zurueckgenommene_vorlage_zeigt_keinen_inhalt(
        self, welt: dict[str, Any], django_capture_on_commit_callbacks: Any
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            welt["top"].is_public = False
            welt["top"].save()
            welt["vorlage"].is_public = False
            welt["vorlage"].save()
        antwort = Client().get(f"/insight/vorgaenge/{welt['p'].id}/")
        assert antwort.status_code == 410
        assert "Radweg" not in antwort.content.decode()
        assert antwort["Cache-Control"] == "no-store"

    def test_datei_wird_nicht_mehr_ausgeliefert(self, welt: dict[str, Any]) -> None:
        welt["f"].mark_deleted()
        antwort = Client().get(f"/insight/dokumente/{welt['f'].id}/preview/")
        assert antwort.status_code == 410

    def test_fremdquelle_bleibt_transparent(self, welt: dict[str, Any]) -> None:
        """Gelöschtes aus fremden Ratsinformationssystemen: weiter mit Hinweis sichtbar (bisherige Praxis)."""
        fremd = OParlPaper.objects.create(
            external_id="https://ris.fremd.example/oparl/paper/1", body=welt["p"].body, name="Fremde Vorlage"
        )
        fremd.mark_deleted()
        antwort = Client().get(f"/insight/vorgaenge/{fremd.id}/")
        assert antwort.status_code == 200 and "zurückgezogen" in antwort.content.decode()

    def test_andere_mandanten_unberuehrt(self, welt: dict[str, Any]) -> None:
        andere = OParlAgendaItem.objects.create(
            external_id=f"{HOST}/session/sued/api/oparl/agendaitem/{welt['top'].id}/", meeting=welt["m"], name="Süd"
        )
        assert retract_from_portal(welt["tenant"].id, "agendaitem", welt["top"].id) == 1
        andere.refresh_from_db()
        assert andere.deleted is False


class TestTagesordnungsregeln:
    def test_noe_unterpunkt_unter_oe_top_verboten(self, welt: dict[str, Any]) -> None:
        kind = SessionAgendaItem(meeting=welt["sitzung"], name="Grundstück", parent=welt["top"], is_public=False)
        assert "is_public" in agenda_service.visibility_errors(kind)

    def test_noe_vorlage_nicht_auf_oe_top(self, welt: dict[str, Any]) -> None:
        geheim = SessionPaper.objects.create(tenant=welt["tenant"], name="Personalie", is_public=False)
        top = SessionAgendaItem(meeting=welt["sitzung"], name="Personalie", paper=geheim, is_public=True)
        assert "paper" in agenda_service.visibility_errors(top)
        top.is_public = False
        assert agenda_service.visibility_errors(top) == {}

    def test_unterpunkte_folgen_dem_top(self, welt: dict[str, Any]) -> None:
        kind = SessionAgendaItem.objects.create(
            meeting=welt["sitzung"], name="Teil a", parent=welt["top"], number="1.1", order=2
        )
        welt["top"].is_public = False
        welt["top"].save()
        assert agenda_service.cascade_visibility(welt["top"]) == 1
        kind.refresh_from_db()
        assert kind.is_public is False

    def test_oeffentliche_ansicht_ohne_noe_unterpunkte(self, welt: dict[str, Any]) -> None:
        # Altbestand vor dieser Regel: NÖ-Unterpunkt unter Ö-TOP darf nirgends öffentlich erscheinen
        SessionAgendaItem.objects.create(
            meeting=welt["sitzung"], name="GEHEIM", parent=welt["top"], number="1.1", order=2, is_public=False
        )
        oeffentlich = agenda_service.grouped_agenda(welt["sitzung"], include_non_public=False)
        assert [c.name for c in oeffentlich["public"][0].children_list] == []
        intern = agenda_service.grouped_agenda(welt["sitzung"], include_non_public=True)
        assert [c.name for c in intern["public"][0].children_list] == ["GEHEIM"]

    def test_vorlage_auf_oe_top_bleibt_oeffentlich(self, welt: dict[str, Any]) -> None:
        role = SessionRole.objects.create(tenant=welt["tenant"], name="Admin", is_admin=True)
        user = cast(Any, UserFactory)()
        su = SessionUser.objects.create(user=user, tenant=welt["tenant"])
        su.roles.add(role)
        client = Client()
        client.force_login(user)
        v = welt["vorlage"]
        antwort = client.post(
            f"/session/nord/papers/{v.id}/edit/",
            {"name": v.name, "paper_type": v.paper_type, "status": v.status},  # is_public fehlt = aus
        )
        assert antwort.status_code == 200
        assert "öffentlichen Tagesordnungspunkten" in antwort.content.decode()
        v.refresh_from_db()
        assert v.is_public is True


class TestOParlNurFreigegebenes:
    def test_entwurf_erscheint_erst_nach_freigabe(self, welt: dict[str, Any]) -> None:
        entwurf = SessionPaper.objects.create(tenant=welt["tenant"], name="ENTWURF-INTERN", is_public=True)
        liste = Client().get("/session/nord/api/oparl/papers/").content.decode()
        assert "ENTWURF-INTERN" not in liste and "Radweg Hauptstraße" in liste
        assert Client().get(f"/session/nord/api/oparl/paper/{entwurf.id}/").status_code == 404
        entwurf.status = "approved"
        entwurf.save()
        assert "ENTWURF-INTERN" in Client().get("/session/nord/api/oparl/papers/").content.decode()
