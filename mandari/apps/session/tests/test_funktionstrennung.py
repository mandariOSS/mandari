# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Funktionstrennung und festgeschriebene Stände im Session RIS.

- Das Dashboard zeigt jede Kachel nur mit ihrem Fachrecht.
- Freigegebene Vorlagen bleiben inhaltlich unverändert: Texte und Anlagen nur als Neufassung oder nach
  Rücknahme und erneutem Freigabelauf; Ö/NÖ einer Anlage bleibt umstellbar.
- Bankdaten im Endgeräte-CSV nur mit dem Recht für Sitzungsgelder; Zuschüsse genehmigt eine zweite Person.
- Ein eingerichteter Virenscan fällt nicht stillschweigend aus.
- Einladen nimmt niemanden ungefragt auf und verrät nicht, ob es ein Konto gibt.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
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
    SessionApplication,
    SessionDeviceGrant,
    SessionFile,
    SessionInvitation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service

pytestmark = pytest.mark.django_db

ALLE_RECHTE = [feld.name for feld in SessionRole._meta.concrete_fields if feld.name.startswith("can_")]
IBAN = "DE02120300000000202051"


def _nutzer(tenant: SessionTenant, *rechte: str, admin: bool = False) -> SessionUser:
    flags = {feld: feld[4:] in rechte for feld in ALLE_RECHTE}
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", is_admin=admin, **flags)
    session_user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    session_user.roles.add(role)
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


def _meldungen(antwort: Any) -> str:
    return " ".join(str(m) for m in get_messages(antwort.wsgi_request))


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Trennung", slug="trennung")


# =============================================================================
# Dashboard
# =============================================================================


def test_dashboard_zeigt_ohne_fachrecht_keine_inhalte(tenant: SessionTenant) -> None:
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    SessionMeeting.objects.create(
        tenant=tenant, name="SITZUNG-KACHEL", organization=gremium, start=timezone.now() + timedelta(days=2)
    )
    SessionPaper.objects.create(tenant=tenant, name="VORLAGE-KACHEL", is_public=True)
    SessionApplication.objects.create(
        tenant=tenant,
        title="ANTRAG-KACHEL",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    revision = _client(_nutzer(tenant, "view_dashboard", "view_audit_log"))
    antwort = revision.get(f"/session/{tenant.slug}/dashboard/")
    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    for titel in ("SITZUNG-KACHEL", "VORLAGE-KACHEL", "ANTRAG-KACHEL"):
        assert titel not in inhalt, titel
    assert antwort.context["stats"] == {}

    alles = _client(_nutzer(tenant, "view_dashboard", "view_meetings", "view_papers", "view_applications"))
    inhalt = alles.get(f"/session/{tenant.slug}/dashboard/").content.decode()
    for titel in ("SITZUNG-KACHEL", "VORLAGE-KACHEL", "ANTRAG-KACHEL"):
        assert titel in inhalt, titel


# =============================================================================
# Freigegebene Vorlagen
# =============================================================================


@pytest.fixture
def bearbeitung(tenant: SessionTenant) -> Client:
    return _client(_nutzer(tenant, "view_papers", "edit_papers"))


def _vorlage(tenant: SessionTenant, status: str) -> SessionPaper:
    return SessionPaper.objects.create(
        tenant=tenant,
        name="Radweg",
        main_text="Sachverhalt wie freigegeben",
        is_public=True,
        status=status,
        has_financial_impact=False,
    )


def _anlage(vorlage: SessionPaper) -> SessionFile:
    datei = SessionFile(tenant=vorlage.tenant, name="plan.txt", is_public=True, paper=vorlage)
    file_version_service.attach_upload(datei, SimpleUploadedFile("plan.txt", b"freigegeben"), user=None)
    return datei


def _bearbeiten(client: Client, vorlage: SessionPaper, **felder: str) -> Any:
    daten = {
        "name": vorlage.name,
        "paper_type": vorlage.paper_type,
        "main_text": vorlage.main_text,
        "is_public": "on",
        "status": vorlage.status,
        "has_financial_impact": "false",
        **felder,
    }
    return client.post(f"/session/{vorlage.tenant.slug}/papers/{vorlage.id}/edit/", daten)


@pytest.mark.parametrize("status", ["approved", "scheduled", "completed", "withdrawn"])
def test_freigegebener_inhalt_ist_festgeschrieben(tenant: SessionTenant, bearbeitung: Client, status: str) -> None:
    vorlage = _vorlage(tenant, status)
    antwort = _bearbeiten(bearbeitung, vorlage, main_text="Nachträglich geändert")
    assert antwort.status_code == 200
    assert "Neufassung" in antwort.content.decode()
    vorlage.refresh_from_db()
    assert vorlage.main_text == "Sachverhalt wie freigegeben"


def test_im_entwurf_bleibt_der_inhalt_aenderbar(tenant: SessionTenant, bearbeitung: Client) -> None:
    vorlage = _vorlage(tenant, "draft")
    assert _bearbeiten(bearbeitung, vorlage, main_text="Neu im Entwurf").status_code == 302
    vorlage.refresh_from_db()
    assert vorlage.main_text == "Neu im Entwurf"


def test_nach_freigabe_bleiben_angaben_ohne_inhalt_aenderbar(tenant: SessionTenant, bearbeitung: Client) -> None:
    vorlage = _vorlage(tenant, "approved")
    assert _bearbeiten(bearbeitung, vorlage, deadline="2030-01-31").status_code == 302
    vorlage.refresh_from_db()
    assert str(vorlage.deadline) == "2030-01-31"


def test_anlagen_freigegebener_vorlagen_sind_festgeschrieben(tenant: SessionTenant, bearbeitung: Client) -> None:
    vorlage = _vorlage(tenant, "approved")
    datei = _anlage(vorlage)
    basis = f"/session/{tenant.slug}"
    bearbeitung.post(
        f"{basis}/files/upload/",
        {"target_type": "paper", "target_id": str(vorlage.id), "files": SimpleUploadedFile("neu.txt", b"neu")},
    )
    bearbeitung.post(f"{basis}/files/{datei.id}/replace/", {"file": SimpleUploadedFile("plan.txt", b"anders")})
    bearbeitung.post(f"{basis}/files/{datei.id}/update/", {"name": "umbenannt.txt", "is_public": "on"})
    bearbeitung.post(f"{basis}/files/{datei.id}/delete/")
    assert list(vorlage.files.values_list("name", flat=True)) == ["plan.txt"]
    datei.refresh_from_db()
    assert datei.version == 1

    # Ö/NÖ bleibt umstellbar (z. B. Datenschutz)
    antwort = bearbeitung.post(f"{basis}/files/{datei.id}/update/", {"name": "plan.txt"})
    assert antwort.status_code == 302
    datei.refresh_from_db()
    assert datei.is_public is False


# =============================================================================
# Endgeräte: Bankdaten und Vier-Augen-Prinzip
# =============================================================================


def _zuschuss(tenant: SessionTenant, erfasst_von: SessionUser) -> SessionDeviceGrant:
    person = SessionPerson.objects.create(tenant=tenant, given_name="Ina", family_name="Iban")
    cast(Any, person).set_bank_iban_encrypted(IBAN)
    person.save()
    return SessionDeviceGrant.objects.create(tenant=tenant, person=person, amount=300, created_by=erfasst_von)


def test_endgeraete_csv_ohne_bankdaten_fuer_die_geraeteverwaltung(tenant: SessionTenant) -> None:
    geraete = _nutzer(tenant, "manage_devices")
    _zuschuss(tenant, geraete)
    antwort = _client(geraete).post(f"/session/{tenant.slug}/device-grants/export/csv/")
    assert antwort.status_code == 200
    assert IBAN not in antwort.content.decode()
    kaemmerei = _nutzer(tenant, "manage_devices", "manage_allowances")
    assert IBAN in _client(kaemmerei).post(f"/session/{tenant.slug}/device-grants/export/csv/").content.decode()


def test_zuschuss_genehmigt_eine_zweite_person(tenant: SessionTenant) -> None:
    erfassung = _nutzer(tenant, "manage_devices")
    zuschuss = _zuschuss(tenant, erfassung)
    _client(erfassung).post(f"/session/{tenant.slug}/device-grants/{zuschuss.id}/approve/")
    zuschuss.refresh_from_db()
    assert zuschuss.status == "pending"
    zweite = _nutzer(tenant, "manage_devices")
    _client(zweite).post(f"/session/{tenant.slug}/device-grants/{zuschuss.id}/approve/")
    zuschuss.refresh_from_db()
    assert zuschuss.status == "approved"


# =============================================================================
# Virenscan
# =============================================================================


def test_nicht_ladbarer_virenscan_lehnt_uploads_ab(tenant: SessionTenant, bearbeitung: Client, settings: Any) -> None:
    settings.SESSION_FILE_SCAN_HOOK = "gibt.es.nicht.scan"
    vorlage = _vorlage(tenant, "draft")
    antwort = bearbeitung.post(
        f"/session/{tenant.slug}/files/upload/",
        {"target_type": "paper", "target_id": str(vorlage.id), "files": SimpleUploadedFile("a.txt", b"x")},
    )
    assert antwort.status_code == 302
    assert not vorlage.files.exists()
    assert "Virenscan" in _meldungen(antwort)


# =============================================================================
# Einladen bestehender Konten
# =============================================================================


def test_bestehendes_konto_wird_nicht_ungefragt_aufgenommen(tenant: SessionTenant) -> None:
    verwaltung = _nutzer(tenant, "manage_users", admin=True)
    bestand = cast(Any, UserFactory)(email="bestand@example.org")
    url = f"/session/{tenant.slug}/settings/users/invite/"
    antwort_bestand = _client(verwaltung).post(url, {"email": "bestand@example.org"})
    antwort_neu = _client(verwaltung).post(url, {"email": "neu@example.org"})

    assert not SessionUser.objects.filter(tenant=tenant, user=bestand).exists()
    assert SessionInvitation.objects.filter(tenant=tenant, email="bestand@example.org").exists()
    assert _meldungen(antwort_bestand).replace("bestand", "X") == _meldungen(antwort_neu).replace("neu", "X")

    # Nur der Hash steht in der Datenbank; den Link gibt es nur in der Mail
    einladungsmail = next(m for m in mail.outbox if m.to == ["bestand@example.org"])
    link = re.search(r"/session/invite/[^/\s\"]+/", str(einladungsmail.body))
    assert link is not None
    annahme = Client()
    annahme.force_login(bestand)
    assert annahme.post(link.group(0)).status_code == 302
    assert SessionUser.objects.filter(tenant=tenant, user=bestand, is_active=True).exists()
