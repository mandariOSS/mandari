# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Editorseite (Issue #170): Die Alpine-Komponente `documentEditor` lebt in
`frontend/alpine/document-editor.ts`; das Template liefert die Konfiguration nur noch als
JSON (`json_script`) und enthält kein Inline-JavaScript mehr.
"""

import json
import re
from typing import Any

import pytest

from apps.work.motions.models import Motion

EDIT_PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]
CONFIG_RE = re.compile(r'<script[^>]*id="document-editor-config"[^>]*>(.*?)</script>', re.S)


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, EDIT_PERMISSIONS, email="autor@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    return Motion.objects.create(organization=org, author=author, title="Antrag Spielplatz", visibility="organization")


def editor_url(org: Any, motion: Motion) -> str:
    return f"/work/{org.slug}/documents/{motion.id}/"


def read_config(html: str) -> dict[str, Any]:
    match = CONFIG_RE.search(html)
    assert match, "json_script #document-editor-config fehlt"
    config: dict[str, Any] = json.loads(match.group(1))
    return config


@pytest.mark.django_db
def test_editor_page_provides_json_config_and_no_inline_script(
    org: Any, motion: Motion, author: Any, client_for: Any
) -> None:
    response = client_for(author.user).get(editor_url(org, motion))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="documentEditor"' in html

    config = read_config(html)
    assert config["motionId"] == str(motion.id)
    assert config["title"] == "Antrag Spielplatz"
    assert config["accessLevel"] == "admin"
    assert config["letterhead"] is None
    assert config["letterheads"] == []
    assert config["inlineComments"] == []
    assert config["urls"]["comment"] == f"/work/{org.slug}/documents/{motion.id}/comment/"
    assert config["urls"]["revisions"] == f"/work/{org.slug}/documents/{motion.id}/revisions/"
    assert config["urls"]["checklist"] == f"/work/{org.slug}/documents/{motion.id}/checklist/"
    assert config["urls"]["documents"] == f"/work/{org.slug}/documents/"

    # Kein Inline-JS aus dem früheren Partial mehr
    used_templates = {t.name for t in response.templates if t.name}
    assert "work/motions/partials/_editor_js.html" not in used_templates
    assert "function documentEditor" not in html
    assert "mandariMotionMeta = function" not in html
    assert 'id="editor-initial-content"' in html


@pytest.mark.django_db
def test_editor_page_for_viewer_has_config_without_editor(
    org: Any, motion: Motion, make_member: Any, client_for: Any
) -> None:
    viewer = make_member(org, ["motions.view"], email="leser@example.org")
    response = client_for(viewer.user).get(editor_url(org, motion))
    html = response.content.decode()

    assert response.status_code == 200
    config = read_config(html)
    assert config["accessLevel"] == "view"
    assert 'id="editor-initial-content"' not in html
    assert "function documentEditor" not in html
