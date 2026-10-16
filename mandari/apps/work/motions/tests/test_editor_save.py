# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Speichern im Editor.

- Die beim Anlegen erfasste Zusammenfassung bleibt erhalten, wenn der Editor
  sie nicht mitschickt (#183).
- Ein per POST geänderter Inhalt verwirft den veralteten Kollaborationsstand,
  damit er beim nächsten Öffnen nicht gewinnt; verbundene Clients laden neu
  (#184). Unveränderter Inhalt lässt beides unberührt.
- Speichern ohne Kollaborationsverbindung schickt den Fingerabdruck des Stands
  mit, von dem der Editor ausgeht. Passt er nicht mehr, antwortet der Server mit
  409 statt still zu überschreiben; nur ``force`` überschreibt bewusst (#184).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, content_fingerprint
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
    motion.set_yjs_state(STALE_STATE)
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
    assert motion.get_yjs_state() is None
    assert [reloaded.pk for reloaded in reloads] == [motion.pk]


@pytest.mark.django_db
def test_unveraenderter_inhalt_behaelt_kollaborationsstand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Alt</p>")

    motion.refresh_from_db()
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_editor_bekommt_fingerabdruck_des_stands(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = client_for(author.user).get(f"/work/{org.slug}/documents/{motion.id}/")

    assert response.status_code == 200
    assert response.context["editor_config"]["contentHash"] == content_fingerprint("<p>Alt</p>")


@pytest.mark.django_db
def test_veralteter_stand_wird_nicht_still_ueberschrieben(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>", base_content_hash="veraltet")

    assert response.status_code == 409
    daten = response.json()
    assert daten["error"] == "conflict"
    assert daten["content_hash"] == content_fingerprint("<p>Alt</p>")
    assert "an anderer Stelle geändert" in daten["message"]
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_passender_stand_speichert_und_liefert_neuen_fingerabdruck(
    org: Any, author: Any, motion: Motion, client_for: Any
) -> None:
    response = save(
        client_for(author.user),
        org,
        motion,
        content="<p>Neu</p>",
        base_content_hash=content_fingerprint("<p>Alt</p>"),
    )

    assert response.status_code == 200
    assert response.json()["content_hash"] == content_fingerprint("<p>Neu</p>")
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"


@pytest.mark.django_db
def test_bewusstes_ueberschreiben_mit_force(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>", base_content_hash="veraltet", force="1")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"


@pytest.mark.django_db
def test_unveraenderter_inhalt_ist_kein_konflikt(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Alt</p>", base_content_hash="veraltet")

    assert response.status_code == 200


@pytest.mark.django_db
def test_ohne_inhalt_nur_titel_typ_und_briefpapier(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Mit Live-Zusammenarbeit schickt der Editor keinen Text (#1018): Text und Yjs-Zustand bleiben unberührt."""
    from apps.work.motions.models import MotionType, OrganizationLetterhead

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
    revisionen_vorher = motion.revisions.count()

    response = save(
        client_for(author.user),
        org,
        motion,
        title="Neuer Titel",
        document_type_id=str(typ.id),
        letterhead_id=str(briefkopf.id),
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    motion.refresh_from_db()
    assert motion.title == "Neuer Titel"
    assert motion.document_type_id == typ.id
    assert motion.letterhead_id == briefkopf.id
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.get_yjs_state() == STALE_STATE
    assert motion.summary == "Kurzfassung"
    assert motion.revisions.count() == revisionen_vorher
    assert reloads == []


@pytest.mark.django_db
def test_ohne_inhalt_ueberschreibt_kein_gleichzeitiges_sichern(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sichert die Verbindung während des POST einen neuen Text, schreibt der POST ihn nicht mit dem alten zurück."""
    from apps.work.motions.views.editor import DocumentEditorView

    echt: Any = DocumentEditorView._get_access_level

    def mit_gleichzeitigem_sichern(self: Any, geladen: Motion) -> str:
        # Nach dem Laden im View: der Consumer speichert den neuen Kollaborationsstand
        neu = Motion.objects.get(pk=geladen.pk)
        cast(Any, neu).set_content_encrypted("<p>Neu über Yjs</p>")
        neu.set_yjs_state(b"neuer-zustand")
        neu.save()
        return str(echt(self, geladen))

    monkeypatch.setattr(DocumentEditorView, "_get_access_level", mit_gleichzeitigem_sichern)

    response = save(client_for(author.user), org, motion, title="Titel neu")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert motion.title == "Titel neu"
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu über Yjs</p>"
    assert motion.get_yjs_state() == b"neuer-zustand"


@pytest.mark.django_db
def test_fremdes_formular_leert_nichts(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Postet ein anderes Formular versehentlich an den Editor (Teilen-Dialog mit leerem Ziel), bleibt alles (#1018)."""
    from apps.work.motions.models import MotionType

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    motion.document_type = typ
    motion.save(update_fields=["document_type"])

    response = client_for(author.user).post(
        f"/work/{org.slug}/documents/{motion.id}/", {"visibility": "organization"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 400
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.document_type_id == typ.id
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_nur_titel_laesst_typ_und_briefpapier(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    from apps.work.motions.models import MotionType

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    motion.document_type = typ
    motion.save(update_fields=["document_type"])

    response = save(client_for(author.user), org, motion, title="Nur der Titel")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert motion.title == "Nur der Titel"
    assert motion.document_type_id == typ.id
