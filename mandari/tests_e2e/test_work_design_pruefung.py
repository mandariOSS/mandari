# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfskript für das neue Work-Design im Browser (``scripts/pruefe_work_design.py``, Issue #884).

Derselbe Ablauf wie vor dem Ausrollen (docs/WORK_NEUES_DESIGN.md), verkleinert für das CI: Demo aufbauen, alle fünf
Rollen anmelden, die Work-Seiten über die sichtbaren Links erkunden, je Seite am Handy (390 px) und breit (1440 px)
messen – einmal mit eingeschaltetem und einmal mit ausgeschaltetem Schalter. Danach steht der Schalter wieder wie
vorher. Es darf keinen Serverfehler, keinen toten Link, keine Fehler in der Konsole und keinen seitlichen Überlauf
geben; Gast und Mitglied erreichen keine Seiten, die ihnen nicht zustehen. Leerflächen meldet das Skript zur
Sichtung, hier zählen sie nicht; ebenso die Zahl der Seiten ohne neue Gestaltung (Start ist neu gestaltet).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.common.demo_daten import DEMO_ORG_SLUG, DEMO_USERS
from apps.tenants.models import Organization
from apps.work import design_schalter
from apps.work.rahmen import neues_design

pytestmark = pytest.mark.django_db(transaction=True)

SKRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pruefe_work_design.py"
PASSWORT = "E2e-Design-Pruefung-1"
#: Nur Seiten, die die Rolle sehen darf (Gast: nur freigegebene Dokumente; Mitglied: keine Verwaltung)
NICHT_FUER = {
    "gast": ("/faction/", "/organization/", "/tasks/", "/meetings/", "/team/"),
    "mitglied": ("/organization/members/", "/organization/roles/", "/organization/email-settings/"),
    "unvereidigt": ("/organization/members/", "/organization/roles/"),
    "sachkundig": ("/organization/members/", "/organization/roles/"),
}
#: Befunde, die das CI ablehnt (Leerflächen bewertet die Abnahme an Bildern aus der Demo)
HARTE_BEFUNDE = {"Serverfehler", "Toter Link", "JS-Ausnahme", "Konsole", "Ressource", "Überlauf", "Anmeldung"}


def _skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pruefe_work_design_e2e", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules["pruefe_work_design_e2e"] = modul
    spec.loader.exec_module(modul)
    return modul


skript = _skript()


@pytest.fixture
def demo(settings: Any, tmp_path: Path) -> Organization:
    settings.MEDIA_ROOT = str(tmp_path / "media")
    call_command("setup_demo_environment", stdout=StringIO())
    for rolle in skript.ROLLEN:
        nutzer = User.objects.get(email=DEMO_USERS[rolle]["email"])
        nutzer.set_password(PASSWORT)
        nutzer.save(update_fields=["password"])
    return Organization.objects.get(slug=DEMO_ORG_SLUG)


def _schalten(aktion: str, org: str) -> str:
    """Wie der Verwaltungsbefehl work_neues_design, im Testprozess (dieselbe Datenbank wie der Live-Server)."""
    organisation = Organization.objects.get(slug=org)
    if aktion == "status":
        return json.dumps({org: neues_design(organisation)})
    design_schalter.setzen(organisation, aktion == "an")
    return ""


def test_alle_rollen_mit_schalter_an_und_aus(browser: Any, live_server: Any, demo: Organization) -> None:
    zugaenge = [skript.Zugang(rolle, DEMO_USERS[rolle]["email"], PASSWORT) for rolle in skript.ROLLEN]
    laeufe = skript.pruefen(
        live_server.url,
        DEMO_ORG_SLUG,
        zugaenge,
        schalter="beide",
        schalten=_schalten,
        breiten=(390, 1440),
        max_seiten=8,
        browser=browser,
        protokoll=lambda _: None,
    )

    assert [(lauf.rolle, lauf.schalter) for lauf in laeufe] == [
        (rolle, zustand) for zustand in ("an", "aus") for rolle in skript.ROLLEN
    ]
    # Der Schalter steht danach wieder wie in der Demo (an)
    assert neues_design(Organization.objects.get(slug=DEMO_ORG_SLUG)) is True

    text, _ = skript.zusammenfassen(laeufe)
    harte = [b for lauf in laeufe for b in lauf.befunde if b.art in HARTE_BEFUNDE]
    assert not harte, text
    for lauf in laeufe:
        assert lauf.erreichbar, f"{lauf.rolle} (Schalter {lauf.schalter}) erreicht keine Seite"
        verboten = [p for p in lauf.erreichbar for teil in NICHT_FUER.get(lauf.rolle, ()) if teil in p]
        assert not verboten, f"{lauf.rolle} erreicht {verboten}"
    vorsitz = next(lauf for lauf in laeufe if lauf.rolle == "vorsitz")
    assert len(vorsitz.erreichbar) == 8, "Der Vorsitz erreicht mehr Seiten, als der Test prüft"
    # Seiten ohne neue Gestaltung: Start ist neu gestaltet; mit ausgeschaltetem Schalter zählt nichts
    assert f"/work/{DEMO_ORG_SLUG}/" in vorsitz.neu_gestaltet
    assert "Seiten ohne neue Gestaltung (Adressmuster, alle Rollen):" in text
    for lauf in laeufe:
        if lauf.schalter == "aus":
            assert not lauf.neu_gestaltet and not lauf.ohne_neue_gestaltung, lauf.rolle


#: Hauptbereich über die ganze Breite (1.920 px) mit einem Kopfband; ``{inhalt}`` steht darunter
MESS_SEITE = (
    '<!doctype html><html lang="de"><body style="margin:0;font:16px sans-serif">'
    '<main style="display:block;width:1920px">'
    '<div style="background:#f3f4f6;padding:24px">Kopfband über die ganze Breite</div>'
    "{inhalt}"
    '<div style="position:fixed;right:8px;bottom:8px;width:320px;background:#f59e0b">Feste Einblendung</div>'
    "</main></body></html>"
)


@pytest.mark.parametrize(
    ("inhalt", "von", "bis"),
    [
        # Karte mit Rahmen über 1.600 px, darin nur kurzer Text: Die Karte füllt die Breite (Team-Detailseite)
        ('<div style="width:1600px;border:1px solid #e5e7eb"><p>Kurz</p></div>', 1595, 1605),
        # Fläche mit Hintergrund zählt ebenso
        ('<div style="width:1500px;background:#fafafa"><p>Kurz</p></div>', 1495, 1505),
        # Ohne Rahmen und Hintergrund zählt nur der Text; Kopfband und feste Einblendung zählen nie
        ('<div style="width:1600px"><p style="margin:0">Kurz</p></div>', 1, 400),
    ],
)
def test_messung_zaehlt_flaechen_aber_keine_baender_und_einblendungen(
    page: Any, inhalt: str, von: int, bis: int
) -> None:
    page.set_viewport_size({"width": 1920, "height": 900})
    page.set_content(MESS_SEITE.format(inhalt=inhalt))
    messung = page.evaluate(skript.MESSUNG)
    assert von <= messung["rechts"] <= bis, messung["rechts"]
