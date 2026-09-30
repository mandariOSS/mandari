# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Slugs für gelistete Kommunen (Issue #373): ``set_body_slugs`` mit eindeutiger Zuordnung über die ID,
idempotent und mit ``--dry-run``; danach Bürgerportal ``/insight/k/<slug>/`` mit Namen und Logo und die
Body-Sitemap im Sitemap-Index. Der Sitemap-Index führt jede gelistete Kommune, auch ohne Slug (per ID).
"""

from __future__ import annotations

import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse

from apps.session.models import SessionTenant
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _umgebung(tmp_path: Path, settings: Any) -> None:
    settings.OPARL_FILES_ROOT = str(tmp_path)
    cache.clear()


def _kommune(
    name: str, *, display_name: str | None = None, short_name: str | None = None, gelistet: bool = True, **felder: Any
) -> OParlBody:
    source = OParlSource.objects.create(name=name, url=f"https://ris.example/{uuid.uuid4()}/system")
    return OParlBody.objects.create(
        source=source,
        external_id=f"https://ris.example/bodies/{uuid.uuid4()}",
        name=name,
        display_name=display_name,
        short_name=short_name,
        is_listed=gelistet,
        **felder,
    )


@pytest.fixture
def muenster() -> OParlBody:
    body = _kommune("Stadt Münster", display_name="Münster", short_name="Münster")
    body.logo.name = "bodies/logos/muenster.svg"
    body.save(update_fields=["logo"])
    # Wie vom Ingestor angelegt: Cache-Verzeichnis noch nicht festgeschrieben
    OParlBody.objects.filter(pk=body.pk).update(file_cache_dir=None)
    body.refresh_from_db()
    return body


def _slugs(*angaben: str, **optionen: Any) -> str:
    out = StringIO()
    call_command("set_body_slugs", *angaben, stdout=out, **optionen)
    return out.getvalue()


class TestBefehl:
    def test_uebersicht_zeigt_ids_und_vorschlag_und_aendert_nichts(self, muenster: OParlBody) -> None:
        regionalrat = _kommune("Bezirksregierung Köln")
        _kommune("Pilotstadt", gelistet=False)

        ausgabe = _slugs()

        assert str(muenster.pk) in ausgabe and str(regionalrat.pk) in ausgabe
        assert "Pilotstadt" not in ausgabe
        assert f"{muenster.pk}=muenster" in ausgabe
        assert f"{regionalrat.pk}=bezirksregierung-koeln" in ausgabe
        assert "Cache-Verzeichnis: muenster" in ausgabe
        assert not OParlBody.objects.exclude(slug=None).exists()

    def test_probelauf_aendert_nichts(self, muenster: OParlBody) -> None:
        ausgabe = _slugs(f"{muenster.pk}=muenster", dry_run=True)

        assert "würde setzen" in ausgabe
        muenster.refresh_from_db()
        assert muenster.slug is None and muenster.file_cache_dir is None

    def test_setzt_slug_idempotent_und_haelt_cache_verzeichnis(self, muenster: OParlBody) -> None:
        ausgabe = _slugs(f"{muenster.pk}=stadt-muenster")
        assert "1 gesetzt" in ausgabe

        muenster.refresh_from_db()
        assert muenster.slug == "stadt-muenster"
        assert muenster.file_cache_dir == "muenster", "bisheriges Verzeichnis vor dem Slug festgeschrieben"

        erneut = _slugs(f"{muenster.pk}=stadt-muenster")
        assert "unverändert" in erneut and "0 gesetzt, 1 unverändert" in erneut

    def test_portal_und_sitemap_nach_dem_setzen(self, muenster: OParlBody) -> None:
        client = Client()
        assert client.get("/insight/k/muenster/").status_code == 404

        _slugs(f"{muenster.pk}=muenster")

        antwort = Client().get("/insight/k/muenster/")
        assert antwort.status_code == 200
        assert antwort.context["insight_portal"].name == "Münster"
        assert antwort.context["active_body"] == muenster
        assert "/media/bodies/logos/muenster.svg" in antwort.content.decode()
        index = client.get("/sitemap-insight-index.xml").content.decode()
        assert "/sitemap-insight-muenster.xml</loc>" in index
        assert client.get("/sitemap-insight-muenster.xml").status_code == 200

    @pytest.mark.parametrize(
        ("angabe", "meldung"),
        [
            ("{id}", "ID=SLUG"),
            ("keine-uuid=muenster", "keine Kommunen-ID"),
            ("00000000-0000-4000-8000-000000000373=muenster", "keine Kommune"),  # feste ID: gleiche Test-IDs je Worker
            ("{id}=Münster", "Kleinbuchstaben"),
            ("{id}=muenster--nord", "Kleinbuchstaben"),
            ("{id}=index", "reserviert"),
        ],
    )
    def test_fehlerhafte_angaben_aendern_nichts(self, muenster: OParlBody, angabe: str, meldung: str) -> None:
        bonn = _kommune("Bundesstadt Bonn", short_name="Bonn")

        with pytest.raises(CommandError, match=meldung):
            _slugs(f"{bonn.pk}=bonn", angabe.format(id=muenster.pk))

        assert not OParlBody.objects.exclude(slug=None).exists(), "auch die gültige Angabe bleibt ungesetzt"

    def test_slug_einer_anderen_kommune(self, muenster: OParlBody) -> None:
        _kommune("Münster (alt)", slug="muenster", gelistet=False)
        with pytest.raises(CommandError, match="hat schon"):
            _slugs(f"{muenster.pk}=muenster")

    def test_widerspruechliche_angaben(self, muenster: OParlBody) -> None:
        bonn = _kommune("Bundesstadt Bonn")
        with pytest.raises(CommandError, match="widerspricht"):
            _slugs(f"{muenster.pk}=muenster", f"{muenster.pk}=ms")
        with pytest.raises(CommandError, match="widerspricht"):
            _slugs(f"{muenster.pk}=muenster", f"{bonn.pk}=muenster")

    def test_anderer_slug_nur_mit_ersetzen(self, muenster: OParlBody) -> None:
        _slugs(f"{muenster.pk}=ms")
        with pytest.raises(CommandError, match="--replace"):
            _slugs(f"{muenster.pk}=muenster")

        _slugs(f"{muenster.pk}=muenster", replace=True)

        muenster.refresh_from_db()
        assert muenster.slug == "muenster"
        assert Client().get("/insight/k/ms/").status_code == 404

    def test_slug_eines_session_mandanten_bleibt_dessen_portal(self, muenster: OParlBody) -> None:
        SessionTenant.objects.create(name="Bezirk Ost", slug="ost", insight_publish=True)
        with pytest.raises(CommandError, match="Session-Mandanten"):
            _slugs(f"{muenster.pk}=ost")


class TestSitemapIndex:
    def test_listet_alle_gelisteten_kommunen(self, muenster: OParlBody) -> None:
        ohne_slug = _kommune("Stadt Darmstadt")
        _slugs(f"{muenster.pk}=muenster")
        versteckt = _kommune("Pilotstadt", gelistet=False)

        index = Client().get("/sitemap-insight-index.xml").content.decode()

        assert "/sitemap-insight-muenster.xml</loc>" in index
        assert f"/sitemap-insight-{ohne_slug.pk}.xml</loc>" in index
        assert str(versteckt.pk) not in index
        assert index.count("<sitemap>") == 2

    def test_body_sitemap_ueber_die_id(self, muenster: OParlBody) -> None:
        ohne_slug = _kommune("Stadt Darmstadt")
        _slugs(f"{muenster.pk}=muenster")
        versteckt = _kommune("Pilotstadt", gelistet=False)
        client = Client()

        assert client.get(f"/sitemap-insight-{ohne_slug.pk}.xml").status_code == 200
        umleitung = client.get(f"/sitemap-insight-{muenster.pk}.xml")
        assert umleitung.status_code == 301 and umleitung["Location"] == "/sitemap-insight-muenster.xml"
        assert client.get(f"/sitemap-insight-{versteckt.pk}.xml").status_code == 404
        assert client.get(f"/sitemap-insight-{uuid.uuid4()}.xml").status_code == 404
        assert client.get("/sitemap-insight-gibt-es-nicht.xml").status_code == 404


class TestAdmin:
    def test_aenderungsseite_zeigt_slug_und_cache_verzeichnis(self, muenster: OParlBody) -> None:
        nutzer = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
        nutzer.set_password("geheim-123")
        nutzer.save()
        client = Client()
        client.force_login(nutzer)
        OParlBody.objects.filter(pk=muenster.pk).update(file_cache_dir="muenster")

        seite = client.get(reverse("admin:insight_core_oparlbody_change", args=[muenster.pk])).content.decode()

        assert 'name="slug"' in seite
        assert "Verzeichnis im Dokument-Cache" in seite and 'name="file_cache_dir"' not in seite

    @pytest.mark.parametrize("slug", ["index", "Koeln", "köln", "-koeln", "koeln_nord"])
    def test_ungueltiger_slug_scheitert_an_der_validierung(self, muenster: OParlBody, slug: str) -> None:
        muenster.slug = slug
        with pytest.raises(ValidationError) as fehler:
            muenster.full_clean()
        assert "slug" in fehler.value.message_dict
