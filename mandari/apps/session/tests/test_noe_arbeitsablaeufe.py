# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliches in den Arbeitsabläufen des Sitzungsdienstes.

Umlaufbeschlüsse, Jahresplanung, Genehmigung der Niederschrift, Beratungsfolge, Anwesenheit,
Beschlussauszüge, Tagesordnungszeilen, Fassungen und Erinnerungs-Mails nennen und berühren
nichtöffentliche Sitzungen, TOPs und Vorlagen nur für Personen mit dem passenden NÖ-Recht.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionCircularResolution,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPaperVersion,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service, paper_version_service, reminder_service

pytestmark = pytest.mark.django_db

ALLE_RECHTE = [feld.name for feld in SessionRole._meta.concrete_fields if feld.name.startswith("can_")]
NOE = ("view_non_public_meetings", "view_non_public_papers")


@dataclass
class Welt:
    tenant: SessionTenant
    objekte: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.objekte[key]

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


def _nutzer(welt: Welt, *rechte: str, email: str = "") -> SessionUser:
    flags = {feld: feld[4:] in rechte for feld in ALLE_RECHTE}
    role = SessionRole.objects.create(tenant=welt.tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", **flags)
    user = cast(Any, UserFactory)(**({"email": email} if email else {}))
    session_user = SessionUser.objects.create(user=user, tenant=welt.tenant)
    session_user.roles.add(role)
    return session_user


def _client(welt: Welt, *rechte: str) -> Client:
    client = Client()
    client.force_login(_nutzer(welt, *rechte).user)
    return client


def _seite(client: Client, url: str) -> str:
    antwort = client.get(url)
    assert antwort.status_code == 200, url
    return antwort.content.decode()


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Ablauf", slug="ablauf")
    w = Welt(tenant)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
    tag = timezone.localdate() + timedelta(days=20)
    beginn = timezone.make_aware(datetime.combine(tag, datetime.min.time().replace(hour=17)))
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="OEFFENTLICHE-SITZUNG", organization=gremium, start=beginn - timedelta(days=10)
    )
    klausur = SessionMeeting.objects.create(
        tenant=tenant, name="KLAUSUR-GEHEIM", organization=gremium, start=beginn, is_public=False, room="Saal 1"
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/1", name="OEFFENTLICHE-VORLAGE", is_public=True, status="approved"
    )
    noe_vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/9", name="PERSONALIE-GEHEIM", is_public=False, status="approved"
    )
    w.objekte.update(
        gremium=gremium, sitzung=sitzung, klausur=klausur, vorlage=vorlage, noe_vorlage=noe_vorlage, tag=tag
    )
    return w


# =============================================================================
# Umlaufbeschlüsse
# =============================================================================


def _umlauf(welt: Welt, *, public: bool, paper: SessionPaper | None = None) -> SessionCircularResolution:
    return SessionCircularResolution.objects.create(
        tenant=welt.tenant,
        organization=welt["gremium"],
        title="UMLAUF-GEHEIM" if not public else "UMLAUF",
        resolution_text="x",
        deadline=welt["tag"],
        is_public=public,
        paper=paper,
    )


def _umlauf_anlegen(client: Client, welt: Welt, paper: SessionPaper, **extra: str) -> Any:
    return client.post(
        welt.url("/circulars/create/"),
        {
            "organization": str(welt["gremium"].id),
            "title": "Neuer Umlauf",
            "resolution_text": "Beschluss",
            "deadline": welt["tag"].isoformat(),
            "paper": str(paper.id),
            **extra,
        },
    )


def test_umlauf_auswahl_nennt_keine_noe_vorlagen(welt: Welt) -> None:
    seite = _seite(_client(welt, "view_meetings", "edit_meetings"), welt.url("/circulars/"))
    assert "OEFFENTLICHE-VORLAGE" in seite
    assert "PERSONALIE-GEHEIM" not in seite


def test_umlauf_nimmt_keine_noe_vorlage_ohne_recht(welt: Welt) -> None:
    _umlauf_anlegen(_client(welt, "view_meetings", "edit_meetings"), welt, welt["noe_vorlage"])
    assert not SessionCircularResolution.objects.filter(paper=welt["noe_vorlage"]).exists()


def test_oeffentlicher_umlauf_nimmt_keine_noe_vorlage(welt: Welt) -> None:
    client = _client(welt, "view_meetings", "edit_meetings", *NOE)
    _umlauf_anlegen(client, welt, welt["noe_vorlage"], is_public="1")
    assert not SessionCircularResolution.objects.filter(paper=welt["noe_vorlage"], is_public=True).exists()
    _umlauf_anlegen(client, welt, welt["noe_vorlage"], is_public="0")
    assert SessionCircularResolution.objects.filter(paper=welt["noe_vorlage"], is_public=False).exists()


@pytest.mark.parametrize("aktion", ["vote", "close"])
def test_noe_umlauf_ohne_recht_nicht_bedienbar(welt: Welt, aktion: str) -> None:
    person = SessionPerson.objects.create(tenant=welt.tenant, given_name="Mia", family_name="Mitglied")
    SessionOrganizationMembership.objects.create(organization=welt["gremium"], person=person)
    umlauf = _umlauf(welt, public=False)
    antwort = _client(welt, "view_meetings", "edit_meetings").post(
        welt.url(f"/circulars/{umlauf.id}/{aktion}/"), {"person": str(person.id), "vote": "yes", "result": "adopted"}
    )
    assert antwort.status_code == 404
    umlauf.refresh_from_db()
    assert umlauf.status == "open" and not umlauf.votes.exists()


# =============================================================================
# Jahresplanung
# =============================================================================


def test_jahresplanung_nennt_noe_sitzungen_nur_als_belegt(welt: Welt) -> None:
    tag: date = welt["tag"]
    daten = {
        "organization": str(welt["gremium"].id),
        "rhythm": "weekly",
        "weekday": str(tag.weekday()),
        "time": "17:00",
        "date_from": tag.isoformat(),
        "date_to": tag.isoformat(),
        "room": "Saal 1",
    }
    seite = _client(welt, "view_meetings", "edit_meetings").post(welt.url("/meetings/plan/"), daten)
    inhalt = seite.content.decode()
    assert seite.status_code == 200
    assert "Termin belegt" in inhalt
    assert "KLAUSUR-GEHEIM" not in inhalt
    noe = _client(welt, "view_meetings", "edit_meetings", *NOE).post(welt.url("/meetings/plan/"), daten)
    assert "KLAUSUR-GEHEIM" in noe.content.decode()


# =============================================================================
# Niederschrift: Genehmigung in einer Folgesitzung
# =============================================================================


def test_genehmigung_nennt_und_nimmt_keine_noe_folgesitzung(welt: Welt) -> None:
    protokoll = SessionProtocol.objects.create(meeting=welt["sitzung"], status="review")
    client = _client(welt, "view_meetings", "view_protocols", "approve_protocols")
    assert "KLAUSUR-GEHEIM" not in _seite(client, welt.url(f"/meetings/{welt['sitzung'].id}/protocol/"))
    client.post(
        welt.url(f"/meetings/{welt['sitzung'].id}/protocol/approve/"), {"approval_meeting": str(welt["klausur"].id)}
    )
    protokoll.refresh_from_db()
    assert protokoll.approval_meeting_id != welt["klausur"].id


# =============================================================================
# Beratungsfolge
# =============================================================================


def test_beratungsfolge_terminiert_nicht_in_noe_sitzungen(welt: Welt) -> None:
    client = _client(welt, "view_papers", "edit_papers", "view_meetings", "edit_meetings")
    client.post(
        welt.url(f"/papers/{welt['vorlage'].id}/consultations/add/"),
        {"organization": str(welt["gremium"].id), "meeting": str(welt["klausur"].id)},
    )
    assert not SessionConsultation.objects.filter(meeting=welt["klausur"]).exists()
    station = SessionConsultation.objects.create(paper=welt["vorlage"], organization=welt["gremium"], order=5)
    client.post(welt.url(f"/consultations/{station.id}/update/"), {"meeting": str(welt["klausur"].id)})
    station.refresh_from_db()
    assert station.meeting_id is None


def test_station_in_noe_sitzung_ohne_recht_nicht_bedienbar(welt: Welt) -> None:
    station = SessionConsultation.objects.create(
        paper=welt["vorlage"], organization=welt["gremium"], meeting=welt["klausur"], order=1
    )
    client = _client(welt, "view_papers", "edit_papers", "view_meetings", "edit_meetings")
    assert client.post(welt.url(f"/consultations/{station.id}/schedule/")).status_code == 404
    assert client.post(welt.url(f"/consultations/{station.id}/delete/")).status_code == 404
    assert not SessionAgendaItem.objects.filter(meeting=welt["klausur"]).exists()
    assert SessionConsultation.objects.filter(pk=station.pk).exists()


# =============================================================================
# Anwesenheit
# =============================================================================


def test_anwesenheit_einer_noe_sitzung_ohne_recht_nicht_aenderbar(welt: Welt) -> None:
    person = SessionPerson.objects.create(tenant=welt.tenant, given_name="Ali", family_name="Anwesend")
    zeile = SessionAttendance.objects.create(meeting=welt["klausur"], person=person)
    client = _client(welt, "view_meetings", "manage_attendance")
    assert client.post(welt.url(f"/attendance/{zeile.id}/update/"), {"status": "present"}).status_code == 404
    assert client.post(welt.url(f"/attendance/{zeile.id}/delete/")).status_code == 404
    zeile.refresh_from_db()
    assert zeile.status == "invited"


# =============================================================================
# Vorlagennummern in Beschlussauszug und Tagesordnung
# =============================================================================


def _pdf_text(inhalt: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(inhalt)).pages)


def test_noe_vorlage_nur_mit_vorlagen_noe_recht(welt: Welt) -> None:
    top = SessionAgendaItem.objects.create(
        meeting=welt["klausur"],
        number="1",
        order=1,
        name="TOP-KLAUSUR",
        paper=welt["noe_vorlage"],
        vote_result="approved",
    )
    nur_sitzung = _client(welt, "view_meetings", "view_non_public_meetings", "view_papers")
    assert "V/9" not in _seite(nur_sitzung, welt.url(f"/meetings/{welt['klausur'].id}/"))
    auszug = nur_sitzung.get(welt.url(f"/agenda/{top.id}/beschlussauszug.pdf"))
    assert auszug.status_code == 200
    assert "PERSONALIE-GEHEIM" not in _pdf_text(auszug.content)
    tagesordnung = nur_sitzung.get(welt.url(f"/meetings/{welt['klausur'].id}/agenda.pdf"))
    assert "V/9" not in _pdf_text(tagesordnung.content)

    beide = _client(welt, "view_meetings", "view_papers", *NOE)
    assert "V/9" in _seite(beide, welt.url(f"/meetings/{welt['klausur'].id}/"))
    assert "PERSONALIE-GEHEIM" in _pdf_text(beide.get(welt.url(f"/agenda/{top.id}/beschlussauszug.pdf")).content)


# =============================================================================
# Fassung wiederherstellen
# =============================================================================


def test_wiederherstellen_laesst_noe_anlagen_unberuehrt(welt: Welt) -> None:
    vorlage = SessionPaper.objects.create(tenant=welt.tenant, name="Entwurf mit Anlagen", is_public=True)
    noe = SessionFile(tenant=welt.tenant, name="GEHEIM-ANLAGE.txt", is_public=False, paper=vorlage)
    file_version_service.attach_upload(noe, SimpleUploadedFile("geheim.txt", b"alt"), user=None)
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL)
    file_version_service.replace_content(
        noe, SimpleUploadedFile("geheim.txt", b"neu"), user=None, mime_type="text/plain", text_content=""
    )
    spaet = SessionFile(tenant=welt.tenant, name="SPAET-GEHEIM.txt", is_public=False, paper=vorlage)
    file_version_service.attach_upload(spaet, SimpleUploadedFile("spaet.txt", b"x"), user=None)

    antwort = _client(welt, "view_papers", "edit_papers").post(
        welt.url(f"/papers/{vorlage.id}/fassungen/1/wiederherstellen/")
    )
    assert antwort.status_code == 302
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "wiederhergestellt" in meldungen
    assert "SPAET-GEHEIM" not in meldungen and "GEHEIM-ANLAGE" not in meldungen
    noe.refresh_from_db()
    assert noe.version == 2


def test_wiederherstellen_nennt_eigene_spaetere_anlagen_weiter(welt: Welt) -> None:
    vorlage = SessionPaper.objects.create(tenant=welt.tenant, name="Entwurf", is_public=True)
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL)
    spaet = SessionFile(tenant=welt.tenant, name="SPAET-OEFFENTLICH.txt", is_public=True, paper=vorlage)
    file_version_service.attach_upload(spaet, SimpleUploadedFile("spaet.txt", b"x"), user=None)
    antwort = _client(welt, "view_papers", "edit_papers").post(
        welt.url(f"/papers/{vorlage.id}/fassungen/1/wiederherstellen/")
    )
    assert "SPAET-OEFFENTLICH" in " ".join(str(m) for m in get_messages(antwort.wsgi_request))


# =============================================================================
# E-Mails: Freigabeanfrage und Erinnerungen
# =============================================================================


def test_freigabeanfrage_zu_noe_vorlage_nur_an_berechtigte(welt: Welt) -> None:
    _nutzer(welt, "approve_papers", email="pruefung-ohne-noe@example.org")
    _nutzer(welt, "approve_papers", "view_non_public_papers", email="pruefung-mit-noe@example.org")
    entwurf = SessionPaper.objects.create(
        tenant=welt.tenant, name="GEHEIMER-ENTWURF", is_public=False, has_financial_impact=False
    )
    mail.outbox = []
    antwort = _client(welt, "view_papers", "edit_papers", "view_non_public_papers").post(
        welt.url(f"/papers/{entwurf.id}/workflow/submit/")
    )
    assert antwort.status_code == 302
    empfaenger = {adresse for nachricht in mail.outbox for adresse in nachricht.to}
    assert "pruefung-mit-noe@example.org" in empfaenger
    assert "pruefung-ohne-noe@example.org" not in empfaenger


def test_erinnerungen_nennen_noe_titel_nur_berechtigten(welt: Welt) -> None:
    _nutzer(welt, "edit_meetings", "edit_papers", email="dienst-ohne-noe@example.org")
    _nutzer(welt, "edit_meetings", "edit_papers", *NOE, email="dienst-mit-noe@example.org")
    heute = timezone.localdate()
    SessionPaper.objects.create(
        tenant=welt.tenant, name="FRIST-GEHEIM", is_public=False, status="draft", deadline=heute + timedelta(days=1)
    )
    SessionAgendaItem.objects.create(
        meeting=welt["klausur"],
        number="1",
        order=1,
        name="BESCHLUSS-GEHEIM",
        vote_result="approved",
        implementation_deadline=heute - timedelta(days=1),
    )
    welt["klausur"].start = timezone.now() + timedelta(days=2)
    welt["klausur"].meeting_state = "scheduled"
    welt["klausur"].save()
    mail.outbox = []
    reminder_service.run_for_tenant(welt.tenant)

    def texte_an(adresse: str) -> str:
        return "\n".join(f"{m.subject}\n{m.body}" for m in mail.outbox if adresse in m.to)

    ohne = texte_an("dienst-ohne-noe@example.org")
    mit = texte_an("dienst-mit-noe@example.org")
    for titel in ("FRIST-GEHEIM", "BESCHLUSS-GEHEIM", "KLAUSUR-GEHEIM"):
        assert titel in mit, titel
        assert titel not in ohne, titel
