# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Dokumentenliste (Issue #174, Satz A): Die Alpine-Komponente `documentManager`
lebt in `frontend/alpine/document-manager.ts`; das Template liefert die URLs nur noch als JSON
(`json_script`), ist in Partials zerlegt und enthält kein Inline-JavaScript und keine
`on*=`-Handler mehr.
"""

import json
import re
from typing import Any

import pytest

from apps.work.motions.models import DocumentFolder, Motion

CONFIG_RE = re.compile(r'<script[^>]*id="document-manager-config"[^>]*>(.*?)</script>', re.S)
INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc=)(?![^>]*type=[\"']application/json[\"'])[^>]*>", re.I)
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.create", "motions.edit"], email="autor@example.org")


@pytest.fixture
def folder(org: Any, author: Any) -> DocumentFolder:
    return DocumentFolder.objects.create(organization=org, name="Haushalt", created_by=author)


def read_config(html: str) -> dict[str, Any]:
    match = CONFIG_RE.search(html)
    assert match, "json_script #document-manager-config fehlt"
    config: dict[str, Any] = json.loads(match.group(1))
    return config


@pytest.mark.django_db
def test_list_page_provides_json_config_and_no_inline_script(
    org: Any, author: Any, folder: DocumentFolder, client_for: Any
) -> None:
    Motion.objects.create(organization=org, author=author, title="Antrag Spielplatz", folder=folder)
    client = client_for(author.user)
    baseline = client.get(f"/work/{org.slug}/dashboard/")

    response = client.get(f"/work/{org.slug}/documents/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="documentManager"' in html
    assert "Antrag Spielplatz" in html
    assert "Haushalt" in html

    config = read_config(html)
    assert config["currentFolderId"] == ""
    assert config["urls"]["folderCreate"] == f"/work/{org.slug}/documents/folders/create/"
    assert PLACEHOLDER in config["urls"]["folderUpdate"]
    assert PLACEHOLDER in config["urls"]["folderDelete"]
    assert config["urls"]["moveToFolder"].startswith(f"/work/{org.slug}/documents/")

    # Kein Inline-JS und keine on*-Handler mehr aus dem Template (Layout-Skripte bleiben gleich)
    assert "function documentManager" not in html
    assert not re.search(r"\son(change|click|submit)=\"", html)
    if baseline.status_code == 200:
        assert len(INLINE_SCRIPT_RE.findall(html)) <= len(INLINE_SCRIPT_RE.findall(baseline.content.decode()))

    used_templates = {t.name for t in response.templates if t.name}
    for partial in ("_folder_sidebar", "_list_filters", "_grid_view", "_table_view", "_folder_modal"):
        assert f"work/motions/partials/{partial}.html" in used_templates, partial
    assert "cotton/ui/kpi_tile.html" in used_templates


@pytest.mark.django_db
def test_list_page_in_folder_sets_current_folder_in_config(
    org: Any, author: Any, folder: DocumentFolder, client_for: Any
) -> None:
    response = client_for(author.user).get(f"/work/{org.slug}/documents/?ordner={folder.id}")
    assert response.status_code == 200
    config = read_config(response.content.decode())
    assert config["currentFolderId"] == str(folder.id)
