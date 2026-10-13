# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 3: Gremien, Personen und Archiv (Issue #944).

Geprüft: Die Gremienliste, die Personenliste und das Archiv nach Wahlperioden haben mit eingeschaltetem Schalter eine
eigene neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion entfällt und keine kommt hinzu: Jede Aktion
der bisherigen Seite (Neues Gremium bzw. Neue Person, Filter mit denselben Feldern und Optionen, Links auf Gremium und
Person, Seitenwahl, Perioden verwalten, die nach Periode gefilterten Listen) steht auch auf der neuen und umgekehrt – für
alle sechs Standardrollen und eine Rolle, die nur Sitzungen sehen darf, mit gesetzten Filtern, zweiter Seite, leeren
Treffern und leerem Bestand. Die Zahlen stehen im Satz statt in Kacheln, der Stand als Punkt mit Text; keine gefüllten
Knöpfe im Inhalt und keine Schrift unter 13 px.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.session.models import (
    SessionBody,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
)
from apps.session.services import body_service
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, rollen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _vorlagen

pytestmark = pytest.mark.django_db

#: Seiten dieses Teils: Name → (bisherige Vorlage, neue Vorlage)
SEITEN = {
    "gremien": ("session/organizations/list.html", "session/neu/gremien.html"),
    "personen": ("session/persons/list.html", "session/neu/personen.html"),
    "archiv": ("session/archive.html", "session/neu/archiv.html"),
}
DATEIEN = ("gremien.html", "personen.html", "archiv.html")
#: Darf Sitzungen sehen, aber keine Vorlagen: Das Archiv zeigt dann weder Zahl noch Link der Vorlagen
NUR_SITZUNGEN = "Nur Sitzungen"


def _adresse(tenant: SessionTenant, name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kw})


def _um(tag: date) -> datetime:
    return timezone.make_aware(datetime.combine(tag, time(17, 0)))


def _welt_listen(tenant: SessionTenant) -> dict[str, Any]:
    """
    Zwei Körperschaften (Filter und Angabe an der Zeile); drei Wahlperioden: laufend ohne Ende, beendet und ohne
    Zeitraum; 23 Gremien (zweite Seite), davon eines inaktiv und eines in der zweiten Körperschaft, mit Besetzungen in
    der laufenden und der beendeten Periode; 53 aktive Personen (zweite Seite), eine mit Titel und E-Mail, und eine
    inaktive; Sitzungen und Vorlagen in beiden Perioden, auch nichtöffentliche; eine Rolle nur mit Sitzungsrecht.
    ``erwartet``: die Aktionen, die nur dieser Bestand zeigt.
    """
    SessionRole.objects.create(tenant=tenant, name=NUR_SITZUNGEN, can_view_meetings=True, can_view_papers=False)
    nord = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Nord", short_name="GN", slug="gemeinde-nord", body_type="mitgliedsgemeinde"
    )
    laufend = SessionLegislativeTerm.objects.create(tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1))
    beendet = SessionLegislativeTerm.objects.create(
        tenant=tenant, name="2020–2025", start_date=date(2020, 11, 1), end_date=date(2025, 10, 31)
    )
    ohne = SessionLegislativeTerm.objects.create(tenant=tenant, name="Übergang")

    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    inaktiv = SessionOrganization.objects.create(
        tenant=tenant, name="Abfallbeirat", organization_type="advisory", is_active=False
    )
    SessionOrganization.objects.create(
        tenant=tenant, name="Bezirksvertretung Nord", organization_type="committee", body=nord
    )
    ausschuesse = [
        SessionOrganization.objects.create(tenant=tenant, name=f"Fachausschuss {n:02d}", organization_type="committee")
        for n in range(1, 21)
    ]

    amberg = SessionPerson.objects.create(
        tenant=tenant,
        title="Dr.",
        given_name="Anna",
        family_name="Amberg",
        email="anna.amberg@example.org",
        start_date=date(2025, 1, 1),
    )
    muster = [
        SessionPerson.objects.create(
            tenant=tenant, given_name="Max", family_name=f"Muster{n:02d}", start_date=date(2025, 1, 1)
        )
        for n in range(1, 53)
    ]
    SessionPerson.objects.create(tenant=tenant, given_name="Inge", family_name="Inaktiv", is_active=False)

    # Besetzungen: laufend 3 in 2 Gremien (Rat 2, Fachausschuss 01 1), beendet 1 im Rat
    for gremium, person in ((rat, amberg), (rat, muster[0]), (ausschuesse[0], muster[1])):
        SessionOrganizationMembership.objects.create(
            organization=gremium, person=person, start_date=date(2025, 11, 1), legislative_term=laufend
        )
    SessionOrganizationMembership.objects.create(
        organization=rat,
        person=muster[2],
        start_date=date(2020, 11, 1),
        end_date=date(2025, 10, 31),
        legislative_term=beendet,
    )

    # Sitzungen: laufend eine öffentliche und eine nichtöffentliche, beendet eine
    heute = timezone.localdate()
    for name, tag, oeffentlich in (
        ("Ratssitzung", heute - timedelta(days=30), True),
        ("Grundstücke", heute - timedelta(days=29), False),
        ("Alte Ratssitzung", date(2024, 3, 1), True),
    ):
        SessionMeeting.objects.create(
            tenant=tenant, organization=rat, name=name, start=_um(tag), meeting_state="completed", is_public=oeffentlich
        )
    # Vorlagen: laufend eine, beendet eine öffentliche und eine nichtöffentliche
    for name, tag, oeffentlich in (
        ("Haushalt", date(2026, 2, 1), True),
        ("Radweg", date(2023, 5, 1), True),
        ("Grundstück", date(2023, 6, 1), False),
    ):
        SessionPaper.objects.create(
            tenant=tenant, name=name, date=tag, is_public=oeffentlich, main_organization=rat, paper_type="proposal"
        )

    gremien = _adresse(tenant, "organizations")
    personen = _adresse(tenant, "persons")
    erwartet = {
        ("GET", ""),  # die Filter
        ("GET", "?page=2"),
        ("GET", _adresse(tenant, "organization_create")),
        ("GET", _adresse(tenant, "organization_detail", organization_id=inaktiv.pk)),
        ("GET", _adresse(tenant, "person_create")),
        ("GET", _adresse(tenant, "person_detail", person_id=amberg.pk)),
        ("GET", _adresse(tenant, "terms")),
    }
    for periode in (laufend, beendet, ohne):
        for liste in ("meetings", "papers", "organizations"):
            erwartet.add(("GET", f"{_adresse(tenant, liste)}?term={periode.pk}"))
    return {
        "seiten": {
            "gremien": gremien,
            "gremien_filter": f"{gremien}?type=council&active=1&term={laufend.pk}",
            "gremien_koerperschaft": f"{gremien}?body={nord.slug}",
            "gremien_leer": f"{gremien}?type=faction&body={nord.slug}",
            "gremien_seite2": f"{gremien}?page=2",
            "personen": personen,
            "personen_suche": f"{personen}?q=Amberg&active=0",
            "personen_inaktive": f"{personen}?active=0&page=2",
            "personen_leer": f"{personen}?q=Niemand",
            "archiv": _adresse(tenant, "archive"),
        },
        "erwartet": erwartet,
        "perioden": (laufend, beendet, ohne),
        "nord": nord,
    }


_INHALT = 'class="work-page-content">'


def _inhalt(html: str) -> str:
    """Der Inhalt unter dem Kopfband (ohne Rahmen und Kopfband)."""
    return html[html.index(_INHALT) + len(_INHALT) : html.index("</main>")]


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []

    def handle_data(self, data: str) -> None:
        self.teile.append(data)


def _text(html: str) -> str:
    """Sichtbarer Text ohne Auszeichnung, Leerraum zusammengefasst."""
    leser = _Text()
    leser.feed(html)
    leser.close()
    return re.sub(r"\s+", " ", "".join(leser.teile)).strip()


# =============================================================================
# Vorlagen nach dem Schalter
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("listen3", neu=False)
    welt = _welt_listen(tenant)
    client = _client(tenant, "Administrator")
    for name, (bisher, neu_vorlage) in SEITEN.items():
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt["seiten"][name])
        assert alt.status_code == 200, name
        assert {bisher, "session/base_session_alt.html"} <= _vorlagen(alt) and neu_vorlage not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt["seiten"][name])
        assert neu.status_code == 200, name
        assert {neu_vorlage, "session/base_session_neu.html"} <= _vorlagen(neu) and bisher not in _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_fuer_alle_rollen() -> None:
    """
    Alle Aktionen mit Feldern und Optionen (Typen, Perioden, Körperschaften, „nur aktive“ = 1, „Inaktive anzeigen“ = 0)
    in beide Richtungen, mit gesetzten Filtern, zweiter Seite und leeren Treffern; Revision und Datenschutz erreichen
    keine der Seiten (403 wie bisher). Ausnahme nur die Reiter des Rahmens.
    """
    tenant = _mandant("listengleich3", neu=False)
    welt = _welt_listen(tenant)
    fehlt, gesehen = vergleichen(
        tenant, welt["seiten"], rollen_liste=[*rollen(), NUR_SITZUNGEN], ausnahmen=reiter_ausnahmen(tenant)
    )
    assert not fehlt, "\n".join(fehlt)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))
    # Die Filter mit allen Feldern und Optionen: Körperschaft erst ab der zweiten, Wahlperiode, „nur aktive“ und
    # „Inaktive anzeigen“ mit ihren Werten
    filter_felder = {
        feld for methode, adresse, felder in gesehen if (methode, adresse) == ("GET", "") for feld in felder
    }
    laufend, beendet, ohne = welt["perioden"]
    assert {"active∈1", "active∈0", "q"} <= filter_felder, sorted(filter_felder)
    assert f"term∈|{'|'.join(sorted(str(p.pk) for p in (laufend, beendet, ohne)))}" in filter_felder
    assert any(feld.startswith("body∈") and welt["nord"].slug in feld for feld in filter_felder), sorted(filter_felder)
    # Kontrollrollen erreichen keine der Seiten, mit und ohne Schalter
    for rolle in ("Revision", "Datenschutz"):
        client = _client(tenant, rolle)
        for adresse in welt["seiten"].values():
            assert client.get(adresse).status_code == 403, (rolle, adresse)


def test_gleichstand_bei_leerem_bestand() -> None:
    tenant = _mandant("listenleer3", neu=False)
    seiten = {name: _adresse(tenant, name) for name in ("organizations", "persons", "archive")}
    fehlt, gesehen = vergleichen(
        tenant, seiten, rollen_liste=["Administrator", "Lesezugriff"], ausnahmen=reiter_ausnahmen(tenant)
    )
    assert not fehlt, "\n".join(fehlt)
    assert {("GET", _adresse(tenant, "organization_create")), ("GET", _adresse(tenant, "terms"))} <= gezeigt(gesehen)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Lesezugriff")
    assert "Es sind noch keine Gremien vorhanden." in client.get(seiten["organizations"]).content.decode()
    assert "Es sind noch keine Personen vorhanden." in client.get(seiten["persons"]).content.decode()
    assert "Noch keine Wahlperioden." in client.get(seiten["archive"]).content.decode()


# =============================================================================
# Darstellung
# =============================================================================


def test_zahlen_im_satz_und_stand_als_punkt() -> None:
    tenant = _mandant("listensatz3", neu=True)
    welt = _welt_listen(tenant)
    seiten = welt["seiten"]
    client = _client(tenant, "Administrator")

    gremien = client.get(seiten["gremien"]).content.decode()
    assert "23 Gremien, nach Namen sortiert." in gremien
    # Mit zwei Körperschaften steht die Körperschaft an jeder Zeile (wie bisher)
    standard = body_service.default_body(tenant).label
    inhalt = _text(_inhalt(gremien))
    assert f"Abfallbeirat Beirat · {standard} 0 Mitglieder Inaktiv" in inhalt
    assert "Bezirksvertretung Nord Ausschuss · GN 0 Mitglieder" in inhalt
    assert f"Fachausschuss 01 Ausschuss · {standard} 1 Mitglied Fachausschuss 02" in inhalt
    assert f"Rat Rat · {standard} 2 Mitglieder" in _text(_inhalt(client.get(seiten["gremien_seite2"]).content.decode()))
    assert 'aria-hidden="true"></span>Inaktiv</span>' in gremien
    assert "1 Gremium für die gewählten Filter" in client.get(seiten["gremien_filter"]).content.decode()
    leer = client.get(seiten["gremien_leer"]).content.decode()
    assert "Für diese Filter gibt es kein Gremium." in leer and "0 Gremien für die gewählten Filter" in leer

    personen = client.get(seiten["personen"]).content.decode()
    assert "53 aktive Personen, nach Nachnamen sortiert." in personen
    assert "Dr. Anna Amberg anna.amberg@example.org" in _text(_inhalt(personen))
    inaktive = client.get(seiten["personen_suche"].replace("q=Amberg&", "q=Inge&")).content.decode()
    assert "1 Person für die Suche" in inaktive and 'aria-hidden="true"></span>Inaktiv</span>' in inaktive
    assert "Für diese Suche gibt es keine Person." in client.get(seiten["personen_leer"]).content.decode()

    archiv = _text(_inhalt(client.get(seiten["archiv"]).content.decode()))
    assert (
        "2025–2030 ab 01.11.2025, Ende offen Aktuell 2 Sitzungen, 1 Vorlage und 3 Besetzungen in 2 Gremien." in archiv
    )
    assert "2020–2025 01.11.2020 bis 31.10.2025 1 Sitzung, 2 Vorlagen und 1 Besetzung in 1 Gremium." in archiv
    # Ohne Zeitraum zählt die Periode alle Vorlagen (term_date_filter ohne Grenzen), Sitzungen nur zugeordnete
    assert "Übergang 0 Sitzungen, 3 Vorlagen und 0 Besetzungen in 0 Gremien." in archiv
    # Ohne Vorlagenrecht weder Zahl noch Link der Vorlagen, nichtöffentliche Sitzungen nicht gezählt
    nur_sitzungen = _client(tenant, NUR_SITZUNGEN).get(seiten["archiv"]).content.decode()
    assert "2025–2030 ab 01.11.2025, Ende offen Aktuell 1 Sitzung und 3 Besetzungen in 2 Gremien." in _text(
        _inhalt(nur_sitzungen)
    )
    assert _adresse(tenant, "papers") not in _inhalt(nur_sitzungen)


def test_keine_gefuellten_knoepfe_und_nicht_in_12_px() -> None:
    """Die eine Hauptaktion steht im Kopfband; im Inhalt keine Füllknöpfe, Karten, Kacheln oder Pillen, nichts unter 13 px."""
    tenant = _mandant("listenknoepfe3", neu=True)
    welt = _welt_listen(tenant)
    client = _client(tenant, "Administrator")
    for name in ("gremien", "personen", "archiv"):
        html = client.get(welt["seiten"][name]).content.decode()
        inhalt = _inhalt(html)
        assert "bg-primary-600 hover:bg-primary-700" not in inhalt, name
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple", "emerald"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, (name, farbe)
            assert f"bg-{farbe}-100" not in inhalt, (name, farbe)
        assert "<table" not in inhalt and "bg-gradient" not in inhalt, name
    # Die Hauptaktion im Kopfband: Neues Gremium und Neue Person gefüllt, Perioden verwalten als Nebenaktion
    kopf = client.get(welt["seiten"]["archiv"]).content.decode()
    kopf = kopf[: kopf.index('class="work-page-content"')]
    assert "Perioden verwalten" in kopf and "bg-primary-600 hover:bg-primary-700" not in kopf
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for verboten in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]", "c-ui.card", "c-ui.badge",
                         "kpi-tile", "rounded-xl", "rounded-full bg-", "bg-gradient", "onclick=", "style="):  # fmt: skip
            assert verboten not in text, f"{datei}: {verboten}"
