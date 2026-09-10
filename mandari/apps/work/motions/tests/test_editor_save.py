# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Speichern im Editor.

- Die beim Anlegen erfasste Zusammenfassung bleibt erhalten, wenn der Editor
  sie nicht mitschickt (#183).
- Ein per POST geänderter Inhalt verwirft den veralteten Kollaborationsstand,
  damit er beim nächsten Öffnen nicht gewinnt; verbundene Clients laden neu
  (#184). Unveränderter Inhalt lässt beides unberührt.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.work.motions.models import Motion
from apps.work.motions.views import editor as editor_views

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]
STALE_STATE = b"veralteter-kollaborationsstand"


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org,
        author=author,
        title="Antrag Radweg",
        summary="Kurzfassung",
        visibility="organization",
    )
    cast(Any, motion).set_content_encrypted("<p>Alt</p>")
    motion.yjs_document = STALE_STATE
    motion.save()
    return motion


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[Motion]:
    calls: list[Motion] = []
    monkeypatch.setattr(editor_views, "_broadcast_doc_reload", lambda motion, version=None: calls.append(motion))
    return calls


def save(client: Any, org: Any, motion: Motion, **fields: str) -> Any:
    data = {"action": "save", "title": motion.title, **fields}
    return client.post(f"/work/{org.slug}/documents/{motion.id}/", data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_speichern_ohne_zusammenfassung_behaelt_sie(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>")

    assert response.status_code == 200
    assert response.json()["success"] is True
    motion.refresh_from_db()
    assert motion.summary == "Kurzfassung"


@pytest.mark.django_db
def test_mitgeschickte_zusammenfassung_wird_uebernommen(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Alt</p>", summary="Neue Kurzfassung")

    motion.refresh_from_db()
    assert motion.summary == "Neue Kurzfassung"


@pytest.mark.django_db
def test_geaenderter_inhalt_verwirft_veralteten_kollaborationsstand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Neu</p>")

    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"
    assert motion.yjs_document is None
    assert [reloaded.pk for reloaded in reloads] == [motion.pk]


@pytest.mark.django_db
def test_unveraenderter_inhalt_behaelt_kollaborationsstand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Alt</p>")

    motion.refresh_from_db()
    assert motion.yjs_document is not None
    assert bytes(motion.yjs_document) == STALE_STATE
    assert reloads == []
