# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Speichern schreibt nur die eigenen Felder und prüft die Status-Sperre auf dem aktuellen Stand.

Zwischen dem Laden eines Dokuments und dem Schreiben liegt Arbeit (Bereinigung des Inhalts). Ändert in dieser Zeit
jemand Status, Sichtbarkeit oder Ordner, darf das Speichern das nicht zurückschreiben, und in ein inzwischen
freigegebenes Dokument gelangt kein Text mehr. Geprüft für den Editor (POST), die Live-Bearbeitung, das
Wiederherstellen einer Fassung und den Papierkorb.

Die Tests stellen die Reihenfolge fest her: Die gleichzeitige Änderung geschieht genau in dem Schritt, der zwischen
Laden und Schreiben liegt. Die Zeilensperre selbst wirkt nur auf PostgreSQL.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any, cast

import pytest
from channels.testing import WebsocketCommunicator
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.motions import consumers
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import DocumentFolder, Motion, MotionRevision, MotionShare
from apps.work.motions.views import editor as editor_views
from apps.work.motions.views import revisions as revision_views
from apps.work.motions.views import trash as trash_views
from apps.work.sanitize import sanitize_editor_html

RECHTE = ["motions.view", "motions.view_drafts", "motions.edit", "motions.comment"]
ALT = "<p>Alt</p>"
NEU = "<p>Neu</p>"


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="autorin@example.org")


@pytest.fixture
def leitung(org: Any, make_member: Any) -> Any:
    return make_member(org, [*RECHTE, "motions.edit_all"], email="leitung@example.org")


@pytest.fixture
def ordner(org: Any, autorin: Any) -> DocumentFolder:
    return DocumentFolder.objects.create(organization=org, name="Ablage", created_by=autorin)


@pytest.fixture
def motion(org: Any, autorin: Any) -> Motion:
    motion = Motion(organization=org, author=autorin, title="Antrag", visibility="organization")
    cast(Any, motion).set_content_encrypted(ALT)
    motion.save()
    return motion


@pytest.fixture(autouse=True)
def _ohne_neuladen(monkeypatch: pytest.MonkeyPatch) -> None:
    for modul in (editor_views, revision_views):
        monkeypatch.setattr(modul, "_broadcast_doc_reload", lambda motion, version=None: None)


def _inhalt(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted())


def _waehrend_der_bereinigung(
    monkeypatch: pytest.MonkeyPatch, modul: Any, motion: Motion, **felder: Any
) -> Callable[[], int]:
    """Die Bereinigung in ``modul`` ändert vorher das Dokument an anderer Stelle; zurück: Zahl der Aufrufe."""
    aufrufe: list[str] = []

    def bereinigen(html: str | None) -> str:
        aufrufe.append(html or "")
        Motion.objects.filter(pk=motion.pk).update(**felder)
        return sanitize_editor_html(html)

    monkeypatch.setattr(modul, "sanitize_editor_html", bereinigen)
    return lambda: len(aufrufe)


def _speichern(client: Any, org: Any, motion: Motion, **felder: str) -> Any:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    return client.post(url, {"action": "save", **felder}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


# --- Editor (POST) -----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_speichern_laesst_inzwischen_geaenderte_felder_unberuehrt(
    org: Any, autorin: Any, motion: Motion, ordner: DocumentFolder, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    aufrufe = _waehrend_der_bereinigung(
        monkeypatch, editor_views, motion, status="internal_review", visibility="private", folder=ordner
    )

    antwort = _speichern(client_for(autorin.user), org, motion, content=NEU, title="Neuer Titel")

    assert antwort.status_code == 200 and aufrufe() == 1
    assert _inhalt(motion) == NEU
    assert motion.title == "Neuer Titel"
    assert (motion.status, motion.visibility, motion.folder_id) == ("internal_review", "private", ordner.id)


@pytest.mark.django_db
def test_speichern_in_ein_inzwischen_freigegebenes_dokument_wird_abgewiesen(
    org: Any, autorin: Any, motion: Motion, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    aufrufe = _waehrend_der_bereinigung(monkeypatch, editor_views, motion, status="approved")

    antwort = _speichern(client_for(autorin.user), org, motion, content=NEU, title="Neuer Titel")

    assert antwort.status_code == 403 and aufrufe() == 1
    assert _inhalt(motion) == ALT
    assert (motion.status, motion.title) == ("approved", "Antrag")
    assert not MotionRevision.objects.filter(motion=motion).exists()


@pytest.mark.django_db
def test_gast_schreibt_freigabe_und_papierkorb_nicht_zurueck(
    org: Any, autorin: Any, motion: Motion, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    MotionShare.objects.create(motion=motion, scope="user", user=gast.user, level="edit", created_by=autorin.user)
    _waehrend_der_bereinigung(monkeypatch, editor_views, motion, status="submitted")

    antwort = _speichern(client_for(gast.user), org, motion, content=NEU)

    assert antwort.status_code == 403
    assert _inhalt(motion) == ALT and motion.status == "submitted"


@pytest.mark.django_db
def test_mit_recht_auf_alle_dokumente_bleibt_speichern_im_gesperrten_status_moeglich(
    org: Any, leitung: Any, motion: Motion, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waehrend_der_bereinigung(monkeypatch, editor_views, motion, status="approved")

    antwort = _speichern(client_for(leitung.user), org, motion, content=NEU)

    assert antwort.status_code == 200
    assert _inhalt(motion) == NEU and motion.status == "approved"


@pytest.mark.django_db
def test_papierkorb_schreibt_nur_status_und_loeschzeit(
    org: Any, autorin: Any, motion: Motion, ordner: DocumentFolder, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    echt = Motion.can_delete

    def inzwischen_verschoben(self: Motion, membership: Any) -> bool:
        Motion.objects.filter(pk=self.pk).update(visibility="private", folder=ordner)
        return echt(self, membership)

    monkeypatch.setattr(Motion, "can_delete", inzwischen_verschoben)
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})

    antwort = client_for(autorin.user).post(url, {"action": "delete"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    assert antwort.status_code == 200
    motion.refresh_from_db()
    assert motion.status == "deleted" and motion.deleted_at is not None
    assert (motion.visibility, motion.folder_id) == ("private", ordner.id)


@pytest.mark.django_db
def test_wiederherstellen_aus_dem_papierkorb_schreibt_nur_status_und_loeschzeit(
    org: Any, autorin: Any, motion: Motion, ordner: DocumentFolder, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    Motion.objects.filter(pk=motion.pk).update(status="deleted")
    echt = cast(Any, trash_views._editable_trashed_motion)

    def inzwischen_geaendert(membership: Any, motion_id: Any) -> Motion:
        geladen = echt(membership, motion_id)
        Motion.objects.filter(pk=geladen.pk).update(visibility="private", folder=ordner, title="Umbenannt")
        return cast(Motion, geladen)

    monkeypatch.setattr(trash_views, "_editable_trashed_motion", inzwischen_geaendert)
    url = reverse("work:document_restore", kwargs={"org_slug": org.slug, "motion_id": motion.id})

    antwort = client_for(autorin.user).post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    assert antwort.status_code == 200
    motion.refresh_from_db()
    assert motion.status == "draft" and motion.deleted_at is None
    assert (motion.visibility, motion.folder_id, motion.title) == ("private", ordner.id, "Umbenannt")


# --- Frühere Fassung wiederherstellen ----------------------------------------------------------------------------


def _fassung(motion: Motion, autorin: Any, inhalt: str) -> MotionRevision:
    fassung = MotionRevision(motion=motion, version=1, changed_by=autorin)
    cast(Any, fassung).set_content_encrypted(inhalt)
    fassung.save()
    return fassung


def _wiederherstellen(client: Any, org: Any, motion: Motion, fassung: MotionRevision) -> Any:
    url = reverse(
        "work:document_revision_restore",
        kwargs={"org_slug": org.slug, "motion_id": motion.id, "revision_id": fassung.id},
    )
    return client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_wiederherstellen_einer_fassung_laesst_andere_felder_unberuehrt(
    org: Any, autorin: Any, motion: Motion, ordner: DocumentFolder, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fassung = _fassung(motion, autorin, "<p>Frühere Fassung</p>")
    _waehrend_der_bereinigung(monkeypatch, revision_views, motion, visibility="private", folder=ordner)

    antwort = _wiederherstellen(client_for(autorin.user), org, motion, fassung)

    assert antwort.status_code == 200
    assert _inhalt(motion) == "<p>Frühere Fassung</p>"
    assert (motion.visibility, motion.folder_id) == ("private", ordner.id)


@pytest.mark.django_db
def test_wiederherstellen_in_ein_inzwischen_freigegebenes_dokument_wird_abgewiesen(
    org: Any, autorin: Any, motion: Motion, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fassung = _fassung(motion, autorin, "<p>Frühere Fassung</p>")
    _waehrend_der_bereinigung(monkeypatch, revision_views, motion, status="approved")

    antwort = _wiederherstellen(client_for(autorin.user), org, motion, fassung)

    assert antwort.status_code == 403
    assert _inhalt(motion) == ALT and motion.status == "approved"
    assert MotionRevision.objects.filter(motion=motion).count() == 1


# --- Live-Bearbeitung --------------------------------------------------------------------------------------------


def _live_speichern(user: Any, motion: Motion) -> bool:
    """Über die Verbindung speichern; zurück, ob der Server das Speichern bestätigt hat."""

    async def lauf() -> bool:
        communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
        communicator.scope["user"] = user
        communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
        connected, _ = await communicator.connect()
        assert connected
        assert (await communicator.receive_json_from())["type"] == "connected"
        assert (await communicator.receive_json_from())["type"] == "yjs_state"
        await communicator.send_json_to(
            {"type": "yjs_save", "data": base64.b64encode(b"neuer-zustand").decode("ascii"), "html": NEU}
        )
        bestaetigt = not await communicator.receive_nothing(timeout=1)
        await communicator.disconnect()
        return bestaetigt

    return asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_live_speichern_schreibt_nicht_in_ein_inzwischen_freigegebenes_dokument(
    autorin: Any, motion: Motion, monkeypatch: pytest.MonkeyPatch
) -> None:
    aufrufe = _waehrend_der_bereinigung(monkeypatch, consumers, motion, status="approved")

    assert not _live_speichern(autorin.user, motion)

    assert aufrufe() == 1
    assert _inhalt(motion) == ALT and motion.status == "approved"
    assert motion.get_yjs_state() is None


@pytest.mark.django_db(transaction=True)
def test_live_speichern_mit_recht_auf_alle_dokumente_schreibt_weiter(
    leitung: Any, motion: Motion, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waehrend_der_bereinigung(monkeypatch, consumers, motion, status="approved")

    assert _live_speichern(leitung.user, motion)

    assert _inhalt(motion) == NEU and motion.status == "approved"


@pytest.mark.django_db(transaction=True)
def test_live_speichern_ohne_gleichzeitigen_statuswechsel_schreibt_wie_bisher(autorin: Any, motion: Motion) -> None:
    assert _live_speichern(autorin.user, motion)

    assert _inhalt(motion) == NEU
    assert motion.get_yjs_state() == b"neuer-zustand"
