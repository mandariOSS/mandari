# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Einstellungen unter den Reitern „Sitzungen“, „Vorlagen“ und
„Datenschutz und Zugänge“ (Issue #944).

Geprüft: Sitzungsformate, Ortsrecht, Körperschaften, Körperschaft anlegen/bearbeiten, Wahlperioden, Textbausteine und
Standard-TOPs, Nummernkreise, Mitzeichnung und Ämter, Datenschutz und Einreichungs-Zugänge haben mit eingeschaltetem
Schalter eine eigene neue Vorlage, ohne Schalter bleiben die bisherigen – auch bei Formularen mit Fehlern. Keine
Funktion entfällt und keine kommt hinzu: Gleichstand für alle sechs Standardrollen und eine Rolle nur mit dem Recht
„Einstellungen verwalten“ in beide Richtungen, mit einem Bestand, der die Aktionen wirklich zeigt (zwei Landesprofile,
mehrere Körperschaften, Perioden mit und ohne Bezug, aktive und inaktive Einträge, abgelaufene und zurückgezogene
Zugänge), und mit leeren Beständen. Reiter und Unterpunkte folgen den Rechten, Unterseiten tragen aria-current="true"
und führen in den Brotkrumen zur Elternseite; ein neuer Token steht genau einmal da.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAPIToken,
    SessionBody,
    SessionCosignatureRule,
    SessionLegislativeTerm,
    SessionNumberRange,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionRole,
    SessionStandardAgendaItem,
    SessionStateProfile,
    SessionTenant,
    SessionTextBlock,
    SessionUser,
)
from apps.session.services import body_service, meeting_format_service
from apps.session.services.state_law_service import LocalRules
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, rollen, vergleichen
from apps.session.tests.test_neues_design import _brotkrumen, _client, _mandant, _vorlagen

pytestmark = pytest.mark.django_db

NEU = "session/neu/einstellungen/"
#: Seiten dieses Teils: Name → (Adressname, neue Vorlage, bisherige Vorlage)
SEITEN: dict[str, tuple[str, str, str]] = {
    "formate": ("settings_meeting_formats", "sitzungsformate.html", "meeting_formats.html"),
    "ortsrecht": ("settings_local_rules", "ortsrecht.html", "local_rules.html"),
    "koerperschaften": ("settings_bodies", "koerperschaften.html", "bodies.html"),
    "koerperschaft_neu": ("settings_body_create", "koerperschaft.html", "body_form.html"),
    "koerperschaft": ("settings_body_edit", "koerperschaft.html", "body_form.html"),
    "wahlperioden": ("terms", "wahlperioden.html", "terms.html"),
    "textbausteine": ("settings_textblocks", "textbausteine.html", "textblocks.html"),
    "nummernkreise": ("settings_numbering", "nummernkreise.html", "numbering.html"),
    "mitzeichnung": ("settings_cosign", "mitzeichnung.html", "cosign.html"),
    "datenschutz": ("privacy_settings", "datenschutz.html", "privacy.html"),
    "zugaenge": ("settings_api_tokens", "zugaenge.html", "api_tokens.html"),
}
#: Rolle nur mit „Einstellungen verwalten“ (ohne Benutzerverwaltung): Amts-Zuordnungen nur lesend
NUR_EINSTELLUNGEN = "Nur Einstellungen"


def _adresse(tenant: SessionTenant, name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kw})


def _seiten(tenant: SessionTenant, body: SessionBody) -> dict[str, str]:
    """Adressen der Seiten; Ortsrecht und Körperschaft für ``body``."""
    adressen: dict[str, str] = {}
    for name, (url_name, _neu, _alt) in SEITEN.items():
        kw = {"body_id": body.pk} if name in ("ortsrecht", "koerperschaft") else {}
        adressen[name] = _adresse(tenant, url_name, **kw)
    return adressen


def _profil(tenant: SessionTenant, code: str) -> None:
    """Landesprofil setzen (NI: Fassungen, Nachweis mit Zweidrittelhinweis; NW: ausgenommene Ausschussarten)."""
    SessionTenant.objects.filter(pk=tenant.pk).update(
        state_profile=SessionStateProfile.objects.get(code=code),
        hybrid_basis_kind=SessionStateProfile.BASIS_HAUPTSATZUNG,
        hybrid_basis_date=date(2024, 3, 12),
        hybrid_basis_reference="§ 7 Hauptsatzung",
    )


def _welt(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand, der jede Aktion der Seiten zeigt: Landesprofile NI und NW, Ortsrecht mit bald ablaufendem
    Notlagenbeschluss, drei Körperschaften (Standard, aktiv mit Zugehörigkeit, inaktiv), Ausschüsse eingeordnet und
    nicht eingeordnet, zwei Ämter, Perioden mit und ohne Bezug, aktive und inaktive Standard-TOPs, Textbausteine und
    Nummernkreise, eine Mitzeichnungsregel, ein Benutzer mit Amt, Personen für die Auskunft und Zugänge in drei
    Ständen (aktiv, abgelaufen, zurückgezogen).
    """
    meeting_format_service.sync_profiles()
    _profil(tenant, "NI")
    heute = timezone.localdate()
    standard = body_service.default_body(tenant)
    standard.local_rules = LocalRules.from_json(
        {
            "rules_date": "2022-03-24",
            "rules_reference": "Amtsblatt 2022 Nr. 4",
            "emergency_date": (heute - timedelta(days=60)).isoformat(),
            "emergency_until": (heute + timedelta(days=5)).isoformat(),
        }
    ).to_json()
    standard.save(update_fields=["local_rules"])
    nord = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Nord", short_name="GN", body_type="mitgliedsgemeinde", ags="03357001"
    )
    nord.parent = standard
    nord.save(update_fields=["parent"])
    SessionBody.objects.create(tenant=tenant, name="Gemeinde Alt", slug="alt", is_active=False)

    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    haupt = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", organization_type="committee")
    SessionOrganization.objects.create(
        tenant=tenant, name="Finanzausschuss", organization_type="committee", committee_kind="finance"
    )
    kaemmerei = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    SessionOrganization.objects.create(tenant=tenant, name="Bauamt", organization_type="department")

    alt = SessionLegislativeTerm.objects.create(
        tenant=tenant, name="2020–2025", number=21, start_date=date(2020, 11, 1), end_date=date(2025, 10, 31)
    )
    aktuell = SessionLegislativeTerm.objects.create(
        tenant=tenant, name="2025–2030", number=22, start_date=date(2025, 11, 1)
    )
    person = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Amberg")
    SessionPerson.objects.create(tenant=tenant, given_name="Bernd", family_name="Birk")
    SessionOrganizationMembership.objects.create(
        organization=rat, person=person, start_date=date(2025, 11, 1), legislative_term=aktuell
    )

    SessionStandardAgendaItem.objects.create(tenant=tenant, organization=rat, name="Eröffnung", order=1)
    SessionStandardAgendaItem.objects.create(
        tenant=tenant, name="Anfragen", placement="end", is_public=False, is_active=False
    )
    SessionTextBlock.objects.create(tenant=tenant, title="Einstimmig", content="Der {gremium} beschließt einstimmig.")
    SessionTextBlock.objects.create(tenant=tenant, title="Alt", content="Vertagt.", category="general", is_active=False)

    kreis = SessionNumberRange.objects.create(
        tenant=tenant, body=standard, name="Vorlagen", pattern="V/{jahr}/{lfd:4}", paper_types=["proposal"]
    )
    SessionNumberRange.objects.create(tenant=tenant, name="Alt", pattern="A/{lfd}", is_active=False, order=2)

    SessionCosignatureRule.objects.create(tenant=tenant, department=kaemmerei, order=1, only_financial=True)
    mitzeichner = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    mitzeichner.departments.add(kaemmerei)

    jetzt = timezone.now()
    aktiv, _ = SessionAPIToken.create_token(tenant=tenant, name="Fraktion A", description="Übergabe am 1.10.")
    SessionAPIToken.create_token(tenant=tenant, name="Fraktion B", expires_at=jetzt - timedelta(days=1))
    SessionAPIToken.create_token(
        tenant=tenant, name="Fraktion C", is_active=False, expires_at=jetzt + timedelta(days=9)
    )

    seiten = _seiten(tenant, standard)
    seiten["formate_nw"] = seiten["formate"]
    seiten["koerperschaft_nord"] = _adresse(tenant, "settings_body_edit", body_id=nord.pk)
    erwartet = {
        ("GET", _adresse(tenant, "settings_local_rules", body_id=standard.pk)),
        ("GET", _adresse(tenant, "organization_edit", organization_id=haupt.pk)),
        ("GET", _adresse(tenant, "settings_body_create")),
        ("GET", _adresse(tenant, "settings_body_edit", body_id=nord.pk)),
        ("POST", _adresse(tenant, "settings_body_default", body_id=nord.pk)),
        ("GET", _adresse(tenant, "archive")),
        ("POST", _adresse(tenant, "term_save")),
        ("POST", _adresse(tenant, "term_delete", term_id=alt.pk)),
        ("POST", _adresse(tenant, "term_change")),
        ("POST", _adresse(tenant, "standard_item_manage")),
        ("POST", _adresse(tenant, "textblock_manage")),
        ("POST", _adresse(tenant, "settings_numbering_save")),
        ("POST", _adresse(tenant, "cosign_rule_manage")),
        ("POST", _adresse(tenant, "department_assignment")),
        ("GET", _adresse(tenant, "organizations")),
        ("POST", _adresse(tenant, "privacy_purge")),
        ("GET", _adresse(tenant, "privacy_notice")),
        ("SKRIPT", f"/session/{tenant.slug}/persons/{{person}}/auskunft.json"),
        ("POST", _adresse(tenant, "settings_api_tokens_create")),
        ("POST", _adresse(tenant, "settings_api_tokens_revoke", token_id=aktiv.pk)),
    }
    # Fassungen des Landesrechts als Links mit Stichtag (NI)
    return {"seiten": seiten, "erwartet": erwartet, "standard": standard, "nord": nord, "kreis": kreis}


def _nur_einstellungen(tenant: SessionTenant) -> None:
    SessionRole.objects.create(tenant=tenant, name=NUR_EINSTELLUNGEN, can_manage_settings=True)


def _vorher_profil(tenant: SessionTenant) -> Any:
    def vorher(_rolle: str, name: str) -> None:
        _profil(tenant, "NW" if name == "formate_nw" else "NI")

    return vorher


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("einst2", neu=False)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, (_url, neu_vorlage, alt_vorlage) in SEITEN.items():
        adresse = welt["seiten"][name]
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse)
        assert alt.status_code == 200, name
        assert {f"session/settings/{alt_vorlage}", "session/base_session_alt.html"} <= _vorlagen(alt), name
        assert NEU + neu_vorlage not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(adresse)
        assert neu.status_code == 200, name
        assert {NEU + neu_vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert f"session/settings/{alt_vorlage}" not in _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


def test_formulare_mit_fehlern_bleiben_im_erscheinungsbild_des_mandanten() -> None:
    """Sitzungsformate, Ortsrecht und Körperschaft rendern Fehler in der Vorlage, die der Schalter wählt."""
    tenant = _mandant("fehler2", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    faelle = (
        ("formate", {"hybrid_basis_kind": SessionStateProfile.BASIS_HAUPTSATZUNG}, "Art, Datum und Fundstelle"),
        ("ortsrecht", {"recording_date": "2024-01-10"}, "bitte Datum und Fundstelle angeben"),
        ("koerperschaft_neu", {"name": "Gemeinde Süd", "slug": "alt", "is_active": "on"}, "schon vergeben"),
    )
    for neu in (True, False):
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=neu)
        for name, daten, fehler in faelle:
            antwort = client.post(welt["seiten"][name], daten)
            assert antwort.status_code == 200, name
            assert fehler in antwort.content.decode(), name
            vorlage = NEU + SEITEN[name][1] if neu else f"session/settings/{SEITEN[name][2]}"
            assert vorlage in _vorlagen(antwort), (name, neu)


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_fuer_alle_rollen() -> None:
    """
    Jede Aktion der bisherigen Seiten steht mit denselben Merkmalen (Rückfrage, Optionen, feste Werte) auf der neuen,
    und die neue bietet nichts an, was die bisherige nicht hatte – außer erreichbaren Links (Reiter, Unterpunkte, Weg
    zurück). Für alle sechs Standardrollen (nur der Administrator darf die Seiten öffnen, die übrigen erhalten auf beiden
    403) und eine Rolle nur mit „Einstellungen verwalten“ (Amts-Zuordnungen nur lesend). Die Sitzungsformate laufen
    einmal mit Niedersachsen (Fassungen, Nachweis mit Hinweis, Notlagenwarnung) und einmal mit NRW (ausgenommene
    Ausschussarten, eingeordnete und nicht eingeordnete Ausschüsse).
    """
    tenant = _mandant("gleich2", neu=False)
    welt = _welt(tenant)
    _nur_einstellungen(tenant)
    fehlt, gesehen = vergleichen(
        tenant,
        welt["seiten"],
        rollen_liste=[*rollen(), NUR_EINSTELLUNGEN],
        ausnahmen=reiter_ausnahmen(tenant),
        vorher=_vorher_profil(tenant),
    )
    assert not fehlt, "\n".join(fehlt)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))
    # Die Fassungen des Landesrechts (Links mit ?stichtag=) und die Vorlagen für verbreitete Praxis waren dabei
    formate = welt["seiten"]["formate"]
    assert any(a.startswith(formate + "?stichtag=") for _m, a in gezeigt(gesehen))
    praxis = {felder for m, a, felder in gesehen if a == _adresse(tenant, "settings_numbering_save")}
    assert any("action=preset" in f for felder in praxis for f in felder)
    assert any("action=next" in f for felder in praxis for f in felder)


def test_gleichstand_mit_leeren_bestaenden() -> None:
    """Ohne Perioden, Bausteine, Kreise, Ämter, Regeln und Zugänge: Leerzustände mit denselben Wegen wie bisher."""
    tenant = _mandant("leer2", neu=False)
    seiten = _seiten(tenant, body_service.default_body(tenant))
    seiten.pop("koerperschaft_neu")
    fehlt, gesehen = vergleichen(tenant, seiten, rollen_liste=["Administrator"], ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    # Ohne Ämter führt die Seite zum Anlegen eines Amts (statt des Formulars für Regeln)
    assert ("GET", _adresse(tenant, "organization_create")) in gezeigt(gesehen)


# =============================================================================
# Reiter, Unterpunkte und Brotkrumen
# =============================================================================


def _navi(antwort: Any) -> dict[str, Any]:
    return cast(dict[str, Any], antwort.context["session_rahmen"]["einstellungen"])


def _unterpunkte(html: str) -> str:
    anfang = html.index('data-testid="einstellungen-unterpunkte"')
    return html[anfang : html.index("</nav>", anfang)]


def test_reiter_und_unterpunkte_je_rolle() -> None:
    tenant = _mandant("reiter2", neu=True)
    welt = _welt(tenant)
    _nur_einstellungen(tenant)
    formate = welt["seiten"]["formate"]

    voll = _navi(_client(tenant, "Administrator").get(formate))
    assert [r["label"] for r in voll["reiter"]] == [
        "Allgemein",
        "Benutzer und Rechte",
        "Sitzungen",
        "Vorlagen",
        "Datenschutz und Zugänge",
    ]
    assert [u["label"] for u in voll["unterpunkte"]] == [
        "Sitzungsformate",
        "Körperschaften",
        "Wahlperioden",
        "Textbausteine und Standard-TOPs",
    ]

    # Ohne Benutzerverwaltung bleibt unter „Benutzer und Rechte“ nur das Vier-Augen-Prinzip
    client = _client(tenant, NUR_EINSTELLUNGEN)
    teil = _navi(client.get(formate))
    benutzer = next(r for r in teil["reiter"] if r["key"] == "benutzer")
    assert benutzer["url"] == _adresse(tenant, "settings_four_eyes")
    for name in ("nummernkreise", "mitzeichnung"):
        navi = _navi(client.get(welt["seiten"][name]))
        assert [u["label"] for u in navi["unterpunkte"]] == ["Nummernkreise", "Mitzeichnung und Ämter"], name
    for name in ("datenschutz", "zugaenge"):
        navi = _navi(client.get(welt["seiten"][name]))
        assert [u["label"] for u in navi["unterpunkte"]] == ["Datenschutz", "Einreichungs-Zugänge"], name

    # Die übrigen Standardrollen öffnen die Seiten nicht (Recht „Einstellungen verwalten“)
    for rolle in rollen():
        if rolle == "Administrator":
            continue
        andere = _client(tenant, rolle)
        for name, adresse in welt["seiten"].items():
            assert andere.get(adresse).status_code == 403, (rolle, name)


def test_aria_current_der_unterpunkte() -> None:
    """Die Seite selbst trägt aria-current="page", Unterseiten (Ortsrecht, Körperschaft-Formular) "true"."""
    tenant = _mandant("aktuell2", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    seiten = welt["seiten"]
    faelle = {
        "formate": ("settings_meeting_formats", "page"),
        "ortsrecht": ("settings_meeting_formats", "true"),
        "koerperschaften": ("settings_bodies", "page"),
        "koerperschaft_neu": ("settings_bodies", "true"),
        "koerperschaft": ("settings_bodies", "true"),
        "wahlperioden": ("terms", "page"),
        "textbausteine": ("settings_textblocks", "page"),
        "nummernkreise": ("settings_numbering", "page"),
        "mitzeichnung": ("settings_cosign", "page"),
        "datenschutz": ("privacy_settings", "page"),
        "zugaenge": ("settings_api_tokens", "page"),
    }
    for name, (url_name, wert) in faelle.items():
        html = client.get(seiten[name]).content.decode()
        unterpunkte = _unterpunkte(html)
        assert f'href="{_adresse(tenant, url_name)}" aria-current="{wert}"' in unterpunkte, name
        assert unterpunkte.count("aria-current=") == 1, name
    # Der Reiter „Sitzungen“ führt zur ersten Seite (Sitzungsformate): dort "page", auf den übrigen Seiten "true"
    reiter = {r["key"]: r for r in _navi(client.get(seiten["formate"]))["reiter"]}
    assert reiter["sitzungen"]["aktiv"] and reiter["sitzungen"]["current"] == "page"
    reiter = {r["key"]: r for r in _navi(client.get(seiten["wahlperioden"]))["reiter"]}
    assert reiter["sitzungen"]["aktiv"] and reiter["sitzungen"]["current"] == "true"


def test_koerperschaften_im_reiter_erst_ab_der_zweiten() -> None:
    tenant = _mandant("eine2", neu=True)
    standard = body_service.default_body(tenant)
    client = _client(tenant, "Administrator")
    seiten = _seiten(tenant, standard)
    labels = [u["label"] for u in _navi(client.get(seiten["formate"]))["unterpunkte"]]
    assert "Körperschaften" not in labels
    # Geöffnet erscheint der Unterpunkt trotzdem (die Seite bleibt per Adresse erreichbar)
    for name in ("koerperschaften", "koerperschaft_neu", "koerperschaft"):
        assert "Körperschaften" in [u["label"] for u in _navi(client.get(seiten[name]))["unterpunkte"]], name
    # Eine zweite, auch inaktive Körperschaft macht ihn sichtbar
    SessionBody.objects.create(tenant=tenant, name="Gemeinde Alt", slug="alt", is_active=False)
    labels = [u["label"] for u in _navi(client.get(seiten["formate"]))["unterpunkte"]]
    assert "Körperschaften" in labels


def test_brotkrumen_der_unterseiten_fuehren_zur_elternseite() -> None:
    tenant = _mandant("krumen2", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    seiten = welt["seiten"]
    for seite, eltern in (
        ("ortsrecht", "settings_meeting_formats"),
        ("koerperschaft_neu", "settings_bodies"),
        ("koerperschaft", "settings_bodies"),
    ):
        html = client.get(seiten[seite]).content.decode()
        assert f'href="{_adresse(tenant, eltern)}"' in _brotkrumen(html), seite
    # Bearbeiten nennt die Körperschaft in der letzten Krume, Anlegen „Anlegen“
    assert "Gemeinde Nord" in _brotkrumen(client.get(seiten["koerperschaft_nord"]).content.decode())
    assert "Anlegen" in _brotkrumen(client.get(seiten["koerperschaft_neu"]).content.decode())


# =============================================================================
# Einreichungs-Zugänge: neuer Token genau einmal
# =============================================================================


def test_neuer_token_steht_genau_einmal_da() -> None:
    tenant = _mandant("token2", neu=True)
    client = _client(tenant, "Administrator")
    liste = _adresse(tenant, "settings_api_tokens")
    antwort = client.post(
        _adresse(tenant, "settings_api_tokens_create"),
        {"name": "Fraktion Grün", "can_read_meetings": "on"},
        follow=True,
    )
    assert antwort.status_code == 200 and NEU + "zugaenge.html" in _vorlagen(antwort)
    html = antwort.content.decode()
    treffer = re.search(r'<code id="new-token" x-ref="token"[^>]*>([^<]+)</code>', html)
    assert treffer is not None, "Token fehlt"
    roh = treffer.group(1)
    token = SessionAPIToken.objects.get(tenant=tenant, name="Fraktion Grün")
    assert token.token == SessionAPIToken.hash_token(roh) and token.can_read_meetings and not token.can_read_papers
    assert 'data-testid="neuer-token"' in html and "Kopieren" in html
    # Der Token steht nur im Code-Feld, nicht zusätzlich in einem Skriptattribut
    assert html.count(roh) == 1
    # Beim nächsten Aufruf ist er weg, nur noch sein Anfang steht in der Liste
    wieder = client.get(liste).content.decode()
    assert roh not in wieder and 'id="new-token"' not in wieder and f"{token.token_prefix}…" in wieder


# =============================================================================
# Darstellung
# =============================================================================


def test_keine_farbigen_fuellknoepfe_im_inhalt() -> None:
    """Höchstens die eine Hauptaktion im Kopfband trägt die Füllfarbe (Körperschaft anlegen), nie der Inhalt."""
    tenant = _mandant("knoepfe2", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        html = client.get(adresse).content.decode()
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        assert "bg-primary-600 hover:bg-primary-700" not in inhalt, name
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple"):
            # Knöpfe tragen ihre Farbe samt Hover; der Punkt des Stands (session/neu/_status.html) ist kein Knopf
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, f"{name}: Knopf in {farbe}"
        kopf = html[html.index('id="inhalt"') : html.index('class="work-page-content"')]
        assert kopf.count("bg-primary-600 hover:bg-primary-700") <= 1, name


def test_vorlagen_ohne_kleine_schrift_und_einzeilige_kommentare() -> None:
    ordner = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu" / "einstellungen"
    dateien = [
        *(f"{n}.html" for n in ("sitzungsformate", "ortsrecht", "koerperschaften", "koerperschaft", "wahlperioden")),
        *(f"{n}.html" for n in ("textbausteine", "nummernkreise", "mitzeichnung", "datenschutz", "zugaenge")),
        "_sitzungsformate_recht.html",
        "_sitzungsformate_ausschuesse.html",
        "_nummernkreise_felder.html",
        "_zahlfeld.html",
    ]
    for datei in dateien:
        text = (ordner / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        for verboten in ("c-ui.card", "c-ui.badge", "c-ui.kpi-tile", "rounded-xl", "onclick=", "rounded-full bg-"):
            assert verboten not in text, f"{datei}: {verboten}"
        assert not re.search(r"\{#(?:(?!#\}).)*\n", text), f"{datei}: mehrzeiliger {{# #}}-Kommentar"
