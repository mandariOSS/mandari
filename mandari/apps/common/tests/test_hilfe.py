# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise in die Anwenderdokumentation (Issue #589).

Die Wissensdatenbank im Support-Bereich ist durch die Dokumentation ersetzt. Geprüft werden die
zentrale Zuordnung der Hilfe-Schlüssel, Template-Tag und Komponente, die Weiterleitung alter
Wissensdatenbank-Adressen sowie die Support-Seiten und kontextbezogenen Links der Oberfläche.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.template import engines
from django.test import Client, override_settings
from django.urls import NoReverseMatch, reverse
from django_cotton.compiler_regex import CottonCompiler

from apps.common import hilfe

PROJEKT = Path(__file__).resolve().parents[3]
TEMPLATES = PROJEKT / "templates"
APPS = PROJEKT / "apps"

TOPIC_RE = re.compile(r"<c-ui\.help-link\b[^>]*\btopic=\"([^\"]+)\"")
TAG_RE = re.compile(r"{%\s*hilfe_url\s+[\"']([^\"']+)[\"']\s*%}")
PY_RE = re.compile(r"\bdocs_url\(\s*[\"']([^\"']+)[\"']\s*\)")


def render(source: str, **context: object) -> str:
    compiled = CottonCompiler().process(source)
    return engines["django"].from_string(compiled).render(context)


# ---------------------------------------------------------------------------
# Zuordnung und Adressen
# ---------------------------------------------------------------------------


def test_docs_url_standardadresse() -> None:
    assert hilfe.docs_url("konto") == "https://docs.mandari.de/work/konto-und-sicherheit/"
    assert hilfe.docs_url("start") == "https://docs.mandari.de/"


@override_settings(DOCS_BASE_URL="https://hilfe.example.org/docs/")
def test_docs_url_eigene_basisadresse() -> None:
    assert hilfe.docs_url("zwei_faktor") == "https://hilfe.example.org/docs/work/konto-und-sicherheit/#zwei-faktor"


def test_unbekannter_schluessel_ist_ein_fehler() -> None:
    with pytest.raises(KeyError):
        hilfe.docs_url("gibt-es-nicht")


def test_alle_verwendeten_schluessel_sind_bekannt() -> None:
    """Jeder Hilfe-Link in Templates und Code nennt einen Schlüssel aus ``SEITEN``."""
    verwendet: dict[str, str] = {}
    for datei in TEMPLATES.rglob("*.html"):
        text = datei.read_text(encoding="utf-8")
        for schluessel in TOPIC_RE.findall(text) + TAG_RE.findall(text):
            verwendet[schluessel] = str(datei.relative_to(PROJEKT))
    for datei in APPS.rglob("*.py"):
        if "tests" in datei.parts:
            continue
        for schluessel in PY_RE.findall(datei.read_text(encoding="utf-8")):
            verwendet[schluessel] = str(datei.relative_to(PROJEKT))

    assert {"konto", "sitzungen", "dokumente", "fraktions_api", "start"} <= set(verwendet)
    unbekannt = {schluessel: ort for schluessel, ort in verwendet.items() if schluessel not in hilfe.SEITEN}
    assert not unbekannt


def test_hilfethemen_verweisen_auf_die_dokumentation() -> None:
    assert hilfe.HILFETHEMEN
    for thema in hilfe.HILFETHEMEN:
        assert thema.url.startswith("https://docs.mandari.de/work/"), thema


@pytest.mark.parametrize(
    ("kategorie", "artikel", "ziel"),
    [
        ("konto-sicherheit", "2fa-einrichten", "work/konto-und-sicherheit/#zwei-faktor"),
        ("faq", "sitzungen-nicht-sichtbar", "work/sitzungen-vorbereiten/#sitzung-fehlt"),
        ("antraege", None, "work/dokumente/"),
        ("faq", "neuer-artikel-aus-dem-admin", "work/haeufige-fragen/"),
        ("unbekannt", None, "work/"),
        (None, None, "work/"),
    ],
)
def test_alte_wissensdatenbank_adressen(kategorie: str | None, artikel: str | None, ziel: str) -> None:
    assert hilfe.alte_wissensdatenbank_url(kategorie, artikel) == f"https://docs.mandari.de/{ziel}"


# ---------------------------------------------------------------------------
# Template-Tag und Komponente
# ---------------------------------------------------------------------------


def test_template_tag() -> None:
    html = render('{% load hilfe_tags %}<a href="{% hilfe_url "dokumente" %}">x</a>')
    assert 'href="https://docs.mandari.de/work/dokumente/"' in html


def test_komponente_oeffnet_die_dokumentation_in_neuem_tab() -> None:
    html = render('<c-ui.help-link topic="sitzungen" />')
    assert 'href="https://docs.mandari.de/work/sitzungen-vorbereiten/"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html
    assert "Hilfe" in html
    assert "neuen Tab" in html


def test_komponente_mit_eigenem_text() -> None:
    html = render('<c-ui.help-link topic="faq">Häufige Fragen</c-ui.help-link>')
    assert "Häufige Fragen" in html
    assert ">\n    Hilfe\n" not in html


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_alte_wissensdatenbank_leitet_in_die_dokumentation(org: Any, make_member: Any, client_for: Any) -> None:
    client = client_for(make_member(org, ["support.view"], email="kb@example.org").user)

    antwort = client.get(f"/work/{org.slug}/support/kb/konto-sicherheit/2fa-einrichten/")
    assert antwort.status_code == 302
    assert antwort["Location"] == "https://docs.mandari.de/work/konto-und-sicherheit/#zwei-faktor"

    antwort = client.get(f"/work/{org.slug}/support/kb/")
    assert antwort["Location"] == "https://docs.mandari.de/work/"


@pytest.mark.django_db
def test_alte_wissensdatenbank_nur_fuer_angemeldete_mitglieder(org: Any) -> None:
    antwort = Client().get(f"/work/{org.slug}/support/kb/")
    assert antwort.status_code == 302
    assert not antwort["Location"].startswith("https://docs.mandari.de")


def test_suche_und_rueckmeldung_der_wissensdatenbank_entfallen() -> None:
    for name in ("kb_search", "kb_article_feedback"):
        with pytest.raises(NoReverseMatch):
            reverse(f"work:{name}", kwargs={"org_slug": "x"})


@pytest.mark.django_db
def test_supportseite_zeigt_hilfethemen_statt_wissensdatenbank(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["support.view", "support.create"], email="support@example.org")
    client = client_for(mitglied.user)

    liste = client.get(reverse("work:support", kwargs={"org_slug": org.slug}))
    assert liste.status_code == 200
    html = liste.content.decode()
    for thema in hilfe.HILFETHEMEN:
        assert thema.url in html
    assert "Wissensdatenbank" not in html
    assert "/support/kb/" not in html

    neu = client.get(reverse("work:support_create", kwargs={"org_slug": org.slug}))
    assert neu.status_code == 200
    html = neu.content.decode()
    assert hilfe.docs_url("faq") in html
    assert "searchArticles" not in html
    assert "/support/kb/" not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "rechte", "schluessel"),
    [
        ("work:security", ["dashboard.view"], "konto"),
        ("work:meetings", ["meetings.view"], "sitzungen"),
        ("work:documents", ["motions.view"], "dokumente"),
    ],
)
def test_kontextbezogene_hilfe(
    org: Any, make_member: Any, client_for: Any, url_name: str, rechte: list[str], schluessel: str
) -> None:
    mitglied = make_member(org, rechte, email="hilfe@example.org")
    antwort = client_for(mitglied.user).get(reverse(url_name, kwargs={"org_slug": org.slug}))
    assert antwort.status_code == 200
    assert hilfe.docs_url(schluessel) in antwort.content.decode()
