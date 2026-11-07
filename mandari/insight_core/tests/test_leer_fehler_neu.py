# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bürgerportal ohne Sackgassen (Issue #1261): leere Beschlusskontrolle, Fehlerseiten im Stil des Bürgerportals und
„Was ist neu?“ hinter der Versionsnummer im Fuß.
"""

from __future__ import annotations

import re
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import mandari.urls as urls
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource
from insight_core.services import neuigkeiten

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"


@pytest.fixture
def kommune() -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Stadt Beispielstadt", display_name="Beispielstadt"
    )


@pytest.fixture
def besucher(client: Client, kommune: OParlBody) -> Client:
    client.get(f"/insight/kommune/{kommune.id}/")
    return client


def _ohne_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class TestBeschluesseOhneDaten:
    def test_leerzustand_mit_zuletzt_beratenen_punkten(self, besucher: Client, kommune: OParlBody) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1", body=kommune, name="Rat", start=timezone.now() - timedelta(days=4)
        )
        OParlAgendaItem.objects.create(
            external_id=f"{RIS}/item/1", meeting=sitzung, number="3", name="Neuer Spielplatz", result="beschlossen"
        )
        OParlAgendaItem.objects.create(external_id=f"{RIS}/item/2", meeting=sitzung, number="4", name="Ohne Ergebnis")

        antwort = besucher.get("/insight/beschluesse/")

        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert "Noch kein Umsetzungsstand gemeldet" in html
        assert "Für Beispielstadt liegt noch keine Meldung der Verwaltung vor." in _ohne_tags(html)
        assert "Zuletzt beraten" in html and "Neuer Spielplatz" in html and "Ohne Ergebnis" not in html
        assert f"/insight/termine/{sitzung.pk}/" in html
        assert 'href="/insight/termine/?period=past"' in html and 'href="/insight/vorgaenge/"' in html
        # Keine leere Liste und kein Abo-Versprechen, nicht im Index
        assert "Abonnieren Sie einzelne Beschlüsse" not in html and "Umsetzung steht an" not in html
        assert '<meta name="robots" content="noindex, follow">' in html

    def test_eingeschaltet_aber_ohne_beschluss(self, besucher: Client, kommune: OParlBody) -> None:
        SessionTenant.objects.create(
            name="Verwaltung", slug="verwaltung", oparl_body=kommune, insight_publish=True, implementation_publish=True
        )
        html = besucher.get("/insight/beschluesse/").content.decode()
        assert "Noch kein Umsetzungsstand gemeldet" in html
        assert "Zuletzt beraten" not in html, "ohne beratene Punkte keine leere Überschrift"
        assert 'href="/insight/vorgaenge/"' in html

    def test_mit_beschluss_die_liste(self, besucher: Client, kommune: OParlBody) -> None:
        tenant = SessionTenant.objects.create(
            name="Verwaltung", slug="verwaltung", oparl_body=kommune, insight_publish=True, implementation_publish=True
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, organization=rat, name="Rat", start=timezone.now() - timedelta(days=7), is_public=True
        )
        SessionAgendaItem.objects.create(
            meeting=sitzung, number="1", name="Radweg Hafen", vote_result="approved", implementation_public=True
        )
        html = besucher.get("/insight/beschluesse/").content.decode()
        assert "Radweg Hafen" in html and "Umsetzung steht an" in html
        assert "Noch kein Umsetzungsstand gemeldet" not in html
        assert '<meta name="robots" content="index, follow">' in html


class TestFehlerseiten:
    def test_404_im_buergerportal(self) -> None:
        with CaptureQueriesContext(connection) as erfasst:
            antwort = Client().get("/insight/gibt-es-nicht/")
        assert antwort.status_code == 404
        assert erfasst.captured_queries == [], "weiter ohne Datenbank (Issue #1024)"
        html = antwort.content.decode()
        assert 'data-portal="insight"' in html, "Grün des Bürgerportals statt Violett"
        assert "Seite nicht gefunden" in html
        assert re.search(r'<form action="/insight/suche/" method="get" role="search"', html)
        assert 'name="q"' in html
        assert 'href="/insight/termine/"' in html and 'href="/insight/vorgaenge/"' in html
        text = _ohne_tags(html)
        assert "Suchen Sie nach einem Thema" in text
        for wort in ("du", "dir", "dich", "dein", "Brauchst"):
            assert not re.search(rf"\b{wort}\b", text), wort
        assert '<meta name="robots" content="noindex, nofollow">' in html
        assert "/impressum/" in html and "/datenschutz/" in html

    def test_404_eines_portal_hosts(self) -> None:
        anfrage = RequestFactory().get("/irgendwo/")
        anfrage.insight_portal_host_slug = "beispielstadt"  # type: ignore[attr-defined]
        antwort = urls.handler_404(anfrage, None)
        assert 'data-portal="insight"' in antwort.content.decode()

    def test_404_anderer_bereiche_unveraendert(self) -> None:
        antwort = Client().get("/gibt-es-nicht/")
        assert antwort.status_code == 404
        seiten = [antwort.content.decode()]
        for pfad in ("/work/fraktion/gibt-es-nicht/", "/session/stadt/gibt-es-nicht/", "/accounts/gibt-es-nicht/"):
            seiten.append(urls.handler_404(RequestFactory().get(pfad), None).content.decode())
        for html in seiten:
            assert 'data-portal="insight"' not in html
            assert "Seite nicht gefunden" in html and "Zum Bürgerportal" in html

    def test_500_im_buergerportal(self) -> None:
        anfrage = RequestFactory().get("/insight/vorgaenge/")
        with CaptureQueriesContext(connection) as erfasst:
            try:
                raise RuntimeError("Fehler in einer View")
            except RuntimeError:
                antwort = urls.handler_500(anfrage)
        assert antwort.status_code == 500
        assert erfasst.captured_queries == []
        html = antwort.content.decode()
        assert 'data-portal="insight"' in html
        assert "Diese Seite lässt sich gerade nicht anzeigen" in html
        assert 'data-action="reload"' in html and 'action="/insight/suche/"' in html
        assert re.search(r"Fehler-ID <code[^>]*>[0-9a-f]{8}</code>", html)
        assert "/feedback/?error_id=" in html
        assert not re.search(r"\b(du|deine?)\b", _ohne_tags(html))

    def test_500_anderer_bereiche_unveraendert(self) -> None:
        anfrage = RequestFactory().get("/work/irgendwo/")
        try:
            raise RuntimeError("Fehler in einer View")
        except RuntimeError:
            antwort = urls.handler_500(anfrage)
        html = antwort.content.decode()
        assert antwort.status_code == 500
        assert 'data-portal="insight"' not in html and "Interner Serverfehler" in html


class TestNeuigkeiten:
    def test_datei_vollstaendig_und_in_sie_form(self) -> None:
        eintraege = neuigkeiten.lies()
        assert eintraege, "mindestens ein Eintrag"
        for eintrag in eintraege:
            assert eintrag.titel.endswith("."), eintrag.titel
            assert len(eintrag.titel) <= neuigkeiten.TITEL_MAX, eintrag.titel
            assert len(eintrag.text) <= neuigkeiten.TEXT_MAX, eintrag.titel
            assert "\n" not in eintrag.text, f"ein Absatz: {eintrag.titel}"
            assert not re.search(r"\b(du|dir|dich|dein\w*|euch|euer\w*)\b", f"{eintrag.titel} {eintrag.text}", re.I)
            assert not eintrag.version or re.fullmatch(r"\d+\.\d+\.\d+", eintrag.version), eintrag.version
        assert [e.datum for e in eintraege] == sorted((e.datum for e in eintraege), reverse=True)

    def test_fehlende_pflichtfelder(self, tmp_path: Path) -> None:
        datei = tmp_path / "neu.toml"
        datei.write_text('[[eintrag]]\ndatum = 2026-11-01\ntitel = "Ohne Text."\n', encoding="utf-8")
        with pytest.raises(ValueError, match="Eintrag 1"):
            neuigkeiten.lies(datei)

    def test_seite_mit_eintraegen_gruppiert(self, besucher: Client) -> None:
        antwort = besucher.get("/insight/neuigkeiten/")
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert "<h1" in html and "Was ist neu?" in html
        assert re.search(r'<span aria-current="page"[^>]*>Was ist neu\?</span>', html), "Brotkrume"
        erster = neuigkeiten.neuigkeiten()[0]
        assert erster.titel in html
        assert f'datetime="{erster.datum:%Y-%m-%d}"' in html
        assert "Version 0.12.0" in html
        assert "War diese Seite hilfreich?" in html and 'name="page_type" value="neuigkeiten"' in html

    def test_kaputte_datei_bricht_die_seite_nicht(
        self, besucher: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = tmp_path / "kaputt.toml"
        datei.write_text("[[eintrag]\n", encoding="utf-8")
        monkeypatch.setattr(neuigkeiten, "DATEI", datei)
        neuigkeiten.neuigkeiten.cache_clear()
        try:
            html = besucher.get("/insight/neuigkeiten/").content.decode()
        finally:
            neuigkeiten.neuigkeiten.cache_clear()
        assert "Gerade gibt es hier nichts zu lesen." in html

    def test_version_aus_pyproject(self) -> None:
        with (Path(django_settings.BASE_DIR) / "pyproject.toml").open("rb") as datei:
            erwartet = tomllib.load(datei)["project"]["version"]
        assert neuigkeiten.mandari_version() == erwartet

    def test_fuss_verlinkt_die_version(self, besucher: Client) -> None:
        html = besucher.get("/insight/vorgaenge/").content.decode()
        fuss = html[html.index("<footer") :]
        version = neuigkeiten.mandari_version()
        assert re.search(rf'<a href="/insight/neuigkeiten/"[^>]*>Version {re.escape(version)}: Was ist neu\?</a>', fuss)

    def test_gruppen_je_tag(self) -> None:
        gruppen = neuigkeiten.gruppen()
        assert all(isinstance(g.datum, date) and g.eintraege for g in gruppen)
        assert len({(g.datum, g.version) for g in gruppen}) == len(gruppen)


def test_kein_violett_auf_den_fehlerseiten_des_buergerportals() -> None:
    """Die Knöpfe nutzen primary (im Bürgerportal Grün); fest verdrahtet ist höchstens der Punkt der Wortmarke."""
    pfad = Path(django_settings.BASE_DIR) / "templates" / "errors"
    for name in ("insight_base.html", "insight_404.html", "insight_500.html", "insight_suche.html"):
        inhalt = (pfad / name).read_text(encoding="utf-8")
        treffer = re.findall(r"\b(?:bg|border|ring)-(?:indigo|violet|purple)-\d+", inhalt)
        assert not treffer, f"{name}: {treffer}"
