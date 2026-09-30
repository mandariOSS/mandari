# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenexporte (Art. 15/20 DSGVO) gehen nur über ``work:export_download`` hinaus.

Die Hintergrundaufgabe legt die Datei unter ``MEDIA_ROOT/exports/<organisation>/<mitgliedschaft>/``
ab. ``/media/`` liefert dieses Präfix an niemanden aus; die Download-Ansicht prüft Organisation
und Mitgliedschaft.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.urls import reverse

from apps.common.tests.factories import OrganizationFactory, UserFactory
from apps.work.background_tasks import generate_dsgvo_export_task
from apps.work.organization.models import DataExport

pytestmark = pytest.mark.django_db

DATEN = {"konto": {"email": "eigentuemerin@example.org"}, "notizen": ["vertraulich"]}


@pytest.fixture(autouse=True)
def media_root(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def export(org: Any, make_member: Any) -> DataExport:
    eigentuemerin = make_member(org, ["dashboard.view"], email="eigentuemerin@example.org")
    export = DataExport.objects.create(organization=org, membership=eigentuemerin, export_format="json")
    with mock.patch("apps.work.organization.export_service.dsgvo_export_service.collect_user_data", return_value=DATEN):
        cast(Any, generate_dsgvo_export_task).enqueue(str(export.id))
    export.refresh_from_db()
    assert export.status == "completed"
    return export


def _download_url(export: DataExport) -> str:
    return reverse("work:export_download", kwargs={"org_slug": export.organization.slug, "export_id": export.id})


def test_exportdatei_liegt_unter_geschuetztem_praefix(export: DataExport, media_root: Path) -> None:
    assert export.file_path == f"exports/{export.organization_id}/{export.membership_id}/dsgvo-export-{export.id}.json"
    assert (media_root / export.file_path).is_file()


def test_media_liefert_exporte_an_niemanden_aus(
    export: DataExport, org: Any, make_member: Any, client_for: Any
) -> None:
    fremde_org = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")  # type: ignore[no-untyped-call]
    konten = {
        "fremdes Konto ohne Mitgliedschaft": UserFactory(email="ohne-org@example.org"),  # type: ignore[no-untyped-call]
        "Mitglied einer anderen Organisation": make_member(
            fremde_org, ["dashboard.view"], email="fremd@example.org"
        ).user,
        "Kollege derselben Organisation": make_member(org, ["dashboard.view"], email="kollege@example.org").user,
        "Eigentümerin selbst": export.membership.user,
    }
    for beschreibung, konto in konten.items():
        antwort = client_for(konto).get(f"/media/{export.file_path}")
        assert antwort.status_code == 404, beschreibung


def test_eigentuemerin_laedt_ueber_die_ansicht(export: DataExport, client_for: Any) -> None:
    antwort = client_for(export.membership.user).get(_download_url(export))

    assert antwort.status_code == 200
    assert b"vertraulich" in antwort.content
    assert antwort["Content-Disposition"].startswith("attachment;")
    assert antwort["Cache-Control"] == "private, no-store"
    assert antwort["X-Content-Type-Options"] == "nosniff"


def test_ansicht_liefert_fremde_exporte_nicht_aus(
    export: DataExport, org: Any, make_member: Any, client_for: Any
) -> None:
    kollege = make_member(org, ["dashboard.view"], email="kollege@example.org")
    fremde_org = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")  # type: ignore[no-untyped-call]
    fremd = make_member(fremde_org, ["dashboard.view"], email="fremd@example.org")

    assert client_for(kollege.user).get(_download_url(export)).status_code == 404
    # Mitglied einer anderen Organisation: weder über den fremden noch über den eigenen Organisationspfad
    assert client_for(fremd.user).get(_download_url(export)).status_code in (403, 404)
    eigener_pfad = reverse("work:export_download", kwargs={"org_slug": fremde_org.slug, "export_id": export.id})
    assert client_for(fremd.user).get(eigener_pfad).status_code == 404
