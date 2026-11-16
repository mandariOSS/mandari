# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergebnis von Hand an einer Beratungsstation: nur an einer freigegebenen Vorlage (Teil von Issue #1172).

- Freigegeben, Terminiert, Abgeschlossen: Das Ergebnis einer Station ohne TOP lässt sich wie bisher eintragen;
  „Angenommen“ an der entscheidenden Station kennzeichnet die beschlossene Fassung
- Entwurf, Prüfung: Das Ergebnis wird mit einer Meldung abgelehnt, es entsteht keine Fassung; geprüft wird der
  gespeicherte Stand der Vorlage. An einer zurückgezogenen Vorlage bleibt das Ergebnis möglich, ohne Kennzeichen
- Wird die Station zwischen Laden und Speichern terminiert oder entfernt, wird nichts gespeichert
- Stationen planen (Rolle, Zielsitzung, „entscheidend“) bleibt im Entwurf möglich, ein Ergebnis aus dem Bestand
  lässt sich dort auf „Ausstehend“ zurücknehmen
- Die Fassungen kennzeichnen ein Ergebnis ohne TOP auch auf anderen Wegen nur an einer freigegebenen Vorlage; die
  spätere Entscheidung über einen TOP erhält die Kennzeichnung
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
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPaperVersion,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.views import consultations as consultation_views

pytestmark = pytest.mark.django_db

FREIGEGEBEN = ("approved", "scheduled", "completed")
NICHT_FREIGEGEBEN = ("draft", "review", "withdrawn")
#: Stände vor der Freigabe: Nur dort ist das Ergebnis von Hand gesperrt
VOR_DER_FREIGABE = ("draft", "review")
Version = SessionPaperVersion


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    sitzung: SessionMeeting
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def vorlage(self, status: str) -> SessionPaper:
        return SessionPaper.objects.create(tenant=self.tenant, name="Radweg Hauptstraße", status=status)

    def station(self, vorlage: SessionPaper, **felder: Any) -> SessionConsultation:
        felder.setdefault("role", "decision")
        felder.setdefault("authoritative", True)
        return SessionConsultation.objects.create(paper=vorlage, organization=self.rat, order=1, **felder)

    def speichern(self, station: SessionConsultation, **daten: str) -> Any:
        """Formular der Station absenden, wie es die Seite der Vorlage sendet (Ergebnis immer dabei)."""
        felder = {"role": station.role, "meeting": "", "authoritative": "on", **daten}
        return self.client.post(self.url(f"/consultations/{station.pk}/update/"), felder)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Ergebnishausen", slug="ergebnishausen")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now() + timedelta(days=14)
    )
    # Sachbearbeitung: Vorlagen sehen und bearbeiten – ohne Sitzungs- und Freigaberechte
    rechte = {f.name: False for f in SessionRole._meta.get_fields() if f.name.startswith("can_") and f.concrete}
    rechte.update(can_view_papers=True, can_edit_papers=True, can_view_non_public_papers=True, can_view_meetings=True)
    role = SessionRole.objects.create(tenant=tenant, name="Sachbearbeitung", **rechte)
    user = cast(User, UserFactory(email="sachbearbeitung@ergebnishausen.example"))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return Welt(tenant, rat, sitzung, client)


def _meldungen(antwort: Any) -> str:
    return " ".join(str(m) for m in get_messages(antwort.wsgi_request))


# =============================================================================
# Freigegebene Vorlage: Ergebnis von Hand wie bisher
# =============================================================================


@pytest.mark.parametrize("status", FREIGEGEBEN)
def test_ergebnis_von_hand_an_freigegebener_vorlage(welt: Welt, status: str) -> None:
    vorlage = welt.vorlage(status)
    station = welt.station(vorlage)

    antwort = welt.speichern(station, result="approved")
    assert antwort.status_code == 302
    assert "wurde aktualisiert" in _meldungen(antwort)
    station.refresh_from_db()
    assert station.result == "approved"
    beschlossen = Version.objects.get(paper=vorlage, is_resolved=True)
    assert beschlossen.trigger == Version.TRIGGER_CONSULTATION
    assert beschlossen.agenda_item is None and beschlossen.note.startswith("Angenommen")


def test_ergebnis_der_vorberatung_kennzeichnet_nicht(welt: Welt) -> None:
    vorlage = welt.vorlage("approved")
    station = welt.station(vorlage, role="preliminary", authoritative=False)

    welt.speichern(station, result="approved", authoritative="")
    station.refresh_from_db()
    assert station.result == "approved"
    assert Version.objects.filter(paper=vorlage, trigger=Version.TRIGGER_CONSULTATION).count() == 1
    assert not Version.objects.filter(paper=vorlage, is_resolved=True).exists()


# =============================================================================
# Nicht freigegebene Vorlage: kein Ergebnis von Hand
# =============================================================================


@pytest.mark.parametrize("ergebnis", ["withdrawn", "approved"])
def test_ergebnis_von_hand_an_zurueckgezogener_vorlage(welt: Welt, ergebnis: str) -> None:
    """Nach dem Rückzug trägt die Verwaltung an den ausstehenden Stationen weiter ein Ergebnis ein, ohne Kennzeichen."""
    vorlage = welt.vorlage("withdrawn")
    station = welt.station(vorlage)

    antwort = welt.speichern(station, result=ergebnis)
    assert antwort.status_code == 302
    assert "wurde aktualisiert" in _meldungen(antwort)
    station.refresh_from_db()
    assert station.result == ergebnis
    assert Version.objects.filter(paper=vorlage, trigger=Version.TRIGGER_CONSULTATION).count() == 1
    assert not Version.objects.filter(paper=vorlage, is_resolved=True).exists()


@pytest.mark.parametrize("status", VOR_DER_FREIGABE)
@pytest.mark.parametrize("ergebnis", ["approved", "rejected"])
def test_kein_ergebnis_von_hand_vor_der_freigabe(welt: Welt, status: str, ergebnis: str) -> None:
    vorlage = welt.vorlage(status)
    station = welt.station(vorlage)

    antwort = welt.speichern(station, result=ergebnis)
    assert antwort.status_code == 302
    assert "ist noch nicht freigegeben" in _meldungen(antwort)
    station.refresh_from_db()
    assert station.result == "pending"
    assert not Version.objects.filter(paper=vorlage).exists()


def test_geprueft_wird_der_gespeicherte_stand_der_vorlage(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    vorlage = welt.vorlage("approved")
    station = welt.station(vorlage)
    laden = consultation_views.ConsultationBaseView.get_consultation

    def laden_und_zurueckweisen(view: Any, consultation_id: Any) -> SessionConsultation:
        # Nach dem Laden der Station geht die Vorlage zurück in den Entwurf; die Anfrage hält noch „Freigegeben“
        geladen = cast(SessionConsultation, laden(view, consultation_id))  # type: ignore[no-untyped-call]
        SessionPaper.objects.filter(pk=vorlage.pk).update(status="draft")
        assert geladen.paper.status == "approved"
        return geladen

    monkeypatch.setattr(consultation_views.ConsultationBaseView, "get_consultation", laden_und_zurueckweisen)
    antwort = welt.speichern(station, result="approved")
    assert "ist noch nicht freigegeben" in _meldungen(antwort)
    station.refresh_from_db()
    assert station.result == "pending"
    assert not Version.objects.filter(paper=vorlage, is_resolved=True).exists()


def test_inzwischen_terminierte_station_wird_nicht_ueberschrieben(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zielsitzung und Ergebnis galten dem geladenen Stand: Mit einem inzwischen angelegten TOP bleibt alles stehen."""
    vorlage = welt.vorlage("scheduled")
    station = welt.station(vorlage, meeting=welt.sitzung)
    andere = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Sondersitzung", organization=welt.rat, start=timezone.now() + timedelta(days=21)
    )
    laden = consultation_views.ConsultationBaseView.get_consultation

    def laden_und_terminieren(view: Any, consultation_id: Any) -> SessionConsultation:
        geladen = cast(SessionConsultation, laden(view, consultation_id))  # type: ignore[no-untyped-call]
        top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="3", name="Radweg", paper=vorlage)
        SessionConsultation.objects.filter(pk=station.pk).update(agenda_item=top)
        assert geladen.agenda_item_id is None
        return geladen

    monkeypatch.setattr(consultation_views.ConsultationBaseView, "get_consultation", laden_und_terminieren)
    antwort = welt.speichern(station, meeting=str(andere.pk), result="approved")
    assert antwort.status_code == 302
    assert "hat sich inzwischen geändert" in _meldungen(antwort)
    station.refresh_from_db()
    assert station.agenda_item_id is not None
    assert (station.meeting_id, station.result) == (welt.sitzung.pk, "pending")
    assert not Version.objects.filter(paper=vorlage, trigger=Version.TRIGGER_CONSULTATION).exists()


def test_inzwischen_entfernte_station_entsteht_nicht_neu(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    vorlage = welt.vorlage("approved")
    station = welt.station(vorlage)
    laden = consultation_views.ConsultationBaseView.get_consultation

    def laden_und_entfernen(view: Any, consultation_id: Any) -> SessionConsultation:
        geladen = cast(SessionConsultation, laden(view, consultation_id))  # type: ignore[no-untyped-call]
        SessionConsultation.objects.filter(pk=station.pk).delete()
        return geladen

    monkeypatch.setattr(consultation_views.ConsultationBaseView, "get_consultation", laden_und_entfernen)
    antwort = welt.speichern(station, result="approved")
    assert "hat sich inzwischen geändert" in _meldungen(antwort)
    assert not SessionConsultation.objects.filter(paper=vorlage).exists()
    assert not Version.objects.filter(paper=vorlage).exists()


def test_station_im_entwurf_bleibt_planbar(welt: Welt) -> None:
    vorlage = welt.vorlage("draft")
    station = welt.station(vorlage, role="preliminary", authoritative=False)

    # Das Formular sendet das unveränderte Ergebnis mit – Rolle, Zielsitzung und „entscheidend“ werden gespeichert
    antwort = welt.speichern(station, role="decision", meeting=str(welt.sitzung.pk), result="pending")
    assert "wurde aktualisiert" in _meldungen(antwort)
    station.refresh_from_db()
    assert (station.role, station.meeting_id, station.authoritative) == ("decision", welt.sitzung.pk, True)
    assert station.result == "pending"


def test_ergebnis_aus_dem_bestand_laesst_sich_im_entwurf_zuruecknehmen(welt: Welt) -> None:
    vorlage = welt.vorlage("draft")
    station = welt.station(vorlage, result="approved")  # Bestand aus der Zeit vor der Prüfung

    # Unverändert mitgesendet: kein Hindernis für das Bearbeiten der Station
    antwort = welt.speichern(station, role="hearing", result="approved")
    assert "wurde aktualisiert" in _meldungen(antwort)
    station.refresh_from_db()
    assert (station.role, station.result) == ("hearing", "approved")

    # Ein anderes Ergebnis wird abgelehnt, zurücknehmen geht
    assert "ist noch nicht freigegeben" in _meldungen(welt.speichern(station, result="rejected"))
    station.refresh_from_db()
    assert station.result == "approved"
    assert "wurde aktualisiert" in _meldungen(welt.speichern(station, result="pending"))
    station.refresh_from_db()
    assert station.result == "pending"
    assert not Version.objects.filter(paper=vorlage).exists()


# =============================================================================
# Seite der Vorlage: Auswahl vor der Freigabe gesperrt
# =============================================================================


def test_formular_sperrt_ergebnisse_vor_der_freigabe(welt: Welt) -> None:
    vorlage = welt.vorlage("draft")
    welt.station(vorlage)
    gesperrt = '<option value="approved" disabled>'

    seite = welt.client.get(welt.url(f"/papers/{vorlage.pk}/")).content.decode()
    assert gesperrt in seite and '<option value="pending" selected>' in seite
    # Der Grund steht sichtbar auf der Seite, nicht nur als Hinweis am Feld
    assert "Beratungsergebnisse lassen sich erst nach der Freigabe der Vorlage eintragen." in seite

    SessionPaper.objects.filter(pk=vorlage.pk).update(status="approved")
    seite = welt.client.get(welt.url(f"/papers/{vorlage.pk}/")).content.decode()
    assert gesperrt not in seite and '<option value="approved" >' in seite
    assert "Beratungsergebnisse lassen sich erst nach der Freigabe der Vorlage eintragen." not in seite


# =============================================================================
# Fassungen: Kennzeichnung ohne TOP nur an einer freigegebenen Vorlage
# =============================================================================


@pytest.mark.parametrize("status", NICHT_FREIGEGEBEN)
def test_ergebnis_ohne_top_kennzeichnet_vor_der_freigabe_keine_fassung(welt: Welt, status: str) -> None:
    vorlage = welt.vorlage(status)
    station = welt.station(vorlage)

    # Auf einem Weg an der Seite vorbei (Verwaltungsoberfläche, Einspielung): Die Fassung entsteht, ohne Kennzeichen
    station.result = "approved"
    station.save()
    fassung = Version.objects.get(paper=vorlage)
    assert fassung.trigger == Version.TRIGGER_CONSULTATION and not fassung.is_resolved


def test_spaetere_entscheidung_ueber_den_top_erhaelt_die_kennzeichnung(welt: Welt) -> None:
    vorlage = welt.vorlage("draft")
    station = welt.station(vorlage)
    assert "ist noch nicht freigegeben" in _meldungen(welt.speichern(station, result="approved"))

    # Freigabe, Terminierung und Beschluss in der Sitzung
    SessionPaper.objects.filter(pk=vorlage.pk).update(status="scheduled")
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="3", name="Radweg", paper=vorlage)
    station.agenda_item = top
    station.meeting = welt.sitzung
    station.save()
    top.vote_result = "approved"
    top.save()

    beschlossen = Version.objects.get(paper=vorlage, is_resolved=True)
    assert beschlossen.agenda_item == top
    assert beschlossen.status == "scheduled"
