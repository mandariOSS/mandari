# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentlichkeit der Gremien (Issue #757, Teil L3).

- Hauptausschuss stets nichtöffentlich (z. B. § 78 Abs. 2 NKomVG): Formular, Jahresplanung und jeder andere
  Speicherweg lassen keine öffentliche Sitzung zu; Vorsitz bei der bzw. dem HVB (§ 74 NKomVG)
- Standard-Öffentlichkeit je Gremientyp, Geschäftsordnung und Einstellung am Gremium
- Termine nichtöffentlicher Sitzungen wahlweise öffentlich: nur Termin und Gremien in der OParl-Schnittstelle,
  nie Tagesordnung, Ort oder Anlagen; Rücknahmen beim Wechsel zwischen den Stufen
- Datenmigration und Rückfall per Image
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionBody,
    SessionFile,
    SessionMeeting,
    SessionOParlTombstone,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionStateProfile,
    SessionTenant,
)
from apps.session.services import attendance_service, body_service, meeting_format_service, state_law_service
from apps.session.services.state_law_service import LocalRules
from apps.session.tests._niederschrift import client, nutzer

pytestmark = pytest.mark.django_db


def _am(tag: date) -> datetime:
    return timezone.make_aware(datetime.combine(tag, time(17, 0)))


def _mandant(code: str = "NI", slug: str = "lk-oe") -> tuple[SessionTenant, dict[str, SessionOrganization]]:
    meeting_format_service.sync_profiles()
    tenant = SessionTenant.objects.create(
        name="Landkreis Muster", slug=slug, state_profile_id=code, body_type="kreis", oparl_public_since=timezone.now()
    )
    gremien = {
        "kt": SessionOrganization.objects.create(tenant=tenant, name="Kreistag", organization_type="council"),
        "ka": SessionOrganization.objects.create(tenant=tenant, name="Kreisausschuss", committee_kind="main"),
        "fa": SessionOrganization.objects.create(tenant=tenant, name="Finanzausschuss", committee_kind="finance"),
        "fr": SessionOrganization.objects.create(tenant=tenant, name="Fraktion A", organization_type="faction"),
    }
    return tenant, gremien


def _sitzung(tenant: SessionTenant, gremium: SessionOrganization, **felder: Any) -> SessionMeeting:
    felder.setdefault("start", _am(date(2026, 11, 20)))
    return SessionMeeting.objects.create(tenant=tenant, organization=gremium, name=f"Sitzung {gremium.name}", **felder)


def _oparl(tenant: SessionTenant, pfad: str) -> Any:
    """Öffentliche OParl-Schnittstelle ohne Anmeldung."""
    antwort = Client().get(f"/session/{tenant.slug}/api/oparl/{pfad}")
    return antwort.status_code, json.loads(antwort.content or b"{}")


# =============================================================================
# Hauptausschuss stets nichtöffentlich, Standard je Gremientyp
# =============================================================================


def test_oeffentlichkeit_nach_gremientyp_geschaeftsordnung_und_einstellung() -> None:
    tenant, g = _mandant()
    ka = state_law_service.publicity(g["ka"])
    assert (ka.public, ka.locked) == (False, True) and "§ 78 Abs. 2 NKomVG" in ka.reason
    assert state_law_service.publicity(g["kt"]).public
    assert state_law_service.publicity(g["fa"]).public
    assert not state_law_service.publicity(g["fr"]).public
    # Geschäftsordnung: Ausschüsse nichtöffentlich
    body = body_service.default_body(tenant)
    body.local_rules = {"committees_public": "non_public"}
    body.save()
    g["fa"].refresh_from_db()
    assert not state_law_service.publicity(g["fa"]).public
    # Einstellung am Gremium geht vor; den Hauptausschuss öffnet auch sie nicht
    g["fa"].publicity = SessionOrganization.PUBLICITY_PUBLIC
    g["ka"].publicity = SessionOrganization.PUBLICITY_PUBLIC
    assert state_law_service.publicity(g["fa"]).public
    assert state_law_service.publicity(g["ka"]).locked


def test_ohne_niedersachsen_ist_der_hauptausschuss_nicht_gesperrt() -> None:
    tenant, g = _mandant("NW", slug="nrw-oe")
    assert not state_law_service.publicity(g["ka"]).locked


def test_hauptausschuss_laesst_sich_nicht_oeffentlich_schalten() -> None:
    tenant, g = _mandant()
    sitzungsdienst = client(
        nutzer(tenant, "sd", "view_meetings", "create_meetings", "edit_meetings", "view_non_public_meetings")
    )
    url = f"/session/{tenant.slug}/meetings/create/"
    daten = {"name": "KA", "organization": str(g["ka"].pk), "start": "2026-11-20T17:00", "format": "presence"}

    antwort = sitzungsdienst.post(url, {**daten, "is_public": "on"})
    assert antwort.status_code == 200
    assert "stets nichtöffentlich (§ 78 Abs. 2 NKomVG)" in antwort.content.decode()
    assert not SessionMeeting.objects.filter(name="KA").exists()
    assert sitzungsdienst.post(url, daten).status_code == 302
    assert not SessionMeeting.objects.get(name="KA").is_public

    # Formular: Vorgabe und Sperre an den Optionen, aus dem Gremium heraus vorbelegt
    seite = sitzungsdienst.get(url).content.decode()
    assert re.search(rf'value="{g["ka"].pk}"\s+data-public="0" data-date-public="0" data-public-locked="1"', seite)
    assert re.search(rf'value="{g["kt"].pk}"\s+data-public="1" data-date-public="0">', seite)
    assert 'data-public-default="#id_is_public"' in seite
    seite = sitzungsdienst.get(f"{url}?organization={g['fr'].pk}").content.decode()
    assert f'value="{g["fr"].pk}" selected' in seite
    # Bearbeiten: Kästchen gesperrt
    sitzung = SessionMeeting.objects.get(name="KA")
    seite = sitzungsdienst.get(f"/session/{tenant.slug}/meetings/{sitzung.pk}/edit/").content.decode()
    assert 'id="id_is_public"' in seite and "disabled" in seite.split('id="id_is_public"')[1][:120]


def test_jeder_speicherweg_haelt_den_hauptausschuss_nichtoeffentlich() -> None:
    tenant, g = _mandant()
    sitzung = _sitzung(tenant, g["ka"], is_public=True)
    assert not sitzung.is_public
    SessionMeeting.objects.filter(pk=sitzung.pk).update(is_public=True)  # Altbestand
    sitzung.refresh_from_db()
    sitzung.is_public = True
    sitzung.save(update_fields=["is_public", "updated_at"])
    sitzung.refresh_from_db()
    assert not sitzung.is_public
    # Andere Gremien bleiben frei
    assert _sitzung(tenant, g["fa"], is_public=True).is_public


def test_vorsitz_im_hauptausschuss_bei_der_hvb() -> None:
    tenant, g = _mandant()
    pflege = client(nutzer(tenant, "stammdaten", "view_meetings", "manage_organizations"))
    landrat = SessionPerson.objects.create(tenant=tenant, given_name="L", family_name="Landrat")
    url = f"/session/{tenant.slug}/organizations/{g['ka'].pk}/memberships/add/"
    seite = pflege.get(f"/session/{tenant.slug}/organizations/{g['ka'].pk}/").content.decode()
    assert 'data-testid="hauptausschuss-hinweis"' in seite

    antwort = pflege.post(url, {"person": str(landrat.pk), "role": "chair", "has_voting_rights": "on"})
    assert "Den Vorsitz im Hauptausschuss führt" in " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert not SessionOrganizationMembership.objects.filter(organization=g["ka"]).exists()

    pflege.post(url, {"person": str(landrat.pk), "role": "hvb", "has_voting_rights": "on"})
    besetzung = SessionOrganizationMembership.objects.get(organization=g["ka"], person=landrat)
    assert attendance_service.attendance_defaults(besetzung)["role"] == "chair"
    seite = pflege.get(f"/session/{tenant.slug}/organizations/{g['ka'].pk}/").content.decode()
    assert 'data-testid="hauptausschuss-hinweis"' not in seite
    # In der Vertretung ist die bzw. der HVB Mitglied
    rat = SessionOrganizationMembership.objects.create(organization=g["kt"], person=landrat, role="hvb")
    assert attendance_service.attendance_defaults(rat)["role"] == "member"


# =============================================================================
# Termine nichtöffentlicher Sitzungen
# =============================================================================


def _termin_welt() -> tuple[SessionTenant, dict[str, SessionOrganization], SessionMeeting]:
    tenant, g = _mandant()
    g["ka"].publish_dates = True
    g["ka"].save()
    sitzung = _sitzung(tenant, g["ka"], location="Kreishaus", room="Sitzungssaal")
    SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="GEHEIMTOP Grundstück")
    return tenant, g, sitzung


def test_termin_eines_nichtoeffentlichen_gremiums_ohne_inhalte() -> None:
    tenant, _g, sitzung = _termin_welt()
    assert not sitzung.is_public and sitzung.date_public

    status, liste = _oparl(tenant, "meetings/")
    assert status == 200
    eintrag = next(m for m in liste["data"] if str(sitzung.pk) in m["id"])
    assert eintrag["mandari:nonPublic"] is True and eintrag["start"].startswith("2026-11-20")
    for feld in ("location", "agendaItem", "auxiliaryFile", "resultsProtocol", "mandari:locationName"):
        assert feld not in eintrag, feld
    assert "GEHEIMTOP" not in json.dumps(liste)
    status, top = _oparl(tenant, "agendaitems/")
    assert "GEHEIMTOP" not in json.dumps(top)
    status, ort = _oparl(tenant, f"location/{sitzung.pk}/")
    assert "Kreishaus" not in json.dumps(ort)

    # Ohne „Termin veröffentlichen“ gar nicht sichtbar
    sitzung.date_public = False
    sitzung.save()
    _status, liste = _oparl(tenant, "meetings/")
    assert not any(str(sitzung.pk) in m["id"] for m in liste["data"])


def test_wechsel_zwischen_oeffentlich_termin_und_nichtoeffentlich() -> None:
    tenant, g = _mandant()
    sitzung = _sitzung(tenant, g["fa"], is_public=True, location="Kreishaus")
    top = SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Haushalt")
    anlage = SessionFile.objects.create(tenant=tenant, meeting=sitzung, name="Plan.pdf", is_public=True)

    # öffentlich -> nur Termin: TOP und Anlage zurückgenommen, die Sitzung bleibt
    sitzung = SessionMeeting.objects.get(pk=sitzung.pk)
    sitzung.is_public, sitzung.date_public = False, True
    sitzung.save()
    grabsteine = set(SessionOParlTombstone.objects.filter(tenant=tenant).values_list("oparl_type", "object_id"))
    assert ("agendaitem", top.pk) in grabsteine and ("file", anlage.pk) in grabsteine
    assert ("meeting", sitzung.pk) not in grabsteine

    # nur Termin -> nichtöffentlich: jetzt auch die Sitzung
    sitzung = SessionMeeting.objects.get(pk=sitzung.pk)
    sitzung.date_public = False
    sitzung.save()
    assert SessionOParlTombstone.objects.filter(tenant=tenant, oparl_type="meeting", object_id=sitzung.pk).exists()

    # wieder öffentlich: alle Grabsteine entfernt
    sitzung = SessionMeeting.objects.get(pk=sitzung.pk)
    sitzung.is_public = True
    sitzung.save()
    assert not SessionOParlTombstone.objects.filter(tenant=tenant).exists()


def test_oeffentliche_sitzung_veroeffentlicht_keinen_extra_termin() -> None:
    tenant, g = _mandant()
    g["fa"].publish_dates = True
    g["fa"].save()
    oeffentlich = _sitzung(tenant, g["fa"], is_public=True)
    assert not oeffentlich.date_public
    # Formular: bei öffentlicher Sitzung entfällt „Termin veröffentlichen“
    sitzungsdienst = client(nutzer(tenant, "sd2", "view_meetings", "create_meetings", "edit_meetings"))
    sitzungsdienst.post(
        f"/session/{tenant.slug}/meetings/create/",
        {
            "name": "FA",
            "organization": str(g["fa"].pk),
            "start": "2026-11-21T17:00",
            "is_public": "on",
            "date_public": "on",
            "format": "presence",
        },
    )
    assert not SessionMeeting.objects.get(name="FA").date_public
    # Im Formular gilt das Kästchen – die Vorgabe des Gremiums zeigt die Option (data-date-public)
    sitzungsdienst.post(
        f"/session/{tenant.slug}/meetings/create/",
        {"name": "FA2", "organization": str(g["fa"].pk), "start": "2026-11-22T17:00", "format": "presence"},
    )
    assert not SessionMeeting.objects.get(name="FA2").date_public
    seite = sitzungsdienst.get(f"/session/{tenant.slug}/meetings/create/").content.decode()
    assert re.search(rf'value="{g["fa"].pk}"\s+data-public="1" data-date-public="1"', seite)
    # Jahresplanung und andere Wege ohne Formular: Vorgabe des Gremiums
    assert _sitzung(tenant, g["fa"], is_public=False).date_public


def test_gremienformular_kennt_oeffentlichkeit_und_termine() -> None:
    tenant, g = _mandant()
    pflege = client(nutzer(tenant, "stammdaten2", "view_meetings", "manage_organizations"))
    seite = pflege.get(f"/session/{tenant.slug}/organizations/{g['fa'].pk}/edit/").content.decode()
    assert 'name="publicity"' in seite and 'name="publish_dates"' in seite


# =============================================================================
# Datenmigration und Rückfall per Image
# =============================================================================

VORHER = ("session", "0064_gremientypen_funktionen")
NACHHER = ("session", "0065_oeffentlichkeit_gremien")


@pytest.mark.django_db(transaction=True)
def test_migration_und_altes_image() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        meeting_format_service.sync_profiles(model=alt.get_model("session", "SessionStateProfile"))
        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        fassung = neu.get_model("session", "SessionStateProfileVersion").objects.get(
            profile_id="NI", valid_from=date(2026, 5, 7)
        )
        assert fassung.law["main_committee_chair"]["value"] == "hvb"

        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Meeting = alt.get_model("session", "SessionMeeting")
        tenant = Tenant.objects.create(name="Alt", slug="alt-c")
        rat = Organization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzung = Meeting.objects.create(tenant=tenant, organization=rat, name="Rat", start="2030-01-01T17:00:00Z")
        org = neu.get_model("session", "SessionOrganization").objects.get(pk=rat.pk)
        assert (org.publicity, org.publish_dates) == ("", False)
        assert neu.get_model("session", "SessionMeeting").objects.get(pk=sitzung.pk).date_public is False
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_body_ohne_ortsrecht_bleibt_lesbar() -> None:
    tenant, g = _mandant()
    SessionBody.objects.filter(tenant=tenant).update(local_rules=None)
    g["fa"].refresh_from_db()
    assert LocalRules.of(g["fa"].body) == LocalRules()
    assert state_law_service.publicity(g["fa"]).public
    assert SessionStateProfile.objects.filter(code="NI").exists()
