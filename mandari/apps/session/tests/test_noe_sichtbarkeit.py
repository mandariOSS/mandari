# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliches bleibt bei denen, die es sehen dürfen – auch hinter dem Fachrecht.

Wer Vorlagen oder Sitzungen bearbeiten darf, aber kein NÖ-Sichtrecht hat, erreicht nichtöffentliche
Anlagen, Vorlagen, Sitzungen und TOPs weder über die Anlagen-Aktionen noch über Detailseiten,
Suche, Mitzeichnung oder Ladung. Geprüft wird jeweils mit und ohne NÖ-Recht.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionCosignature,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import agenda_service, file_version_service, invitation_service

pytestmark = pytest.mark.django_db

LESEN = ("view_dashboard", "view_meetings", "view_papers", "view_applications")
NOE = ("view_non_public_meetings", "view_non_public_papers")
GEHEIM = ("KLAUSUR-GEHEIM", "PERSONALIE-GEHEIM", "NOE-TOP-BERATUNG", "TOP-IN-KLAUSUR")
ALLE_RECHTE = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


@dataclass
class Welt:
    tenant: SessionTenant
    objekte: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.objekte[key]

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


def _client(welt: Welt, *rechte: str, amt: SessionOrganization | None = None) -> Client:
    # Alle Häkchen ausdrücklich setzen – einige Sichtrechte sind am Modell standardmäßig an
    flags = {name: name[4:] in rechte for name in ALLE_RECHTE}
    role = SessionRole.objects.create(tenant=welt.tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", **flags)
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=welt.tenant)
    session_user.roles.add(role)
    if amt is not None:
        session_user.departments.add(amt)
    client = Client()
    client.force_login(user)
    return client


def _anlage(welt: Welt, name: str, *, public: bool, text: str = "", **ziel: Any) -> SessionFile:
    datei = SessionFile(tenant=welt.tenant, name=name, is_public=public, text_content=text, **ziel)
    inhalt = f"{name} {uuid.uuid4().hex}".encode()
    file_version_service.attach_upload(datei, SimpleUploadedFile(name, inhalt), user=None)
    return datei


def _seite(client: Client, url: str) -> str:
    antwort = client.get(url)
    assert antwort.status_code == 200, url
    return antwort.content.decode()


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Sichtbarkeit", slug="sichtbarkeit")
    w = Welt(tenant)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    amt = SessionOrganization.objects.create(tenant=tenant, name="Amt 10", organization_type="department")
    start = timezone.now() + timedelta(days=10)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="OEFFENTLICHE-SITZUNG", organization=gremium, start=start, is_public=True
    )
    klausur = SessionMeeting.objects.create(
        tenant=tenant, name="KLAUSUR-GEHEIM", organization=gremium, start=start, is_public=False
    )
    antrag = SessionApplication.objects.create(
        tenant=tenant,
        title="ANTRAG-SICHTBAR",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    noe_vorlage = SessionPaper.objects.create(
        tenant=tenant,
        reference="V/2026/9",
        name="PERSONALIE-GEHEIM",
        is_public=False,
        status="approved",
        main_organization=gremium,
        source_application=antrag,
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant,
        reference="V/2026/1",
        name="OEFFENTLICHE-VORLAGE",
        is_public=True,
        status="approved",
        main_organization=gremium,
        parent_paper=noe_vorlage,
        relation_type="supplement",
    )
    top = SessionAgendaItem.objects.create(
        meeting=sitzung, number="1", order=1, name="OEFFENTLICHER-TOP", paper=vorlage
    )
    noe_top = SessionAgendaItem.objects.create(
        meeting=sitzung, number="N1", order=2, name="NOE-TOP-BERATUNG", is_public=False, paper=vorlage
    )
    top_in_klausur = SessionAgendaItem.objects.create(
        meeting=klausur, number="1", order=1, name="TOP-IN-KLAUSUR", paper=vorlage
    )
    noe_top_ohne_vorlage = SessionAgendaItem.objects.create(
        meeting=sitzung, number="N2", order=3, name="NOE-TOP-OHNE-VORLAGE", is_public=False
    )
    SessionConsultation.objects.create(paper=vorlage, organization=gremium, meeting=klausur, order=1)
    person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Person")
    SessionAttendance.objects.create(meeting=klausur, person=person)

    w.objekte.update(
        gremium=gremium,
        amt=amt,
        sitzung=sitzung,
        klausur=klausur,
        antrag=antrag,
        vorlage=vorlage,
        noe_vorlage=noe_vorlage,
        top=top,
        noe_top=noe_top,
        top_in_klausur=top_in_klausur,
        noe_top_ohne_vorlage=noe_top_ohne_vorlage,
        person=person,
        noe_anlage=_anlage(w, "geheim-anlage.txt", public=False, paper=vorlage),
        anlage=_anlage(w, "oeffentlich.txt", public=True, text="ZAUBERWORT", paper=vorlage),
        anlage_noe_vorlage=_anlage(w, "anlage-noe-vorlage.txt", public=True, text="ZAUBERWORT", paper=noe_vorlage),
        anlage_klausur=_anlage(w, "anlage-klausur.txt", public=True, text="ZAUBERWORT", meeting=klausur),
        anlage_noe_top=_anlage(w, "anlage-noe-top.txt", public=True, text="ZAUBERWORT", agenda_item=noe_top),
    )
    return w


# =============================================================================
# Anlagen: hochladen, ändern, ersetzen, löschen nur mit NÖ-Recht an NÖ-Objekten
# =============================================================================


@pytest.mark.parametrize("anlage", ["noe_anlage", "anlage_noe_vorlage"])
def test_noe_anlage_nicht_oeffentlich_schaltbar(welt: Welt, anlage: str) -> None:
    client = _client(welt, "view_papers", "edit_papers")
    datei = welt[anlage]
    antwort = client.post(welt.url(f"/files/{datei.id}/update/"), {"is_public": "on", "name": "umbenannt.txt"})
    assert antwort.status_code == 404
    datei.refresh_from_db()
    assert datei.name != "umbenannt.txt"
    assert datei.is_public is (anlage == "anlage_noe_vorlage")


def test_noe_anlage_nicht_loeschbar_und_nicht_ersetzbar(welt: Welt) -> None:
    client = _client(welt, "view_papers", "edit_papers")
    datei = welt["noe_anlage"]
    assert client.post(welt.url(f"/files/{datei.id}/delete/")).status_code == 404
    ersatz = SimpleUploadedFile("ersatz.txt", b"neu")
    assert client.post(welt.url(f"/files/{datei.id}/replace/"), {"file": ersatz}).status_code == 404
    datei.refresh_from_db()
    assert datei.version == 1


@pytest.mark.parametrize(
    ("rechte", "ziel_typ", "ziel"),
    [
        (("view_papers", "edit_papers"), "paper", "noe_vorlage"),
        (("view_meetings", "edit_meetings"), "meeting", "klausur"),
        (("view_meetings", "edit_meetings"), "agenda_item", "noe_top_ohne_vorlage"),
        (("view_meetings", "edit_meetings"), "agenda_item", "top_in_klausur"),
    ],
)
def test_kein_upload_an_noe_ziele(welt: Welt, rechte: tuple[str, ...], ziel_typ: str, ziel: str) -> None:
    vorher = SessionFile.objects.count()
    antwort = _client(welt, *rechte).post(
        welt.url("/files/upload/"),
        {"target_type": ziel_typ, "target_id": str(welt[ziel].id), "files": SimpleUploadedFile("neu.txt", b"x")},
    )
    assert antwort.status_code == 404
    assert SessionFile.objects.count() == vorher


def test_upload_an_top_braucht_das_sitzungsrecht(welt: Welt) -> None:
    antwort = _client(welt, "view_papers", "edit_papers").post(
        welt.url("/files/upload/"),
        {"target_type": "agenda_item", "target_id": str(welt["top"].id), "files": SimpleUploadedFile("t.txt", b"x")},
    )
    assert antwort.status_code == 403


def test_mit_noe_recht_bleiben_die_aktionen_moeglich(welt: Welt) -> None:
    client = _client(welt, "view_papers", "edit_papers", *NOE)
    datei = welt["noe_anlage"]
    assert client.post(welt.url(f"/files/{datei.id}/update/"), {"name": "freigegeben.txt"}).status_code == 302
    datei.refresh_from_db()
    assert datei.name == "freigegeben.txt"
    antwort = client.post(
        welt.url("/files/upload/"),
        {"target_type": "paper", "target_id": str(welt["noe_vorlage"].id), "files": SimpleUploadedFile("n.txt", b"x")},
    )
    assert antwort.status_code == 302
    assert welt["noe_vorlage"].files.count() == 2


# =============================================================================
# Detailseiten: NÖ-Titel nur mit NÖ-Recht
# =============================================================================


def test_gremium_nennt_keine_noe_sitzungen_und_vorlagen(welt: Welt) -> None:
    seite = _seite(_client(welt, *LESEN), welt.url(f"/organizations/{welt['gremium'].id}/"))
    assert "OEFFENTLICHE-SITZUNG" in seite and "OEFFENTLICHE-VORLAGE" in seite
    assert "KLAUSUR-GEHEIM" not in seite and "PERSONALIE-GEHEIM" not in seite


def test_gremium_nennt_vorlagen_nur_mit_vorlagenrecht(welt: Welt) -> None:
    seite = _seite(_client(welt, "view_meetings"), welt.url(f"/organizations/{welt['gremium'].id}/"))
    assert "OEFFENTLICHE-VORLAGE" not in seite


def test_vorlage_nennt_keine_noe_beratungen_und_bezuege(welt: Welt) -> None:
    seite = _seite(_client(welt, *LESEN), welt.url(f"/papers/{welt['vorlage'].id}/"))
    assert "OEFFENTLICHER-TOP" in seite
    for titel in GEHEIM:
        assert titel not in seite, titel


def test_person_nennt_keine_noe_sitzungen(welt: Welt) -> None:
    seite = _seite(_client(welt, *LESEN), welt.url(f"/persons/{welt['person'].id}/"))
    assert "KLAUSUR-GEHEIM" not in seite


def test_antrag_nennt_keine_noe_vorlagen(welt: Welt) -> None:
    seite = _seite(_client(welt, *LESEN), welt.url(f"/applications/{welt['antrag'].id}/"))
    assert "PERSONALIE-GEHEIM" not in seite


def test_mit_noe_recht_sind_die_titel_sichtbar(welt: Welt) -> None:
    client = _client(welt, *LESEN, *NOE)
    assert "KLAUSUR-GEHEIM" in _seite(client, welt.url(f"/organizations/{welt['gremium'].id}/"))
    vorlage = _seite(client, welt.url(f"/papers/{welt['vorlage'].id}/"))
    assert all(titel in vorlage for titel in GEHEIM)
    assert "KLAUSUR-GEHEIM" in _seite(client, welt.url(f"/persons/{welt['person'].id}/"))
    assert "PERSONALIE-GEHEIM" in _seite(client, welt.url(f"/applications/{welt['antrag'].id}/"))


# =============================================================================
# Suche nach Anlagen
# =============================================================================


def _anlagen_treffer(client: Client, welt: Welt) -> set[str]:
    antwort = client.get(welt.url("/search/"), {"q": "ZAUBERWORT", "kind": "files"})
    assert antwort.status_code == 200
    return {datei.name for datei in antwort.context["results"].get("files", [])}


def test_anlagensuche_ohne_noe_kontext(welt: Welt) -> None:
    assert _anlagen_treffer(_client(welt, *LESEN), welt) == {"oeffentlich.txt"}


def test_anlagensuche_an_vorlagen_braucht_das_vorlagenrecht(welt: Welt) -> None:
    assert _anlagen_treffer(_client(welt, "view_dashboard", "view_meetings"), welt) == set()


def test_anlagensuche_mit_noe_recht_findet_alles(welt: Welt) -> None:
    assert _anlagen_treffer(_client(welt, *LESEN, *NOE), welt) == {
        "oeffentlich.txt",
        "anlage-noe-vorlage.txt",
        "anlage-klausur.txt",
        "anlage-noe-top.txt",
    }


# =============================================================================
# Mitzeichnung
# =============================================================================


def _station(welt: Welt, paper: SessionPaper) -> SessionCosignature:
    paper.status = "review"
    paper.save()
    return SessionCosignature.objects.create(paper=paper, department=welt["amt"], order=1)


def test_mitzeichnung_zeigt_und_entscheidet_keine_noe_vorlagen(welt: Welt) -> None:
    station = _station(welt, welt["noe_vorlage"])
    client = _client(welt, "view_papers", amt=welt["amt"])
    antwort = client.get(welt.url("/cosignatures/"))
    assert antwort.status_code == 200
    assert "PERSONALIE-GEHEIM" not in antwort.content.decode()
    assert antwort.context["cosign_count"] == 0
    assert client.post(welt.url(f"/cosignatures/{station.id}/sign/")).status_code == 404
    station.refresh_from_db()
    assert station.status == "pending"


def test_mitzeichnung_oeffentlicher_und_mit_noe_recht(welt: Welt) -> None:
    oeffentlich = _station(welt, welt["vorlage"])
    geheim = _station(welt, welt["noe_vorlage"])
    client = _client(welt, "view_papers", amt=welt["amt"])
    assert "OEFFENTLICHE-VORLAGE" in _seite(client, welt.url("/cosignatures/"))
    assert client.post(welt.url(f"/cosignatures/{oeffentlich.id}/sign/")).status_code == 302
    noe_client = _client(welt, "view_papers", "view_non_public_papers", amt=welt["amt"])
    assert "PERSONALIE-GEHEIM" in _seite(noe_client, welt.url("/cosignatures/"))
    assert noe_client.post(welt.url(f"/cosignatures/{geheim.id}/sign/")).status_code == 302
    geheim.refresh_from_db()
    assert geheim.status == "signed"


# =============================================================================
# Ladung: Gäste einer NÖ-Sitzung erhalten keine Tagesordnung
# =============================================================================


def _pdf_text(inhalt: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(inhalt)).pages)


def _ladung_an(adresse: str) -> str:
    treffer = [m for m in mail.outbox if m.to == [adresse]]
    assert treffer, adresse
    pdf = next(inhalt for name, inhalt, _typ in treffer[-1].attachments if name.endswith(".pdf"))
    return _pdf_text(pdf)


def test_gaeste_einer_noe_sitzung_erhalten_keine_tagesordnung(welt: Welt) -> None:
    gremium = welt["gremium"]
    for rolle, adresse in (("member", "mitglied@example.org"), ("guest", "gast@example.org")):
        person = SessionPerson.objects.create(tenant=welt.tenant, given_name=rolle, family_name="X", email=adresse)
        SessionOrganizationMembership.objects.create(organization=gremium, person=person, role=rolle)
    mail.outbox = []
    invitation_service.send_invitations(welt["klausur"], sent_by=None)

    assert "TOP-IN-KLAUSUR" in _ladung_an("mitglied@example.org")
    assert "TOP-IN-KLAUSUR" not in _ladung_an("gast@example.org")


def test_oeffentliche_fassung_einer_noe_sitzung_ist_leer(welt: Welt) -> None:
    agenda = agenda_service.grouped_agenda(welt["klausur"], include_non_public=False)
    assert agenda == {"public": [], "non_public": []}
    oeffentlich = agenda_service.grouped_agenda(welt["sitzung"], include_non_public=False)
    assert [item.name for item in oeffentlich["public"]] == ["OEFFENTLICHER-TOP"]
