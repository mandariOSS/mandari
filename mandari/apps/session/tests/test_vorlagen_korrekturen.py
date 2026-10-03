# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Korrekturen an Vorlagen, Anträgen, Beratungsfolge, Mitzeichnung und Beschlussregister.

Jeder Test hält einen Fehler fest, der beim Durchlauf durch das Session-RIS aufgefallen ist:
Auswahlfelder, die beim Speichern geleert wurden, Knöpfe, die nur zu einer Fehlermeldung führten,
Serverfehler bei ungültigen Kennungen, Datumsangaben in UTC statt Ortszeit und Exporte, die die
gesetzten Filter nicht übernahmen.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionConsultation,
    SessionCosignature,
    SessionFile,
    SessionMeeting,
    SessionNumberRange,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import agenda_service, numbering_service
from apps.session.services.application_service import convert_to_paper

pytestmark = pytest.mark.django_db


# =============================================================================
# Hilfen
# =============================================================================


#: Alle Rechte-Schalter der Rolle – manche stehen standardmäßig auf „an“
ALLE_RECHTE = [f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_") and f.concrete]


def _nutzer(tenant: SessionTenant, name: str, *perms: str, admin: bool = False) -> SessionUser:
    """Konto mit genau diesen Rechten (alle anderen Schalter aus)."""
    flags = {flag: flag[4:] in perms for flag in ALLE_RECHTE}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle-{name}", is_admin=admin, **flags)
    user = cast(User, UserFactory(email=f"{name}@example.org"))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


class _Formular(HTMLParser):
    """Liest POST-Formulare wie ein Browser: Werte der Felder, gewählte Optionen, angehakte Kästchen."""

    def __init__(self) -> None:
        super().__init__()
        self.formulare: list[dict[str, str]] = []
        self.daten: dict[str, str] = {}
        self._select: str | None = None
        self._erste: str | None = None
        self._gewaehlt = False
        self._textarea: str | None = None
        self._text: list[str] = []
        self._im_formular = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form" and a.get("method", "").lower() == "post" and not self._im_formular:
            self._im_formular = True
            self.daten = {}
            self.formulare.append(self.daten)
        if not self._im_formular:
            return
        if tag == "input" and a.get("name"):
            typ = a.get("type", "text")
            if typ in ("checkbox", "radio"):
                if "checked" in a:
                    self.daten[a["name"]] = a.get("value", "on")
            elif typ != "file":
                self.daten[a["name"]] = a.get("value", "")
        elif tag == "select" and a.get("name"):
            self._select, self._erste, self._gewaehlt = a["name"], None, False
        elif tag == "option" and self._select:
            wert = a.get("value", "")
            if self._erste is None:
                self._erste = wert
            if "selected" in a and not self._gewaehlt:
                self.daten[self._select] = wert
                self._gewaehlt = True
        elif tag == "textarea" and a.get("name"):
            self._textarea, self._text = a["name"], []

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select:
            if not self._gewaehlt:
                self.daten[self._select] = self._erste or ""
            self._select = None
        elif tag == "textarea" and self._textarea:
            self.daten[self._textarea] = "".join(self._text).strip("\n")
            self._textarea = None
        elif tag == "form" and self._im_formular:
            self._im_formular = False

    def handle_data(self, data: str) -> None:
        if self._textarea:
            self._text.append(data)


def _formular(html: str, feld: str = "status") -> dict[str, str]:
    """Daten des ersten POST-Formulars mit diesem Feld."""
    parser = _Formular()
    parser.feed(html)
    return next(daten for daten in parser.formulare if feld in daten)


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Korrektur", slug="korrektur")


@pytest.fixture
def welt(tenant: SessionTenant) -> Any:
    w = SimpleNamespace()
    w.tenant = tenant
    w.gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    w.bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", short_name="BA")
    w.amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    w.person = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Antrag")
    w.admin = _nutzer(tenant, "admin", admin=True)
    w.sachbearbeitung = _nutzer(
        tenant,
        "sachbearbeitung",
        "view_papers",
        "create_papers",
        "edit_papers",
        "view_applications",
        "process_applications",
        "view_meetings",
        "edit_meetings",
    )
    return w


# =============================================================================
# Vorlagenformular: Vorauswahl und Datumsfelder
# =============================================================================


def test_vorlage_bearbeiten_behaelt_zuordnungen_beim_speichern(welt: Any) -> None:
    paper = SessionPaper.objects.create(
        tenant=welt.tenant,
        name="Spielplatz",
        status="approved",
        has_financial_impact=False,
        main_organization=welt.gremium,
        lead_department=welt.amt,
        originator_organization=welt.bau,
        originator_person=welt.person,
        date=date(2026, 9, 1),
        deadline=date(2026, 11, 15),
    )
    client = _client(welt.admin)
    url = f"/session/{welt.tenant.slug}/papers/{paper.id}/edit/"
    html = client.get(url).content.decode()
    for objekt in (welt.gremium, welt.amt, welt.bau, welt.person):
        assert f'<option value="{objekt.id}" selected>' in " ".join(html.split()).replace(" >", ">")

    # Wie im Browser absenden, nur den Status ändern
    daten = _formular(html)
    daten["status"] = "withdrawn"
    daten["has_financial_impact"] = "False"
    antwort = client.post(url, daten)
    assert antwort.status_code == 302, antwort.content.decode()[:2000]
    paper.refresh_from_db()
    assert paper.status == "withdrawn"
    assert paper.main_organization == welt.gremium
    assert paper.lead_department == welt.amt
    assert paper.originator_organization == welt.bau
    assert paper.originator_person == welt.person
    assert paper.date == date(2026, 9, 1)
    assert paper.deadline == date(2026, 11, 15)


def test_vorlagenformular_behaelt_datum_und_frist_nach_validierungsfehler(welt: Any) -> None:
    client = _client(welt.sachbearbeitung)
    antwort = client.post(
        f"/session/{welt.tenant.slug}/papers/create/",
        {"name": "", "paper_type": "proposal", "date": "2026-10-01", "deadline": "2026-11-15"},
    )
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert 'value="2026-10-01"' in html
    assert 'value="2026-11-15"' in html


def test_antrag_bearbeiten_behaelt_zielgremium(welt: Any) -> None:
    antrag = SessionApplication.objects.create(
        tenant=welt.tenant,
        title="Mehr Bänke",
        justification="b",
        resolution_proposal="r",
        submitter_name="N",
        submitter_email="n@example.org",
        target_organization=welt.gremium,
    )
    client = _client(welt.sachbearbeitung)
    url = f"/session/{welt.tenant.slug}/applications/{antrag.id}/process/"
    html = client.get(url).content.decode()
    daten = _formular(html)
    assert daten["target_organization"] == str(welt.gremium.id)
    daten["processing_notes"] = "Notiz"
    assert client.post(url, daten).status_code == 302
    antrag.refresh_from_db()
    assert antrag.target_organization == welt.gremium
    assert antrag.processing_notes == "Notiz"


# =============================================================================
# Anlagen einer freigegebenen Vorlage
# =============================================================================


def _anlage(paper: SessionPaper) -> SessionFile:
    return SessionFile.objects.create(
        tenant=paper.tenant,
        name="anlage.txt",
        file=SimpleUploadedFile("anlage.txt", b"Inhalt"),
        is_public=True,
        paper=paper,
    )


def test_freigegebene_vorlage_bietet_nur_noch_sichtbarkeit_der_anlagen_an(welt: Any, tmp_path: Any) -> None:
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        frei = SessionPaper.objects.create(tenant=welt.tenant, name="Frei", status="approved")
        entwurf = SessionPaper.objects.create(tenant=welt.tenant, name="Entwurf", status="draft")
        datei = _anlage(frei)
        _anlage(entwurf)
        client = _client(welt.sachbearbeitung)
        html = client.get(f"/session/{welt.tenant.slug}/papers/{frei.id}/").content.decode()
        assert f"/files/{datei.id}/update/" in html  # Ö/NÖ bleibt umstellbar
        assert f"/files/{datei.id}/replace/" not in html
        assert f"/files/{datei.id}/delete/" not in html
        assert "/files/upload/" not in html
        assert 'data-testid="anlagen-gesperrt"' in html

        html = client.get(f"/session/{welt.tenant.slug}/papers/{entwurf.id}/").content.decode()
        assert "/files/upload/" in html
        assert 'data-testid="anlagen-gesperrt"' not in html


# =============================================================================
# Beratungsfolge
# =============================================================================


def test_top_aus_beratungsfolge_ohne_nummer_ohne_praefix(welt: Any) -> None:
    # Freigegebene Vorlage ohne Nummer (kein Nummernkreis): TOP nur mit dem Titel, ohne „: “-Präfix
    SessionNumberRange.objects.filter(tenant=welt.tenant).delete()
    paper = SessionPaper.objects.create(
        tenant=welt.tenant, name="Testvorlage ohne Nummer", is_public=True, status="approved"
    )
    assert paper.reference == ""
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="HA", organization=welt.gremium, start=timezone.now() + timedelta(days=7)
    )
    station = SessionConsultation.objects.create(paper=paper, organization=welt.gremium, meeting=sitzung, order=1)
    client = _client(welt.admin)
    client.post(f"/session/{welt.tenant.slug}/consultations/{station.id}/schedule/")
    top = SessionAgendaItem.objects.get(meeting=sitzung, paper=paper)
    assert top.name == "Testvorlage ohne Nummer"


def test_top_vor_nummernvergabe_erhaelt_die_nummer_bei_der_freigabe(welt: Any) -> None:
    # Bestand aus der Zeit, als sich auch Entwürfe terminieren ließen (vor Issue #721): Die Freigabe zieht die
    # Nummer im TOP-Namen nach
    SessionNumberRange.objects.filter(tenant=welt.tenant).update(assign_on="release")
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Testvorlage Freigabe-Nummer", is_public=True)
    assert paper.reference == ""
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="HA", organization=welt.gremium, start=timezone.now() + timedelta(days=7)
    )
    top = SessionAgendaItem.objects.create(
        meeting=sitzung, number="1", name=agenda_service.paper_item_name(paper), paper=paper
    )
    assert top.name == "Testvorlage Freigabe-Nummer"

    paper.status = "approved"
    paper.save()
    assert paper.reference
    top.refresh_from_db()
    assert top.name == f"{paper.reference}: Testvorlage Freigabe-Nummer"


def test_nummernvergabe_laesst_umbenannte_tops_unberuehrt(welt: Any) -> None:
    SessionNumberRange.objects.filter(tenant=welt.tenant).update(assign_on="release")
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Vorlage", is_public=True)
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="HA", organization=welt.gremium, start=timezone.now()
    )
    eigen = SessionAgendaItem.objects.create(meeting=sitzung, number="1", name="Eigener Betreff", paper=paper)
    alt = SessionAgendaItem.objects.create(meeting=sitzung, number="2", name=": Vorlage", paper=paper)
    paper.status = "approved"
    paper.save()
    eigen.refresh_from_db()
    alt.refresh_from_db()
    assert eigen.name == "Eigener Betreff"
    assert alt.name == f"{paper.reference}: Vorlage"


def test_terminieren_meldet_das_sitzungsdatum_in_ortszeit(welt: Any) -> None:
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Vorlage", is_public=True, status="approved")
    # 22:30 UTC am 08.10. ist in Deutschland schon der 09.10.
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="HA", organization=welt.gremium, start=datetime(2026, 10, 8, 22, 30, tzinfo=UTC)
    )
    station = SessionConsultation.objects.create(paper=paper, organization=welt.gremium, meeting=sitzung, order=1)
    antwort = _client(welt.admin).post(f"/session/{welt.tenant.slug}/consultations/{station.id}/schedule/")
    assert "(09.10.2026)" in _meldungen(antwort)


def test_station_hinzufuegen_bietet_gemeinsame_sitzungen_allen_gremien_an(welt: Any) -> None:
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Vorlage", is_public=True)
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Gemeinsam", organization=welt.gremium, start=timezone.now() + timedelta(days=3)
    )
    sitzung.joint_organizations.add(welt.bau)
    html = _client(welt.admin).get(f"/session/{welt.tenant.slug}/papers/{paper.id}/").content.decode()
    start = html.index(f'<option value="{sitzung.id}" data-org="')
    data_org = html[start:].split('data-org="', 1)[1].split('"', 1)[0].split()
    assert sorted(data_org) == sorted([str(welt.gremium.id), str(welt.bau.id)])


@pytest.mark.parametrize(
    "daten",
    [
        {"organization": "abc"},
        {"organization": "{gremium}", "meeting": "abc"},
    ],
)
def test_station_mit_ungueltiger_kennung_ohne_serverfehler(welt: Any, daten: dict[str, str]) -> None:
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Vorlage", is_public=True)
    daten = {k: v.format(gremium=welt.gremium.id) for k, v in daten.items()}
    antwort = _client(welt.admin).post(f"/session/{welt.tenant.slug}/papers/{paper.id}/consultations/add/", daten)
    assert antwort.status_code == 302
    assert not SessionConsultation.objects.filter(paper=paper).exists()


def test_station_bearbeiten_mit_ungueltiger_sitzung_ohne_serverfehler(welt: Any) -> None:
    paper = SessionPaper.objects.create(tenant=welt.tenant, name="Vorlage", is_public=True)
    station = SessionConsultation.objects.create(paper=paper, organization=welt.gremium, order=1)
    antwort = _client(welt.admin).post(
        f"/session/{welt.tenant.slug}/consultations/{station.id}/update/", {"meeting": "abc"}
    )
    assert antwort.status_code == 302
    assert "nicht gefunden" in _meldungen(antwort)


# =============================================================================
# Nummernkreise
# =============================================================================


@pytest.mark.parametrize(
    "daten",
    [
        {"action": "range", "range_id": "abc", "pattern": "V/{jahr}/{lfd:4}", "reset": "yearly"},
        {"action": "next", "range_id": "abc", "next_number": "5"},
    ],
)
def test_nummernkreis_mit_ungueltiger_kennung_ist_nicht_gefunden(welt: Any, daten: dict[str, str]) -> None:
    antwort = _client(welt.admin).post(f"/session/{welt.tenant.slug}/settings/numbering/save/", daten)
    assert antwort.status_code == 404


def test_vorschau_mit_gremium_platzhalter_zeigt_das_schema(welt: Any) -> None:
    kreis = SessionNumberRange.objects.create(
        tenant=welt.tenant, name="Je Gremium", pattern="{gremium}/{lfd:3}/{jahr}", reset="yearly"
    )
    jahr = timezone.localdate().year
    assert numbering_service.preview(kreis) == f"GREMIUM/001/{jahr}"
    mit_gremium = SessionPaper(tenant=welt.tenant, name="x", main_organization=welt.gremium)
    assert numbering_service.preview(kreis, mit_gremium) == f"HA/001/{jahr}"


# =============================================================================
# Freigabevermerk
# =============================================================================


def test_zurueck_in_den_entwurf_entfernt_den_freigabevermerk(welt: Any) -> None:
    paper = SessionPaper.objects.create(
        tenant=welt.tenant,
        name="Vorlage",
        status="withdrawn",
        has_financial_impact=False,
        approved_by=welt.admin,
        approved_at=timezone.now(),
    )
    client = _client(welt.admin)
    url = f"/session/{welt.tenant.slug}/papers/{paper.id}/edit/"
    daten = _formular(client.get(url).content.decode())
    daten.update(status="draft", has_financial_impact="False")
    assert client.post(url, daten).status_code == 302
    paper.refresh_from_db()
    assert paper.status == "draft"
    assert paper.approved_by is None
    assert paper.approved_at is None
    html = client.get(f"/session/{welt.tenant.slug}/papers/{paper.id}/").content.decode()
    assert "Freigegeben von" not in html


# =============================================================================
# Anträge: Status und Umwandlung
# =============================================================================


def _antrag(welt: Any, **kwargs: Any) -> SessionApplication:
    kwargs.setdefault("title", "Mehr Bänke")
    return SessionApplication.objects.create(
        tenant=welt.tenant,
        justification="Begründung",
        resolution_proposal="Beschluss",
        submitter_name="N",
        submitter_email="n@example.org",
        **kwargs,
    )


def test_antragsstatus_umgewandelt_ist_nicht_von_hand_waehlbar(welt: Any) -> None:
    antrag = _antrag(welt)
    client = _client(welt.sachbearbeitung)
    url = f"/session/{welt.tenant.slug}/applications/{antrag.id}/process/"
    antwort = client.get(url)
    angeboten = [wert for wert, _ in antwort.context["form"].fields["status"].choices]
    assert "converted" not in angeboten
    # Der Status-Verlauf zeigt weiterhin alle Stufen
    assert "In Vorlage umgewandelt" in antwort.content.decode()
    client.post(url, {"status": "converted"})
    antrag.refresh_from_db()
    assert antrag.status == "submitted"


def test_umgewandelter_antrag_bleibt_umgewandelt(welt: Any) -> None:
    antrag = _antrag(welt)
    convert_to_paper(antrag, session_user=welt.admin)
    client = _client(welt.sachbearbeitung)
    url = f"/session/{welt.tenant.slug}/applications/{antrag.id}/process/"
    antwort = client.get(url)
    assert [wert for wert, _ in antwort.context["form"].fields["status"].choices] == ["converted"]
    client.post(url, {"status": "submitted"})
    antrag.refresh_from_db()
    assert antrag.status == "converted"


@pytest.mark.parametrize("status", ["withdrawn", "rejected"])
def test_zurueckgezogener_oder_abgelehnter_antrag_wird_nicht_umgewandelt(welt: Any, status: str) -> None:
    antrag = _antrag(welt, status=status)
    client = _client(welt.admin)
    detail = client.get(f"/session/{welt.tenant.slug}/applications/{antrag.id}/").content.decode()
    assert f"/applications/{antrag.id}/convert/" not in detail
    url = f"/session/{welt.tenant.slug}/applications/{antrag.id}/convert/"
    assert client.get(url).status_code == 302
    antwort = client.post(url, {"name": "Vorlage", "paper_type": "motion"})
    assert antwort.status_code == 302
    assert "Umwandlung nicht möglich" in _meldungen(antwort)
    assert not SessionPaper.objects.filter(source_application=antrag).exists()
    antrag.refresh_from_db()
    assert antrag.status == status


@pytest.mark.parametrize(
    ("angabe", "erwartet"),
    [
        ("Kosten: ca. 45.000 Euro brutto, Deckung aus dem Ergebnishaushalt", True),
        ("Keine.", False),
        ("", None),
    ],
)
def test_umwandlung_uebernimmt_finanzielle_auswirkungen(welt: Any, angabe: str, erwartet: bool | None) -> None:
    antrag = _antrag(welt, financial_impact=angabe)
    paper, neu = convert_to_paper(antrag, session_user=welt.admin)
    assert neu
    assert paper.has_financial_impact is erwartet
    assert paper.financial_impact_note == angabe


# =============================================================================
# Mitzeichnung
# =============================================================================


@pytest.fixture
def mitzeichnung(welt: Any) -> Any:
    SessionNumberRange.objects.filter(tenant=welt.tenant).update(assign_on="release")
    paper = SessionPaper.objects.create(
        tenant=welt.tenant,
        name="Vorlage in Prüfung",
        status="review",
        is_public=True,
        has_financial_impact=False,
        created_by=welt.sachbearbeitung,
    )
    station = SessionCosignature.objects.create(paper=paper, department=welt.amt, order=1)
    return paper, station


def test_mitzeichnung_nennt_vorlage_ohne_nummer(welt: Any, mitzeichnung: Any) -> None:
    paper, station = mitzeichnung
    client = _client(welt.admin)
    html = client.get(f"/session/{welt.tenant.slug}/cosignatures/").content.decode()
    assert "Nummer folgt: Vorlage in Prüfung" in html
    html = client.get(f"/session/{welt.tenant.slug}/papers/review/").content.decode()
    assert "Nummer folgt" in html
    antwort = client.post(f"/session/{welt.tenant.slug}/cosignatures/{station.id}/sign/")
    assert "für Nummer folgt erteilt" in _meldungen(antwort)


def test_zurueckweisung_der_mitzeichnung_benachrichtigt_die_erstellende_person(welt: Any, mitzeichnung: Any) -> None:
    paper, station = mitzeichnung
    mail.outbox.clear()
    _client(welt.admin).post(
        f"/session/{welt.tenant.slug}/cosignatures/{station.id}/reject/", {"comment": "Bitte Kosten klären"}
    )
    paper.refresh_from_db()
    assert paper.status == "draft"
    assert len(mail.outbox) == 1
    nachricht = mail.outbox[0]
    assert nachricht.to == ["sachbearbeitung@example.org"]
    assert "zurückgewiesen" in nachricht.subject
    assert "Kämmerei" in nachricht.body
    assert "Bitte Kosten klären" in nachricht.body


def test_amts_zuordnung_nur_mit_benutzerverwaltung(welt: Any) -> None:
    nur_einstellungen = _nutzer(welt.tenant, "einstellungen", "manage_settings")
    beides = _nutzer(welt.tenant, "beides", "manage_settings", "manage_users")
    url = f"/session/{welt.tenant.slug}/settings/cosign/"
    html = _client(nur_einstellungen).get(url).content.decode()
    assert "/settings/cosign/assignment/" not in html
    assert "/settings/cosign/rule/" in html
    html = _client(beides).get(url).content.decode()
    assert "/settings/cosign/assignment/" in html


# =============================================================================
# Vorlage aus einem Antrag
# =============================================================================


def test_ursprungsantrag_nur_mit_recht_auf_antraege_verlinkt(welt: Any) -> None:
    antrag = _antrag(welt)
    paper, _ = convert_to_paper(antrag, session_user=welt.admin)
    nur_vorlagen = _nutzer(welt.tenant, "nur-vorlagen", "view_papers")
    url = f"/session/{welt.tenant.slug}/papers/{paper.id}/"
    html = _client(nur_vorlagen).get(url).content.decode()
    assert f"/applications/{antrag.id}/" not in html
    assert antrag.reference in html
    html = _client(welt.sachbearbeitung).get(url).content.decode()
    assert f"/applications/{antrag.id}/" in html


# =============================================================================
# Beschlussregister
# =============================================================================


@pytest.fixture
def beschluesse(welt: Any) -> Any:
    # 23:30 UTC am 31.12.2025 ist in Deutschland schon der 01.01.2026
    neujahr = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Neujahr", organization=welt.gremium, start=datetime(2025, 12, 31, 23, 30, tzinfo=UTC)
    )
    vorjahr = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Vorjahr", organization=welt.gremium, start=datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    )
    erledigt = SessionAgendaItem.objects.create(
        meeting=neujahr, number="1", name="Erledigt", vote_result="approved", implementation_status="done"
    )
    offen = SessionAgendaItem.objects.create(
        meeting=vorjahr, number="1", name="Offen", vote_result="approved", implementation_status="open"
    )
    return erledigt, offen


def _csv(antwort: Any) -> list[list[str]]:
    text = antwort.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=";"))[1:]


def test_beschlussregister_rechnet_in_ortszeit(welt: Any, beschluesse: Any) -> None:
    client = _client(welt.admin)
    antwort = client.get(f"/session/{welt.tenant.slug}/resolutions/")
    assert antwort.context["years"] == [2026, 2025]
    zeilen = _csv(client.get(f"/session/{welt.tenant.slug}/resolutions/export.csv?year=2026"))
    assert [(z[2], z[5]) for z in zeilen] == [("Erledigt", "01.01.2026")]


@pytest.mark.parametrize("jahr", ["0", "99999999999", "²", "abc"])
def test_ungueltiges_jahr_ohne_serverfehler(welt: Any, beschluesse: Any, jahr: str) -> None:
    client = _client(welt.admin)
    seite = client.get(f"/session/{welt.tenant.slug}/resolutions/", {"year": jahr})
    assert seite.status_code == 200
    assert len(seite.context["items"]) == 2  # ungültiges Jahr filtert nicht
    assert client.get(f"/session/{welt.tenant.slug}/resolutions/export.csv", {"year": jahr}).status_code == 200


def test_jahresauswahl_bleibt_beim_filtern_vollstaendig(welt: Any, beschluesse: Any) -> None:
    client = _client(welt.admin)
    for abfrage in ("?year=2026", "?status=done", "?overdue=1"):
        antwort = client.get(f"/session/{welt.tenant.slug}/resolutions/{abfrage}")
        assert antwort.context["years"] == [2026, 2025], abfrage


def test_csv_export_uebernimmt_umsetzungsstand_und_ueberfaellig(welt: Any, beschluesse: Any) -> None:
    erledigt, offen = beschluesse
    offen.implementation_deadline = timezone.localdate() - timedelta(days=1)
    offen.save()
    client = _client(welt.admin)
    seite = client.get(f"/session/{welt.tenant.slug}/resolutions/?status=done")
    assert [item.name for item in seite.context["items"]] == ["Erledigt"]
    assert "status=done" in seite.context["export_query"]
    assert "export.csv?status=done" in seite.content.decode()

    zeilen = _csv(client.get(f"/session/{welt.tenant.slug}/resolutions/export.csv?status=done"))
    assert [z[2] for z in zeilen] == ["Erledigt"]
    zeilen = _csv(client.get(f"/session/{welt.tenant.slug}/resolutions/export.csv?overdue=1"))
    assert [z[2] for z in zeilen] == ["Offen"]
