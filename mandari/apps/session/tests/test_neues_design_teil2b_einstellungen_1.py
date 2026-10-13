# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Einstellungen, Reiter „Allgemein“ und „Benutzer und Rechte“
(Issue #944).

Geprüft: Übersicht, Veröffentlichung beenden (Auswahl und Bestätigung), Benutzer, Benutzer einladen (auch mit Fehlern),
Rollen und Rechte (neu und bearbeiten), Vier-Augen-Prinzip und Vertretungen wählen mit Schalter ihre neue Vorlage, ohne
Schalter bleiben die bisherigen. Keine Funktion entfällt und keine kommt hinzu: Jede Aktion der bisherigen Seite steht
mit denselben Merkmalen auf der neuen und umgekehrt (``gleichstand.vergleichen``) – für die sechs Standardrollen und
zwei eigene Rollen nur mit „Benutzer verwalten“ bzw. „Einstellungen verwalten“, die Übersicht in jedem Stand der
Veröffentlichung. Die Kacheln der bisherigen Übersicht entfallen, weil die Reiter und Unterpunkte sie tragen: Jede
entfallene Kachel-Adresse ist für dieselbe Rolle über einen Reiter der Übersicht und dessen Unterpunkt erreichbar.
Reiter und Unterpunkte erscheinen nach Recht, mit aria-current, und „Benutzer einladen“ sowie „Vertretungen“ führen in
den Brotkrumen zurück zu „Benutzer“.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session import einstellungen
from apps.session.models import (
    SessionBody,
    SessionDelegation,
    SessionInvitation,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import body_service, portal_publication
from apps.session.tests.gleichstand import (
    aktionen,
    fehlende,
    gezeigt,
    neue,
    reiter_ausnahmen,
    rollen,
    vergleichen,
)
from apps.session.tests.test_neues_design import Aktion, _brotkrumen, _client, _vorlagen
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _datenkatalog(settings: Any) -> None:
    """Datenkatalog (DCAT-AP.de) eingeschaltet, damit beide Übersichten seinen Link bzw. Hinweis zeigen."""
    settings.DCAT_ENABLED = True


def _meldungen(antwort: Any) -> list[str]:
    """Nachrichten der Seite: Der neue Rahmen zeigt sie als Hinweis (JSON für den Toast), der bisherige im HTML."""
    treffer = re.search(
        r'<script type="application/json" id="django-messages">(.*?)</script>', antwort.content.decode(), re.S
    )
    return [m["message"] for m in json.loads(treffer.group(1))] if treffer else []


#: Seiten dieses Teils: Name → neue Vorlage
SEITEN = {
    "allgemein": "session/neu/einstellungen/allgemein.html",
    "veroeffentlichung_beenden": "session/neu/einstellungen/veroeffentlichung_beenden.html",
    "benutzer": "session/neu/einstellungen/benutzer.html",
    "einladen": "session/neu/einstellungen/einladen.html",
    "rollen": "session/neu/einstellungen/rollen.html",
    "vier_augen": "session/neu/einstellungen/vier_augen.html",
    "vertretungen": "session/neu/vertretungen.html",
}
#: Eigene Rollen neben den Standardrollen: nur Benutzer bzw. nur Einstellungen verwalten
NUR_BENUTZER = "Benutzerverwaltung"
NUR_EINSTELLUNGEN = "Einstellungsverwaltung"
#: Kacheln der bisherigen Übersicht (Adressnamen); im neuen Rahmen tragen sie die Reiter und ihre Unterpunkte
KACHELN = (
    "users",
    "settings_roles",
    "settings_bodies",
    "settings_four_eyes",
    "terms",
    "settings_cosign",
    "settings_numbering",
    "settings_textblocks",
    "settings_meeting_formats",
    "privacy_settings",
    "settings_api_tokens",
)
#: Stände der Veröffentlichung für die Übersicht: Name → Felder des Mandanten
STAENDE: dict[str, dict[str, Any]] = {
    "veroeffentlicht": {"insight_publish": True, "insight_end_mode": "", "implementation_publish": False},
    "tracking": {
        "insight_publish": True,
        "insight_end_mode": "",
        "implementation_publish": True,
        "require_2fa": True,
        "oparl_license": "https://www.govdata.de/dl-de/by-2-0",
    },
    "beendet": {"insight_publish": False, "insight_end_mode": SessionTenant.PORTAL_END_PAUSED},
    "altbestand": {"insight_publish": False, "insight_end_mode": ""},
    "gesperrt": {"insight_publish": False, "insight_end_mode": "", "oparl_public_since": None},
}
GRUND: dict[str, Any] = {
    "insight_publish": True,
    "insight_end_mode": "",
    "implementation_publish": False,
    "require_2fa": False,
    "oparl_license": "",
}


def _adresse(tenant: SessionTenant, name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kw})


def _mandant_2b(slug: str) -> SessionTenant:
    """Mandant mit freigeschalteter Schnittstelle und Veröffentlichung im Bürgerportal (Quelle samt Bestand)."""
    tenant = SessionTenant.objects.create(
        name=f"Stadt {slug.title()}", slug=slug, insight_publish=True, oparl_public_since=timezone.now()
    )
    SessionRole.create_default_roles(tenant)
    source = OParlSource.objects.get(sync_config__session_tenant=slug)
    body = OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name=tenant.name, slug=slug)
    OParlMeeting.objects.create(external_id=f"{source.url}meeting/{uuid.uuid4()}/", body=body, name="Ratssitzung")
    OParlPaper.objects.create(external_id=f"{source.url}paper/{uuid.uuid4()}/", body=body, name="Radweg")
    return tenant


def _welt(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand, der alle Aktionen der sieben Seiten zeigt: eigene Rollen (nur Benutzer bzw. nur Einstellungen verwalten,
    „Kämmerei“ zum Löschen und Bearbeiten), weitere Benutzer (mit Namen, ohne Rolle, deaktiviert), eine offene
    Einladung, aktive, geplante und aufgehobene Vertretungen und eine zweite Körperschaft (Kachel „Körperschaften“).
    """
    SessionRole.objects.create(tenant=tenant, name=NUR_BENUTZER, can_manage_users=True)
    SessionRole.objects.create(tenant=tenant, name=NUR_EINSTELLUNGEN, can_manage_settings=True)
    kaemmerei = SessionRole.objects.create(
        tenant=tenant, name="Kämmerei", description="Nur Sitzungsgelder", can_manage_allowances=True, color="#0f766e"
    )
    sachbearbeitung = SessionRole.objects.get(tenant=tenant, name="Sachbearbeiter")
    mit_namen = SessionUser.objects.create(
        user=cast(Any, UserFactory)(email="anna@example.org", first_name="Anna", last_name="Amberg"),
        tenant=tenant,
        last_access=timezone.now(),
    )
    mit_namen.roles.add(sachbearbeitung, kaemmerei)
    ohne_rolle = SessionUser.objects.create(user=cast(Any, UserFactory)(email="bernd@example.org"), tenant=tenant)
    SessionUser.objects.create(user=cast(Any, UserFactory)(email="clara@example.org"), tenant=tenant, is_active=False)
    einladung = SessionInvitation.create_for_tenant(tenant, "neu@example.org", roles=[sachbearbeitung])
    heute = timezone.localdate()
    for start, ende, aufgehoben in ((-1, 5, False), (10, 20, False), (-30, -20, True)):
        SessionDelegation.objects.create(
            tenant=tenant,
            principal=mit_namen,
            deputy=ohne_rolle,
            start_date=heute + timedelta(days=start),
            end_date=heute + timedelta(days=ende),
            revoked_at=timezone.now() if aufgehoben else None,
        )
    SessionBody.objects.create(tenant=tenant, name="Gemeinde Nord", short_name="GN", body_type="mitgliedsgemeinde")
    assert body_service.has_several(tenant)

    seiten = {
        **{f"allgemein_{stand}": _adresse(tenant, "settings") for stand in STAENDE},
        "veroeffentlichung_beenden": _adresse(tenant, "portal_publication_end"),
        "veroeffentlichung_beenden_beendet": _adresse(tenant, "portal_publication_end"),
        "benutzer": _adresse(tenant, "users"),
        "einladen": _adresse(tenant, "user_invite"),
        "rollen": _adresse(tenant, "settings_roles"),
        "rollen_bearbeiten": _adresse(tenant, "settings_roles") + f"?edit={kaemmerei.pk}",
        "vier_augen": _adresse(tenant, "settings_four_eyes"),
        "vertretungen": _adresse(tenant, "settings_delegations"),
    }
    erwartet = {
        ("POST", _adresse(tenant, "settings_two_factor")),
        ("POST", _adresse(tenant, "settings_reminders")),
        ("POST", _adresse(tenant, "oparl_access")),
        ("POST", _adresse(tenant, "oparl_license")),
        ("POST", _adresse(tenant, "insight_publish")),
        ("POST", _adresse(tenant, "implementation_publish")),
        ("GET", _adresse(tenant, "portal_publication_end")),
        ("GET", _adresse(tenant, "oparl_system")),
        ("GET", _adresse(tenant, "dcat_catalog_format", endung="ttl")),
        ("GET", _adresse(tenant, "api_root")),
        ("POST", ""),  # Auswahl beim Beenden der Veröffentlichung und Einladen: an dieselbe Seite
        ("POST", _adresse(tenant, "invitation_resend", invitation_id=einladung.pk)),
        ("POST", _adresse(tenant, "invitation_cancel", invitation_id=einladung.pk)),
        ("POST", _adresse(tenant, "user_roles_update", session_user_id=mit_namen.pk)),
        ("POST", _adresse(tenant, "user_deactivate", session_user_id=mit_namen.pk)),
        ("GET", _adresse(tenant, "user_invite")),
        ("POST", _adresse(tenant, "role_save")),
        ("POST", _adresse(tenant, "role_delete")),
        ("GET", f"?edit={kaemmerei.pk}"),  # Anker „#rollen-formular“ zählt nicht
        ("POST", _adresse(tenant, "settings_four_eyes")),
        ("POST", _adresse(tenant, "settings_protocol_approval")),
        ("GET", _adresse(tenant, "settings_delegations")),
        ("POST", _adresse(tenant, "settings_delegation_create")),
        *(("GET", _adresse(tenant, name)) for name in KACHELN),
    }
    return {"seiten": seiten, "erwartet": erwartet, "kaemmerei": kaemmerei, "einladung": einladung}


def _stand(tenant: SessionTenant, name: str) -> None:
    """Stand der Veröffentlichung für die Seite ``name`` (Übersicht je Stand, sonst veröffentlichend)."""
    felder = dict(GRUND, oparl_public_since=timezone.now())
    if name.startswith("allgemein_"):
        felder.update(STAENDE[name.removeprefix("allgemein_")])
    elif name == "veroeffentlichung_beenden_beendet":
        felder.update(STAENDE["beendet"])
    SessionTenant.objects.filter(pk=tenant.pk).update(**felder)


def _kachel_ausnahmen(tenant: SessionTenant) -> set[Aktion]:
    """Die Kachel-Links der bisherigen Übersicht: Sie entfallen dort, weil Reiter und Unterpunkte sie tragen."""
    return {("GET", _adresse(tenant, name), ()) for name in KACHELN}


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant_2b("vorlagen2b")
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in SEITEN.items():
        adresse = welt["seiten"]["allgemein_veroeffentlicht" if name == "allgemein" else name]
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse)
        assert alt.status_code == 200, name
        assert vorlage not in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(adresse)
        assert neu.status_code == 200, name
        assert {vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


def test_bestaetigung_beim_beenden_im_neuen_rahmen_und_unveraendert() -> None:
    """
    Schritt 2 (POST ohne ``confirm``) antwortet über ``get_template_names`` im Erscheinungsbild des Mandanten, mit
    denselben Aktionen wie bisher in beide Richtungen und ohne etwas zu ändern – für jede der drei Möglichkeiten.
    """
    tenant = _mandant_2b("bestaetigen2b")
    _welt(tenant)
    adresse = _adresse(tenant, "portal_publication_end")
    reiter = reiter_ausnahmen(tenant)
    for rolle in ("Administrator", NUR_EINSTELLUNGEN):
        client = _client(tenant, rolle)
        for option in portal_publication.OPTIONS:
            SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
            alt = client.post(adresse, {"mode": option.key})
            SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
            neu = client.post(adresse, {"mode": option.key})
            assert alt.status_code == neu.status_code == 200, (rolle, option.key)
            assert "session/settings/portal_publication.html" in _vorlagen(alt)
            assert {SEITEN["veroeffentlichung_beenden"], "session/base_session_neu.html"} <= _vorlagen(neu)
            html = neu.content.decode()
            assert 'data-testid="bestaetigung"' in html and 'data-gestaltung="neu"' in html
            assert "1 Sitzungen, 1 Vorlagen" in html and option.undo[:25] in html
            bisher, jetzt = aktionen(alt), aktionen(neu)
            assert ("POST", "", ("confirm=1", f"mode={option.key}")) in bisher
            assert not fehlende(bisher, jetzt, reiter), (rolle, option.key, fehlende(bisher, jetzt, reiter))
            # Neu sind nur die Reiter (für die Rolle erreichbar), kein Formular und keine fremde Aktion
            for methode, ziel, felder in neue(bisher, jetzt):
                assert methode == "GET" and not felder, (methode, ziel, felder)
                assert client.get(ziel).status_code == 200, ziel
    stand = SessionTenant.objects.get(pk=tenant.pk)
    assert (stand.insight_publish, stand.insight_end_mode) == (True, ""), "Die Bestätigung ändert noch nichts"


def test_einladen_mit_fehlern_und_erfolg_im_neuen_rahmen() -> None:
    """Fehler meldet die View als Nachricht auf der Einladungsseite (neu wie bisher); eine gültige Einladung entsteht."""
    tenant = _mandant_2b("einladen2b")
    _welt(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Administrator")
    adresse = _adresse(tenant, "user_invite")
    for daten, meldung in (
        ({"email": "kaputt"}, "Bitte eine gültige E-Mail-Adresse angeben."),
        ({"email": "x@example.org", "roles": [str(uuid.uuid4())]}, "Mindestens eine der gewählten Rollen"),
    ):
        antwort = client.post(adresse, daten, follow=True)
        assert antwort.redirect_chain[-1][0] == adresse
        assert SEITEN["einladen"] in _vorlagen(antwort)
        assert any(meldung in text for text in _meldungen(antwort)), _meldungen(antwort)
    rolle = SessionRole.objects.get(tenant=tenant, name="Lesezugriff")
    antwort = client.post(adresse, {"email": "Dora@Example.org", "roles": [str(rolle.pk)]}, follow=True)
    assert antwort.redirect_chain[-1][0] == _adresse(tenant, "users")
    assert SEITEN["benutzer"] in _vorlagen(antwort)
    html = antwort.content.decode()
    assert "dora@example.org" in html[html.index('data-testid="offene-einladungen"') :]
    assert SessionInvitation.objects.get(tenant=tenant, email="dora@example.org").roles.get() == rolle


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_keine_funktion_der_bisherigen_seiten_entfaellt_einstellungen_1() -> None:
    """
    Jede Aktion der bisherigen Seiten steht mit denselben Merkmalen (Rückfrage beim Löschen einer Rolle …) auf der
    neuen, und die neue bietet nichts an, was die bisherige nicht hatte (neue Links nur, wenn für die Rolle erreichbar:
    Reiter, Unterpunkte, Weg zurück). Rollen: die sechs Standardrollen sowie je eine Rolle nur mit „Benutzer
    verwalten“ und nur mit „Einstellungen verwalten“. Die Übersicht in fünf Ständen der Veröffentlichung, die Auswahl
    beim Beenden mit und ohne eingestellte Möglichkeit, die Rollen neu und beim Bearbeiten.

    Ausnahmen: die vier Seiten der bisherigen Seitenleiste (Rahmen, wie Teil 1 und 2a) und die Kacheln der bisherigen
    Übersicht, die jetzt Reiter und Unterpunkte tragen (Nachweis: ``test_entfallene_kacheln_ueber_reiter_erreichbar``).
    """
    tenant = _mandant_2b("vergleich2b")
    welt = _welt(tenant)
    fehlt, gesehen = vergleichen(
        tenant,
        welt["seiten"],
        rollen_liste=[*rollen(), NUR_BENUTZER, NUR_EINSTELLUNGEN],
        ausnahmen=reiter_ausnahmen(tenant) | _kachel_ausnahmen(tenant),
        vorher=lambda _rolle, name: _stand(tenant, name),
    )
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))
    # Rückfrage beim Löschen einer Rolle ist Teil des Vergleichs
    loeschen = next(a for a in gesehen if a[1] == _adresse(tenant, "role_delete") and "role_id=" in str(a[2]))
    assert any(m.startswith("data-confirm=Rolle „") for m in gesehen[loeschen])


def _url_name(adresse: str) -> str:
    from django.urls import resolve

    return resolve(adresse.split("?")[0]).url_name or ""


def _wege_ueber_reiter(tenant: SessionTenant, rechte: Any) -> dict[str, str]:
    """
    Adresse → Weg für jede Seite, die von der Übersicht aus über einen Reiter (Link auf seine erste sichtbare Seite) und
    dessen Unterpunkte erreichbar ist (``apps.session.einstellungen.navigation``), etwa „Benutzer und Rechte ›
    Rollen und Rechte“.
    """
    wege: dict[str, str] = {}
    uebersicht = einstellungen.navigation(tenant, rechte, "settings")
    assert uebersicht is not None
    for reiter in uebersicht["reiter"]:
        wege.setdefault(reiter["url"], reiter["label"])
        erste = einstellungen.navigation(tenant, rechte, _url_name(reiter["url"]))
        assert erste is not None
        for unterpunkt in erste["unterpunkte"]:
            wege.setdefault(unterpunkt["url"], f"{reiter['label']} › {unterpunkt['label']}")
    return wege


def test_entfallene_kacheln_ueber_reiter_erreichbar() -> None:
    """
    Die bisherige Übersicht führte über Kacheln zu den Einstellungsseiten. Jede Kachel, die die neue Übersicht nicht
    mehr als Link trägt, ist für dieselbe Rolle erreichbar: Ihr Reiter steht als Link auf der neuen Übersicht, dessen
    Unterpunkte führen zu ihr, und sie antwortet mit 200. Umgekehrt bieten Reiter und Unterpunkte keine Seite an, deren
    Kachel die Rolle bisher nicht sah (gleiche Rechte, Körperschaften erst ab der zweiten).
    """
    tenant = _mandant_2b("kacheln2b")
    _welt(tenant)
    adresse = _adresse(tenant, "settings")
    geprueft: dict[str, set[str]] = {}
    for rolle in ("Administrator", NUR_BENUTZER, NUR_EINSTELLUNGEN):
        client = _client(tenant, rolle)
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse)
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(adresse)
        assert alt.status_code == neu.status_code == 200, rolle
        html_alt, html_neu = alt.content.decode(), neu.content.decode()
        wege = _wege_ueber_reiter(tenant, neu.context["permission_checker"].permissions)
        uebersicht = einstellungen.navigation(tenant, neu.context["permission_checker"].permissions, "settings")
        assert uebersicht is not None
        for reiter in uebersicht["reiter"]:
            assert f'href="{reiter["url"]}"' in html_neu, f"{rolle}: Reiter {reiter['label']} fehlt auf der Übersicht"
        for name in KACHELN:
            ziel = _adresse(tenant, name)
            if f'href="{ziel}"' not in html_alt:
                assert ziel not in wege, f"{rolle}: {name} bisher ohne Kachel, jetzt über {wege.get(ziel)}"
                continue
            assert ziel in wege, f"{rolle}: Kachel {name} weder auf der Übersicht noch über Reiter und Unterpunkt"
            assert client.get(ziel).status_code == 200, (rolle, name)
            geprueft.setdefault(rolle, set()).add(name)
    # Alle elf Kacheln für die Verwaltung, Benutzer und Rollen für „nur Benutzer“, die neun übrigen für „nur Einstellungen“
    assert geprueft["Administrator"] == set(KACHELN)
    assert geprueft[NUR_BENUTZER] == {"users", "settings_roles"}
    assert geprueft[NUR_EINSTELLUNGEN] == set(KACHELN) - {"users", "settings_roles"}


# =============================================================================
# Reiter, Unterpunkte und Brotkrumen
# =============================================================================


class _Navigation(HTMLParser):
    """Reiter (nav „Einstellungen“) und Unterpunkte (data-testid einstellungen-unterpunkte): (Text, aria-current)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.reiter: list[tuple[str, str]] = []
        self.unterpunkte: list[tuple[str, str]] = []
        self._ziel: list[tuple[str, str]] | None = None
        self._link: list[str] | None = None
        self._current = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "nav" and a.get("aria-label") == "Einstellungen":
            self._ziel = self.reiter
        elif tag == "nav" and a.get("data-testid") == "einstellungen-unterpunkte":
            self._ziel = self.unterpunkte
        elif tag == "a" and self._ziel is not None:
            self._link, self._current = [], a.get("aria-current", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._ziel is not None and self._link is not None:
            self._ziel.append((" ".join("".join(self._link).split()), self._current))
            self._link = None
        elif tag == "nav":
            self._ziel = None

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link.append(data)


def _navigation(html: str) -> _Navigation:
    leser = _Navigation()
    leser.feed(html)
    return leser


ALLE_REITER = ["Allgemein", "Benutzer und Rechte", "Sitzungen", "Vorlagen", "Datenschutz und Zugänge"]
ALLE_UNTERPUNKTE = ["Benutzer", "Rollen und Rechte", "Vertretungen", "Vier-Augen-Prinzip"]


def test_reiter_und_unterpunkte_nach_recht_mit_aria_current() -> None:
    """
    Verwaltung: fünf Reiter, unter „Benutzer und Rechte“ vier Unterpunkte. Nur „Benutzer verwalten“: „Allgemein“ und
    „Benutzer und Rechte“ ohne Vier-Augen-Prinzip. Nur „Einstellungen verwalten“: unter „Benutzer und Rechte“ nur das
    Vier-Augen-Prinzip, also keine Unterpunkte. Die geöffnete Seite trägt aria-current="page", ihr Reiter "page" nur
    auf seiner ersten Seite, sonst "true"; Unterseiten (Einladen, Veröffentlichung beenden) tragen "true".
    """
    tenant = _mandant_2b("reiter2b")
    welt = _welt(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    s = welt["seiten"]
    faelle: list[tuple[str, str, list[tuple[str, str]], list[tuple[str, str]]]] = [
        ("Administrator", s["allgemein_veroeffentlicht"], [("Allgemein", "page")], []),
        ("Administrator", s["veroeffentlichung_beenden"], [("Allgemein", "true")], []),
        ("Administrator", s["benutzer"], [("Benutzer und Rechte", "page")], [("Benutzer", "page")]),
        ("Administrator", s["einladen"], [("Benutzer und Rechte", "true")], [("Benutzer", "true")]),
        ("Administrator", s["rollen"], [("Benutzer und Rechte", "true")], [("Rollen und Rechte", "page")]),
        ("Administrator", s["vertretungen"], [("Benutzer und Rechte", "true")], [("Vertretungen", "page")]),
        ("Administrator", s["vier_augen"], [("Benutzer und Rechte", "true")], [("Vier-Augen-Prinzip", "page")]),
        (NUR_BENUTZER, s["allgemein_veroeffentlicht"], [("Allgemein", "page")], []),
        (NUR_BENUTZER, s["benutzer"], [("Benutzer und Rechte", "page")], [("Benutzer", "page")]),
        (NUR_BENUTZER, s["rollen"], [("Benutzer und Rechte", "true")], [("Rollen und Rechte", "page")]),
        (NUR_EINSTELLUNGEN, s["vier_augen"], [("Benutzer und Rechte", "page")], []),
    ]
    sichtbar = {
        "Administrator": (ALLE_REITER, ALLE_UNTERPUNKTE),
        NUR_BENUTZER: (["Allgemein", "Benutzer und Rechte"], ["Benutzer", "Rollen und Rechte", "Vertretungen"]),
        NUR_EINSTELLUNGEN: (ALLE_REITER, []),
    }
    clients = {rolle: _client(tenant, rolle) for rolle in sichtbar}
    for rolle, adresse, reiter_aktiv, unterpunkt_aktiv in faelle:
        antwort = clients[rolle].get(adresse)
        assert antwort.status_code == 200, (rolle, adresse)
        navi = _navigation(antwort.content.decode())
        alle_reiter, alle_unterpunkte = sichtbar[rolle]
        assert [text for text, _ in navi.reiter] == alle_reiter, (rolle, adresse)
        assert [(t, c) for t, c in navi.reiter if c] == reiter_aktiv, (rolle, adresse)
        auf_benutzer = reiter_aktiv[0][0] == "Benutzer und Rechte"
        assert [text for text, _ in navi.unterpunkte] == (alle_unterpunkte if auf_benutzer else []), (rolle, adresse)
        assert [(t, c) for t, c in navi.unterpunkte if c] == unterpunkt_aktiv, (rolle, adresse)
    # Nur „Benutzer verwalten“ erreicht das Vier-Augen-Prinzip nicht, nur „Einstellungen verwalten“ die Benutzer nicht
    assert clients[NUR_BENUTZER].get(s["vier_augen"]).status_code == 403
    assert clients[NUR_EINSTELLUNGEN].get(s["benutzer"]).status_code == 403


def test_einladen_und_vertretungen_fuehren_in_den_brotkrumen_zu_den_benutzern() -> None:
    tenant = _mandant_2b("krumen2b")
    welt = _welt(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Administrator")
    benutzer = _adresse(tenant, "users")
    for name in ("einladen", "vertretungen"):
        antwort = client.get(welt["seiten"][name])
        assert antwort.status_code == 200 and "session/base_session_neu.html" in _vorlagen(antwort), name
        krumen = _brotkrumen(antwort.content.decode())
        assert f'href="{benutzer}"' in krumen, name
        assert f'href="{_adresse(tenant, "settings")}"' in krumen, name
    # Die übrigen Seiten führen über „Einstellungen“ zurück zur Übersicht
    for name in ("benutzer", "rollen", "vier_augen", "veroeffentlichung_beenden"):
        krumen = _brotkrumen(client.get(welt["seiten"][name]).content.decode())
        assert f'href="{_adresse(tenant, "settings")}"' in krumen, name


# =============================================================================
# Prüfmerkmale der bisherigen Tests auch im neuen Erscheinungsbild
# =============================================================================


def test_merkmale_der_bisherigen_tests_auch_neu() -> None:
    """
    Was die Tests der bisherigen Seiten prüfen (Bürgerportal, OParl-Freischaltung, Datenkatalog, Erinnerungen,
    Rollen-Matrix, Vier-Augen-Prinzip), steht auch auf den neuen Seiten – mit denselben data-testid.
    """
    tenant = _mandant_2b("merkmale2b")
    welt = _welt(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Administrator")
    s = welt["seiten"]
    beenden = _adresse(tenant, "portal_publication_end")

    _stand(tenant, "allgemein_veroeffentlicht")
    html = client.get(s["allgemein_veroeffentlicht"]).content.decode()
    for merkmal in ('data-testid="karte-oparl"', 'data-testid="oparl-oeffentlich"', 'data-testid="karte-buergerportal"',
                    'data-testid="karte-erinnerungen"', 'data-testid="erinnerung-ladung"', 'data-testid="oparl-lizenz"',
                    "Veröffentlichung beenden", beenden, "Ohne Lizenz gibt es keinen Datenkatalog"):  # fmt: skip
        assert merkmal in html, merkmal
    assert 'data-testid="buergerportal-voraussetzung"' not in html and "api/dcat/catalog" not in html

    _stand(tenant, "allgemein_tracking")
    html = client.get(s["allgemein_tracking"]).content.decode()
    assert f"/session/{tenant.slug}/api/dcat/catalog.ttl" in html and "Beschluss-Tracking beenden" in html
    assert "Pflicht für alle aufheben" in html

    _stand(tenant, "allgemein_beendet")
    html = client.get(s["allgemein_beendet"]).content.decode()
    assert "Wieder veröffentlichen" in html and "Möglichkeit ändern" in html

    _stand(tenant, "allgemein_altbestand")
    html = client.get(s["allgemein_altbestand"]).content.decode()
    assert 'data-testid="altbestand-hinweis"' in html and "Umgang mit dem bisherigen Bestand festlegen" in html

    _stand(tenant, "allgemein_gesperrt")
    html = client.get(s["allgemein_gesperrt"]).content.decode()
    assert 'data-testid="oparl-gesperrt"' in html and 'data-testid="buergerportal-voraussetzung"' in html

    # Erinnerungen: Zeitpunkte und gewählter Empfängerkreis vorbelegt
    kreis = SessionTenant.RSVP_AUDIENCE_CHOICES[-1][0]
    SessionTenant.objects.filter(pk=tenant.pk).update(reminder_settings={"rsvp_days": "7, 2", "rsvp_audience": kreis})
    html = client.get(s["allgemein_veroeffentlicht"]).content.decode()
    assert 'value="7, 2"' in html and f'value="{kreis}" selected' in html

    # Auswahl beim Beenden: drei Möglichkeiten mit Wert
    html = client.get(beenden).content.decode()
    for option in portal_publication.OPTIONS:
        assert option.title in html and f'value="{option.key}"' in html

    # Rollen-Matrix: neue Rolle ohne Vorbelegung, bearbeitete Rolle mit ihren Rechten
    neu = client.get(s["rollen"]).content.decode()
    assert 'data-perm="can_view_papers"' in neu and not _angekreuzt(neu)
    bearbeiten = client.get(s["rollen_bearbeiten"]).content.decode()
    # Angekreuzt sind genau die Rechte der Rolle: „Sitzungsgelder“ und die Leserechte, die eine Rolle von Haus aus hat (soweit die Matrix sie zeigt)
    rolle = welt["kaemmerei"]
    rechte = {
        f.name for f in rolle._meta.concrete_fields if f.name.startswith("can_") and getattr(rolle, f.name) is True
    }
    matrix = set(re.findall(r'data-perm="(can_\w+)"', bearbeiten))
    assert "can_manage_allowances" in rechte and _angekreuzt(bearbeiten) == rechte & matrix
    assert 'name="role_id" value="' + str(welt["kaemmerei"].pk) + '"' in bearbeiten

    assert 'data-testid="vier-augen-einstellungen"' in client.get(s["vier_augen"]).content.decode()


def _angekreuzt(html: str) -> set[str]:
    """Angekreuzte Rechte der Matrix (Ankreuzfelder mit der Kennung recht-<Feld>)."""
    return set(re.findall(r'id="recht-(can_\w+)"(?:\s+value="1")?\s+checked', html))


# =============================================================================
# Darstellung
# =============================================================================


def test_keine_farbigen_fuellknoepfe_im_inhalt() -> None:
    """Auf den neuen Seiten trägt höchstens die eine Hauptaktion im Kopfband die Füllfarbe, nie der Inhalt."""
    tenant = _mandant_2b("knoepfe2b")
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        _stand(tenant, name)
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        html = client.get(adresse).content.decode()
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        fuellung = inhalt.count("bg-primary-600 hover:bg-primary-700")
        erlaubt = 1 if name == "vertretungen" else 0  # aus Teil 2a: „Vertretung eintragen“ (Inhalt unverändert)
        assert fuellung <= erlaubt, f"{name}: {fuellung} gefüllte Knöpfe im Inhalt"
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, f"{name}: Knopf in {farbe}"
        assert "<table" not in inhalt, f"{name}: Tabelle statt Liste"


def test_listen_nicht_in_12_px_und_ohne_inline_stil() -> None:
    """Die neuen Vorlagen setzen Text in 13 bis 14 px, nicht in text-xs oder kleiner, und ohne style= (CSP)."""
    ordner = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu" / "einstellungen"
    dateien = [p for p in ordner.glob("*.html") if p.name not in ("_reiter.html", "_unterpunkte.html")]
    eigene = {"allgemein.html", "veroeffentlichung_beenden.html", "benutzer.html", "einladen.html", "rollen.html",
              "vier_augen.html", "_allgemein_mandant.html", "_allgemein_sicherheit.html", "_allgemein_erinnerungen.html",
              "_allgemein_frist.html", "_allgemein_buergerportal.html", "_allgemein_api.html",
              "_benutzer_einladungen.html", "_benutzer_zeile.html", "_rollen_matrix.html"}  # fmt: skip
    assert eigene <= {p.name for p in dateien}
    for datei in sorted(eigene):
        text = (ordner / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        assert "style=" not in text and "onclick=" not in text, datei
        assert "<c-ui.card" not in text and "<c-ui.badge" not in text and "<c-ui.kpi-tile" not in text, datei
