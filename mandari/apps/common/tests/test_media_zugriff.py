# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Medienauslieferung über ``/media/``: Die Zugriffsregeln (geschützte Präfixe nie,
öffentliche Präfixe ohne Anmeldung, alles Übrige nur angemeldet) gelten für den
Pfad, der tatsächlich ausgeliefert würde – nicht für eine Schreibweise davon.
Pfade mit Punkt-Segmenten, leeren Segmenten, Backslashes oder weiteren
Prozent-Kodierungen werden deshalb gar nicht erst ausgeliefert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from django.test import Client

from apps.common.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

INHALT = b"GEHEIMER-INHALT"

#: Dateien unter geschützten bzw. anmeldepflichtigen Präfixen
DATEIEN = (
    "session/files/2026/09/geheim.txt",
    "motions/documents/2026/09/antrag.txt",
    "audit_archive/paket-2026.json",
    "tasks/attachments/aufgabe.txt",
    "demo/logo.png",
    "avatars/bild.png",
)


@pytest.fixture
def media(tmp_path: Path, settings: Any) -> Path:
    settings.MEDIA_ROOT = tmp_path
    for relativ in DATEIEN:
        ziel = tmp_path / relativ
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(INHALT)
    return tmp_path


def _geliefert(antwort: Any) -> bool:
    if antwort.status_code != 200:
        return False
    return INHALT in b"".join(antwort.streaming_content)


ANONYM_GESPERRT = (
    # Umwege über öffentliche Präfixe zu geschützten Dateien
    "/media/avatars/../session/files/2026/09/geheim.txt",
    "/media/avatars/%2e%2e/session/files/2026/09/geheim.txt",
    "/media/avatars/%2E%2E/session/files/2026/09/geheim.txt",
    "/media/avatars/..%2fsession/files/2026/09/geheim.txt",
    "/media/avatars/%2e%2e%2fsession%2ffiles%2f2026%2f09%2fgeheim.txt",
    "/media/demo/../motions/documents/2026/09/antrag.txt",
    "/media/demo/%2e%2e/audit_archive/paket-2026.json",
    "/media/demo/./../tasks/attachments/aufgabe.txt",
    # Backslash als Trenner (Windows-Pfade) und doppelt kodierte Varianten
    "/media/avatars/..%5csession%5cfiles%5c2026%5c09%5cgeheim.txt",
    "/media/avatars/%252e%252e/session/files/2026/09/geheim.txt",
    "/media/avatars/..%252fsession/files/2026/09/geheim.txt",
    # Leere und Punkt-Segmente vor geschützten Präfixen
    "/media//session/files/2026/09/geheim.txt",
    "/media/./session/files/2026/09/geheim.txt",
    "/media/session//files/2026/09/geheim.txt",
    # Nur angemeldet: auch nicht über den Umweg eines öffentlichen Präfixes
    "/media/avatars/../tasks/attachments/aufgabe.txt",
    "/media/demo/%2e%2e/tasks/attachments/aufgabe.txt",
)


@pytest.mark.parametrize("url", ANONYM_GESPERRT)
def test_umwege_liefern_anonym_nichts(media: Path, client: Client, url: str) -> None:
    antwort = client.get(url)
    assert not _geliefert(antwort), url
    assert antwort.status_code == 404


ANGEMELDET_GESPERRT = (
    "/media/session//files/2026/09/geheim.txt",
    "/media/./session/files/2026/09/geheim.txt",
    "/media/avatars/../session/files/2026/09/geheim.txt",
    "/media/tasks/../motions/documents/2026/09/antrag.txt",
    "/media/tasks/%2e%2e/audit_archive/paket-2026.json",
    "/media/Session/files/2026/09/geheim.txt",
    "/media/SESSION/FILES/2026/09/geheim.txt",
)


@pytest.mark.parametrize("url", ANGEMELDET_GESPERRT)
def test_geschuetzte_praefixe_auch_angemeldet_nie(media: Path, client: Client, url: str) -> None:
    client.force_login(cast(Any, UserFactory)())
    antwort = client.get(url)
    assert not _geliefert(antwort), url
    assert antwort.status_code == 404


def test_oeffentliche_dateien_weiter_anonym(media: Path, client: Client) -> None:
    antwort = client.get("/media/demo/logo.png")
    assert _geliefert(antwort)
    assert antwort["Cache-Control"] == "public, max-age=3600"


def test_anmeldepflichtige_dateien_angemeldet_weiter(media: Path, client: Client) -> None:
    assert client.get("/media/tasks/attachments/aufgabe.txt").status_code == 404
    client.force_login(cast(Any, UserFactory)())
    antwort = client.get("/media/tasks/attachments/aufgabe.txt")
    assert _geliefert(antwort)
    assert antwort["Cache-Control"] == "private, no-store"


def test_umweg_auf_oeffentliche_datei_ist_nicht_oeffentlich_gecacht(media: Path, client: Client) -> None:
    """Ein Umweg über ``..`` wird nicht ausgeliefert – auch nicht zu einer öffentlichen Datei."""
    antwort = client.get("/media/avatars/../demo/logo.png")
    assert antwort.status_code == 404
