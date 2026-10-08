# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter „neues Design“ je Organisation und Verwaltungsbefehl ``work_neues_design`` (Issue #884).

Kern der Prüfung: Ein- und Ausschalten ändert nur diesen einen Schalter. Verglichen wird dazu der gesamte
Datenbestand (jede Zeile jedes Modells) vor und nach dem Umschalten – nur das Feld ``work_new_design`` der
geschalteten Organisation darf sich unterscheiden, auch ``updated_at`` und ``settings`` bleiben gleich.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import Any, cast

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command

from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.tenants.models import Organization
from apps.work import design_schalter
from apps.work.models import FactionAgendaItem, FactionMeeting, Motion, Task
from apps.work.rahmen import neues_design

pytestmark = pytest.mark.django_db


def _befehl(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("work_neues_design", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def _bestand() -> dict[str, list[dict[str, Any]]]:
    """Jede Zeile jedes Modells (sortiert nach Primärschlüssel)."""
    return {
        modell._meta.label: list(modell._default_manager.order_by("pk").values())
        for modell in apps.get_models()
        if modell._meta.managed and not modell._meta.proxy
    }


def _ohne_schalter(bestand: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Bestand ohne das Feld des Schalters an den Organisationen."""
    kopie = dict(bestand)
    kopie[Organization._meta.label] = [
        {k: v for k, v in zeile.items() if k != design_schalter.FELD} for zeile in bestand[Organization._meta.label]
    ]
    return kopie


@pytest.fixture
def welt() -> dict[str, Any]:
    """Zwei Organisationen mit Einstellungen, Mitgliedern und Inhalten."""
    org = cast(Any, OrganizationFactory)(
        slug="fraktion-a",
        settings={"faction": {"invitation_mode": "approval", "agenda_reminder_enabled": True}, "eigenes": [1, 2]},
    )
    andere = cast(Any, OrganizationFactory)(slug="fraktion-b", settings={"faction": {"quorum_rule": "half"}})
    mitglied = cast(Any, MembershipFactory)(organization=org)
    cast(Any, MembershipFactory)(organization=andere)
    Task.objects.create(organization=org, created_by=mitglied, title="Aufgabe bleibt")
    Motion.objects.create(organization=org, title="Antrag bleibt", author=mitglied)
    sitzung = FactionMeeting.objects.create(organization=org, title="Sitzung bleibt", start="2026-10-12T18:00:00Z")
    FactionAgendaItem.objects.create(meeting=sitzung, title="TOP bleibt", number="1")
    org.refresh_from_db()
    andere.refresh_from_db()
    return {"org": org, "andere": andere}


class TestHilfsfunktion:
    def test_standard_ist_aus(self, welt: dict[str, Any]) -> None:
        assert neues_design(welt["org"]) is False

    def test_setzen_meldet_aenderung(self, welt: dict[str, Any]) -> None:
        org = welt["org"]
        assert design_schalter.setzen(org, True) is True
        assert design_schalter.setzen(org, True) is False
        org.refresh_from_db()
        assert neues_design(org) is True
        assert design_schalter.setzen(org, False) is True
        org.refresh_from_db()
        assert neues_design(org) is False

    def test_veraltete_instanz_ueberschreibt_keine_neueren_einstellungen(self, welt: dict[str, Any]) -> None:
        """Wer zwischendurch andere Einstellungen speichert, verliert sie nicht durch das Umschalten."""
        veraltet = Organization.objects.get(pk=welt["org"].pk)
        frisch = Organization.objects.get(pk=welt["org"].pk)
        frisch.settings["faction"]["invitation_mode"] = "auto"
        frisch.name = "Neuer Name"
        frisch.save(update_fields=["settings", "name"])

        design_schalter.setzen(veraltet, True)

        gespeichert = Organization.objects.get(pk=welt["org"].pk)
        assert gespeichert.settings["faction"]["invitation_mode"] == "auto"
        assert gespeichert.name == "Neuer Name"
        assert neues_design(gespeichert) is True
        assert neues_design(veraltet) is True, "die übergebene Instanz zeigt den neuen Stand"

    def test_veralteter_stand_der_instanz_zaehlt_nicht(self, welt: dict[str, Any]) -> None:
        """Maßgeblich ist der Stand in der Datenbank, nicht der der übergebenen Instanz."""
        veraltet = Organization.objects.get(pk=welt["org"].pk)
        design_schalter.setzen(Organization.objects.get(pk=welt["org"].pk), True)
        assert neues_design(veraltet) is False

        assert design_schalter.setzen(veraltet, False) is True
        assert neues_design(Organization.objects.get(pk=welt["org"].pk)) is False


class TestKeineAnderenDaten:
    def test_ein_und_ausschalten_aendert_nur_den_schalter(self, welt: dict[str, Any]) -> None:
        vorher = _bestand()

        _befehl("an", "--org", "fraktion-a")
        eingeschaltet = _bestand()
        assert eingeschaltet != vorher, "der Schalter selbst muss sich ändern"
        assert _ohne_schalter(eingeschaltet) == _ohne_schalter(vorher)
        assert neues_design(Organization.objects.get(slug="fraktion-a")) is True
        assert neues_design(Organization.objects.get(slug="fraktion-b")) is False
        assert Organization.objects.get(slug="fraktion-a").updated_at == welt["org"].updated_at

        _befehl("aus", "--org", "fraktion-a")
        ausgeschaltet = _bestand()
        assert _ohne_schalter(ausgeschaltet) == _ohne_schalter(vorher)
        assert neues_design(Organization.objects.get(slug="fraktion-a")) is False

    def test_probelauf_schreibt_nichts(self, welt: dict[str, Any]) -> None:
        vorher = _bestand()
        ausgabe = _befehl("an", "--org", "fraktion-a", "--probelauf")
        assert "würde von aus auf an schalten" in ausgabe
        assert "Probelauf: 1 von 1" in ausgabe
        assert _bestand() == vorher


class TestBefehl:
    def test_status_aller_organisationen(self, welt: dict[str, Any]) -> None:
        design_schalter.setzen(welt["andere"], True)
        ausgabe = _befehl("status")
        assert "fraktion-a: neues Design aus" in ausgabe
        assert "fraktion-b: neues Design an" in ausgabe
        assert "Neues Design an: 1 von 2" in ausgabe

    def test_status_als_json(self, welt: dict[str, Any]) -> None:
        design_schalter.setzen(welt["org"], True)
        assert json.loads(_befehl("status", "--org", "fraktion-a", "--json")) == {"fraktion-a": True}

    def test_bereits_im_gewuenschten_zustand(self, welt: dict[str, Any]) -> None:
        ausgabe = _befehl("aus", "--org", "fraktion-a")
        assert "bereits aus – nichts zu tun" in ausgabe
        assert "0 von 1 Organisation(en) umgeschaltet" in ausgabe

    def test_rueckweg_fuer_alle(self, welt: dict[str, Any]) -> None:
        design_schalter.setzen(welt["org"], True)
        design_schalter.setzen(welt["andere"], True)
        _befehl("aus", "--alle")
        assert not any(neues_design(org) for org in Organization.objects.all())

    @pytest.mark.parametrize(
        ("argumente", "meldung"),
        [
            (("an", "--alle"), "je Organisation"),
            (("an",), "--org <slug>"),
            (("aus",), "--org <slug>"),
            (("an", "--org", "gibt-es-nicht"), "nicht gefunden: gibt-es-nicht"),
            (("status", "--org", "fraktion-a", "--alle"), "nicht beides"),
            (("an", "--org", "fraktion-a", "--json"), "nur für status"),
        ],
    )
    def test_fehlbedienung(self, welt: dict[str, Any], argumente: tuple[str, ...], meldung: str) -> None:
        vorher = _bestand()
        with pytest.raises(CommandError, match=meldung):
            _befehl(*argumente)
        assert _bestand() == vorher


# ---- Kennzeichnung der Gestaltung (Prüfskript zählt Seiten ohne neue Gestaltung) ----------------------------


def test_neue_seiten_tragen_die_kennzeichnung(org: Organization, make_member: Any, client_for: Any) -> None:
    """Im neuen Rahmen sagt ``data-gestaltung``, ob der Inhalt neu gestaltet ist; im bisherigen fehlt sie."""
    from django.urls import reverse

    vorsitz = make_member(org, [], email="vorsitz@example.org", is_admin=True)
    client = client_for(vorsitz.user)

    def html(name: str) -> str:
        antwort = client.get(reverse(f"work:{name}", kwargs={"org_slug": org.slug}))
        assert antwort.status_code == 200, (name, antwort.status_code)
        return str(antwort.content.decode())

    design_schalter.setzen(org, True)
    assert 'data-gestaltung="neu"' in html("dashboard")
    assert 'data-gestaltung="bisher"' in html("team")
    design_schalter.setzen(org, False)
    assert "data-gestaltung" not in html("dashboard")
    assert "data-gestaltung" not in html("team")
