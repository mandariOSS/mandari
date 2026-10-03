# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminieren nur freigegebener Vorlagen; ein übernommener Antrag wird Vorlage im Entwurf (Teil von Issue #721).

- Beratungsstation terminieren und weiterleiten: nur mit freigegebener Vorlage (Freigegeben, Terminiert,
  Abgeschlossen); Entwurf, Prüfung und Zurückgezogen erhalten eine Meldung, es entsteht kein TOP
- Tagesordnung: Als Vorlage eines TOP sind nur freigegebene Vorlagen wählbar; eine schon verknüpfte bleibt
- Vorlagenseite: „TOP anlegen“ erst nach der Freigabe, vorher ein Hinweis
- Umwandlung eines Antrags: Vorlage im Entwurf statt „Freigegeben“, ohne Freigabevermerk; die Fraktion erfährt
  die Drucksachennummer erst mit der Veröffentlichung
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import application_feedback
from apps.session.services.application_service import convert_to_paper

pytestmark = pytest.mark.django_db

NICHT_FREIGEGEBEN = ("draft", "review", "withdrawn")


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    bau: SessionOrganization
    sitzung: SessionMeeting
    admin: SessionUser
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Terminhausen", slug="terminhausen")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now() + timedelta(days=14)
    )
    role = SessionRole.objects.create(tenant=tenant, name="Administration", is_admin=True)
    user = cast(User, UserFactory(email="admin@terminhausen.example"))  # type: ignore[no-untyped-call]
    admin = SessionUser.objects.create(user=user, tenant=tenant)
    admin.roles.add(role)
    client = Client()
    client.force_login(user)
    return Welt(tenant, rat, bau, sitzung, admin, client)


def _vorlage(w: Welt, status: str, name: str = "Radweg Hauptstraße") -> SessionPaper:
    return SessionPaper.objects.create(tenant=w.tenant, name=name, status=status)


def _meldungen(antwort: Any) -> str:
    return " ".join(str(m) for m in get_messages(antwort.wsgi_request))


# =============================================================================
# Beratungsfolge: terminieren und weiterleiten
# =============================================================================


@pytest.mark.parametrize("status", NICHT_FREIGEGEBEN)
def test_station_terminieren_nur_mit_freigegebener_vorlage(welt: Welt, status: str) -> None:
    vorlage = _vorlage(welt, status)
    station = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, meeting=welt.sitzung, order=1)

    antwort = welt.client.post(welt.url(f"/consultations/{station.pk}/schedule/"))
    assert antwort.status_code == 302
    assert "ist nicht freigegeben" in _meldungen(antwort)
    station.refresh_from_db()
    vorlage.refresh_from_db()
    assert station.agenda_item_id is None and vorlage.status == status
    assert not SessionAgendaItem.objects.filter(meeting=welt.sitzung).exists()


def test_freigegebene_vorlage_wird_terminiert(welt: Welt) -> None:
    vorlage = _vorlage(welt, "approved")
    station = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, meeting=welt.sitzung, order=1)
    welt.client.post(welt.url(f"/consultations/{station.pk}/schedule/"))
    station.refresh_from_db()
    vorlage.refresh_from_db()
    assert station.agenda_item is not None and station.agenda_item.paper_id == vorlage.pk
    assert vorlage.status == "scheduled"


def test_weiterleiten_nur_mit_freigegebener_vorlage(welt: Welt) -> None:
    vorlage = _vorlage(welt, "approved")
    erste = SessionConsultation.objects.create(
        paper=vorlage, organization=welt.bau, order=1, role="preliminary", result="approved"
    )
    zweite = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, meeting=welt.sitzung, order=2)
    # Zwischenzeitlich zurückgezogen: keine weitere Beratung
    SessionPaper.objects.filter(pk=vorlage.pk).update(status="withdrawn")

    antwort = welt.client.post(welt.url(f"/consultations/{erste.pk}/forward/"))
    assert "ist nicht freigegeben" in _meldungen(antwort)
    zweite.refresh_from_db()
    assert zweite.agenda_item_id is None


def test_vorlagenseite_bietet_terminieren_erst_nach_der_freigabe(welt: Welt) -> None:
    vorlage = _vorlage(welt, "draft")
    station = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, meeting=welt.sitzung, order=1)
    schalter = f"/consultations/{station.pk}/schedule/"

    seite = welt.client.get(welt.url(f"/papers/{vorlage.pk}/")).content.decode()
    assert schalter not in seite and 'data-testid="terminieren-nach-freigabe"' in seite

    SessionPaper.objects.filter(pk=vorlage.pk).update(status="approved")
    seite = welt.client.get(welt.url(f"/papers/{vorlage.pk}/")).content.decode()
    assert schalter in seite and 'data-testid="terminieren-nach-freigabe"' not in seite


# =============================================================================
# Tagesordnung: Vorlage eines TOP
# =============================================================================


def test_tagesordnung_bietet_nur_freigegebene_vorlagen(welt: Welt) -> None:
    entwurf = _vorlage(welt, "draft", "Entwurf Spielplatz")
    frei = _vorlage(welt, "approved", "Freigegeben Radweg")
    hinzufuegen = welt.url(f"/meetings/{welt.sitzung.pk}/agenda/add/")

    formular = welt.client.get(hinzufuegen, headers={"HX-Request": "true"}).content.decode()
    assert str(frei.pk) in formular and str(entwurf.pk) not in formular

    welt.client.post(hinzufuegen, {"name": "Spielplatz", "is_public": "on", "paper": str(entwurf.pk)})
    assert not SessionAgendaItem.objects.filter(meeting=welt.sitzung, paper=entwurf).exists()
    welt.client.post(hinzufuegen, {"name": "Radweg", "is_public": "on", "paper": str(frei.pk)})
    assert SessionAgendaItem.objects.filter(meeting=welt.sitzung, paper=frei).exists()


def test_bestehende_verknuepfung_bleibt_neue_mit_entwurf_nicht(welt: Welt) -> None:
    alt = _vorlage(welt, "draft", "Altbestand")
    neu = _vorlage(welt, "review", "In Prüfung")
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Altbestand", paper=alt)
    bearbeiten = welt.url(f"/agenda/{top.pk}/edit/")

    # Speichern ohne Wechsel der Vorlage: Die Verknüpfung aus der Zeit vor der Prüfung bleibt
    welt.client.post(bearbeiten, {"name": "Altbestand (neu)", "is_public": "on", "paper": str(alt.pk)})
    top.refresh_from_db()
    assert (top.name, top.paper_id) == ("Altbestand (neu)", alt.pk)

    # Wechsel auf eine nicht freigegebene Vorlage: abgelehnt
    antwort = welt.client.post(bearbeiten, {"name": "Wechsel", "is_public": "on", "paper": str(neu.pk)})
    assert antwort.status_code == 200
    top.refresh_from_db()
    assert (top.name, top.paper_id) == ("Altbestand (neu)", alt.pk)


# =============================================================================
# Umwandlung eines Antrags
# =============================================================================


def _antrag(w: Welt) -> SessionApplication:
    return SessionApplication.objects.create(
        tenant=w.tenant,
        title="Bänke am Markt",
        justification="Weil.",
        resolution_proposal="Aufstellen.",
        submitter_name="Fraktion",
        submitter_email="fraktion@example.org",
        status="received",
    )


def test_umgewandelter_antrag_wird_vorlage_im_entwurf(welt: Welt) -> None:
    antrag = _antrag(welt)
    vorlage, neu = convert_to_paper(antrag, session_user=welt.admin)
    assert neu and vorlage.status == "draft"
    assert vorlage.approved_by_id is None and vorlage.approved_at is None
    antrag.refresh_from_db()
    assert antrag.status == "converted"

    # Die Fraktion sieht „umgewandelt“, die Nummer aber erst mit der Veröffentlichung
    rueckmeldung = application_feedback.build(antrag)
    assert rueckmeldung.converted and not rueckmeldung.paper_public and rueckmeldung.paper_reference == ""

    # Terminieren erst nach der Freigabe
    station = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, meeting=welt.sitzung, order=1)
    welt.client.post(welt.url(f"/consultations/{station.pk}/schedule/"))
    station.refresh_from_db()
    assert station.agenda_item_id is None


def test_umwandlung_ueber_die_oberflaeche_meldet_den_entwurf(welt: Welt) -> None:
    antrag = _antrag(welt)
    antwort = welt.client.post(welt.url(f"/applications/{antrag.pk}/convert/"), {"name": "Bänke am Markt"})
    assert "Die Vorlage ist im Entwurf und durchläuft den Freigabelauf." in _meldungen(antwort)
    vorlage = SessionPaper.objects.get(source_application=antrag)
    assert vorlage.status == "draft"
    formular = welt.client.get(welt.url(f"/applications/{_antrag(welt).pk}/convert/")).content.decode()
    assert "neue Vorlage im Entwurf" in formular
