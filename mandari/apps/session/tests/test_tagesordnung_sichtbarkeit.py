# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnung bearbeiten nur im Rahmen der eigenen Sichtrechte.

Das Recht, Sitzungen zu bearbeiten, erlaubt keinen Blick auf nichtöffentliche TOPs: Ohne das
NÖ-Sichtrecht sind Bearbeitungsseite, Absetzen, Löschen und Verschieben eines NÖ-TOPs (oder
eines TOPs in einer NÖ-Sitzung) nicht erreichbar, und die Vorlagenauswahl nennt keine
nichtöffentlichen Vorlagen.
"""

from __future__ import annotations

import uuid
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

pytestmark = pytest.mark.django_db


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(name="Bezirk West", slug="west")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Sitzung HA", organization=gremium, start=timezone.now(), is_public=True
    )
    geheim_sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Klausur HA", organization=gremium, start=timezone.now(), is_public=False
    )
    oe_vorlage = SessionPaper.objects.create(tenant=tenant, name="Radweg Hauptstraße", is_public=True)
    noe_vorlage = SessionPaper.objects.create(tenant=tenant, name="Personalie Kämmerei", is_public=False)
    oe_top = SessionAgendaItem.objects.create(meeting=sitzung, name="Radweg", number="1", order=1, paper=oe_vorlage)
    noe_top = SessionAgendaItem.objects.create(
        meeting=sitzung, name="Grundstücksverkauf Parzelle 7", number="2", order=2, is_public=False
    )
    top_in_noe_sitzung = SessionAgendaItem.objects.create(
        meeting=geheim_sitzung, name="Strategie Haushalt", number="1", order=1
    )
    return {
        "tenant": tenant,
        "sitzung": sitzung,
        "oe_top": oe_top,
        "noe_top": noe_top,
        "top_in_noe_sitzung": top_in_noe_sitzung,
        "noe_vorlage": noe_vorlage,
    }


def _client(tenant: SessionTenant, **rechte: bool) -> Client:
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", **rechte)
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def bearbeiter(welt: dict[str, Any]) -> Client:
    """Darf Sitzungen bearbeiten, aber nichts Nichtöffentliches sehen."""
    return _client(welt["tenant"], can_view_meetings=True, can_edit_meetings=True, can_view_papers=True)


@pytest.fixture
def noe_bearbeiter(welt: dict[str, Any]) -> Client:
    return _client(
        welt["tenant"],
        can_view_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_view_papers=True,
        can_view_non_public_papers=True,
    )


@pytest.mark.parametrize("top", ["noe_top", "top_in_noe_sitzung"])
def test_bearbeitungsseite_eines_noe_tops_ohne_noe_recht_nicht_erreichbar(
    welt: dict[str, Any], bearbeiter: Client, top: str
) -> None:
    antwort = bearbeiter.get(f"/session/west/agenda/{welt[top].id}/edit/")
    assert antwort.status_code == 404
    assert welt[top].name not in antwort.content.decode()


@pytest.mark.parametrize("aktion", ["withdraw", "delete", "move"])
def test_noe_top_ohne_noe_recht_nicht_veraenderbar(welt: dict[str, Any], bearbeiter: Client, aktion: str) -> None:
    antwort = bearbeiter.post(f"/session/west/agenda/{welt['noe_top'].id}/{aktion}/", {"direction": "up"})
    assert antwort.status_code == 404
    top = SessionAgendaItem.objects.filter(pk=welt["noe_top"].pk).first()
    assert top is not None and top.is_withdrawn is False and top.order == 2


def test_oeffentlicher_top_bleibt_bearbeitbar_ohne_noe_vorlagen_in_der_auswahl(
    welt: dict[str, Any], bearbeiter: Client
) -> None:
    antwort = bearbeiter.get(f"/session/west/agenda/{welt['oe_top'].id}/edit/")
    assert antwort.status_code == 200
    seite = antwort.content.decode()
    assert "Radweg Hauptstraße" in seite
    assert "Personalie Kämmerei" not in seite
    # Unterpunkt-Auswahl nennt keine NÖ-TOPs
    assert "Grundstücksverkauf Parzelle 7" not in seite


def test_neuer_top_nennt_keine_noe_vorlagen(welt: dict[str, Any], bearbeiter: Client) -> None:
    antwort = bearbeiter.get(f"/session/west/meetings/{welt['sitzung'].id}/agenda/add/")
    assert antwort.status_code == 200
    assert "Personalie Kämmerei" not in antwort.content.decode()


def test_mit_noe_recht_bleibt_alles_erreichbar(welt: dict[str, Any], noe_bearbeiter: Client) -> None:
    for top in ("noe_top", "top_in_noe_sitzung"):
        assert noe_bearbeiter.get(f"/session/west/agenda/{welt[top].id}/edit/").status_code == 200
    seite = noe_bearbeiter.get(f"/session/west/agenda/{welt['oe_top'].id}/edit/").content.decode()
    assert "Personalie Kämmerei" in seite
    antwort = noe_bearbeiter.post(f"/session/west/agenda/{welt['noe_top'].id}/withdraw/", {"reason": "vertagt"})
    assert antwort.status_code == 302
    welt["noe_top"].refresh_from_db()
    assert welt["noe_top"].is_withdrawn is True


def test_tops_einer_noe_sitzung_nicht_anlegbar_ohne_noe_recht(welt: dict[str, Any], bearbeiter: Client) -> None:
    sitzung = SessionMeeting.objects.get(name="Klausur HA")
    antwort = bearbeiter.post(f"/session/west/meetings/{sitzung.id}/agenda/add/", {"name": "Neu", "is_public": "on"})
    assert antwort.status_code == 404
    assert not SessionAgendaItem.objects.filter(meeting=sitzung, name="Neu").exists()
