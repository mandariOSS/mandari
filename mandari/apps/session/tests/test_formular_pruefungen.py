# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Formularprüfungen im Bereich Sitzungen (Teil von #708).

- „TOP hinzufügen“ prüft die Sitzung schon beim Aufruf des Formulars
- Ein TOP wird nur öffentlich, wenn auch seine Unterpunkte keine nichtöffentliche Vorlage beraten
- Ein abgewähltes Häkchen „Öffentlich“ gilt in Jahresplanung und Umlaufbeschluss
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionCircularResolution,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db


@dataclass
class Welt:
    tenant: SessionTenant
    gremium: SessionOrganization
    sitzung: SessionMeeting
    admin: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


def _client(tenant: SessionTenant, name: str, **rechte: bool) -> Client:
    rolle = SessionRole.objects.create(tenant=tenant, name=name, **rechte)
    user = cast(Any, UserFactory)(email=f"{name}@{tenant.slug}.example.org")
    SessionUser.objects.create(user=user, tenant=tenant).roles.add(rolle)
    client = Client()
    client.force_login(user)
    return client


def _sitzung(tenant: SessionTenant, gremium: SessionOrganization, **extra: Any) -> SessionMeeting:
    return SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss", organization=gremium, start=timezone.now() + timedelta(days=7), **extra
    )


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Nord", slug="nord")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    return Welt(tenant, gremium, _sitzung(tenant, gremium), _client(tenant, "admin", is_admin=True))


# =============================================================================
# „TOP hinzufügen“
# =============================================================================


def test_formular_top_hinzufuegen_nur_fuer_erreichbare_sitzungen(welt: Welt) -> None:
    fremd_tenant = SessionTenant.objects.create(name="Stadt Süd", slug="sued")
    fremd = _sitzung(fremd_tenant, SessionOrganization.objects.create(tenant=fremd_tenant, name="Rat"))
    noe = _sitzung(welt.tenant, welt.gremium, is_public=False)
    ohne_noe = _client(welt.tenant, "bearbeitung", can_view_meetings=True, can_edit_meetings=True)

    assert welt.admin.get(welt.url(f"/meetings/{welt.sitzung.pk}/agenda/add/")).status_code == 200
    assert welt.admin.get(welt.url(f"/meetings/{fremd.pk}/agenda/add/")).status_code == 404
    assert welt.admin.get(welt.url("/meetings/00000000-0000-4000-8000-000000000000/agenda/add/")).status_code == 404
    assert ohne_noe.get(welt.url(f"/meetings/{noe.pk}/agenda/add/")).status_code == 404


# =============================================================================
# Öffentlichkeit von Unterpunkten
# =============================================================================


def _noe_top_mit_unterpunkt(welt: Welt, vorlage: SessionPaper | None) -> tuple[SessionAgendaItem, SessionAgendaItem]:
    top = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="N1", order=1, name="Grundstück", is_public=False
    )
    unter = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="N1.1", order=2, name="Teilfläche", is_public=False, parent=top, paper=vorlage
    )
    return top, unter


def test_top_wird_nicht_oeffentlich_wenn_ein_unterpunkt_eine_noe_vorlage_beraet(welt: Welt) -> None:
    vorlage = SessionPaper.objects.create(tenant=welt.tenant, reference="V/1", name="Kaufpreis", is_public=False)
    top, unter = _noe_top_mit_unterpunkt(welt, vorlage)

    antwort = welt.admin.post(welt.url(f"/agenda/{top.pk}/edit/"), {"name": top.name, "is_public": "on"})

    assert antwort.status_code == 200
    assert "N1.1 berät eine nicht-öffentliche Vorlage" in antwort.content.decode()
    assert "Vorlage V/1" not in antwort.content.decode(), "Die Meldung nennt die Vorlage nicht"
    top.refresh_from_db()
    unter.refresh_from_db()
    assert (top.is_public, unter.is_public) == (False, False)


def test_top_mit_unterpunkt_ohne_noe_vorlage_wird_samt_unterpunkt_oeffentlich(welt: Welt) -> None:
    vorlage = SessionPaper.objects.create(tenant=welt.tenant, reference="V/2", name="Spielplatz", is_public=True)
    top, unter = _noe_top_mit_unterpunkt(welt, vorlage)

    antwort = welt.admin.post(welt.url(f"/agenda/{top.pk}/edit/"), {"name": top.name, "is_public": "on"})

    assert antwort.status_code == 302
    unter.refresh_from_db()
    assert unter.is_public


# =============================================================================
# Häkchen „Öffentlich“
# =============================================================================


def _planung(welt: Welt, **extra: str) -> None:
    beginn = timezone.localdate() + timedelta(days=30)
    welt.admin.post(
        welt.url("/meetings/plan/"),
        {
            "organization": str(welt.gremium.pk),
            "rhythm": "monthly_2",
            "weekday": "2",
            "time": "18:00",
            "date_from": beginn.isoformat(),
            "date_to": (beginn + timedelta(days=62)).isoformat(),
            "action": "create",
            **extra,
        },
    )


def test_jahresplanung_ohne_haekchen_legt_nichtoeffentliche_termine_an(welt: Welt) -> None:
    _planung(welt)
    entwuerfe = SessionMeeting.objects.filter(tenant=welt.tenant, meeting_state="draft").exclude(pk=welt.sitzung.pk)
    assert entwuerfe.exists()
    assert not entwuerfe.filter(is_public=True).exists()


def test_jahresplanung_mit_haekchen_legt_oeffentliche_termine_an(welt: Welt) -> None:
    _planung(welt, is_public="1")
    entwuerfe = SessionMeeting.objects.filter(tenant=welt.tenant, meeting_state="draft").exclude(pk=welt.sitzung.pk)
    assert entwuerfe.exists()
    assert not entwuerfe.filter(is_public=False).exists()


def _umlauf(welt: Welt, vorlage: SessionPaper, **extra: str) -> Any:
    return welt.admin.post(
        welt.url("/circulars/create/"),
        {
            "organization": str(welt.gremium.pk),
            "title": "Umlauf",
            "resolution_text": "Text",
            "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "paper": str(vorlage.pk),
            **extra,
        },
    )


def test_umlauf_ohne_haekchen_ist_nichtoeffentlich(welt: Welt) -> None:
    vorlage = SessionPaper.objects.create(
        tenant=welt.tenant, reference="V/3", name="Personal", is_public=False, status="approved"
    )
    _umlauf(welt, vorlage)
    umlauf = SessionCircularResolution.objects.get(tenant=welt.tenant)
    assert umlauf.is_public is False

    _umlauf(welt, vorlage, is_public="1")
    assert SessionCircularResolution.objects.filter(tenant=welt.tenant).count() == 1, "öffentlich mit NÖ-Vorlage"
