# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen im neuen Erscheinungsbild (Issue #951, Teilpaket 3).

Mit eingeschaltetem Schalter (Organization.work_new_design) rendern die Seiten der Einstellungen eigene Vorlagen aus
work/organization/neu/ bzw. work/motions/settings/neu/; ausgeschaltet bleibt alles wie bisher, und ``?ansicht=bisher``
öffnet die bisherige Vorlage auch bei eingeschaltetem Schalter. Geprüft wird:

- Gleichstand der Funktionen je Seite und Rolle (gleichstand.py): Formulare mit Methode, Ziel, Feldern, Pflichtfeldern
  und Rückfragen, Work-Links, Aktionen und Alpine-Komponenten der bisherigen Vorlage stehen auch in der neuen.
  Ausnahmen stehen begründet in AUSNAHMEN.
- Rechte: dieselben Antwortcodes je Rolle in beiden Vorlagen; Reiter nur mit den Rechten der bisherigen Leiste.
- Bausteine statt Hand-Markup: keine Kästen, kein Inline-Stil, keine Inline-Handler.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.work.faction.models import FactionPublicApiAccess
from apps.work.organization import einstellungen

from .einstellungen_hilfen import fixtures_uebernehmen
from .einstellungen_hilfen import html as _html
from .einstellungen_hilfen import rechte_wie_bisher as _rechte_wie_bisher
from .einstellungen_hilfen import unterpunkte_anderer_reiter as _unterpunkte_anderer_reiter
from .einstellungen_hilfen import url as _url
from .einstellungen_hilfen import vergleich as _vergleich
from .einstellungen_hilfen import vorlagen as _vorlagen
from .gleichstand import fehlend

pytestmark = pytest.mark.django_db
fixtures_uebernehmen(globals())

NEU = Path(settings.BASE_DIR) / "templates" / "work" / "organization" / "neu"

#: Begründete Ausnahmen je Seite: Kennungen der bisherigen Vorlage, die die neue bewusst nicht mehr hat
AUSNAHMEN: dict[str, set[str]] = {
    # Die Kachel „Mitglieder“ mit Zähler doppelte den Reiter „Mitglieder“; „Mitglied einladen“ ist dort die
    # Hauptaktion (Plan #951: KPI „Mitglieder“ entfällt). Die „Gefahrenzone“ hatte nur einen gesperrten Knopf.
    "organization": {"link:/work/fraktion-test/organization/members/invite/"},
}


# ---------------------------------------------------------------------------
# Teil 3a: Allgemein (Organisation, E-Mail-Versand, Registrierung, API) und Sicherheit
# ---------------------------------------------------------------------------

SEITEN_3A = {
    "organization": "work/organization/neu/allgemein.html",
    "organization_email_settings": "work/organization/neu/email.html",
    "organization_registration": "work/organization/neu/registrierung.html",
    "organization_api_settings": "work/organization/neu/api.html",
}


@pytest.mark.parametrize(("name", "vorlage"), SEITEN_3A.items())
def test_schalter_waehlt_die_neue_vorlage(org: Any, admin: Any, client_for: Any, name: str, vorlage: str) -> None:
    client = client_for(admin.user)
    assert vorlage in _vorlagen(client.get(_url(name, org)))
    bisher = _vorlagen(client.get(_url(name, org) + "?ansicht=bisher"))
    assert vorlage not in bisher
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert vorlage not in _vorlagen(client.get(_url(name, org)))


SEITE_3A = {
    "organization": "organisation",
    "organization_email_settings": "email",
    "organization_registration": "registrierung",
    "organization_api_settings": "api",
}


@pytest.mark.parametrize("name", SEITEN_3A)
def test_gleichstand_3a(org: Any, admin: Any, client_for: Any, name: str) -> None:
    FactionPublicApiAccess.for_organization(org)
    client = client_for(admin.user)
    pfad = _url(name, org)
    weitere = (f"{pfad}?bereich=sicherheit",) if name == "organization" else ()
    alt, neu = _vergleich(client, pfad, *weitere)
    ausnahmen = AUSNAHMEN.get(name, set()) | _unterpunkte_anderer_reiter(org, SEITE_3A[name])
    assert fehlend(alt, neu, ausnahmen) == []


def test_gleichstand_allgemein_nur_lesend(org: Any, leser: Any, client_for: Any) -> None:
    client = client_for(leser.user)
    pfad = _url("organization", org)
    alt, neu = _vergleich(client, pfad, f"{pfad}?bereich=sicherheit")
    assert fehlend(alt, neu, AUSNAHMEN["organization"] | _unterpunkte_anderer_reiter(org, "organisation")) == []
    html = _html(client.get(pfad))
    assert 'name="action"' not in html
    assert "Ändern darf, wer die Organisation bearbeiten darf" in html


#: Antwortcodes je Rolle (einstellungen_hilfen.ROLLEN): unverändert gegenüber der bisherigen Vorlage
CODES_3A = {
    "organization": {"admin": 200, "leser": 200, "bearbeitung": 200, "fraktionsleitung": 200, "mitglied": 403},
    "organization_email_settings": {"admin": 200, "leser": 403, "bearbeitung": 200, "fraktionsleitung": 403},
    "organization_registration": {"admin": 200, "leser": 403, "bearbeitung": 200, "mitgliederverwaltung": 403},
    "organization_api_settings": {"admin": 200, "leser": 403, "bearbeitung": 403, "fraktionsleitung": 200},
}


@pytest.mark.parametrize("name", SEITEN_3A)
def test_rechte_und_gleichstand_je_rolle_3a(org: Any, rollen: dict[str, Any], client_for: Any, name: str) -> None:
    """Je Rolle derselbe Antwortcode und, wo die Seite sich öffnet, alle Funktionen der bisherigen Vorlage."""
    FactionPublicApiAccess.for_organization(org)
    ausnahmen = AUSNAHMEN.get(name, set()) | _unterpunkte_anderer_reiter(org, SEITE_3A[name])
    pfad = _url(name, org)
    # Die bisherige Seite „Allgemein“ trug auch die Anmeldesicherheit, jetzt der Reiter „Sicherheit“
    weitere = (f"{pfad}?bereich=sicherheit",) if name == "organization" else ()
    codes = _rechte_wie_bisher(client_for, rollen, pfad, ausnahmen, weitere)
    assert {rolle: codes[rolle] for rolle in CODES_3A[name]} == CODES_3A[name]


def test_sicherheit_ist_eigener_reiter_und_bleibt_nach_dem_speichern(org: Any, admin: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    pfad = _url("organization", org)
    html = _html(client.get(f"{pfad}?bereich=sicherheit"))
    assert 'name="require_2fa"' in html
    assert f'href="{pfad}?bereich=sicherheit" aria-current="page"' in html
    assert 'name="require_2fa"' not in _html(client.get(pfad))
    antwort = client.post(f"{pfad}?bereich=sicherheit", {"action": "update_security", "require_2fa": "on"})
    assert antwort.status_code == 302
    assert antwort["Location"] == f"{pfad}?bereich=sicherheit"
    org.refresh_from_db()
    assert org.require_2fa
    # Ohne Reiter (bisherige Vorlage) wie bisher zurück auf die Seite selbst
    antwort = client.post(pfad, {"action": "update_security"})
    assert antwort["Location"] == pfad
    org.refresh_from_db()
    assert not org.require_2fa


def test_partei_der_organisation_bleibt_gewaehlt(org: Any, admin: Any, client_for: Any) -> None:
    """Die eigene Partei ist gesperrt und geht über ein verstecktes Feld mit (bisher Inline-onclick)."""
    html = _html(client_for(admin.user).get(_url("organization", org)))
    assert f'<input type="hidden" name="parties" value="{org.party_group_id}">' in html
    assert "onclick" not in html


# ---------------------------------------------------------------------------
# Reiter und Unterpunkte
# ---------------------------------------------------------------------------


def _reiter(html: str) -> list[str]:
    teil = html.split('<nav aria-label="Einstellungen"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r"<a [^>]*>\s*([^<]+?)\s*</a>", teil)


def test_reiter_fuer_die_administration(org: Any, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("organization_email_settings", org)))
    assert _reiter(html) == ["Allgemein", "Mitglieder", "Fraktionssitzungen", "Dokumente", "Verwaltung", "Sicherheit"]
    unterpunkte = html.split('data-testid="einstellungen-unterpunkte"', 1)[1].split("</nav>", 1)[0]
    for name in (
        "organization",
        "organization_email_settings",
        "organization_registration",
        "organization_api_settings",
    ):
        assert f'href="{_url(name, org)}"' in unterpunkte
    aktuell = re.findall(r'<a href="([^"]+)" aria-current="page"', unterpunkte)
    assert aktuell == [_url("organization_email_settings", org)]


def test_jede_bisherige_reiterseite_bleibt_erreichbar(org: Any, admin: Any, client_for: Any) -> None:
    """Jedes Ziel der bisherigen Leiste steht als Reiter oder als Unterpunkt auf der Seite seines Reiters."""
    client = client_for(admin.user)
    bisher = [
        "organization",
        "organization_faction_settings",
        "organization_documents",
        "organization_api_settings",
        "organization_ris_settings",
        "organization_email_settings",
        "council_parties",
        "members",
        "roles",
        "organization_registration",
    ]
    reiterseiten = {r["url"] for r in einstellungen.navigation(org, admin, "organisation")["reiter"]}
    erreichbar: set[str] = set()
    for url in reiterseiten:
        html = client.get(url).content.decode()
        erreichbar |= set(re.findall(r'href="([^"]+)"', html))
    assert [name for name in bisher if _url(name, org) not in erreichbar] == []


def test_reiter_ohne_fraktionsrechte_wie_bisher(org: Any, leser: Any, client_for: Any) -> None:
    """Fraktionssitzungen, API und Einreichung nur mit faction.manage, Rollen nur mit organization.manage_roles."""
    html = _html(client_for(leser.user).get(_url("organization", org)))
    assert _reiter(html) == ["Allgemein", "Mitglieder", "Dokumente", "Verwaltung", "Sicherheit"]
    for name in ("organization_faction_settings", "organization_api_settings", "organization_ris_settings", "roles"):
        assert f'href="{_url(name, org)}"' not in html
    # „Verwaltung“ führt ohne faction.manage direkt zu den Ratsfraktionen (wie die bisherige Leiste)
    assert f'href="{_url("council_parties", org)}"' in html


# ---------------------------------------------------------------------------
# Bausteine statt Hand-Markup
# ---------------------------------------------------------------------------


def test_neue_vorlagen_ohne_kaesten_inline_stil_und_handler() -> None:
    for datei in sorted(NEU.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert ' style="' not in text, datei.name
        assert " onclick=" not in text and " onsubmit=" not in text, datei.name
        assert "rounded-xl border" not in text, datei.name
        assert "shadow-sm" not in text, datei.name
        assert text.count("\n") < 300, datei.name
        # Seiten im Rahmen von Work zählen für das Prüfskript (Einladung annehmen liegt außerhalb des Rahmens)
        if '{% extends "work/base_work.html" %}' in text:
            assert "{% block gestaltung %}neu{% endblock %}" in text, datei.name


def test_gleichstand_erkennt_fehlende_funktionen() -> None:
    """Der Vergleich bemerkt verlorene Rückfragen (auch einzelne data-confirm-Angaben), Sperren, Werte, Grenzen,
    Dateitypen und einmaliges Absenden."""
    from .gleichstand import funktionen

    bisher = """
    <form method="post" action="/work/o/x/" enctype="multipart/form-data" data-submit-once data-confirm="Sicher?">
      <input type="hidden" name="section" value="api_save"><input type="hidden" name="item_id" value="7">
      <input type="checkbox" name="flag" value="1" disabled><input type="number" name="n" min="1" max="9">
      <input type="file" name="logo" accept="image/png"><button type="submit" name="direction" value="up">Hoch</button>
    </form>
    <form method="post" action="/work/o/y/" x-data @submit.prevent="confirmAction({variant: 'danger'}).then(ok => ok)"></form>
    <button data-post="/work/o/z/" data-confirm-variant="danger">Weg</button>
    <button data-post="/work/o/w/" data-confirm-title="Löschen" data-confirm-message="Wirklich?">Löschen</button>
    <button type="button" data-action="confirm-submit" data-target="#f" data-confirm-message="Sicher?">Senden</button>"""
    neu = """
    <form method="post" action="/work/o/x/"><input type="hidden" name="section" value="api_save">
      <input type="hidden" name="item_id" value="8"><input type="checkbox" name="flag" value="2">
      <input type="number" name="n"><input type="file" name="logo"><button type="submit" name="direction" value="down">Hoch</button>
    </form>
    <form method="post" action="/work/o/y/"></form>
    <button data-post="/work/o/z/">Weg</button>
    <button data-post="/work/o/w/" data-confirm-title="Entfernen">Löschen</button>
    <button type="button" data-action="confirm-submit" data-target="#f">Senden</button>"""
    luecken = fehlend(funktionen(bisher, "/work/o/"), funktionen(neu, "/work/o/"))
    erwartet = {
        "rueckfrage:post /work/o/x/ section=api_save:data-confirm",
        "einmal:post /work/o/x/ section=api_save",
        "kodierung:post /work/o/x/ section=api_save:multipart/form-data",
        "wert:post /work/o/x/ section=api_save:item_id=7",
        "gesperrt:post /work/o/x/ section=api_save:flag=1",
        "grenze:post /work/o/x/ section=api_save:n:min=1",
        "accept:post /work/o/x/ section=api_save:logo=image/png",
        "wert:post /work/o/x/ section=api_save:direction=up",
        "rueckfrage:post /work/o/y/:confirmAction:danger",
        "aktion:data-post:/work/o/z/:danger",
        "rueckfrage:data-post:/work/o/z/:data-confirm-variant=danger",
        # Ohne Meldung fragt data-post nicht mehr nach: Titel allein genügt nicht
        "rueckfrage:data-post:/work/o/w/:data-confirm-message",
        "rueckfrage:data-action:confirm-submit:#f:data-confirm-message",
    }
    assert erwartet <= set(luecken), sorted(erwartet - set(luecken))
