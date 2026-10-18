# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichern im Editor vor Statuswechsel und Vorschau (#1018).

- Ein Statuswechsel über die Sperrgrenze fordert alle verbundenen Fenster zum Neuladen auf, außer dem auslösenden
  (Kennung ``fenster``): Es hat vorher gesichert und lädt selbst neu.
- „Vorschau (PDF)“ öffnet die Datei im Browser (inline), der Export bleibt ein Download.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.work.motions import export_service
from apps.work.motions.models import Motion
from apps.work.motions.views import actions as actions_views

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]
PDF_BYTES = b"%PDF-1.4 vorschau"


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=author, title="Antrag Radweg", summary="Kurzfassung", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted("<p>Text</p>")
    motion.save()
    return motion


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def merken(motion: Motion, version: Any = None, ausser_fenster: str = "") -> None:
        calls.append({"motion": motion.pk, "ausser_fenster": ausser_fenster})

    monkeypatch.setattr(actions_views, "_broadcast_doc_reload", merken)
    return calls


def _status(client: Any, org: Any, motion: Motion, **daten: str) -> Any:
    return client.post(f"/work/{org.slug}/documents/{motion.id}/status/", daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_statuswechsel_nimmt_das_ausloesende_fenster_aus(
    org: Any, author: Any, motion: Motion, reloads: list[dict[str, Any]], client_for: Any
) -> None:
    motion.status = "internal_review"
    motion.save(update_fields=["status"])

    response = _status(client_for(author.user), org, motion, status="approved", fenster="0f8b6c1e-2d3a-4b5c")

    assert response.status_code == 200, response.content
    assert reloads == [{"motion": motion.pk, "ausser_fenster": "0f8b6c1e-2d3a-4b5c"}]


@pytest.mark.django_db
def test_statuswechsel_ohne_gueltige_kennung_laedt_alle_neu(
    org: Any, author: Any, motion: Motion, reloads: list[dict[str, Any]], client_for: Any
) -> None:
    motion.status = "internal_review"
    motion.save(update_fields=["status"])

    response = _status(client_for(author.user), org, motion, status="approved", fenster="<script>")

    assert response.status_code == 200, response.content
    assert reloads == [{"motion": motion.pk, "ausser_fenster": ""}]


@pytest.fixture
def export_ohne_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_service.motion_export_service, "export_to_pdf", lambda motion: PDF_BYTES)


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_vorschau_zeigt_pdf_im_browser(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    client = client_for(author.user)
    url = f"/work/{org.slug}/documents/{motion.id}/export/"

    vorschau = client.get(url, {"format": "pdf", "vorschau": "1"})
    download = client.get(url, {"format": "pdf"})

    assert vorschau.status_code == 200
    assert vorschau["Content-Disposition"].startswith("inline;")
    assert download["Content-Disposition"].startswith("attachment;")
