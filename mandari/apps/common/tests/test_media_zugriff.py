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
    "berichte/verfuegbarkeit-2026-09.md",
    "tasks/attachments/aufgabe.txt",
    "exports/org-1/mitglied-1/dsgvo-export-1.json",
    "oparl_files/musterstadt/2026/vorlage.pdf",
    "dokumente/musterstadt/2026/vorlage.pdf",
    "demo/logo.png",
    "avatars/bild.png",
    "sonstiges/notiz.txt",
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
    # Berichte der Zeitpläne (REPORTS_ROOT, Issue #516) nie über eine URL
    "/media/berichte/verfuegbarkeit-2026-09.md",
    # Datenexporte nur über work:export_download, Dokument-Cache nur über den Datei-Proxy
    "/media/exports/org-1/mitglied-1/dsgvo-export-1.json",
    "/media/Exports/org-1/mitglied-1/dsgvo-export-1.json",
    "/media/sonstiges/../exports/org-1/mitglied-1/dsgvo-export-1.json",
    "/media/oparl_files/musterstadt/2026/vorlage.pdf",
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
    # Weder öffentlich noch geschützt: nur angemeldet, nie im öffentlichen Cache
    assert client.get("/media/sonstiges/notiz.txt").status_code == 404
    client.force_login(cast(Any, UserFactory)())
    antwort = client.get("/media/sonstiges/notiz.txt")
    assert _geliefert(antwort)
    assert antwort["Cache-Control"] == "private, no-store"


def test_work_anhaenge_auch_angemeldet_nur_ueber_ansichten(media: Path, client: Client) -> None:
    # Aufgaben-Anhänge laufen nur über zugriffsgeprüfte Download-Ansichten (apps/work/files.py)
    client.force_login(cast(Any, UserFactory)())
    assert client.get("/media/tasks/attachments/aufgabe.txt").status_code == 404


def test_eingestellte_ablagen_unter_media_root_sind_geschuetzt(media: Path, client: Client, settings: Any) -> None:
    """Liegt der Dokument-Cache in einem anders benannten Verzeichnis unter MEDIA_ROOT, gilt es als geschützt."""
    client.force_login(cast(Any, UserFactory)())
    url = "/media/dokumente/musterstadt/2026/vorlage.pdf"
    settings.OPARL_FILES_ROOT = str(media / "elsewhere")
    assert _geliefert(client.get(url))

    settings.OPARL_FILES_ROOT = str(media / "dokumente")
    antwort = client.get(url)
    assert not _geliefert(antwort)
    assert antwort.status_code == 404


def test_ablagen_ausserhalb_von_media_root_aendern_nichts(
    media: Path, client: Client, settings: Any, tmp_path_factory: Any
) -> None:
    """Ablagen außerhalb von MEDIA_ROOT (Container: eigenes Volume) schränken ``/media/`` nicht ein."""
    settings.OPARL_FILES_ROOT = str(tmp_path_factory.mktemp("files"))
    settings.AUDIT_ARCHIVE_ROOT = str(tmp_path_factory.mktemp("archiv"))
    assert _geliefert(client.get("/media/demo/logo.png"))
    client.force_login(cast(Any, UserFactory)())
    assert _geliefert(client.get("/media/sonstiges/notiz.txt"))


ARCHIVPAKET = "session/3f6c1d2e/20261101-020000-protokoll-2026-10.zip"


@pytest.fixture
def archivpaket(media: Path) -> str:
    """Archivpaket des Protokolls, wie es ohne eigenes Verzeichnis unter MEDIA_ROOT läge."""
    ziel = media / ARCHIVPAKET
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(INHALT)
    return f"/media/{ARCHIVPAKET}"


@pytest.mark.parametrize("einstellung", ["AUDIT_ARCHIVE_ROOT", "REPORTS_ROOT", "OPARL_FILES_ROOT"])
def test_ablage_gleich_media_root_liefert_nur_oeffentliches(
    media: Path, client: Client, settings: Any, archivpaket: str, einstellung: str
) -> None:
    """Ohne eigenes Verzeichnis liegen die Dateien der Ablage zwischen den Uploads: nichts davon geht hinaus."""
    client.force_login(cast(Any, UserFactory)())
    assert _geliefert(client.get(archivpaket)), "mit eigener Ablage ist der Pfad ein gewöhnlicher Upload"

    setattr(settings, einstellung, str(media))

    for url in (archivpaket, "/media/sonstiges/notiz.txt"):
        antwort = client.get(url)
        assert not _geliefert(antwort), url
        assert antwort.status_code == 404
    assert _geliefert(client.get("/media/demo/logo.png"))
    assert _geliefert(Client().get("/media/avatars/bild.png"))


def test_archivspeicher_ueber_alias_ist_geschuetzt(
    media: Path, client: Client, settings: Any, archivpaket: str
) -> None:
    """``AUDIT_ARCHIVE_STORAGE`` nennt einen lokalen Speicher: Sein Verzeichnis zählt wie ``AUDIT_ARCHIVE_ROOT``."""
    client.force_login(cast(Any, UserFactory)())

    # Der Standardspeicher ist MEDIA_ROOT selbst
    settings.AUDIT_ARCHIVE_STORAGE = "default"
    assert client.get(archivpaket).status_code == 404
    assert _geliefert(client.get("/media/demo/logo.png"))

    # Ein eigener lokaler Speicher unter MEDIA_ROOT: sein Verzeichnis ist geschützt, alles andere wie zuvor
    settings.STORAGES = {
        **settings.STORAGES,
        "archiv": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(media / "session")},
        },
    }
    settings.AUDIT_ARCHIVE_STORAGE = "archiv"
    assert client.get(archivpaket).status_code == 404
    assert _geliefert(client.get("/media/sonstiges/notiz.txt"))

    # Ein unbekannter Alias stört die Auslieferung nicht
    settings.AUDIT_ARCHIVE_STORAGE = "gibt-es-nicht"
    assert _geliefert(client.get("/media/sonstiges/notiz.txt"))


def test_umweg_auf_oeffentliche_datei_ist_nicht_oeffentlich_gecacht(media: Path, client: Client) -> None:
    """Ein Umweg über ``..`` wird nicht ausgeliefert – auch nicht zu einer öffentlichen Datei."""
    antwort = client.get("/media/avatars/../demo/logo.png")
    assert antwort.status_code == 404
