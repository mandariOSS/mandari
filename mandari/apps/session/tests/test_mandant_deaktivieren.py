# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Deaktivieren eines Mandanten nimmt seine Bürgerportal-Quelle zurück (Issue #317, Teil A).

Quelle inaktiv, Kommune nicht mehr gelistet, alle gespiegelten Einträge zurückgenommen; das
Reaktivieren stellt genau das wieder her – ohne Einträge, die inzwischen in Session gelöscht oder
nichtöffentlich sind. Die Admin-Aktionen speichern einzeln über den Service statt per
``queryset.update()``; auch das Speichern im Formular und ``save()`` lösen die Rücknahme aus.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from django.contrib.admin.sites import site
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
    SessionOParlTombstone,
    SessionPaper,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
)
from apps.session.services import insight_service, tenant_provisioning
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db
CHANGELIST = "/admin/session/sessiontenant/"


@pytest.fixture
def welt() -> dict[str, Any]:
    """Veröffentlichender Mandant mit gespiegelter Kommune, wie sie der Ingestor anlegt."""
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", insight_publish=True)
    source = OParlSource.objects.get(sync_config__session_tenant="nord")
    basis = source.url
    body = OParlBody.objects.create(external_id=f"{basis}body/", source=source, name="Bezirk Nord", slug="bezirk-nord")
    ids = {name: uuid.uuid4() for name in ("m", "t", "p", "f", "o", "pe", "ms", "wp")}
    meeting = OParlMeeting.objects.create(external_id=f"{basis}meeting/{ids['m']}/", body=body, name="Sitzung")
    paper = OParlPaper.objects.create(external_id=f"{basis}paper/{ids['p']}/", body=body, name="Radweg")
    organization = OParlOrganization.objects.create(external_id=f"{basis}organization/{ids['o']}/", body=body)
    person = OParlPerson.objects.create(external_id=f"{basis}person/{ids['pe']}/", body=body, name="Erika Muster")
    eintraege = [
        meeting,
        paper,
        organization,
        person,
        OParlAgendaItem.objects.create(external_id=f"{basis}agendaitem/{ids['t']}/", meeting=meeting, name="TOP 1"),
        OParlFile.objects.create(external_id=f"{basis}file/{ids['f']}/", paper=paper, name="Anlage"),
        OParlMembership.objects.create(
            external_id=f"{basis}membership/{ids['ms']}/", person=person, organization=organization
        ),
        OParlLegislativeTerm.objects.create(external_id=f"{basis}legislativeterm/{ids['wp']}/", body=body),
    ]
    return {"tenant": tenant, "source": source, "body": body, "eintraege": eintraege, "ids": ids}


def _neu_geladen(objekte: list[Any]) -> list[Any]:
    return [type(obj).objects.get(pk=obj.pk) for obj in objekte]


class TestService:
    def test_deaktivieren_nimmt_quelle_zurueck(self, welt: dict[str, Any]) -> None:
        ergebnis = tenant_provisioning.set_tenant_active(welt["tenant"], False, actor="Test")

        assert ergebnis.changed and ergebnis.portal is not None
        assert ergebnis.portal.as_dict() == {"quellen": 1, "kommunen": 1, "eintraege": 8}
        welt["source"].refresh_from_db()
        welt["body"].refresh_from_db()
        assert welt["source"].is_active is False
        assert welt["body"].is_listed is False
        eintraege = _neu_geladen(welt["eintraege"])
        assert all(obj.deleted for obj in eintraege)
        assert len({obj.deleted_at for obj in eintraege}) == 1, "ein gemeinsamer Zeitpunkt"
        assert all(obj.withdrawn_by_publisher for obj in eintraege)
        aktionen = list(
            SessionAuditLog.objects.filter(tenant=welt["tenant"], model_name="SessionTenant").values_list(
                "action", flat=True
            )
        )
        assert sorted(aktionen) == ["unpublish", "update"]

    def test_reaktivieren_stellt_wieder_her(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant_provisioning.set_tenant_active(tenant, False, actor="Test")

        ergebnis = tenant_provisioning.set_tenant_active(tenant, True, actor="Test")

        assert ergebnis.portal is not None and ergebnis.portal.entries == 8
        welt["source"].refresh_from_db()
        welt["body"].refresh_from_db()
        assert welt["source"].is_active is True
        assert insight_service.RETRACTION_KEY not in welt["source"].sync_config
        assert welt["body"].is_listed is True
        assert not any(obj.deleted for obj in _neu_geladen(welt["eintraege"]))
        assert SessionAuditLog.objects.filter(tenant=tenant, action="publish").exists()

    def test_inzwischen_zurueckgenommenes_bleibt_zurueckgenommen(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        vorher = welt["eintraege"][1]  # Vorlage war schon vor der Deaktivierung zurückgenommen
        vorher.mark_deleted()
        tenant_provisioning.set_tenant_active(tenant, False)
        # Während der Deaktivierung wurde die Sitzung in Session nichtöffentlich (Tombstone)
        SessionOParlTombstone.objects.create(
            tenant=tenant, oparl_type="meeting", object_id=welt["ids"]["m"], object_created_at=timezone.now()
        )

        tenant_provisioning.set_tenant_active(tenant, True)

        sitzung, vorlage, *uebrige = _neu_geladen(welt["eintraege"])
        assert sitzung.deleted and vorlage.deleted
        assert not any(obj.deleted for obj in uebrige)

    def test_nicht_gelistete_kommune_bleibt_ungelistet(self, welt: dict[str, Any]) -> None:
        welt["body"].is_listed = False
        welt["body"].save(update_fields=["is_listed"])

        tenant_provisioning.set_tenant_active(welt["tenant"], False)
        tenant_provisioning.set_tenant_active(welt["tenant"], True)

        welt["body"].refresh_from_db()
        assert welt["body"].is_listed is False

    def test_ohne_veroeffentlichung_bleibt_quelle_aus(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant_provisioning.set_tenant_active(tenant, False)
        tenant.insight_publish = False
        tenant.save(update_fields=["insight_publish"])

        tenant_provisioning.set_tenant_active(tenant, True)

        welt["source"].refresh_from_db()
        assert welt["source"].is_active is False
        assert all(obj.deleted for obj in _neu_geladen(welt["eintraege"]))
        # Erst das erneute Veröffentlichen hebt die Rücknahme auf
        tenant.insight_publish = True
        tenant.save(update_fields=["insight_publish"])
        welt["source"].refresh_from_db()
        assert welt["source"].is_active is True
        assert not any(obj.deleted for obj in _neu_geladen(welt["eintraege"]))

    def test_speichern_ohne_service_nimmt_ebenfalls_zurueck(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant.is_active = False
        tenant.save()

        welt["body"].refresh_from_db()
        assert welt["body"].is_listed is False
        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="update", model_name="SessionTenant")
        assert eintrag.changes["is_active"] == {"alt": True, "neu": False}

    def test_update_fields_ohne_is_active_loest_nichts_aus(self, welt: dict[str, Any]) -> None:
        tenant = SessionTenant.objects.get(pk=welt["tenant"].pk)
        tenant.is_active = False  # veralteter Stand im Speicher, aber nicht mitgespeichert
        cast(Any, tenant).save(update_fields=["description"])

        welt["body"].refresh_from_db()
        assert welt["body"].is_listed is True

    def test_altbestand_inaktiv_wird_nachtraeglich_zurueckgenommen(self, welt: dict[str, Any]) -> None:
        # Früher setzte die Admin-Aktion per update(): Quelle und Einträge blieben öffentlich
        SessionTenant.objects.filter(pk=welt["tenant"].pk).update(is_active=False)
        tenant = SessionTenant.objects.get(pk=welt["tenant"].pk)

        ergebnis = tenant_provisioning.set_tenant_active(tenant, False, actor="Test")

        assert not ergebnis.changed and ergebnis.portal is not None and ergebnis.portal.entries == 8
        welt["source"].refresh_from_db()
        assert welt["source"].is_active is False
        assert SessionAuditLog.objects.filter(tenant=tenant, action="unpublish").exists()

    def test_zweite_ruecknahme_behaelt_zeitpunkt(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant_provisioning.set_tenant_active(tenant, False)
        welt["source"].refresh_from_db()
        erster = welt["source"].sync_config[insight_service.RETRACTION_KEY]["at"]

        insight_service.retract_source(tenant)

        welt["source"].refresh_from_db()
        assert welt["source"].sync_config[insight_service.RETRACTION_KEY]["at"] == erster
        assert welt["source"].sync_config[insight_service.RETRACTION_KEY]["listed_bodies"] == [str(welt["body"].pk)]

    def test_fremde_quelle_bleibt_unberuehrt(self, welt: dict[str, Any]) -> None:
        fremd = OParlSource.objects.create(name="Fremdes RIS", url="https://ris.example.org/oparl/system")
        kommune = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=fremd, name="Fremd")
        welt["tenant"].oparl_body = kommune
        welt["tenant"].save(update_fields=["oparl_body"])

        tenant_provisioning.set_tenant_active(welt["tenant"], False)

        fremd.refresh_from_db()
        kommune.refresh_from_db()
        assert fremd.is_active is True and kommune.is_listed is True


class TestAdmin:
    @pytest.fixture
    def admin_client(self) -> Client:
        user = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(user)
        return client

    def test_aktion_deaktivieren_nimmt_buergerportal_zurueck(self, welt: dict[str, Any], admin_client: Client) -> None:
        antwort = admin_client.post(
            CHANGELIST,
            {"action": "deactivate_tenants", "_selected_action": [str(welt["tenant"].pk)]},
            follow=True,
        )

        assert antwort.status_code == 200
        assert "1 Mandant(en) wurden deaktiviert; 8 Einträge" in antwort.content.decode()
        welt["tenant"].refresh_from_db()
        welt["body"].refresh_from_db()
        assert welt["tenant"].is_active is False and welt["body"].is_listed is False
        eintrag = SessionAuditLog.objects.get(tenant=welt["tenant"], action="update", model_name="SessionTenant")
        assert eintrag.changes["durch"] == "Django-Admin (betrieb@example.org)"
        assert eintrag.ip_address, "IP aus der Admin-Anfrage"

    def test_aktion_aktivieren_stellt_wieder_her(self, welt: dict[str, Any], admin_client: Client) -> None:
        tenant_provisioning.set_tenant_active(welt["tenant"], False)

        admin_client.post(CHANGELIST, {"action": "activate_tenants", "_selected_action": [str(welt["tenant"].pk)]})

        welt["tenant"].refresh_from_db()
        welt["body"].refresh_from_db()
        assert welt["tenant"].is_active is True and welt["body"].is_listed is True
        assert not any(obj.deleted for obj in _neu_geladen(welt["eintraege"]))

    def test_formular_speichern_mit_handelnder_person(self, welt: dict[str, Any]) -> None:
        admin = site._registry[SessionTenant]
        request = RequestFactory().post("/admin/")
        request.user = cast(Any, UserFactory)(email="formular@example.org", is_staff=True, is_superuser=True)
        tenant = welt["tenant"]
        tenant.is_active = False

        admin.save_model(request, tenant, form=None, change=True)

        welt["body"].refresh_from_db()
        assert welt["body"].is_listed is False
        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="unpublish")
        assert eintrag.changes["durch"] == "Django-Admin (formular@example.org)"

    def test_einstieg_im_buergerportal_folgt_dem_status(self, welt: dict[str, Any]) -> None:
        client = Client()
        assert client.get("/insight/k/bezirk-nord/").status_code == 200

        tenant_provisioning.set_tenant_active(welt["tenant"], False)
        assert client.get("/insight/k/bezirk-nord/").status_code == 404
        assert client.get("/insight/k/nord/").status_code == 404

        tenant_provisioning.set_tenant_active(welt["tenant"], True)
        assert client.get("/insight/k/nord/").status_code == 200


class TestLeitstelle:
    """Ein deaktivierter Mandant verschwindet aus der Leitstellen-Übersicht seiner Gruppe (Teil B, #317)."""

    def test_deaktivierter_mandant_nicht_in_uebersicht_und_suche(self, welt: dict[str, Any]) -> None:
        nord = welt["tenant"]
        sued = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
        gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke")
        for tenant in (nord, sued):
            SessionTenantGroupTenant.objects.create(group=gruppe, tenant=tenant)
            SessionPaper.objects.create(
                tenant=tenant, name=f"Marktplatz {tenant.slug}", is_public=True, status="review", reference=tenant.slug
            )
        leitstelle = cast(Any, UserFactory)(email="leitstelle@example.org")
        SessionTenantGroupMembership.objects.create(group=gruppe, user=leitstelle)
        client = Client()
        client.force_login(leitstelle)

        vorher = client.get("/session/leitstelle/bezirke/")
        assert {a.tenant.slug for a in vorher.context["accesses"]} == {"nord", "sued"}

        tenant_provisioning.set_tenant_active(nord, False, actor="Test")

        nachher = client.get("/session/leitstelle/bezirke/")
        assert [a.tenant.slug for a in nachher.context["accesses"]] == ["sued"]
        seite = nachher.content.decode()
        assert "Marktplatz nord" not in seite and "Marktplatz sued" in seite
        suche = client.get("/session/leitstelle/bezirke/suche/?q=Marktplatz").content.decode()
        assert "Marktplatz nord" not in suche and "Marktplatz sued" in suche

        tenant_provisioning.set_tenant_active(nord, True, actor="Test")
        wieder = client.get("/session/leitstelle/bezirke/")
        assert {a.tenant.slug for a in wieder.context["accesses"]} == {"nord", "sued"}
