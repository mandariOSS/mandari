# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Zustand des gemeinsamen Editors (Yjs) liegt mit dem Organisationsschlüssel verschlüsselt vor.

- Über die WebSocket-Verbindung gespeichert und wieder geladen ist er Byte für Byte derselbe,
  auch groß (eingebettete Bilder) und mit beliebigen Bytes.
- In der Datenbank steht nur Geheimtext; die frühere Klartextspalte enthält die leere Markierung.
- Übergang zur früheren Klartextspalte: Daten einer älteren Version sind die jüngeren; hat sie
  den Zustand verworfen, wird der verschlüsselte nicht mehr ausgeliefert (#184).
- Die Migration work/0057 verschlüsselt den Bestand, ist wiederholbar und bricht ohne gültigen
  Hauptschlüssel ab, ohne etwas zu ändern.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import secrets
from typing import Any, cast

import pytest
from channels.testing import WebsocketCommunicator
from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.common.encryption import TenantEncryption
from apps.common.tests.factories import OrganizationFactory
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion

MIGRATION = importlib.import_module("apps.work.migrations.0057_editorzustand_verschluesselt")
VORHER = ("work", "0056_anhaenge_zufallspfade")
NACHHER = ("work", "0057_editorzustand_verschluesselt")
PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]

#: Wie ein echter Zustand: beliebige Bytes, darin ein wiedererkennbarer Inhalt
INHALT = b"vertraulicher-antragstext-" + secrets.token_hex(8).encode()
ZUSTAND = bytes(range(256)) + INHALT + secrets.token_bytes(4096)


def _roh(motion: Motion) -> tuple[bytes | None, bytes | None]:
    """(verschlüsselt, frühere Klartextspalte) direkt aus der Datenbank."""
    verschluesselt, alt = Motion.objects.filter(pk=motion.pk).values_list(*Motion.YJS_FIELDS).get()
    return (bytes(verschluesselt) if verschluesselt is not None else None, bytes(alt) if alt is not None else None)


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(organization=org, author=author, title="Antrag", visibility="organization")
    motion.set_content_encrypted("<p>Antrag</p>")  # type: ignore[attr-defined]
    motion.save()
    return motion


# =============================================================================
# Speichern und Laden über die WebSocket-Verbindung
# =============================================================================


def _communicator(user: Any, motion: Motion) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    return communicator


async def _verbinden(user: Any, motion: Motion) -> tuple[WebsocketCommunicator, bytes | None]:
    communicator = _communicator(user, motion)
    connected, _ = await communicator.connect()
    assert connected
    assert (await communicator.receive_json_from())["type"] == "connected"
    nachricht = await communicator.receive_json_from()
    assert nachricht["type"] == "yjs_state"
    return communicator, base64.b64decode(nachricht["data"]) if nachricht["data"] else None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("zustand", [ZUSTAND, ZUSTAND * 400], ids=["klein", "gross-1_6MB"])
def test_zustand_bleibt_nach_speichern_und_laden_identisch(author: Any, motion: Motion, zustand: bytes) -> None:
    async def lauf() -> bytes | None:
        communicator, anfang = await _verbinden(author.user, motion)
        assert anfang is None
        daten = base64.b64encode(zustand).decode("ascii")
        await communicator.send_json_to({"type": "yjs_save", "data": daten, "html": "<p>Antrag geändert</p>"})
        assert (await communicator.receive_json_from())["type"] == "yjs_saved"
        await communicator.disconnect()

        communicator, geladen = await _verbinden(author.user, motion)
        await communicator.disconnect()
        return geladen

    geladen = asyncio.run(lauf())
    assert geladen == zustand
    verschluesselt, alt = _roh(motion)
    assert alt == b""
    assert verschluesselt is not None and INHALT not in verschluesselt
    assert TenantEncryption(motion.organization).decrypt_bytes(verschluesselt) == zustand


# =============================================================================
# Modell: Übergang zur früheren Klartextspalte
# =============================================================================


@pytest.mark.django_db
class TestModell:
    def test_speichern_und_laden(self, motion: Motion) -> None:
        motion.set_yjs_state(ZUSTAND)
        motion.save()
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() == ZUSTAND
        verschluesselt, alt = _roh(motion)
        assert alt == b"" and verschluesselt is not None and INHALT not in verschluesselt

    def test_verwerfen_leert_beide_spalten(self, motion: Motion) -> None:
        motion.set_yjs_state(ZUSTAND)
        motion.save()
        motion.set_yjs_state(None)
        motion.save()
        assert _roh(motion) == (None, None)
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() is None

    def test_zustand_einer_aelteren_version_ist_der_juengere(self, motion: Motion) -> None:
        motion.set_yjs_state(ZUSTAND)
        motion.save()
        Motion.objects.filter(pk=motion.pk).update(yjs_document_legacy=b"nach-rueckfall-gespeichert")
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() == b"nach-rueckfall-gespeichert"

    def test_von_aelterer_version_verworfener_zustand_wird_nicht_ausgeliefert(self, motion: Motion) -> None:
        """Eine ältere Version setzt beim Speichern ohne Verbindung nur die alte Spalte auf NULL (#184)."""
        motion.set_yjs_state(ZUSTAND)
        motion.save()
        Motion.objects.filter(pk=motion.pk).update(yjs_document_legacy=None)
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() is None

    def test_mit_fremdem_schluessel_nicht_lesbar(self, motion: Motion, caplog: pytest.LogCaptureFixture) -> None:
        fremd = TenantEncryption(cast(Any, OrganizationFactory)(name="Fremd", slug="fremd")).encrypt_bytes(ZUSTAND)
        Motion.objects.filter(pk=motion.pk).update(yjs_document_encrypted=fremd, yjs_document_legacy=b"")
        with caplog.at_level(logging.WARNING):
            assert Motion.objects.get(pk=motion.pk).get_yjs_state() is None
        assert "nicht lesbar" in caplog.text
        assert INHALT.decode() not in caplog.text


# =============================================================================
# Migration work/0057
# =============================================================================


def _altbestand(motion: Motion, zustand: bytes = ZUSTAND) -> None:
    """Stand vor der Migration: Klartext in der früheren Spalte, nichts verschlüsselt."""
    Motion.objects.filter(pk=motion.pk).update(yjs_document_legacy=zustand, yjs_document_encrypted=None)


@pytest.mark.django_db
class TestMigration:
    def test_verschluesselt_den_bestand(self, motion: Motion, org: Any, author: Any) -> None:
        ohne = Motion.objects.create(organization=org, author=author, title="Ohne Zustand")
        _altbestand(motion)
        MIGRATION.encrypt_editor_states(django_apps, None)
        verschluesselt, alt = _roh(motion)
        assert alt == b"" and verschluesselt is not None and INHALT not in verschluesselt
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() == ZUSTAND
        assert _roh(ohne) == (None, None)

    def test_wiederholbar(self, motion: Motion) -> None:
        _altbestand(motion)
        MIGRATION.encrypt_editor_states(django_apps, None)
        erster_lauf = _roh(motion)
        MIGRATION.encrypt_editor_states(django_apps, None)
        assert _roh(motion) == erster_lauf

    def test_seitenweise_ueber_mehrere_organisationen(self, org: Any, author: Any, make_member: Any) -> None:
        andere = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
        andere_autorin = make_member(andere, PERMISSIONS, email="andere@example.org")
        dokumente = [
            Motion.objects.create(organization=o, author=a, title=f"Antrag {i}")
            for i in range(MIGRATION.PAGE_SIZE + 3)
            for o, a in ((org, author), (andere, andere_autorin))
        ]
        for nummer, dokument in enumerate(dokumente):
            _altbestand(dokument, ZUSTAND + str(nummer).encode())
        MIGRATION.encrypt_editor_states(django_apps, None)
        for nummer, dokument in enumerate(dokumente):
            assert Motion.objects.get(pk=dokument.pk).get_yjs_state() == ZUSTAND + str(nummer).encode()

    @pytest.mark.parametrize("schluessel", ["", base64.b64encode(b"x" * 24).decode()], ids=["fehlt", "zu-kurz"])
    def test_ohne_gueltigen_hauptschluessel_bricht_ab_ohne_etwas_zu_aendern(
        self, motion: Motion, schluessel: str
    ) -> None:
        _altbestand(motion)
        with override_settings(ENCRYPTION_MASTER_KEY=schluessel), pytest.raises(RuntimeError) as fehler:
            MIGRATION.encrypt_editor_states(django_apps, None)
        assert "ENCRYPTION_MASTER_KEY" in str(fehler.value)
        assert _roh(motion) == (None, ZUSTAND)

    def test_ohne_zustaende_braucht_es_keinen_schluessel(self, motion: Motion) -> None:
        with override_settings(ENCRYPTION_MASTER_KEY=""):
            MIGRATION.encrypt_editor_states(django_apps, None)


@pytest.mark.django_db(transaction=True)
def test_migration_im_schema(motion: Motion) -> None:
    """Echter Lauf über die Migrationsgeschichte: Zustand im Klartext vorher, verschlüsselt nachher."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps.get_model("work", "Motion")
        alt.objects.filter(pk=motion.pk).update(yjs_document=ZUSTAND)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        verschluesselt, spalte = _roh(motion)
        assert spalte == b"" and verschluesselt is not None and INHALT not in verschluesselt
        assert Motion.objects.get(pk=motion.pk).get_yjs_state() == ZUSTAND
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
