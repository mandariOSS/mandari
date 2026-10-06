# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rahmen von Work, Teil 2 (Issue #852): Navigation nach der Entscheidung im Issue vom 06.10.2026.

Geprüft: Die Seitenleiste ist kompakt und hat „Fraktionssitzungen“ als eigenen Eintrag (Start, Sitzungen,
Fraktionssitzungen, Dokumente, Aufgaben, Team, Recherche); „Sitzungen“ hat die Reiter „Für mich“ und „Alle Gremien“;
die Recherche trägt alle Seiten des Ratsinformationssystems als Unterpunkte (auf ihren Seiten offen, sonst
aufklappbar). Am Handy: Start, Sitzungen, Fraktion, Recherche und „Mehr“. ``aria-current`` unterscheidet die
geöffnete Seite (``page``) vom Bereich, in dem sie liegt (``true``). Leiste, Kopfzeile, Reiter und Leiste unten
fehlen im Druck. Kein Eintrag der bisherigen Navigation geht verloren: Jede Adresse aus dem bisherigen Rahmen steht
im neuen und antwortet.
"""

from __future__ import annotations

import re
from typing import Any, cast

import pytest
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.common.tests import factories
from apps.tenants.models import Organization
from apps.work import rahmen

ALT_RAHMEN = ("base_work_alt_leiste.html", "base_work_alt_kopf.html")
SIEBEN = ["Start", "Sitzungen", "Fraktionssitzungen", "Dokumente", "Aufgaben", "Team", "Recherche"]
RECHERCHE_UNTERPUNKTE = {
    "ris_meetings": "Sitzungen der Gremien",
    "ris_papers": "Vorgänge",
    "ris_decisions": "Beschlüsse",
    "ris_organizations": "Gremien",
    "ris_persons": "Personen",
    "ris_files": "Dokumente",
    "ris_map": "Karte",
    "ris_search": "Suche",
}


def _url(name: str, org: Organization) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug})


def _html(response: Any) -> str:
    assert response.status_code == 200, response.status_code
    return str(response.content.decode())


def _teil(html: str, anfang: str, ende: str) -> str:
    """Ausschnitt ab ``anfang`` bis zum nächsten ``ende``."""
    assert anfang in html, anfang
    return html.split(anfang, 1)[1].split(ende, 1)[0]


def _leiste(html: str) -> str:
    return _teil(html, 'id="work-navigation"', "</aside>")


def _oeffnendes_tag(html: str, merkmal: str) -> str:
    """Das öffnende Tag, das ``merkmal`` enthält (Attribute dürfen über mehrere Zeilen gehen)."""
    treffer = re.search(r"<[a-z]+\b[^>]*" + re.escape(merkmal) + r"[^>]*>", html)
    assert treffer, merkmal
    return treffer.group(0)


def _current(html: str, href: str) -> list[str]:
    """aria-current aller Links auf ``href`` im Ausschnitt (leer, wenn ein Link keinen Wert trägt)."""
    werte = []
    for tag in re.findall(r'<a href="' + re.escape(href) + r'"[^>]*>', html):
        treffer = re.search(r'aria-current="([^"]+)"', tag)
        werte.append(treffer.group(1) if treffer else "")
    return werte


@pytest.fixture
def neu(org: Organization) -> Organization:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def admin(org: Organization, make_member: Any) -> Any:
    return make_member(org, [], email="vorsitz@example.org", is_admin=True)


# ---- Seitenleiste ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_seitenleiste_kompakt_mit_fraktionssitzungen(neu: Organization, admin: Any, client_for: Any) -> None:
    leiste = _leiste(_html(client_for(admin.user).get(_url("dashboard", neu))))
    bereiche = _teil(leiste, 'aria-label="Bereiche"', "</nav>")
    assert re.findall(r'<span class="flex-1 min-w-0 truncate rahmen-label">([^<]+)</span>', bereiche) == SIEBEN
    assert f'href="{_url("faction", neu)}"' in bereiche
    unten = leiste.split("</nav>", 1)[1]
    assert ">Einstellungen</span>" in unten and ">Hilfe und Support</span>" in unten


@pytest.mark.django_db
def test_fraktionssitzungen_sind_eigener_bereich(neu: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("faction", neu)))
    leiste = _leiste(html)
    assert _current(leiste, _url("faction", neu)) == ["page"]
    assert _current(leiste, _url("meetings", neu)) == [""]
    # Kein Reiter „Fraktion“ mehr unter Sitzungen, die Liste steht für sich
    assert '<nav aria-label="Sitzungen"' not in html and ">Für mich</a>" not in html
    krumen = _teil(html, 'aria-label="Brotkrumen"', "</nav>")
    assert re.search(r'aria-current="page"[^>]*>Fraktionssitzungen<', krumen)
    assert f'href="{_url("meetings", neu)}"' not in krumen


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["meetings", "ris_meetings"])
def test_sitzungen_haben_die_reiter_fuer_mich_und_alle_gremien(
    neu: Organization, admin: Any, client_for: Any, name: str
) -> None:
    html = _html(client_for(admin.user).get(_url(name, neu)))
    reiter = _teil(html, '<nav aria-label="Sitzungen"', "</nav>")
    assert re.findall(r'whitespace-nowrap[^"]*">([^<]+)</a>', reiter) == ["Für mich", "Alle Gremien"]
    assert _current(reiter, _url(name, neu)) == ["page"]


@pytest.mark.django_db
def test_recherche_zugeklappt_ausserhalb_ihrer_seiten(neu: Organization, admin: Any, client_for: Any) -> None:
    leiste = _leiste(_html(client_for(admin.user).get(_url("dashboard", neu))))
    knopf = _oeffnendes_tag(leiste, 'aria-controls="leiste-recherche"')
    assert 'aria-expanded="false"' in knopf and 'aria-label="Unterpunkte von Recherche"' in knopf
    liste = _oeffnendes_tag(leiste, 'id="leiste-recherche"')
    assert "x-cloak" in liste and 'x-show="offen"' in liste
    unterpunkte = _teil(leiste, 'id="leiste-recherche"', "</ul>")
    # Alle Seiten des Ratsinformationssystems stehen als Unterpunkte bereit, keiner ist aktuell
    for name, label in RECHERCHE_UNTERPUNKTE.items():
        assert _current(unterpunkte, _url(name, neu)) == [""], name
        assert f">{label}</a>" in unterpunkte, label


@pytest.mark.django_db
def test_recherche_offen_auf_ihren_seiten(neu: Organization, admin: Any, client_for: Any) -> None:
    leiste = _leiste(_html(client_for(admin.user).get(_url("ris_search", neu))))
    assert 'aria-expanded="true"' in _oeffnendes_tag(leiste, 'aria-controls="leiste-recherche"')
    assert "x-cloak" not in _oeffnendes_tag(leiste, 'id="leiste-recherche"')
    unterpunkte = _teil(leiste, 'id="leiste-recherche"', "</ul>")
    assert _current(unterpunkte, _url("ris_search", neu)) == ["page"]
    assert _current(leiste.split('id="leiste-recherche"', 1)[0], _url("ris_overview", neu)) == ["true"]


# ---- aria-current: Bereich (true) und Seite (page) -------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "bereich", "wert", "unterpunkt", "wert_unterpunkt"),
    [
        ("dashboard", "start", "page", None, None),
        ("dashboard_explicit", "start", "page", None, None),
        ("meetings", "sitzungen", "page", None, None),
        ("meeting_prepare", "sitzungen", "true", None, None),
        ("faction", "fraktionssitzungen", "page", None, None),
        ("faction_detail", "fraktionssitzungen", "true", None, None),
        ("ris_overview", "recherche", "page", None, None),
        ("ris_papers", "recherche", "true", "Vorgänge", "page"),
        ("ris_paper_detail", "recherche", "true", "Vorgänge", "true"),
        ("ris_meetings", "sitzungen", "true", "Sitzungen der Gremien", "page"),
        ("ris_meeting_detail", "sitzungen", "true", "Sitzungen der Gremien", "true"),
    ],
)
def test_aria_current_unterscheidet_bereich_und_seite(
    neu: Organization,
    admin: Any,
    url_name: str,
    bereich: str,
    wert: str,
    unterpunkt: str | None,
    wert_unterpunkt: str | None,
) -> None:
    nav = rahmen.navigation(neu, admin, url_name)
    aktuell = {b["key"]: b["current"] for b in nav["bereiche"] if b["current"]}
    assert aktuell == {bereich: wert}
    recherche = next(b for b in nav["bereiche"] if b["key"] == "recherche")
    unter = {u["label"]: u["current"] for u in recherche["unterpunkte"] if u["current"]}
    assert unter == ({unterpunkt: wert_unterpunkt} if unterpunkt else {})
    # Offen, wo die Seite in der Recherche liegt oder einer ihrer Unterpunkte sie zeigt
    assert recherche["offen"] is bool(bereich == "recherche" or unterpunkt)


def test_brotkrume_ohne_adresse_ist_nicht_die_aktuelle_seite() -> None:
    """Nur die Krume, die die Seite selbst ist, trägt aria-current (vorher jede Krume ohne Adresse)."""
    krumen = [
        {"label": "Raum", "url": "", "aktuell": False},
        {"label": "Recherche", "url": "/r/", "aktuell": False},
        {"label": "Kalender", "url": "", "aktuell": True},
    ]
    html = render_to_string("work/rahmen/_brotkrumen.html", {"work_rahmen": {"brotkrumen": krumen}})
    assert re.findall(r'aria-current="page"[^>]*>([^<]+)<', html) == ["Kalender"]
    assert '<span class="truncate">Raum</span>' in html and '<a href="/r/"' in html


# ---- Handy ---------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_leiste_unten_start_sitzungen_fraktion_recherche_mehr(neu: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("faction", neu)))
    leiste_unten = _teil(html, 'aria-label="Hauptbereiche"', "</nav>")
    assert re.findall(r'<span class="max-w-full truncate">([^<]+)</span>', leiste_unten) == [
        "Start",
        "Sitzungen",
        "Fraktion",
        "Recherche",
    ]
    assert "<span>Mehr</span>" in leiste_unten
    assert _current(leiste_unten, _url("faction", neu)) == ["page"]
    mehr = _teil(html, 'aria-label="Weitere Bereiche"', "</nav>")
    beschriftungen = re.findall(r'<span class="flex-1 min-w-0 truncate">([^<]+)</span>', mehr)
    assert beschriftungen == [
        "Dokumente",
        "Aufgaben",
        "Team",
        "Benachrichtigungen",
        "Einstellungen",
        "Hilfe und Support",
    ]


# ---- Druck ---------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_druck_zeigt_nur_den_inhalt(neu: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("meetings", neu)))
    assert "print:!hidden" in _oeffnendes_tag(html, 'id="work-navigation"')
    assert "print:hidden" in _oeffnendes_tag(html, 'class="sticky top-0')
    assert "print:hidden" in _oeffnendes_tag(html, 'aria-label="Sitzungen"')
    assert "print:!hidden" in _oeffnendes_tag(html, 'aria-label="Hauptbereiche"')
    assert "print:!p-0" in _oeffnendes_tag(html, 'class="work-main')
    # Der Seitentitel bleibt
    assert 'class="work-page-header"' in html


# ---- Kein Eintrag der bisherigen Navigation geht verloren ----------------------------------------------------


def _bisherige_ziele() -> list[str]:
    """Adressnamen der Organisation aus Seitenleiste und Kopf des bisherigen Rahmens (base_work_alt_*.html)."""
    namen: list[str] = []
    for datei in ALT_RAHMEN:
        text = (settings.BASE_DIR / "templates" / "work" / datei).read_text(encoding="utf-8")
        for name in re.findall(r"{% url 'work:(\w+)' org_slug=organization\.slug %}", text):
            if name not in namen:
                namen.append(name)
    return namen


def test_bisherige_ziele_werden_gefunden() -> None:
    ziele = _bisherige_ziele()
    # 17 Einträge der Seitenleiste (Gast eingeschlossen) und das Menü oben rechts
    for name in ("dashboard", "faction", "ris_overview", "ris_search", "organization", "support", "security"):
        assert name in ziele
    assert len(ziele) >= 20


@pytest.mark.django_db
def test_kein_eintrag_der_bisherigen_navigation_fehlt(neu: Organization, admin: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    html = _html(client.get(_url("dashboard", neu)))
    ziele = [name for name in _bisherige_ziele() if name != "guest_documents"]  # nur für Gäste, Test unten
    fehlend = [name for name in ziele if f'"{_url(name, neu)}"' not in html]
    assert fehlend == [], fehlend
    # Das Logo der Organisation führte auf die Startseite von mandari: jetzt im Raum-Dialog
    raum_dialog = html.split('id="raum-dialog"', 1)[1]
    assert re.search(r'<a href="/"[^>]*>Zur Startseite von mandari</a>', raum_dialog)
    # Alle Seiten antworten im neuen Rahmen (ohne die Abfragen der Glocke)
    for name in ziele:
        if name.startswith("notification") and name != "notifications":
            continue
        antwort = client.get(_url(name, neu))
        assert antwort.status_code == 200, (name, antwort.status_code)


@pytest.mark.django_db
def test_gast_behaelt_freigaben_und_profil(neu: Organization, client_for: Any) -> None:
    gast = cast(Any, factories.MembershipFactory)(organization=neu, is_guest=True, roles=[])
    html = _html(client_for(gast.user).get(_url("guest_documents", neu)))
    assert f'href="{_url("guest_documents", neu)}"' in _leiste(html)
    assert f'href="{_url("profile", neu)}"' in html
    assert 'id="leiste-recherche"' not in html
