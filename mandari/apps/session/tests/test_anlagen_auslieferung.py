# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslieferung von Anlagen: Im Browser angezeigt werden nur PDF und Rasterbilder, alles andere kommt
als Download – mit ``X-Content-Type-Options: nosniff`` und einer Sandbox-CSP. Den MIME-Typ bestimmt
der Server aus der Dateiendung, nie aus der Angabe des hochladenden Browsers. Das gilt für die
öffentliche OParl-Auslieferung, den geschützten Download und die Fassungen einer Anlage.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.session.models import SessionFile, SessionPaper, SessionRole, SessionTenant, SessionUser
from apps.session.services import file_version_service

pytestmark = pytest.mark.django_db

AKTIV = b"<html><body><script>alert(document.cookie)</script></body></html>"


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Auslieferung", slug="auslieferung")


@pytest.fixture
def vorlage(tenant: SessionTenant) -> SessionPaper:
    return SessionPaper.objects.create(
        tenant=tenant, reference="V/2026/1", name="Öffentliche Vorlage", is_public=True, status="approved"
    )


@pytest.fixture
def entwurf(tenant: SessionTenant) -> SessionPaper:
    """Hochladen und Ersetzen gibt es nur vor der Freigabe."""
    return SessionPaper.objects.create(tenant=tenant, name="Entwurf", is_public=True, status="draft")


@pytest.fixture
def sachbearbeitung(tenant: SessionTenant) -> Client:
    role = SessionRole.objects.create(
        tenant=tenant,
        name=f"Rolle {uuid.uuid4().hex[:8]}",
        can_view_papers=True,
        can_edit_papers=True,
    )
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def _anlage(vorlage: SessionPaper, name: str, mime_type: str, inhalt: bytes = AKTIV) -> SessionFile:
    """Bestand, wie ihn der Browser beim Hochladen beschrieben hat (Typ ungeprüft übernommen)."""
    datei = SessionFile(tenant=vorlage.tenant, name=name, mime_type=mime_type, is_public=True, paper=vorlage)
    file_version_service.attach_upload(datei, SimpleUploadedFile(name, inhalt, content_type=mime_type), user=None)
    return datei


def _oparl(tenant: SessionTenant, datei: SessionFile, *, download: bool = False) -> Any:
    suffix = "?download=1" if download else ""
    return Client().get(f"/session/{tenant.slug}/api/oparl/file/{datei.pk}/download/{suffix}")


def _sandboxed(response: Any) -> bool:
    return "sandbox" in response.get("Content-Security-Policy", "")


def test_upload_bestimmt_den_mime_typ_aus_der_endung(
    tenant: SessionTenant, entwurf: SessionPaper, sachbearbeitung: Client
) -> None:
    upload = SimpleUploadedFile("bericht.pdf", AKTIV, content_type="text/html")
    antwort = sachbearbeitung.post(
        f"/session/{tenant.slug}/files/upload/",
        {"target_type": "paper", "target_id": str(entwurf.pk), "is_public": "on", "files": upload},
    )
    assert antwort.status_code == 302
    datei = SessionFile.objects.get(paper=entwurf)
    assert datei.mime_type == "application/pdf"


def test_ersetzen_bestimmt_den_mime_typ_aus_der_endung(
    tenant: SessionTenant, entwurf: SessionPaper, sachbearbeitung: Client
) -> None:
    datei = _anlage(entwurf, "tabelle.csv", "text/csv", b"a;b\n1;2\n")
    upload = SimpleUploadedFile("tabelle.csv", AKTIV, content_type="text/html")
    antwort = sachbearbeitung.post(f"/session/{tenant.slug}/files/{datei.pk}/replace/", {"file": upload})
    assert antwort.status_code == 302
    datei.refresh_from_db()
    assert datei.version == 2
    assert datei.mime_type == "text/csv"


def test_oparl_liefert_falsch_deklarierten_bestand_nie_als_html(tenant: SessionTenant, vorlage: SessionPaper) -> None:
    datei = _anlage(vorlage, "bericht.pdf", "text/html")
    antwort = _oparl(tenant, datei)
    assert antwort.status_code == 200
    assert antwort["Content-Type"] == "application/pdf"
    assert antwort["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    ("name", "mime_type"),
    [("plan.svg", "image/svg+xml"), ("notiz.txt", "text/html"), ("liste.csv", "text/csv")],
)
def test_oparl_liefert_aktive_und_textinhalte_nur_als_download(
    tenant: SessionTenant, vorlage: SessionPaper, name: str, mime_type: str
) -> None:
    antwort = _oparl(tenant, _anlage(vorlage, name, mime_type))
    assert antwort.status_code == 200
    assert antwort["Content-Disposition"].startswith("attachment")
    assert "html" not in antwort["Content-Type"]
    assert antwort["X-Content-Type-Options"] == "nosniff"
    assert _sandboxed(antwort)


def test_oparl_zeigt_pdf_und_bilder_weiter_im_browser(tenant: SessionTenant, vorlage: SessionPaper) -> None:
    pdf = _oparl(tenant, _anlage(vorlage, "bericht.pdf", "application/pdf", b"%PDF-1.4\n%%EOF\n"))
    assert pdf["Content-Disposition"].startswith("inline")
    assert pdf["Content-Type"] == "application/pdf"
    assert pdf["X-Content-Type-Options"] == "nosniff"
    bild = _oparl(tenant, _anlage(vorlage, "foto.png", "image/png", b"\x89PNG\r\n\x1a\n"))
    assert bild["Content-Disposition"].startswith("inline")
    assert bild["Content-Type"] == "image/png"
    assert _sandboxed(bild)
    assert _oparl(tenant, _anlage(vorlage, "anhang.pdf", "application/pdf"), download=True)[
        "Content-Disposition"
    ].startswith("attachment")


def test_oparl_metadaten_nennen_den_ausgelieferten_typ(tenant: SessionTenant, vorlage: SessionPaper) -> None:
    datei = _anlage(vorlage, "bericht.pdf", "text/html")
    antwort = Client().get(f"/session/{tenant.slug}/api/oparl/file/{datei.pk}/")
    assert antwort.status_code == 200
    assert antwort.json()["mimeType"] == "application/pdf"


def test_geschuetzter_download_und_fassungen_mit_sandbox(
    tenant: SessionTenant, vorlage: SessionPaper, sachbearbeitung: Client
) -> None:
    datei = _anlage(vorlage, "plan.svg", "image/svg+xml")
    for pfad in (f"/files/{datei.pk}/download/", f"/files/{datei.pk}/fassungen/1/"):
        antwort = sachbearbeitung.get(f"/session/{tenant.slug}{pfad}")
        assert antwort.status_code == 200, pfad
        assert antwort["Content-Disposition"].startswith("attachment"), pfad
        assert antwort["X-Content-Type-Options"] == "nosniff", pfad
        assert _sandboxed(antwort), pfad

    legacy = _anlage(vorlage, "bericht.pdf", "text/html")
    antwort = sachbearbeitung.get(f"/session/{tenant.slug}/files/{legacy.pk}/download/")
    assert antwort["Content-Type"] == "application/pdf"
