# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlermeldungen an Nutzer bestehen aus festen Texten. Texte fremder Ausnahmen (Anbieter,
Bibliotheken, Datenbank) landen nur im Protokoll, nie in Antworten, Flash-Meldungen oder
gespeicherten Statusfeldern, die angezeigt werden.
"""

from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

GEHEIM = "interne-details?key=GEHEIM123"

pytestmark = pytest.mark.django_db


def test_zusammenfassung_zeigt_keine_ausnahmetexte(client: Client) -> None:
    from insight_core.models import OParlBody, OParlPaper, OParlSource

    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.invalid/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.invalid/oparl/bodies/1", source=quelle, name="Stadt"
    )
    paper = OParlPaper.objects.create(
        external_id="https://ris.example.invalid/oparl/papers/1", body=body, name="Vorlage", reference="V/1"
    )
    for fehler in (RuntimeError(GEHEIM), ValueError(GEHEIM)):
        with (
            mock.patch("insight_ai.services.summarizer.SummaryService.__init__", return_value=None),
            mock.patch("insight_ai.services.summarizer.SummaryService.generate_summary", side_effect=fehler),
        ):
            antwort = client.get(f"/insight/vorgaenge/{paper.id}/zusammenfassung/")
        assert GEHEIM not in antwort.content.decode()
        assert "GEHEIM123" not in antwort.content.decode()


def test_zusammenfassungsdienst_gibt_anbietertext_nicht_weiter() -> None:
    from insight_ai.services.summarizer import SummaryError, SummaryService

    anbieter = mock.Mock()
    anbieter.is_available.return_value = True
    anbieter.chat_completion.side_effect = RuntimeError(GEHEIM)
    dienst = cast(Any, SummaryService)(provider=anbieter)
    paper = mock.Mock(id="1", reference="V/1", paper_type="", date=None, body=None)
    paper.name = "Vorlage"
    paper.consultations.values_list.return_value.distinct.return_value = []
    with (
        mock.patch.object(SummaryService, "_collect_text_content_with_extraction", return_value="Text"),
        pytest.raises(SummaryError) as fehler,
    ):
        dienst.generate_summary(paper, save=False)
    assert "GEHEIM123" not in str(fehler.value)


def test_einladung_zeigt_keinen_ausnahmetext(org: Any, make_member: Any) -> None:
    from apps.work.organization import services
    from apps.work.organization.services import ServiceError

    einladend = make_member(org, ["members.invite"], email="orga@example.org")
    with (
        mock.patch("apps.tenants.models.UserInvitation.create_for_organization", side_effect=RuntimeError(GEHEIM)),
        pytest.raises(ServiceError) as fehler,
    ):
        services.invite_member(org, einladend.user, "neu@example.org", [], "")
    assert "GEHEIM123" not in str(fehler.value)


def test_datenexport_speichert_keinen_ausnahmetext(org: Any, make_member: Any) -> None:
    from apps.work.background_tasks import generate_dsgvo_export_task
    from apps.work.organization.models import DataExport

    mitglied = make_member(org, ["dashboard.view"], email="export@example.org")
    export = DataExport.objects.create(organization=org, membership=mitglied, export_format="json")
    with mock.patch(
        "apps.work.organization.export_service.dsgvo_export_service.collect_user_data",
        side_effect=RuntimeError(GEHEIM),
    ):
        cast(Any, generate_dsgvo_export_task).call(str(export.id))
    export.refresh_from_db()
    assert export.status == "failed"
    assert export.error_message
    assert "GEHEIM123" not in export.error_message


@pytest.mark.parametrize("art", ["pdf", "docx"])
def test_dokumentimport_zeigt_keinen_ausnahmetext(org: Any, make_member: Any, art: str) -> None:
    pytest.importorskip("docx")
    from apps.work.motions.import_service import MotionImportService

    autor = make_member(org, ["motions.create"], email="import@example.org")
    datei = SimpleUploadedFile(f"antrag.{art}", b"kaputt")
    ziel = "apps.work.motions.import_service.extract_text_from_file" if art == "pdf" else "docx.Document"
    with mock.patch(ziel, side_effect=RuntimeError(GEHEIM)):
        methode = MotionImportService.import_pdf if art == "pdf" else MotionImportService.import_docx
        ergebnis = methode(datei, org, autor)
    assert not ergebnis.success
    assert ergebnis.error
    assert "GEHEIM123" not in ergebnis.error


def test_admin_testmail_zeigt_keinen_ausnahmetext(admin_client: Client) -> None:
    from django.contrib.messages import get_messages

    from apps.common.models import SiteSettings

    einstellungen = cast(Any, SiteSettings).get_settings()
    with mock.patch("apps.common.mail_backends.build_backend", side_effect=RuntimeError(GEHEIM)):
        antwort = admin_client.get(f"/admin/common/sitesettings/{einstellungen.pk}/test_email/", HTTP_REFERER="/admin/")
    assert antwort.status_code == 302
    meldungen = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert meldungen, "Fehlermeldung fehlt"
    assert not any("GEHEIM123" in m for m in meldungen)
