"""Betriebshinweis aus der Umgebung (SEITENHINWEIS) in Insight und Work."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings

from apps.common.seitenhinweis import seitenhinweis_context


def _html() -> str:
    request = RequestFactory().get("/")
    return render_to_string("partials/seitenhinweis.html", {**seitenhinweis_context(request), "rahmen": "pt-4"})


@override_settings(SEITENHINWEIS="")
def test_ohne_text_keine_leiste() -> None:
    assert "seitenhinweis" not in _html()


@override_settings(SEITENHINWEIS="Viele Ratsinformationssysteme sind nicht erreichbar.")
def test_text_mit_link_zur_statusseite() -> None:
    html = _html()
    assert "Viele Ratsinformationssysteme sind nicht erreichbar." in html
    assert 'role="status"' in html
    assert 'href="https://status.mandari.de/"' in html


@override_settings(SEITENHINWEIS="<script>x</script>", SEITENHINWEIS_LINK="")
def test_text_wird_maskiert_und_link_ist_abschaltbar() -> None:
    html = _html()
    assert "<script>x</script>" not in html
    assert "Zur Statusseite" not in html


def test_insight_und_beide_work_rahmen_binden_den_hinweis_ein() -> None:
    vorlagen = Path(settings.BASE_DIR) / "templates"
    for name in ("base_insight.html", "work/base_work_neu.html", "work/base_work_alt.html"):
        assert '{% include "partials/seitenhinweis.html"' in (vorlagen / name).read_text(encoding="utf-8"), name
