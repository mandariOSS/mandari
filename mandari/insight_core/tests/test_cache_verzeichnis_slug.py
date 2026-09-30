# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ein neuer Slug verschiebt den Dokument-Cache nicht (Issue #373).

Der Cache leitete seinen Verzeichnisnamen aus Slug bzw. Kurznamen ab. Mit einem neuen Slug wären neue
Downloads in ein zweites Verzeichnis gewandert, und ``prune_file_cache --unlisted`` hätte mit dem neuen
Namen gerechnet – und so das Verzeichnis einer gelisteten Kommune als „ungeteilt“ gelöscht, wenn eine
ausgeblendete Kommune denselben alten Namen trug. Jetzt wird der Name einmal festgeschrieben.
"""

from __future__ import annotations

import importlib
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache

pytestmark = pytest.mark.django_db
MIGRATION = ("insight_core", "0039_oparlbody_file_cache_dir")


@pytest.fixture(autouse=True)
def _cache_wurzel(tmp_path: Path, settings: Any) -> Path:
    settings.OPARL_FILES_ROOT = str(tmp_path)
    return tmp_path


def _kommune(name: str, *, short_name: str | None = None, gelistet: bool = True, slug: str | None = None) -> OParlBody:
    source = OParlSource.objects.create(name=name, url=f"https://ris.example/{uuid.uuid4()}/system")
    return OParlBody.objects.create(
        source=source,
        external_id=f"https://ris.example/bodies/{uuid.uuid4()}",
        name=name,
        short_name=short_name,
        slug=slug,
        is_listed=gelistet,
    )


def _datei(body: OParlBody) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example/files/{uuid.uuid4()}",
        name="Vorlage.pdf",
        mime_type="application/pdf",
        download_url="https://ris.example/getfile?id=1",
    )


def _abgelegt(body: OParlBody) -> OParlFile:
    """Datei so ablegen, wie es der Cache beim Download tut."""
    datei = _datei(OParlBody.objects.get(pk=body.pk))
    file_cache.store_bytes(datei, b"%PDF-1.4 " + b"x" * 2048)
    datei.refresh_from_db()
    return datei


def _verzeichnis(datei: OParlFile, root: Path) -> str:
    return Path(datei.local_path or "").relative_to(root).parts[0]


def _slug_setzen(body: OParlBody, slug: str) -> None:
    """Slug so ändern wie der Admin: geladenes Objekt, vollständiges Speichern."""
    body = OParlBody.objects.get(pk=body.pk)
    body.slug = slug
    body.save()


def test_neuer_slug_legt_keine_zweite_ablage_an(_cache_wurzel: Path) -> None:
    koeln = _kommune("Stadt Köln", short_name="Köln")
    vorher = _abgelegt(koeln)
    assert _verzeichnis(vorher, _cache_wurzel) == "koeln"

    _slug_setzen(koeln, "stadt-koeln")
    nachher = _abgelegt(koeln)

    assert _verzeichnis(nachher, _cache_wurzel) == "koeln", "neue Downloads landen im bisherigen Verzeichnis"
    assert file_cache.local_file(vorher) is not None, "vorhandene Kopie wird weiter gefunden"
    assert not file_cache.pending_queryset().filter(pk=vorher.pk).exists(), "nichts wird neu geladen"


def test_geaenderter_kurzname_im_ris_verschiebt_nichts(_cache_wurzel: Path) -> None:
    """Der Ingestor aktualisiert den Kurznamen am Django-Modell vorbei – der Name bleibt festgeschrieben."""
    bonn = _kommune("Bundesstadt Bonn", short_name="Bonn")
    vorher = _abgelegt(bonn)
    OParlBody.objects.filter(pk=bonn.pk).update(short_name="Stadt Bonn")

    assert _verzeichnis(_abgelegt(bonn), _cache_wurzel) == _verzeichnis(vorher, _cache_wurzel) == "bonn"


def test_prune_loescht_nach_neuem_slug_nichts_von_gelisteten(_cache_wurzel: Path) -> None:
    """Pilot mit demselben alten Verzeichnisnamen: Das Verzeichnis ist geteilt und bleibt."""
    koeln = _kommune("Stadt Köln", short_name="Köln")
    gelistet = _abgelegt(koeln)
    _slug_setzen(koeln, "koeln-stadt")
    # Bestand aus der Zeit, als auch ausgeblendete Kommunen zwischengespeichert wurden
    pilot = _kommune("Köln (Pilot)", short_name="Köln", gelistet=False)
    file_cache.store_bytes(_datei(pilot), b"%PDF-1.4 pilot")

    out = StringIO()
    call_command("prune_file_cache", unlisted=True, stdout=out)

    assert "geteilt" in out.getvalue()
    gelistet.refresh_from_db()
    assert gelistet.local_status == "ok"
    assert Path(gelistet.local_path or "").is_file()


def test_prune_schont_tatsaechliche_ablage_gelisteter(_cache_wurzel: Path) -> None:
    """Liegt eine Datei einer gelisteten Kommune anderswo als ihr Verzeichnisname sagt, bleibt sie trotzdem."""
    koeln = _kommune("Stadt Köln", short_name="Köln", slug="stadt-koeln")
    OParlBody.objects.filter(pk=koeln.pk).update(file_cache_dir="stadt-koeln")
    datei = _datei(koeln)
    abweichend = _cache_wurzel / "koeln" / "2026" / f"{datei.id}.pdf"
    abweichend.parent.mkdir(parents=True)
    abweichend.write_bytes(b"%PDF-1.4 alt")
    OParlFile.objects.filter(pk=datei.pk).update(local_path=str(abweichend), local_status="ok")
    pilot = _kommune("Köln (Pilot)", short_name="Köln", gelistet=False)
    file_cache.store_bytes(_datei(pilot), b"%PDF-1.4 pilot")

    call_command("prune_file_cache", unlisted=True, stdout=StringIO())

    assert abweichend.is_file()


def test_prune_leert_weiter_ungeteilte_verzeichnisse(_cache_wurzel: Path) -> None:
    _abgelegt(_kommune("Münster", short_name="Münster", slug="muenster"))
    pilot = _kommune("Pilotstadt", gelistet=False)
    datei = _datei(pilot)
    file_cache.store_bytes(datei, b"%PDF-1.4 pilot")

    call_command("prune_file_cache", unlisted=True, stdout=StringIO())

    assert not (_cache_wurzel / "pilotstadt").exists()
    datei.refresh_from_db()
    assert (datei.local_status, datei.local_path) == ("none", None)


class TestFestschreiben:
    def test_erstes_ablegen_schreibt_den_namen_fest(self) -> None:
        body = _kommune("Stadt Darmstadt", short_name="Darmstadt")
        assert OParlBody.objects.get(pk=body.pk).file_cache_dir is None
        _abgelegt(body)
        assert OParlBody.objects.get(pk=body.pk).file_cache_dir == "darmstadt"

    def test_speichern_schreibt_den_stand_vor_der_aenderung_fest(self) -> None:
        """Kommune ohne festgeschriebenen Namen (etwa vom Ingestor angelegt): der alte Name gilt."""
        body = _kommune("Stadt Münster", short_name="Münster")
        _slug_setzen(body, "muenster-stadt")
        assert OParlBody.objects.get(pk=body.pk).file_cache_dir == "muenster"

    def test_veraltetes_objekt_ueberschreibt_den_namen_nicht(self) -> None:
        body = _kommune("Stadt Bonn", short_name="Bonn")
        veraltet = OParlBody.objects.get(pk=body.pk)
        _abgelegt(body)  # schreibt „bonn“ fest
        veraltet.display_name = "Bonn"
        veraltet.save()
        assert OParlBody.objects.get(pk=body.pk).file_cache_dir == "bonn"

    def test_zu_langer_name_wird_nicht_festgeschrieben(self) -> None:
        """Umschriebene Umlaute können die Spaltenlänge sprengen – Speichern darf daran nicht scheitern."""
        body = _kommune("ä" * 128)
        body = OParlBody.objects.get(pk=body.pk)
        body.display_name = "Lang"
        body.save()
        assert body.file_cache_dir is None
        assert OParlBody.objects.get(pk=body.pk).file_cache_dir is None

    def test_festgeschriebener_name_wird_nie_ersetzt(self) -> None:
        body = _kommune("Stadt Köln", short_name="Köln")
        OParlBody.objects.filter(pk=body.pk).update(file_cache_dir="koeln-alt")
        body.refresh_from_db()
        assert file_cache.pin_body_dir(body) == "koeln-alt"
        body.file_cache_dir = None
        assert file_cache.pin_body_dir(body) == "koeln-alt"
        assert file_cache.body_dir_name(body) == "koeln-alt"


def test_migration_schreibt_die_bisherigen_namen_fest() -> None:
    """Die Datenmigration übernimmt genau den Namen, den der Cache bisher verwendet hat."""
    faelle = {
        _kommune("Demo", slug="musterstadt-demo").pk: "musterstadt-demo",
        _kommune("Stadt Münster", short_name="Münster").pk: "muenster",
        _kommune("Bezirksregierung Köln").pk: "bezirksregierung-koeln",
        _kommune("Straße ß").pk: "strasse-ss",
    }
    ohne_namen = _kommune("")
    faelle[ohne_namen.pk] = str(ohne_namen.pk)
    schon_fest = _kommune("Bonn", short_name="Bonn")
    zu_lang = _kommune("ä" * 128)  # umschrieben 256 Zeichen
    OParlBody.objects.update(file_cache_dir=None)  # Stand vor der Migration
    OParlBody.objects.filter(pk=schon_fest.pk).update(file_cache_dir="bonn-bleibt")

    historisch = MigrationExecutor(connection).loader.project_state([MIGRATION]).apps
    migration = importlib.import_module(f"insight_core.migrations.{MIGRATION[1]}")
    migration.festschreiben(historisch, None)
    migration.festschreiben(historisch, None)  # idempotent

    gespeichert = dict(OParlBody.objects.values_list("pk", "file_cache_dir"))
    for pk, erwartet in faelle.items():
        assert gespeichert[pk] == erwartet
        body = OParlBody.objects.get(pk=pk)
        assert erwartet == file_cache.derive_body_dir_name(body.slug, body.short_name, body.name, body.pk)
    assert gespeichert[schon_fest.pk] == "bonn-bleibt"
    assert gespeichert[zu_lang.pk] is None, "solch ein Verzeichnis ließ sich nie anlegen"
