# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen in fünf Bereichen (Issue #1230, Teil von #1226): Organisation, Mitglieder und Rechte, Sitzungen,
Dokumente sowie Rat und Verwaltung.

Geprüft wird:

- Reiter und Unterpunkte nur mit Zugriff: Was die Navigation zeigt, öffnet sich; was sich öffnet, zeigt sie.
- Weiterleitungen: Die Dokumenteinstellungen leiten auf die eine Übersicht, die Verwaltungskontakte im bisherigen
  Erscheinungsbild auf die Ratsfraktionen. Keine Funktion fällt weg.
- Zweitfarbe im Profil, Anmeldesicherheit mit allen Rollen, Aufgaben-Labels in den Einstellungen.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.urls import reverse

from apps.common.tests.factories import OrganizationFactory, RoleFactory
from apps.tenants.models import AdministrationContact, CouncilParty, Topic
from apps.work.organization import einstellungen, selectors
from apps.work.tasks.models import TaskLabel

from .einstellungen_hilfen import (
    fixtures_uebernehmen,
    html,
    kontakte,
    unterpunkte_anderer_reiter,
    url,
    vorlagen,
)
from .gleichstand import fehlend, funktionen

pytestmark = pytest.mark.django_db
fixtures_uebernehmen(globals())


@pytest.fixture
def aufgaben(org: Any, make_member: Any) -> Any:
    """Pflegt Aufgaben und ihre Labels, darf die Organisation aber nicht bearbeiten."""
    return make_member(org, ["dashboard.view", "organization.view", "tasks.manage"], email="aufgaben@example.org")


def _seiten_url(org: Any, seite: einstellungen.Seite) -> str:
    adresse = reverse(seite.url_name, kwargs={"org_slug": org.slug})
    return f"{adresse}?{seite.abfrage}" if seite.abfrage else adresse


def _gezeigt(org: Any, person: Any) -> set[str]:
    """Alle Ziele, die die Navigation der Person auf irgendeiner Seite der Einstellungen zeigt."""
    ziele: set[str] = set()
    for reiter in einstellungen.REITER:
        for seite in reiter.seiten:
            navi = einstellungen.navigation(org, person, seite.key)
            ziele |= {r["url"] for r in navi["reiter"]} | {u["url"] for u in navi["unterpunkte"]}
    return ziele


def _unterpunkte(seite: str) -> list[str]:
    teil = seite.split('data-testid="einstellungen-unterpunkte"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r"<a [^>]*>\s*([^<]+?)\s*</a>", teil)


# ---------------------------------------------------------------------------
# Reiter und Unterpunkte nur mit Zugriff
# ---------------------------------------------------------------------------


def test_reiter_nur_mit_zugriff(
    org: Any, rollen: dict[str, Any], aufgaben: Any, make_member: Any, client_for: Any
) -> None:
    """Je Rolle: Jede Seite, die die Navigation zeigt, öffnet sich, und jede Seite, die sich öffnet, steht darin."""
    personen = {
        **rollen,
        "aufgaben": aufgaben,
        "sitzungsinfo": make_member(org, ["dashboard.view", "faction.view_public"], email="info@example.org"),
    }
    for name, person in personen.items():
        client = client_for(person.user)
        gezeigt = _gezeigt(org, person)
        for reiter in einstellungen.REITER:
            for seite in reiter.seiten:
                ziel = _seiten_url(org, seite)
                code = client.get(ziel).status_code
                assert (ziel in gezeigt) == (code == 200), (name, ziel, code)


def test_unterpunkte_der_fuenf_bereiche(org: Any, admin: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    erwartet = {
        "organization": ["Profil", "E-Mail-Versand", "Themen und Labels"],
        "members": ["Mitglieder", "Rollen", "Registrierung", "Anmeldesicherheit"],
        "organization_faction_settings": ["Fraktionssitzungen", "Veröffentlichung"],
        "organization_documents": ["Übersicht", "Dokumenttypen", "Vorlagen", "Briefköpfe"],
        "council_parties": ["Ratsfraktionen", "Verwaltungskontakte", "Einreichung"],
    }
    for name, punkte in erwartet.items():
        assert _unterpunkte(html(client.get(url(name, org)))) == punkte, name
    sicherheit = html(client.get(f"{url('organization', org)}?bereich=sicherheit"))
    assert _unterpunkte(sicherheit) == erwartet["members"]


# ---------------------------------------------------------------------------
# Dokumente: eine Übersicht
# ---------------------------------------------------------------------------


def test_dokumenteinstellungen_leiten_auf_die_uebersicht(org: Any, admin: Any, leser: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    antwort = client.get(url("document_settings", org))
    assert antwort.status_code == 302
    assert antwort["Location"] == url("organization_documents", org)
    # Bisheriges Erscheinungsbild unverändert: die bisherige Seite
    assert "work/motions/settings/index.html" in vorlagen(client.get(url("document_settings", org) + "?ansicht=bisher"))
    assert client_for(leser.user).get(url("document_settings", org)).status_code == 403
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert "work/motions/settings/index.html" in vorlagen(client.get(url("document_settings", org)))


def test_dokumenteinstellungen_stehen_in_der_uebersicht(org: Any, admin: Any, client_for: Any) -> None:
    """Die Übersicht trägt alle Wege der bisherigen Dokumenteinstellungen; Logo und Farben nur als Verweis."""
    client = client_for(admin.user)
    bisher = url("document_settings", org)
    alt = funktionen(html(client.get(f"{bisher}?ansicht=bisher")), bisher)
    seite = html(client.get(url("organization_documents", org)))
    neu = funktionen(seite, url("organization_documents", org))
    assert fehlend(alt, neu, unterpunkte_anderer_reiter(org, "uebersicht")) == []
    assert 'id="erscheinung-titel"' in seite
    assert f'href="{url("organization", org)}"' in seite
    assert 'name="primary_color"' not in seite


# ---------------------------------------------------------------------------
# Organisation › Profil: Zweitfarbe
# ---------------------------------------------------------------------------


def test_zweitfarbe_im_profil(org: Any, admin: Any, leser: Any, client_for: Any) -> None:
    client = client_for(admin.user)
    pfad = url("organization", org)
    assert 'name="secondary_color"' in html(client.get(pfad))
    felder = {"action": "update_general", "name": org.name, "description": ""}
    antwort = client.post(pfad, {**felder, "primary_color": "#112233", "secondary_color": "#445566"})
    assert antwort.status_code == 302
    org.refresh_from_db()
    assert (org.primary_color, org.secondary_color) == ("#112233", "#445566")
    # Ungültig oder nicht mitgeschickt (bisherige Vorlage): bleibt
    client.post(pfad, {**felder, "secondary_color": "rot"})
    client.post(pfad, felder)
    org.refresh_from_db()
    assert org.secondary_color == "#445566"
    nur_lesend = html(client_for(leser.user).get(pfad))
    assert "#445566" in nur_lesend
    assert 'name="secondary_color"' not in nur_lesend


# ---------------------------------------------------------------------------
# Mitglieder und Rechte › Anmeldesicherheit
# ---------------------------------------------------------------------------


def test_anmeldesicherheit_zeigt_jede_rolle(org: Any, admin: Any, leser: Any, client_for: Any) -> None:
    RoleFactory(organization=org, name="Mitgliederpflege", permissions=["members.invite"])  # type: ignore[no-untyped-call]
    RoleFactory(organization=org, name="Vorstand", permissions=["motions.view"], require_2fa=True)  # type: ignore[no-untyped-call]
    RoleFactory(organization=org, name="Basis", permissions=["motions.view"])  # type: ignore[no-untyped-call]
    stand = {e.role.name: (e.pflicht, e.grund) for e in selectors.roles_two_factor(org)}
    admin_rolle = admin.roles.get().name
    assert stand[admin_rolle] == (True, "Immer, Administration")
    assert stand["Mitgliederpflege"][0] and stand["Mitgliederpflege"][1].startswith("Immer")
    assert stand["Vorstand"] == (True, "Verlangt, in der Rolle eingestellt")
    assert stand["Basis"] == (False, "Freiwillig")

    pfad = f"{url('organization', org)}?bereich=sicherheit"
    seite = html(client_for(admin.user).get(pfad))
    liste = seite.split('data-testid="rollen-zweiter-faktor"', 1)[1].split("</ul>", 1)[0]
    for name in ("Mitgliederpflege", "Vorstand", "Basis", admin_rolle):
        assert name in liste
    assert 'href="/work/fraktion-test/organization/roles/' in liste
    # Ohne Rollenverwaltung: dieselbe Liste ohne Links
    liste_lesend = html(client_for(leser.user).get(pfad)).split('data-testid="rollen-zweiter-faktor"', 1)[1]
    assert "/organization/roles/" not in liste_lesend.split("</ul>", 1)[0]

    org.require_2fa = True
    org.save(update_fields=["require_2fa"])
    stand = {e.role.name: (e.pflicht, e.grund) for e in selectors.roles_two_factor(org)}
    assert stand["Basis"] == (True, "Verlangt, gilt für alle Mitglieder")


# ---------------------------------------------------------------------------
# Organisation › Themen und Labels
# ---------------------------------------------------------------------------


def test_labels_mit_tasks_manage(org: Any, aufgaben: Any, client_for: Any) -> None:
    client = client_for(aufgaben.user)
    pfad = url("document_topic_list", org)
    seite = html(client.get(pfad))
    assert 'value="add_label"' in seite
    assert 'id="thema-neu"' not in seite
    antwort = client.post(pfad, {"action": "add_label", "name": "Presse", "color": "green"})
    assert antwort.status_code == 302
    assert antwort["Location"] == pfad
    label = TaskLabel.objects.get(organization=org, name="Presse")
    assert label.color == "green"
    assert "Presse" in html(client.get(pfad))
    # Derselbe Name in anderer Schreibweise legt kein zweites Label an
    client.post(pfad, {"action": "add_label", "name": "presse", "color": "red"})
    assert TaskLabel.objects.filter(organization=org).count() == 1
    # Themen bleiben organization.edit vorbehalten, die bisherige Vorlage auch
    assert client.post(pfad, {"name": "Verkehr", "color": "blue"}).status_code == 403
    assert not Topic.objects.filter(organization=org).exists()
    assert client.get(f"{pfad}?ansicht=bisher").status_code == 403
    client.post(pfad, {"action": "delete_label", "label_id": str(label.id)})
    assert not TaskLabel.objects.filter(id=label.id).exists()


def test_themen_ohne_tasks_manage(org: Any, rollen: dict[str, Any], client_for: Any) -> None:
    """organization.edit ohne tasks.manage: Themen wie bisher, keine Labels."""
    client = client_for(rollen["bearbeitung"].user)
    pfad = url("document_topic_list", org)
    seite = html(client.get(pfad))
    assert 'id="thema-neu"' in seite
    assert 'value="add_label"' not in seite
    assert client.post(pfad, {"action": "add_label", "name": "Presse", "color": "green"}).status_code == 403
    assert not TaskLabel.objects.filter(organization=org).exists()
    assert client.post(pfad, {"name": "Verkehr", "color": "blue"}).status_code == 302
    assert Topic.objects.filter(organization=org, name="Verkehr").exists()


def test_label_einer_fremden_organisation(org: Any, admin: Any, client_for: Any) -> None:
    fremde_org: Any = OrganizationFactory()  # type: ignore[no-untyped-call]
    fremd = TaskLabel.objects.create(organization=fremde_org, name="Fremd", color="red")
    client = client_for(admin.user)
    pfad = url("document_topic_list", org)
    assert client.post(pfad, {"action": "delete_label", "label_id": str(fremd.id)}).status_code == 404
    assert client.post(pfad, {"action": "delete_label", "label_id": "kein-label"}).status_code == 404
    assert TaskLabel.objects.filter(id=fremd.id).exists()


# ---------------------------------------------------------------------------
# Rat und Verwaltung › Verwaltungskontakte
# ---------------------------------------------------------------------------


def test_verwaltungskontakte_auf_eigener_seite(org: Any, admin: Any, leser: Any, client_for: Any) -> None:
    AdministrationContact.objects.create(organization=org, label="Ratsbüro", email="ratsbuero@example.org")
    client = client_for(admin.user)
    pfad = url("organization_admin_contacts", org)
    antwort = client.get(pfad)
    assert "work/organization/neu/verwaltungskontakte.html" in vorlagen(antwort)
    assert "Ratsbüro" in html(antwort)

    antwort = client.post(
        pfad, {"action": "add_admin_contact", "contact_label": "Kämmerei", "contact_email": "kaemmerei@example.org"}
    )
    assert antwort["Location"] == pfad
    kontakt = AdministrationContact.objects.get(organization=org, label="Kämmerei")
    client.post(pfad, {"action": "delete_admin_contact", "contact_id": str(kontakt.id)})
    assert not AdministrationContact.objects.filter(id=kontakt.id).exists()
    # Nur die Kontakte: Aktionen der Ratsfraktionen nimmt die Seite nicht an
    client.post(pfad, {"action": "add_party", "name": "Neue Liste", "short_name": "NL"})
    assert not CouncilParty.objects.filter(organization=org).exists()

    assert client_for(leser.user).get(pfad).status_code == 403
    # Bisheriges Erscheinungsbild: Die Kontakte stehen auf der Seite der Ratsfraktionen
    antwort = client.get(f"{pfad}?ansicht=bisher")
    assert antwort.status_code == 302
    assert antwort["Location"] == f"{url('council_parties', org)}?ansicht=bisher"


def test_kontakte_tragen_die_bisherigen_formulare(org: Any, admin: Any, client_for: Any) -> None:
    """Die Formulare der Kontakte von der bisherigen Seite der Ratsfraktionen stehen auf der eigenen Seite (dort an
    die eigene Adresse); die neue Seite der Ratsfraktionen hat keine mehr, die bisherige weiter alle."""
    AdministrationContact.objects.create(organization=org, label="Ratsbüro", email="ratsbuero@example.org")
    client = client_for(admin.user)
    rat = url("council_parties", org)
    alt = kontakte(funktionen(html(client.get(f"{rat}?ansicht=bisher")), rat))
    assert any("add_admin_contact" in k for k in alt) and any("delete_admin_contact" in k for k in alt)
    # Gelesen mit der Adresse der Ratsfraktionen: dieselben Kennungen, nur das Ziel ist die eigene Seite
    eigene = html(client.get(url("organization_admin_contacts", org)))
    assert alt <= funktionen(eigene, rat)
    assert kontakte(funktionen(html(client.get(rat)), rat)) == set()
