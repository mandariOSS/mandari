# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Inhalt einer Vorlage ab der Vorlage zur Freigabe festgeschrieben.

Mitzeichnung und Freigabe beziehen sich auf genau den Stand, der zur Freigabe vorgelegt wurde. In der
Prüfung lassen sich Texte, Angaben zu finanziellen Auswirkungen und Anlagen deshalb nicht mehr ändern;
Änderungen entstehen nach einer Zurückweisung im Entwurf, und das erneute Vorlegen baut die
Mitzeichnungskette neu auf. Angaben ohne Inhalt (Frist, Zuordnung) und die Ö/NÖ-Kennzeichnung einer
Anlage bleiben änderbar.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionCosignature,
    SessionCosignatureRule,
    SessionFile,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service, paper_version_service

pytestmark = pytest.mark.django_db

ALLE_RECHTE = [feld.name for feld in SessionRole._meta.concrete_fields if feld.name.startswith("can_")]


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


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Prüfstand", slug="pruefstand")


@pytest.fixture
def kaemmerei(tenant: SessionTenant) -> SessionOrganization:
    amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    SessionCosignatureRule.objects.create(tenant=tenant, department=amt, order=1, only_financial=True)
    return amt


@pytest.fixture
def sachbearbeitung(tenant: SessionTenant) -> SessionUser:
    return _nutzer(tenant, "view_papers", "create_papers", "edit_papers")


@pytest.fixture
def freigabe(tenant: SessionTenant, kaemmerei: SessionOrganization) -> SessionUser:
    session_user = _nutzer(tenant, "view_papers", "approve_papers")
    session_user.departments.add(kaemmerei)
    return session_user


def _vorlage(tenant: SessionTenant, ersteller: SessionUser) -> SessionPaper:
    return SessionPaper.objects.create(
        tenant=tenant,
        name="Neubau Kita",
        main_text="Sachverhalt wie vorgelegt",
        has_financial_impact=False,
        created_by=ersteller,
    )


def _bearbeiten(client: Client, vorlage: SessionPaper, **felder: str) -> Any:
    daten = {
        "name": vorlage.name,
        "paper_type": vorlage.paper_type,
        "main_text": vorlage.main_text,
        "is_public": "on",
        "status": vorlage.status,
        "has_financial_impact": "True" if vorlage.has_financial_impact else "False",
        "financial_impact_note": vorlage.financial_impact_note,
        **felder,
    }
    return client.post(f"/session/{vorlage.tenant.slug}/papers/{vorlage.id}/edit/", daten)


def _aktion(client: Client, vorlage: SessionPaper, aktion: str, **daten: str) -> Any:
    return client.post(f"/session/{vorlage.tenant.slug}/papers/{vorlage.id}/workflow/{aktion}/", daten)


def test_vorlage_in_pruefung_ist_inhaltlich_festgeschrieben() -> None:
    vorlage = SessionPaper(status="review")
    assert paper_version_service.content_locked(vorlage)
    assert not paper_version_service.content_locked(SessionPaper(status="draft"))


@pytest.mark.parametrize(
    "felder",
    [
        {"main_text": "Nachträglich geändert"},
        {"name": "Neubau Kita und Hort"},
        {"has_financial_impact": "True", "financial_impact_note": "500.000 Euro"},
    ],
)
def test_inhalt_in_der_pruefung_nicht_aenderbar(
    tenant: SessionTenant, sachbearbeitung: SessionUser, felder: dict[str, str]
) -> None:
    vorlage = _vorlage(tenant, sachbearbeitung)
    vorlage.status = "review"
    vorlage.save()
    antwort = _bearbeiten(_client(sachbearbeitung), vorlage, **felder)
    assert antwort.status_code == 200
    assert "zurückweisen" in antwort.content.decode()
    vorlage.refresh_from_db()
    assert vorlage.name == "Neubau Kita"
    assert vorlage.main_text == "Sachverhalt wie vorgelegt"
    assert vorlage.has_financial_impact is False


def test_angaben_ohne_inhalt_bleiben_in_der_pruefung_aenderbar(
    tenant: SessionTenant, sachbearbeitung: SessionUser
) -> None:
    vorlage = _vorlage(tenant, sachbearbeitung)
    vorlage.status = "review"
    vorlage.save()
    assert _bearbeiten(_client(sachbearbeitung), vorlage, deadline="2030-01-31").status_code == 302
    vorlage.refresh_from_db()
    assert str(vorlage.deadline) == "2030-01-31"


def test_anlagen_in_der_pruefung_festgeschrieben(
    tenant: SessionTenant, sachbearbeitung: SessionUser, tmp_path: Any
) -> None:
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        vorlage = _vorlage(tenant, sachbearbeitung)
        datei = SessionFile(tenant=tenant, name="plan.txt", is_public=True, paper=vorlage)
        file_version_service.attach_upload(datei, SimpleUploadedFile("plan.txt", b"vorgelegt"), user=None)
        vorlage.status = "review"
        vorlage.save()
        client = _client(sachbearbeitung)
        basis = f"/session/{tenant.slug}"
        client.post(
            f"{basis}/files/upload/",
            {"target_type": "paper", "target_id": str(vorlage.id), "files": SimpleUploadedFile("neu.txt", b"neu")},
        )
        client.post(f"{basis}/files/{datei.id}/replace/", {"file": SimpleUploadedFile("plan.txt", b"anders")})
        client.post(f"{basis}/files/{datei.id}/delete/")
        assert list(vorlage.files.values_list("name", flat=True)) == ["plan.txt"]
        datei.refresh_from_db()
        assert datei.version == 1

        # Ö/NÖ bleibt umstellbar
        assert client.post(f"{basis}/files/{datei.id}/update/", {"name": "plan.txt"}).status_code == 302
        datei.refresh_from_db()
        assert datei.is_public is False


def test_aenderung_nur_ueber_zurueckweisung_und_erneute_mitzeichnung(
    tenant: SessionTenant,
    sachbearbeitung: SessionUser,
    freigabe: SessionUser,
    kaemmerei: SessionOrganization,
) -> None:
    vorlage = _vorlage(tenant, sachbearbeitung)
    vorlage.has_financial_impact = True
    vorlage.financial_impact_note = "50.000 Euro"
    vorlage.save()
    bearbeitung = _client(sachbearbeitung)
    pruefung = _client(freigabe)

    _aktion(bearbeitung, vorlage, "submit")
    station = SessionCosignature.objects.get(paper=vorlage, department=kaemmerei)
    pruefung.post(f"/session/{tenant.slug}/cosignatures/{station.id}/sign/")
    station.refresh_from_db()
    assert station.status == "signed"

    # Nach der Mitzeichnung bleibt der mitgezeichnete Stand unverändert
    vorlage.refresh_from_db()
    _bearbeiten(bearbeitung, vorlage, financial_impact_note="500.000 Euro", main_text="Anderer Sachverhalt")
    vorlage.refresh_from_db()
    assert vorlage.financial_impact_note == "50.000 Euro"
    assert vorlage.main_text == "Sachverhalt wie vorgelegt"

    # Änderung: zurückweisen, im Entwurf ändern, erneut vorlegen – die Kämmerei zeichnet neu
    _aktion(pruefung, vorlage, "reject", comment="Kosten neu berechnen")
    vorlage.refresh_from_db()
    assert vorlage.status == "draft"
    assert _bearbeiten(bearbeitung, vorlage, financial_impact_note="500.000 Euro").status_code == 302
    _aktion(bearbeitung, vorlage, "submit")
    vorlage.refresh_from_db()
    assert vorlage.status == "review"
    assert vorlage.financial_impact_note == "500.000 Euro"
    assert list(SessionCosignature.objects.filter(paper=vorlage).values_list("status", flat=True)) == ["pending"]
    _aktion(pruefung, vorlage, "approve")
    vorlage.refresh_from_db()
    assert vorlage.status == "review"
