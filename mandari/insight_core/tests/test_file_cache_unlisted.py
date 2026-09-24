# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dokument-Cache nur für gelistete Kommunen; ``prune_file_cache --unlisted`` räumt Piloten ab.

Im September 2026 luden ausgeblendete Pilotquellen ihr ganzes Archiv in den Cache (rund 46 GB,
knapp die Hälfte), obwohl sie niemand im Portal sieht.
"""

from __future__ import annotations

import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache

pytestmark = pytest.mark.django_db


def _kommune(name: str, gelistet: bool) -> OParlBody:
    source = OParlSource.objects.create(name=name, url=f"https://ris.example/{uuid.uuid4()}/system")
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris.example/bodies/{uuid.uuid4()}", name=name, is_listed=gelistet
    )


def _datei(body: OParlBody, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example/files/{uuid.uuid4()}",
        name="Vorlage.pdf",
        download_url="https://ris.example/getfile?id=1",
        **felder,
    )


def _zwischengespeichert(body: OParlBody) -> OParlFile:
    datei = _datei(body)
    pfad = file_cache.target_path(datei)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(b"%PDF-1.4 " + b"x" * 2048)
    datei.local_path = str(pfad)
    datei.local_status = "ok"
    datei.save(update_fields=["local_path", "local_status"])
    return datei


def test_nur_gelistete_kommunen_werden_nachgeladen() -> None:
    gelistet = _datei(_kommune("Gelistet", True))
    _datei(_kommune("Pilot", False))
    assert list(file_cache.pending_queryset()) == [gelistet]
    assert file_cache.caches_body(gelistet.body)
    assert not file_cache.caches_body(_kommune("Pilot 2", False))


def test_prune_leert_nur_ausgeblendete_kommunen(tmp_path: Path, settings: Any) -> None:
    settings.OPARL_FILES_ROOT = str(tmp_path)
    oeffentlich = _zwischengespeichert(_kommune("Gelistet", True))
    pilot = _zwischengespeichert(_kommune("Pilot", False))
    pilot_verzeichnis = tmp_path / file_cache.body_dir_name(pilot.body)

    out = StringIO()
    call_command("prune_file_cache", unlisted=True, dry_run=True, stdout=out)
    assert "würden frei" in out.getvalue()
    assert pilot_verzeichnis.is_dir()
    pilot.refresh_from_db()
    assert pilot.local_status == "ok"

    call_command("prune_file_cache", unlisted=True, stdout=StringIO())
    assert not pilot_verzeichnis.exists()
    pilot.refresh_from_db()
    assert (pilot.local_status, pilot.local_path) == ("none", None)

    oeffentlich.refresh_from_db()
    assert oeffentlich.local_status == "ok"
    assert Path(oeffentlich.local_path or "").is_file()


def test_prune_verlangt_ausdruecklich_unlisted() -> None:
    with pytest.raises(CommandError):
        call_command("prune_file_cache", stdout=StringIO())
