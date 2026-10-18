# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Formulare zum Anlegen und Bearbeiten (Issue #944).

Geprüft: Sitzung, Vorlage, Gremium und Person anlegen und bearbeiten, TOP bearbeiten und die Teilantwort „TOP
hinzufügen“ auf der Sitzung haben mit eingeschaltetem Schalter je eine eigene neue Vorlage, ohne Schalter bleiben die
bisherigen. Keine Funktion entfällt und keine kommt hinzu: Formular (Methode, Ziel, Feldnamen, Optionen jedes
Auswahlfelds, Werte der Options- und Ankreuzfelder) und Links stimmen in beide Richtungen überein – für alle sechs
Standardrollen und eine Stammdatenrolle ohne Sitzungsgelder, mit mehreren Gremien und Ämtern, zweiter Körperschaft,
Wahlperiode und Textbausteinen, die Vorlage ohne und mit Nummer, im Entwurf, in der Prüfung (Inhalt schreibgeschützt)
und freigegeben, der TOP offen und nach genehmigter Niederschrift; dazu ein Landkreis mit Landesprofil und stets
nichtöffentlichem Kreisausschuss. Beide Fassungen schicken unverändert abgesendet dieselben Werte, und die neuen
speichern dann ohne Fehler. Ein ungültiger POST zeigt die neue Vorlage mit dem Fehler am Feld und einem Satz oben. Die
Kopplungen an form-behaviors.ts und Alpine bleiben; gefüllt ist nur der eine Speichern-Knopf, keine Schrift unter 13 px.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionTextBlock,
)
from apps.session.services import meeting_format_service, paper_version_service
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, rollen, vergleichen
from apps.session.tests.test_neues_design import _brotkrumen, _client, _mandant, _vorlagen
from apps.session.tests.test_neues_design_teil2a import _welt_2a

pytestmark = pytest.mark.django_db

#: Formulare dieses Teils: Art → (bisherige Vorlage, neue Vorlage)
VORLAGEN = {
    "sitzung": ("session/meetings/form.html", "session/neu/sitzung_formular.html"),
    "vorlage": ("session/papers/form.html", "session/neu/vorlage_formular.html"),
    "gremium": ("session/organizations/form.html", "session/neu/gremium_formular.html"),
    "person": ("session/persons/form.html", "session/neu/person_formular.html"),
    "top": ("session/meetings/agenda_form.html", "session/neu/top_formular.html"),
    "topneu": ("session/partials/agenda_item_form.html", "session/neu/_formular_top_anlegen.html"),
}

#: Neue Vorlagen dieses Teils (für die Prüfung der Schriftgrößen)
DATEIEN = (
    "sitzung_formular.html",
    "vorlage_formular.html",
    "gremium_formular.html",
    "person_formular.html",
    "top_formular.html",
    "_formular_aktionen.html",
    "_formular_fehler.html",
    "_formular_feld.html",
    "_formular_format.html",
    "_formular_feldfehler.html",
    "_formular_textbaustein.html",
    "_formular_top_anlegen.html",
    "_formular_vorlage_zuordnung.html",
    "_formular_vorlagennummer.html",
)

#: Stand der Vorlage je Seite (Formular im Entwurf, in der Prüfung mit gesperrtem Inhalt, nach der Freigabe)
STAND = {"vorlage-entwurf": "draft", "vorlage-pruefung": "review", "vorlage-freigegeben": "approved"}

#: Rolle, die Gremien und Personen pflegt, aber keine Sitzungsgelder (Person ohne Bankdaten)
STAMMDATEN = "Stammdaten"


def _art(name: str) -> str:
    return name.split("-")[0]


def _welt_formulare(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand auf der Grundlage von ``_welt_2a`` (Rat und Bauausschuss, drei Ämter, Wahlperiode, zweite Körperschaft,
    Personen mit Bankdaten, Vorlage mit Nummer, genehmigte Niederschrift): dazu Textbausteine je Kategorie, eine
    Vorlage ohne Nummer mit finanziellen Auswirkungen und eine Stammdatenrolle ohne Sitzungsgelder. ``seiten``: Name →
    Adresse; ``erwartet``: Felder und Links, die der Bestand auf den bisherigen Seiten zeigen muss.
    """
    welt = _welt_2a(tenant)
    slug = tenant.slug
    sitzung, vorlage, vergangen = welt["sitzung_obj"], welt["vorlage_obj"], welt["vergangen_obj"]
    ausschuss = welt["ausschuss_obj"]
    for titel, kategorie in (("Zustimmung", "resolution"), ("Hinweis", "general"), ("Protokoll", "protocol")):
        SessionTextBlock.objects.create(
            tenant=tenant, title=titel, content=f"{titel}: {{gremium}} am {{datum}}", category=kategorie
        )
    ohne_nummer = SessionPaper.objects.create(
        tenant=tenant,
        name="Ohne Nummer",
        paper_type="proposal",
        status="draft",
        main_organization=ausschuss,
        has_financial_impact=True,
        financial_impact_note="10.000 EUR",
    )
    # Nummer noch nicht vergeben (etwa Vergabe erst bei der Freigabe): nur dann bietet das Formular das Feld an
    SessionPaper.objects.filter(pk=ohne_nummer.pk).update(reference="", reference_assigned_at=None)
    ohne_nummer.refresh_from_db()
    SessionRole.objects.create(tenant=tenant, name=STAMMDATEN, can_view_meetings=True, can_manage_organizations=True)
    person = SessionPerson.objects.get(tenant=tenant, family_name="Amberg")
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="2")
    beschlossen = SessionAgendaItem.objects.get(meeting=vergangen, number="1")

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    welt["seiten"] = {
        "sitzung-neu": adresse("meeting_create"),
        "sitzung-aus-gremium": adresse("meeting_create") + f"?organization={ausschuss.pk}",
        "sitzung-bearbeiten": adresse("meeting_edit", meeting_id=sitzung.pk),
        "vorlage-neu": adresse("paper_create"),
        **{name: adresse("paper_edit", paper_id=vorlage.pk) for name in STAND},
        "vorlage-ohne-nummer": adresse("paper_edit", paper_id=ohne_nummer.pk),
        "gremium-neu": adresse("organization_create"),
        "gremium-bearbeiten": adresse("organization_edit", organization_id=ausschuss.pk),
        "person-neu": adresse("person_create"),
        "person-bearbeiten": adresse("person_edit", person_id=person.pk),
        "top-bearbeiten": adresse("agenda_item_edit", item_id=top.pk),
        "top-gesperrt": adresse("agenda_item_edit", item_id=beschlossen.pk),
        "topneu-sitzung": adresse("agenda_item_create", meeting_id=sitzung.pk),
    }
    welt["eltern"] = {
        "sitzung-bearbeiten": adresse("meeting_detail", meeting_id=sitzung.pk),
        "vorlage-entwurf": adresse("paper_detail", paper_id=vorlage.pk),
        "gremium-bearbeiten": adresse("organization_detail", organization_id=ausschuss.pk),
        "person-bearbeiten": adresse("person_detail", person_id=person.pk),
        "top-bearbeiten": adresse("meeting_detail", meeting_id=sitzung.pk),
    }
    welt["erwartet_links"] = {
        ("GET", adresse("settings_meeting_formats")),
        ("GET", adresse("meeting_detail", meeting_id=sitzung.pk)),
        ("GET", adresse("paper_detail", paper_id=vorlage.pk)),
        ("GET", adresse("organization_detail", organization_id=ausschuss.pk)),
        ("GET", adresse("person_detail", person_id=person.pk)),
        ("POST", adresse("agenda_item_create", meeting_id=sitzung.pk)),
    }
    #: Felder (Anfang des Eintrags), die die bisherigen Formulare mit diesem Bestand zeigen
    welt["erwartet_felder"] = {
        "reference",
        "bank_iban",
        "bank_bic",
        "bank_account_holder",
        "body∈",
        "joint_organizations∈",
        "lead_department∈",
        "organization∈",
        "main_organization∈",
        "status∈approved|completed|withdrawn",
        "status∈draft",
        "meeting_state∈",
        "cancelled∈on",
        "has_financial_impact∈False|True",
        "requires_secrecy∈on",
        "kind∈",
        "organization_type∈",
        "committee_kind∈",
        "publicity∈",
        "delivery_channel∈",
        "contact_publish∈on",
        "format∈",
        "paper∈",
        "parent∈",
    }
    welt.update(ohne_nummer=ohne_nummer, person_obj=person, top_obj=top)
    return welt


def _stand(tenant: SessionTenant, vorlage: SessionPaper) -> Any:
    """``vorher`` für ``vergleichen``: die Vorlage je Seite in den Stand aus ``STAND`` bringen."""

    def vorher(_rolle: str, name: str) -> None:
        if name in STAND:
            SessionPaper.objects.filter(pk=vorlage.pk).update(status=STAND[name])

    return vorher


def _felder(gesehen: Any) -> set[str]:
    return {feld for methode, _, felder in gesehen if methode == "POST" for feld in felder}


# =============================================================================
# Vorlagenwahl
# =============================================================================


def test_formulare_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("formulare2b", neu=False)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        alt_vorlage, neu_vorlage = VORLAGEN[_art(name)]
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse, HTTP_HX_REQUEST="true") if _art(name) == "topneu" else client.get(adresse)
        assert alt.status_code == 200, name
        assert alt_vorlage in _vorlagen(alt) and neu_vorlage not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(adresse, HTTP_HX_REQUEST="true") if _art(name) == "topneu" else client.get(adresse)
        assert neu.status_code == 200, name
        assert neu_vorlage in _vorlagen(neu) and alt_vorlage not in _vorlagen(neu), name
        if _art(name) == "topneu":
            assert "session/base_session_neu.html" not in _vorlagen(neu), "Teilantwort ohne Rahmen"
            continue
        assert "session/base_session_alt.html" in _vorlagen(alt), name
        assert "session/base_session_neu.html" in _vorlagen(neu) and 'data-gestaltung="neu"' in neu.content.decode()


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_der_formulare_fuer_alle_rollen() -> None:
    """
    Jedes Formular (Methode, Ziel, Feldnamen, Optionen, Werte der Options- und Ankreuzfelder) und jeder Link der
    bisherigen Seiten steht auf den neuen und umgekehrt – für alle sechs Standardrollen und die Stammdatenrolle, die
    Vorlage im Entwurf, in der Prüfung und freigegeben. Einzige Ausnahme: die vier Seiten der bisherigen Seitenleiste, die
    im neuen Rahmen Reiter über ihrer Liste sind (Rahmen, nicht Seite).
    """
    tenant = _mandant("gleich2bf", neu=False)
    welt = _welt_formulare(tenant)
    fehlt, gesehen = vergleichen(
        tenant,
        welt["seiten"],
        rollen_liste=[*rollen(), STAMMDATEN],
        ausnahmen=reiter_ausnahmen(tenant),
        vorher=_stand(tenant, welt["vorlage_obj"]),
    )
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet_links"] <= gezeigt(gesehen), sorted(welt["erwartet_links"] - gezeigt(gesehen))
    felder = _felder(gesehen)
    fehlende_felder = {e for e in welt["erwartet_felder"] if not any(f.startswith(e) for f in felder)}
    assert not fehlende_felder, sorted(fehlende_felder)


def _landkreis(slug: str, *, neu: bool) -> tuple[SessionTenant, SessionOrganization, SessionMeeting]:
    """Landkreis mit Landesprofil Niedersachsen: Kreisausschuss stets nichtöffentlich (§ 78 Abs. 2 NKomVG)."""
    meeting_format_service.sync_profiles()
    tenant = SessionTenant.objects.create(
        name="Landkreis Muster", slug=slug, state_profile_id="NI", body_type="kreis", session_new_design=neu
    )
    SessionRole.create_default_roles(tenant)
    SessionOrganization.objects.create(tenant=tenant, name="Kreistag", organization_type="council")
    ka = SessionOrganization.objects.create(tenant=tenant, name="Kreisausschuss", committee_kind="main")
    start = timezone.make_aware(datetime.combine(date(2026, 11, 20), time(17, 0)))
    sitzung = SessionMeeting.objects.create(tenant=tenant, organization=ka, name="KA", start=start, is_public=False)
    return tenant, ka, sitzung


def test_gleichstand_mit_landesprofil_und_gesperrter_oeffentlichkeit() -> None:
    """Mit Landesprofil steht es als Satz über dem Format; „Öffentliche Sitzung“ ist beim Kreisausschuss gesperrt."""
    tenant, ka, sitzung = _landkreis("lk2bf", neu=False)
    kw = {"tenant_slug": tenant.slug}
    seiten = {
        "sitzung-neu": reverse("session:meeting_create", kwargs=kw),
        "sitzung-bearbeiten": reverse("session:meeting_edit", kwargs={**kw, "meeting_id": sitzung.pk}),
    }
    fehlt, gesehen = vergleichen(tenant, seiten, ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    assert any(f.startswith("organization∈") for f in _felder(gesehen))

    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    html = _client(tenant, "Sachbearbeiter").get(seiten["sitzung-bearbeiten"]).content.decode()
    assert "session/neu/sitzung_formular.html" in _vorlagen(
        _client(tenant, "Sachbearbeiter").get(seiten["sitzung-neu"])
    )
    assert "disabled" in html.split('id="id_is_public"')[1][:120]
    # Die Sitzung gehört zum Kreisausschuss: seine Option ist ausgewählt und gesperrt nichtöffentlich
    assert re.search(
        rf'value="{ka.pk}"\s+(?:selected)?\s*data-public="0" data-date-public="0" data-public-locked="1"', html
    )
    assert "Landesprofil Niedersachsen" in html and "Einstellungen „Sitzungsformate“" not in html


# =============================================================================
# Werte und Fehler
# =============================================================================


class _Formular(HTMLParser):
    """
    Was ein Browser beim Absenden der POST-Formulare einer Seite schickt: Eingaben mit Wert, nur angekreuzte Kästchen,
    die gewählte Option (sonst die erste), Textfelder; ohne Knöpfe und gesperrte Felder. Optionsfelder mit
    ``x-model="fin"`` folgen wie im Browser dem Alpine-Zustand ``fin`` (finanzielle Auswirkungen).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.formulare: list[list[tuple[str, str]]] = []
        self._felder: list[tuple[str, str]] | None = None
        self._auswahl: tuple[str, list[tuple[str, bool]]] | None = None
        self._text: tuple[str, list[str]] | None = None
        self._fin = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        fin = re.search(r"fin:\s*'(\w*)'", a.get("x-data", ""))
        if fin:
            self._fin = fin.group(1)
        if tag == "form":
            self._felder = [] if a.get("method", "").lower() == "post" else None
            return
        if self._felder is None:
            return
        if tag == "option" and self._auswahl is not None:
            self._auswahl[1].append((a.get("value", ""), "selected" in a))
            return
        name = a.get("name", "")
        if not name or "disabled" in a:
            return
        if tag == "input":
            typ = a.get("type", "text").lower()
            if typ in ("submit", "button", "image", "reset", "file"):
                return
            if typ == "checkbox" and "checked" not in a:
                return
            if (
                typ == "radio"
                and "checked" not in a
                and not (a.get("x-model") == "fin" and a.get("value") == self._fin)
            ):
                return
            self._felder.append((name, a.get("value", "on" if typ in ("checkbox", "radio") else "")))
        elif tag == "select":
            self._auswahl = (name, [])
        elif tag == "textarea":
            self._text = (name, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._auswahl is not None and self._felder is not None:
            name, optionen = self._auswahl
            gewaehlt = [wert for wert, ja in optionen if ja] or [wert for wert, _ in optionen[:1]]
            self._felder.extend((name, wert) for wert in gewaehlt[:1])
            self._auswahl = None
        elif tag == "textarea" and self._text is not None and self._felder is not None:
            text = "".join(self._text[1])
            self._felder.append((self._text[0], text[1:] if text.startswith("\n") else text))
            self._text = None
        elif tag == "form" and self._felder is not None:
            self.formulare.append(self._felder)
            self._felder = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text[1].append(data)


def _werte(antwort: Any) -> list[tuple[str, str]]:
    """Felder des Hauptformulars (das POST-Formular mit den meisten Feldern) ohne CSRF-Kennung."""
    leser = _Formular()
    leser.feed(antwort.content.decode())
    leser.close()
    formular = max(leser.formulare, key=len)
    return sorted((name, wert) for name, wert in formular if name != "csrfmiddlewaretoken")


def _daten(werte: list[tuple[str, str]]) -> dict[str, list[str]]:
    daten: dict[str, list[str]] = {}
    for name, wert in werte:
        daten.setdefault(name, []).append(wert)
    return daten


def test_unveraendert_abgesendet_dieselben_werte_und_gespeichert() -> None:
    """
    Bisherige und neue Formulare zeigen jedes Feld mit demselben Wert (Datum mit Uhrzeit, ISO-Daten, Zeit, Zahlen mit
    Vorgabe, entschlüsselte Angaben, gewählte Optionen); unverändert abgesendet speichert das neue ohne Fehler.
    """
    tenant = _mandant("werte2bf", neu=False)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    for name in (
        "sitzung-bearbeiten",
        "vorlage-ohne-nummer",
        "gremium-bearbeiten",
        "person-bearbeiten",
        "top-bearbeiten",
    ):
        adresse = welt["seiten"][name]
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = _werte(client.get(adresse))
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = _werte(client.get(adresse))
        assert alt == neu, name
        antwort = client.post(adresse, _daten(neu))
        assert antwort.status_code == 302, (
            name,
            re.findall(r'id="id_\w+-error"[^>]*>([^<]*)', antwort.content.decode()),
        )
    person = SessionPerson.objects.get(pk=welt["person_obj"].pk)
    assert cast(Any, person).get_bank_iban_decrypted() == "DE02120300000000202051"  # Accessor zur Laufzeit
    ohne_nummer = SessionPaper.objects.get(pk=welt["ohne_nummer"].pk)
    assert ohne_nummer.has_financial_impact is True and ohne_nummer.financial_impact_note == "10.000 EUR"


def test_ungueltige_eingaben_zeigen_die_neue_vorlage_mit_fehlern() -> None:
    """Ein ungültiger POST rendert das neue Formular: Fehler am Feld (aria-invalid) und ein Satz über dem Formular."""
    tenant = _mandant("fehler2bf", neu=True)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    rat = welt["sitzung_obj"].organization
    faelle: list[tuple[str, dict[str, Any], str, str]] = [
        ("sitzung-neu", {"name": "", "organization": str(rat.pk), "start": "2031-03-01T17:00"}, "name", ""),
        ("sitzung-bearbeiten", {"name": "Rat", "organization": str(rat.pk), "start": ""}, "start", ""),
        ("vorlage-neu", {"name": "Neue Vorlage", "paper_type": "", "main_organization": str(rat.pk)}, "paper_type", ""),
        ("gremium-neu", {"name": "", "organization_type": "committee", "invitation_period_days": "7"}, "name", ""),
        (
            "person-neu",
            {"given_name": "Eva", "family_name": "Ernst", "bank_iban": "DE00 1234", "is_active": "on"},
            "bank_iban",
            "Die IBAN ist ungültig",
        ),
        ("top-bearbeiten", {"name": "", "is_public": "on", "kind": ""}, "name", ""),
    ]
    for name, daten, feld, text in faelle:
        antwort = client.post(welt["seiten"][name], daten)
        assert antwort.status_code == 200, name
        assert VORLAGEN[_art(name)][1] in _vorlagen(antwort), name
        html = antwort.content.decode()
        assert 'data-testid="formular-fehler"' in html and "Bitte prüfen Sie die markierten Angaben." in html, name
        assert f'id="id_{feld}-error"' in html and f'aria-describedby="id_{feld}-error"' in html, name
        assert 'aria-invalid="true"' in html, name
        assert text in html, name


def test_inhalt_der_vorlage_in_der_pruefung_gesperrt() -> None:
    """In der Prüfung sind Titel und Texte schreibgeschützt; eine Änderung meldet der Satz über dem Formular."""
    tenant = _mandant("gesperrt2bf", neu=True)
    welt = _welt_formulare(tenant)
    SessionPaper.objects.filter(pk=welt["vorlage_obj"].pk).update(status="review")
    client = _client(tenant, "Administrator")
    adresse = welt["seiten"]["vorlage-pruefung"]
    seite = client.get(adresse)
    html = seite.content.decode()
    assert 'data-testid="inhalt-gesperrt"' in html
    for feld in ("id_name", "id_main_text", "id_resolution_text"):
        assert "readonly" in html.split(f'id="{feld}"')[1].split(">")[0], feld
    daten = _daten(_werte(seite))
    daten["name"] = ["Geänderter Titel"]
    antwort = client.post(adresse, daten)
    assert antwort.status_code == 200
    html = antwort.content.decode()
    fehler = html[html.index('data-testid="formular-fehler"') :]
    assert paper_version_service.CONTENT_LOCKED_MESSAGE[:40] in fehler[:600]
    assert 'data-testid="inhalt-gesperrt"' not in html  # die Meldung steht nur einmal
    assert SessionPaper.objects.get(pk=welt["vorlage_obj"].pk).name == "Neue Brücke"


# =============================================================================
# Kopplungen, Wege zurück, Darstellung
# =============================================================================


def test_kopplungen_an_skripte_und_alpine_bleiben() -> None:
    """Öffentlichkeit nach Gremium, Textbausteine, Vorlagennummer, finanzielle Auswirkungen und Ämter ohne Körperschaft."""
    tenant = _mandant("kopplung2bf", neu=True)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    seiten = welt["seiten"]

    sitzung = client.get(seiten["sitzung-aus-gremium"]).content.decode()
    assert 'data-public-default="#id_is_public"' in sitzung and 'data-date-public-default="#id_date_public"' in sitzung
    assert 'id="id_is_public"' in sitzung and 'id="id_date_public"' in sitzung
    assert f'value="{welt["ausschuss_obj"].pk}" selected' in sitzung
    assert "<optgroup" in sitzung, "Gremien nach Körperschaft gruppiert"

    vorlage = client.get(seiten["vorlage-ohne-nummer"]).content.decode()
    assert "data-textblock-picker" in vorlage and "tb-select" in vorlage and "tb-insert" in vorlage
    assert "Zustimmung (Beschlusstext)" in vorlage and "Protokoll (" not in vorlage
    assert 'name="reference" id="id_reference"' in vorlage and 'data-testid="vorlagennummer"' in vorlage
    assert "x-data=\"{ fin: 'True' }\"" in vorlage and "x-show=\"fin === 'True'\"" in vorlage
    mit_nummer = client.get(seiten["vorlage-entwurf"]).content.decode()
    assert 'name="reference"' not in mit_nummer and "V/2026/001" in mit_nummer

    gremium = client.get(seiten["gremium-bearbeiten"]).content.decode()
    assert "x-data=\"{ orgType: 'committee' }\"" in gremium and 'x-model="orgType"' in gremium
    assert "x-show=\"orgType !== 'department'\"" in gremium

    # Ohne Sitzungsgelder-Recht keine Bankdaten, mit Recht verschlüsselt vorbelegt
    person = seiten["person-bearbeiten"]
    assert 'name="bank_iban"' not in _client(tenant, STAMMDATEN).get(person).content.decode()
    assert 'value="DE02120300000000202051"' in client.get(person).content.decode()

    # TOP hinzufügen: Abbrechen leert den Bereich, Hinzufügen per htmx
    top = client.get(seiten["topneu-sitzung"], HTTP_HX_REQUEST="true").content.decode()
    assert 'data-action="clear-target" data-target="#agenda-form"' in top and 'hx-swap="none"' in top


def test_bearbeiten_fuehrt_in_den_brotkrumen_zum_eintrag() -> None:
    tenant = _mandant("krumen2bf", neu=True)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    for name, eltern in welt["eltern"].items():
        html = client.get(welt["seiten"][name]).content.decode()
        assert f'href="{eltern}"' in _brotkrumen(html), f"{name} → {eltern}"
    gesperrt = client.get(welt["seiten"]["top-gesperrt"]).content.decode()
    assert 'data-testid="top-gesperrt"' in gesperrt


def test_nur_der_speichern_knopf_ist_gefuellt() -> None:
    """Das Kopfband trägt keinen gefüllten Knopf, der Inhalt genau einen (Speichern); „TOP hinzufügen“ keinen."""
    tenant = _mandant("knoepfe2bf", neu=True)
    welt = _welt_formulare(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        html = client.get(adresse).content.decode()
        if _art(name) == "topneu":
            assert "bg-primary-600 hover:bg-primary-700" not in html, name
            continue
        kopf, inhalt = html[: html.index('class="work-page-content"')], html[html.index('class="work-page-content"') :]
        assert "bg-primary-600 hover:bg-primary-700" not in kopf[kopf.index("<main") :], f"{name}: Kopfband"
        assert inhalt[: inhalt.index("</main>")].count("bg-primary-600 hover:bg-primary-700") == 1, name
        for farbe in ("green", "blue", "indigo", "amber", "red"):
            assert f"bg-{farbe}-600" not in inhalt[: inhalt.index("</main>")], f"{name}: {farbe}"


def test_formulare_nicht_in_12_px() -> None:
    """Die neuen Vorlagen setzen Beschriftungen, Felder und Hilfetexte in 13 bis 14 px, nie in text-xs oder kleiner."""
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        assert "{#" not in text or all("#}" in zeile for zeile in text.splitlines() if "{#" in zeile), datei
