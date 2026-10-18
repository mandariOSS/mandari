# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 3: Auswertung (Issue #944).

Geprüft: Berichte, Jahresplanung (auch mit Vorschau) und die Jahresübersicht der Sitzungsgelder haben mit
eingeschaltetem Schalter eine eigene neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion entfällt und
keine kommt hinzu: Jede Aktion der bisherigen Seite (Jahres- und Gremienwahl, CSV-Exporte, Formular der Serie mit allen
Feldern, Optionen und Knöpfen ``action=preview|create``, Personenlinks, CSV der Jahresübersicht) steht mit denselben
Feldern und Optionen auf der neuen und umgekehrt – für alle sechs Standardrollen und zwei Fachrollen (planen ohne
Sicht auf Nichtöffentliches, Sitzungsgelder ohne Sitzungsrecht), mit Bestand, leerem Bestand und nach Körperschaft
gruppierten Gremien. Zahlen stehen in Sätzen oder ruhigen Tabellen mit Linien statt in Zählerkacheln, der Stand der
Vorschau als Punkt mit Text; höchstens „Termine als Entwurf anlegen“ trägt die Füllfarbe.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAttendance,
    SessionBody,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
)
from apps.session.tests.gleichstand import (
    Aktionen,
    aktionen,
    fehlende,
    gezeigt,
    neue,
    reiter_ausnahmen,
    rollen,
    vergleichen,
)
from apps.session.tests.test_neues_design import _client, _mandant, _vorlagen

pytestmark = pytest.mark.django_db

#: Seite → (bisherige Vorlage, neue Vorlage)
VORLAGEN = {
    "berichte": ("session/reports/index.html", "session/neu/berichte.html"),
    "jahresplanung": ("session/calendar/plan.html", "session/neu/jahresplanung.html"),
    "sitzungsgelder_jahr": ("session/allowances/year.html", "session/neu/sitzungsgelder_jahr.html"),
}
DATEIEN = ("berichte.html", "jahresplanung.html", "sitzungsgelder_jahr.html")
#: Fachrollen neben den Standardrollen: planen ohne Sicht auf Nichtöffentliches, Sitzungsgelder ohne Sitzungsrecht
PLANER = "Planer"
KASSE = "Kasse"
FUELLUNG = "bg-primary-600 hover:bg-primary-700"


def _adresse(tenant: SessionTenant, name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kw})


def _sortiert(adresse: str) -> str:
    """Anfrageparameter in fester Reihenfolge, wie ``gezeigt`` sie liefert."""
    teile = urlsplit(adresse)
    if not teile.query:
        return adresse
    return f"{teile.path}?" + "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(teile.query, keep_blank_values=True)))


def _um(tag: date, stunde: int = 18) -> datetime:
    return timezone.make_aware(datetime.combine(tag, time(stunde, 0)))


def _fachrollen(tenant: SessionTenant) -> None:
    SessionRole.objects.create(tenant=tenant, name=PLANER, can_view_meetings=True, can_edit_meetings=True)
    SessionRole.objects.create(
        tenant=tenant, name=KASSE, can_manage_allowances=True, can_view_meetings=False, can_view_papers=False
    )


def _welt_auswertung(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand des laufenden Jahres: Rat und Ausschuss (aktiv), ein Fachbereich und ein inaktives Gremium (beide nicht in
    der Jahresplanung), eine öffentliche Ratssitzung mit Dauer, TOPs und Beschluss, eine nichtöffentliche
    Ausschusssitzung, eine Sitzung im Vorjahr (zweites Jahr zur Auswahl), Anwesenheit in allen Ständen, Sitzungsgelder
    ausstehend, genehmigt, ausgezahlt und storniert, Monatspauschalen, zwei freigegebene Vorlagen. Für die Vorschau der
    Jahresplanung im Folgejahr eine öffentliche Sitzung zur selben Zeit und eine nichtöffentliche im selben Raum.
    """
    _fachrollen(tenant)
    jahr = timezone.localdate().year
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    ausschuss = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")
    ohne_planung = [
        SessionOrganization.objects.create(tenant=tenant, name="Amt 61", organization_type="department"),
        SessionOrganization.objects.create(
            tenant=tenant, name="Alter Beirat", organization_type="committee", is_active=False
        ),
    ]
    personen = [
        SessionPerson.objects.create(tenant=tenant, given_name=v, family_name=n, start_date=date(jahr - 1, 1, 1))
        for v, n in (("Anna", "Amberg"), ("Bernd", "Birk"), ("Clara", "Conrad"))
    ]

    beginn = _um(date(jahr, 3, 10))
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=rat,
        name="Ratssitzung",
        start=beginn,
        actual_start=beginn,
        actual_end=beginn + timedelta(minutes=120),
        meeting_state="completed",
    )
    geheim = SessionMeeting.objects.create(
        tenant=tenant,
        organization=ausschuss,
        name="Grundstücke",
        start=_um(date(jahr, 4, 14)),
        is_public=False,
        meeting_state="completed",
    )
    SessionMeeting.objects.create(tenant=tenant, organization=rat, name="Vorjahr", start=_um(date(jahr - 1, 6, 1)))
    SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Haushalt", vote_result="approved")
    SessionAgendaItem.objects.create(meeting=sitzung, number="2", order=2, name="Bericht")

    teilnahmen = {
        (sitzung.pk, person.pk): SessionAttendance.objects.create(meeting=sitzung, person=person, status=status)
        for person, status in zip(personen, ("present", "excused", "absent"), strict=True)
    }
    for person in personen[:2]:
        teilnahmen[(geheim.pk, person.pk)] = SessionAttendance.objects.create(
            meeting=geheim, person=person, status="present"
        )
    for (meeting_id, person_id), status in (
        ((sitzung.pk, personen[0].pk), "pending"),
        ((geheim.pk, personen[0].pk), "paid"),
        ((geheim.pk, personen[1].pk), "approved"),
        ((sitzung.pk, personen[1].pk), "cancelled"),
    ):
        SessionAllowance.objects.create(
            attendance=teilnahmen[(meeting_id, person_id)],
            amount=Decimal("30.00"),
            status=status,
            export_reference="SG-1" if status == "paid" else "",
        )
    pauschale = SessionMonthlyRate.objects.create(tenant=tenant, name="Teilpauschale", amount=Decimal("322.50"))
    for person, monat, status in ((personen[2], 2, "approved"), (personen[0], 3, "paid")):
        SessionMonthlyAllowance.objects.create(
            tenant=tenant,
            person=person,
            rate=pauschale,
            period=date(jahr, monat, 1),
            amount=pauschale.amount,
            status=status,
        )

    for nummer, tage in ((1, 10), (2, 30)):
        vorlage = SessionPaper.objects.create(tenant=tenant, name=f"Vorlage {nummer}", reference=f"V/{jahr}/00{nummer}")
        angelegt = _um(date(jahr, 2, 1))
        SessionPaper.objects.filter(pk=vorlage.pk).update(
            status="approved", created_at=angelegt, approved_at=angelegt + timedelta(days=tage)
        )

    # Jahresplanung im Folgejahr: wöchentlich ab einem Montag; am ersten Termin eine Sitzung zur selben Zeit, am
    # zweiten eine nichtöffentliche am Vormittag im selben Raum
    erster = date(jahr + 1, 1, 7)
    erster -= timedelta(days=erster.weekday())
    SessionMeeting.objects.create(tenant=tenant, organization=ausschuss, name="Bauausschuss", start=_um(erster))
    SessionMeeting.objects.create(
        tenant=tenant,
        organization=ausschuss,
        name="Vergabe",
        start=_um(erster + timedelta(days=7), 9),
        room="Ratssaal",
        is_public=False,
    )
    serie = {
        "organization": str(rat.pk),
        "name": "Sitzung des Rates",
        "rhythm": "weekly",
        "weekday": str(erster.weekday()),
        "time": "18:00",
        "date_from": erster.isoformat(),
        "date_to": (erster + timedelta(days=21)).isoformat(),
        "location": "Rathaus",
        "room": "Ratssaal",
        "is_public": "1",
        "action": "preview",
    }

    berichte = _adresse(tenant, "reports")
    export = _adresse(tenant, "reports_export_csv")
    jahresuebersicht = _adresse(tenant, "allowance_year")
    return {
        "jahr": jahr,
        "rat": rat,
        "ohne_planung": ohne_planung,
        "personen": personen,
        "serie": serie,
        "seiten": {
            "berichte": berichte,
            "berichte_gremium": f"{berichte}?year={jahr}&organization={rat.pk}",
            "berichte_vorjahr": f"{berichte}?year={jahr - 1}",
            "jahresplanung": _adresse(tenant, "meeting_plan"),
            "sitzungsgelder_jahr": jahresuebersicht,
            "sitzungsgelder_vorjahr": f"{jahresuebersicht}?year={jahr - 1}",
        },
        "erwartet": {
            ("GET", ""),
            ("POST", ""),
            ("GET", _sortiert(f"{export}?type=attendance&year={jahr}")),
            ("GET", _sortiert(f"{export}?type=attendance&year={jahr}&organization={rat.pk}")),
            ("GET", _sortiert(f"{export}?type=allowances&year={jahr}")),
            ("GET", _sortiert(f"?year={jahr}&format=csv")),
            ("GET", _adresse(tenant, "person_detail", person_id=personen[0].pk)),
        },
    }


def _vergleich_post(
    tenant: SessionTenant, client: Client, adresse: str, daten: dict[str, str]
) -> tuple[list[str], Aktionen, str]:
    """
    Wie ``vergleichen``, aber für die Vorschau der Jahresplanung (POST): beide Vorlagen mit denselben Formulardaten,
    Vergleich in beide Richtungen; neue Links nur, wenn sie für die Rolle erreichbar sind. Liefert auch die neue Seite.
    """
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
    alt = client.post(adresse, daten)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    neu = client.post(adresse, daten)
    assert alt.status_code == neu.status_code == 200, (alt.status_code, neu.status_code)
    assert VORLAGEN["jahresplanung"][0] in _vorlagen(alt) and VORLAGEN["jahresplanung"][1] in _vorlagen(neu)
    bisher, jetzt = aktionen(alt), aktionen(neu)
    fehlt = fehlende(bisher, jetzt, reiter_ausnahmen(tenant))
    for methode, ziel, felder in neue(bisher, jetzt):
        if methode == "GET" and not felder and client.get(ziel).status_code in (200, 301, 302):
            continue
        fehlt.append(f"neu {(methode, ziel, felder)}")
    return fehlt, bisher, neu.content.decode()


class _Formularstand(HTMLParser):
    """Was ein Formular nach dem Absenden wieder anzeigt: Werte der Eingaben, gewählte Optionen, gesetzte Häkchen."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stand: dict[str, str] = {}
        self._auswahl = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        name = a.get("name", "")
        if tag == "input" and name and name != "csrfmiddlewaretoken":
            if a.get("type") == "checkbox":
                self.stand[name] = "an" if "checked" in a else "aus"
            elif a.get("type") not in ("submit", "button", "hidden"):
                self.stand[name] = a.get("value", "")
        elif tag == "select":
            self._auswahl = name
        elif tag == "option" and self._auswahl and "selected" in a:
            self.stand[self._auswahl] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._auswahl = ""


def _formularstand(html: str) -> dict[str, str]:
    """Stand des Formulars der Jahresplanung (das Formular mit dem Rhythmus), in beiden Rahmen."""
    feld = html.index('name="rhythm"')
    leser = _Formularstand()
    leser.feed(html[html.rindex("<form", 0, feld) : html.index("</form>", feld)])
    leser.close()
    return leser.stand


def _inhalt(html: str) -> str:
    return html[html.index('class="work-page-content"') : html.index("</main>")]


def _kopf(html: str) -> str:
    return html[html.index("<main") : html.index('class="work-page-content"')]


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("auswertung3", neu=False)
    welt = _welt_auswertung(tenant)
    client = _client(tenant, "Administrator")
    for name, (bisher, neu_vorlage) in VORLAGEN.items():
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt["seiten"][name])
        assert alt.status_code == 200, name
        assert bisher in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        assert neu_vorlage not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt["seiten"][name])
        assert neu.status_code == 200, name
        assert {neu_vorlage, "session/base_session_neu.html"} <= _vorlagen(neu) and bisher not in _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


def test_rahmen_ordnet_die_seiten_ein() -> None:
    """Berichte unter Verwaltung, Jahresplanung als Reiter der Sitzungen, Jahresübersicht unter Sitzungsgelder."""
    tenant = _mandant("rahmen3", neu=True)
    welt = _welt_auswertung(tenant)
    client = _client(tenant, "Administrator")

    def krumen(html: str) -> str:
        anfang = html.index('aria-label="Brotkrumen"')
        return " ".join(html[anfang : html.index("</nav>", anfang)].split())

    plan_url = welt["seiten"]["jahresplanung"]
    plan = client.get(plan_url).content.decode()
    assert f'href="{plan_url}" aria-current="page"' in plan, "Reiter „Jahresplanung“ aktiv"
    berichte = client.get(welt["seiten"]["berichte"]).content.decode()
    assert "Verwaltung" in krumen(berichte) and "Berichte" in krumen(berichte)
    jahr = client.get(welt["seiten"]["sitzungsgelder_jahr"]).content.decode()
    assert _adresse(tenant, "allowances") in krumen(jahr) and "Jahresübersicht" in krumen(jahr)


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_fuer_alle_rollen() -> None:
    """
    Alle Aktionen der drei Seiten (mit Gremienfilter und im Vorjahr) mit Feldern und Optionen in beide Richtungen, für
    die sechs Standardrollen und die beiden Fachrollen; ohne das Recht der Seite antworten beide gleich (403).
    Ausnahme nur die Reiter des Rahmens (der Kalender stand bisher in den Brotkrumen der Jahresplanung und ist jetzt der
    Reiter daneben).
    """
    tenant = _mandant("gleich3", neu=False)
    welt = _welt_auswertung(tenant)
    fehlt, gesehen = vergleichen(
        tenant, welt["seiten"], rollen_liste=[*rollen(), PLANER, KASSE], ausnahmen=reiter_ausnahmen(tenant)
    )
    assert not fehlt, "\n".join(fehlt)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))
    # Die Formulare tragen alle Felder und Optionen der bisherigen Seiten
    formulare = {felder for methode, adresse, felder in gesehen if adresse == "" and methode in ("GET", "POST")}
    plan = next(f for f in formulare if "action=preview" in f)
    assert {"name", "time", "date_from", "date_to", "location", "room", "is_public∈1"} <= set(plan), plan
    assert any(f.startswith("rhythm∈") and "monthly_4" in f for f in plan) and any(
        f.startswith("weekday∈") for f in plan
    )
    gremien = next(f for f in plan if f.startswith("organization∈"))
    assert str(welt["rat"].pk) in gremien and not any(str(o.pk) in gremien for o in welt["ohne_planung"])

    # Wer die Seite nicht öffnen darf, bekommt sie auch im neuen Rahmen nicht
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    erlaubt = {
        "berichte": {"Administrator", "Sachbearbeiter", "Protokollant", "Lesezugriff", PLANER},
        "jahresplanung": {"Administrator", "Sachbearbeiter", PLANER},
        "sitzungsgelder_jahr": {"Administrator", KASSE},
    }
    for rolle in [*rollen(), PLANER, KASSE]:
        client = _client(tenant, rolle)
        for name, rollen_mit_recht in erlaubt.items():
            status = client.get(welt["seiten"][name]).status_code
            assert status == (200 if rolle in rollen_mit_recht else 403), (rolle, name, status)


def test_gleichstand_der_vorschau() -> None:
    """
    Vorschau der Jahresplanung (POST): Formular mit allen eingegebenen Werten, Knöpfe ``action=preview`` und
    ``action=create``, Kollisionen (zeitlich, Raum, für die Fachrolle ohne Sicht auf Nichtöffentliches nur „Termin
    belegt“). Die Vorschau legt nichts an.
    """
    tenant = _mandant("vorschau3", neu=False)
    welt = _welt_auswertung(tenant)
    adresse = welt["seiten"]["jahresplanung"]
    vorher = SessionMeeting.objects.filter(tenant=tenant).count()
    for rolle in ("Administrator", "Sachbearbeiter", PLANER):
        fehlt, bisher, html = _vergleich_post(tenant, _client(tenant, rolle), adresse, welt["serie"])
        assert not fehlt, f"{rolle}:\n" + "\n".join(fehlt)
        formular = next(felder for methode, ziel, felder in bisher if methode == "POST" and "action=create" in felder)
        assert "action=preview" in formular, formular
        inhalt = _inhalt(html)
        assert inhalt.count("<li") >= 4 and "Kollision" in inhalt and "frei" in inhalt, rolle
        assert "Zeitliche Überschneidung: Bauausschuss" in inhalt, rolle
        if rolle == PLANER:
            assert "Raum belegt: Termin belegt" in " ".join(inhalt.split()) and "Vergabe" not in inhalt
        else:
            assert "Raum belegt: Vergabe" in inhalt
        # Eingegebene Werte bleiben stehen
        assert 'value="Sitzung des Rates"' in inhalt and 'value="Ratssaal"' in inhalt and 'value="Rathaus"' in inhalt
    # Werte mit Sonderzeichen, andere Auswahl und ohne Häkchen stehen neu genauso (einfach maskiert) wie bisher
    client = _client(tenant, "Administrator")
    daten = {**welt["serie"], "name": 'Rat & "Haupt" <b>', "location": "Saal 1 & 2", "rhythm": "monthly_2"}
    del daten["is_public"]
    for werte in (welt["serie"], daten):
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.post(adresse, werte).content.decode()
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.post(adresse, werte).content.decode()
        stand = _formularstand(neu)
        assert stand == _formularstand(alt), (stand, _formularstand(alt))
        assert stand["is_public"] == ("an" if "is_public" in werte else "aus") and stand["rhythm"] == werte["rhythm"]
    assert stand["name"] == 'Rat & "Haupt" <b>' and 'value="Rat &amp; &quot;Haupt&quot; &lt;b&gt;"' in neu
    assert SessionMeeting.objects.filter(tenant=tenant).count() == vorher


def test_anlegen_aus_der_neuen_vorschau() -> None:
    """Der Knopf der neuen Vorschau legt die Termine wie bisher als Entwürfe an und führt zum Kalender."""
    tenant = _mandant("anlegen3", neu=True)
    welt = _welt_auswertung(tenant)
    client = _client(tenant, "Sachbearbeiter")
    html = _inhalt(client.post(welt["seiten"]["jahresplanung"], welt["serie"]).content.decode())
    assert 'name="action" value="create"' in html and "4 Termine als Entwurf anlegen" in " ".join(html.split())
    antwort = client.post(welt["seiten"]["jahresplanung"], {**welt["serie"], "action": "create"})
    assert antwort.status_code == 302 and antwort["Location"] == _adresse(tenant, "meeting_calendar")
    angelegt = SessionMeeting.objects.filter(tenant=tenant, name="Sitzung des Rates")
    assert angelegt.count() == 4 and set(angelegt.values_list("meeting_state", flat=True)) == {"draft"}


def test_gleichstand_bei_leerem_bestand_und_gruppierten_gremien() -> None:
    """
    Ohne Sitzungen, Sitzungsgelder und Vorlagen: dieselben Ziele und die Sätze des Leerzustands. Mit einer zweiten
    Körperschaft stehen die Gremien der Jahresplanung wie bisher nach Körperschaft gruppiert zur Auswahl.
    """
    tenant = _mandant("leer3", neu=False)
    seiten = {
        "berichte": _adresse(tenant, "reports"),
        "jahresplanung": _adresse(tenant, "meeting_plan"),
        "sitzungsgelder_jahr": _adresse(tenant, "allowance_year"),
    }
    fehlt, _ = vergleichen(tenant, seiten, rollen_liste=["Administrator"], ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Administrator")
    jahr = timezone.localdate().year
    berichte = _inhalt(client.get(seiten["berichte"]).content.decode())
    for satz in (
        f"Keine Sitzungen in {jahr}.",
        f"Im Jahr {jahr} wurde keine Vorlage freigegeben.",
        f"Keine Anwesenheitsdaten in {jahr}.",
        f"Keine Sitzungsgelder in {jahr}.",
    ):
        assert satz in berichte, satz
    assert "<table" not in berichte
    uebersicht = _inhalt(client.get(seiten["sitzungsgelder_jahr"]).content.decode())
    assert (
        f"Keine Sitzungsgeld- oder Pauschalen-Positionen im Jahr {jahr}." in uebersicht and "<table" not in uebersicht
    )

    # Zweite Körperschaft: Gremien gruppiert (optgroup) wie bisher
    SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    nord = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Nord", short_name="GN", body_type="mitgliedsgemeinde"
    )
    SessionOrganization.objects.create(tenant=tenant, name="Ortsrat Nord", organization_type="council", body=nord)
    fehlt, gesehen = vergleichen(
        tenant,
        {"jahresplanung": seiten["jahresplanung"]},
        rollen_liste=["Administrator"],
        ausnahmen=reiter_ausnahmen(tenant),
    )
    assert not fehlt, "\n".join(fehlt)
    plan = _inhalt(client.get(seiten["jahresplanung"]).content.decode())
    assert "<optgroup" in plan and 'label="Gemeinde Nord"' in plan


# =============================================================================
# Darstellung
# =============================================================================


def test_zahlen_in_saetzen_und_ruhigen_tabellen() -> None:
    tenant = _mandant("saetze3", neu=True)
    welt = _welt_auswertung(tenant)
    client = _client(tenant, "Administrator")
    jahr = welt["jahr"]
    berichte = " ".join(_inhalt(client.get(welt["seiten"]["berichte"]).content.decode()).split())
    # Sitzungen je Gremium und Durchlaufzeit als Satz statt Zählerkacheln
    assert "1 Sitzung, 2 TOPs, 1 Beschluss, im Schnitt 120 min" in berichte
    assert (
        f"2 Vorlagen wurden {jahr} freigegeben. Vom Entwurf bis zur Freigabe vergingen im Median 20 Tage, höchstens 30 Tage."
        in berichte
    )
    # Anwesenheit und Sitzungsgeld als Tabellen mit Linien, am Handy waagerecht verschiebbar; Summen im Fuß
    assert berichte.count("<table") == 2 and berichte.count('class="mt-2 overflow-x-auto"') == 2
    assert "<tfoot" in berichte and "Gesamt" in berichte
    # Lesezugriff: nur öffentliche Sitzungen, kein Sitzungsgeld
    lesen = _inhalt(_client(tenant, "Lesezugriff").get(welt["seiten"]["berichte"]).content.decode())
    gremien = lesen[lesen.index('data-testid="berichte-gremien"') : lesen.index('data-testid="berichte-durchlauf"')]
    assert "Rat" in gremien and "Bauausschuss" not in gremien
    assert "Sitzungsgeld und Pauschalen" not in lesen and lesen.count("<table") == 1

    uebersicht = _inhalt(client.get(welt["seiten"]["sitzungsgelder_jahr"]).content.decode())
    assert uebersicht.count("<table") == 1 and "overflow-x-auto" in uebersicht and "sticky left-0" in uebersicht
    assert "Sitzungsgeld zählt im Jahr der Sitzung" in uebersicht
    # Kasse ohne Sitzungsrecht: Namen ohne Link zur Personenseite (sonst 403)
    kasse = _inhalt(_client(tenant, KASSE).get(welt["seiten"]["sitzungsgelder_jahr"]).content.decode())
    assert "Anna Amberg" in kasse
    assert _adresse(tenant, "person_detail", person_id=welt["personen"][0].pk) not in kasse

    for html in (berichte, uebersicht):
        for verboten in ("kpi", "text-2xl font-bold", "rounded-xl", "uppercase"):
            assert verboten not in html, verboten


def test_hoechstens_ein_gefuellter_knopf_und_nur_an_seiner_stelle() -> None:
    """
    Berichte und Jahresübersicht: kein gefüllter Knopf (Filter und Exporte ohne Füllfarbe). Jahresplanung: vor der
    Vorschau keiner, mit Vorschau genau einer – „Termine als Entwurf anlegen“ unter den Terminen, wie das Speichern
    der Formularseiten (session/neu/_formular_aktionen.html); das Kopfband trägt dann keinen.
    """
    tenant = _mandant("knoepfe3", neu=True)
    welt = _welt_auswertung(tenant)
    client = _client(tenant, "Administrator")
    for name in ("berichte", "berichte_gremium", "jahresplanung", "sitzungsgelder_jahr"):
        html = client.get(welt["seiten"][name]).content.decode()
        assert FUELLUNG not in _inhalt(html) and FUELLUNG not in _kopf(html), name
    html = client.post(welt["seiten"]["jahresplanung"], welt["serie"]).content.decode()
    inhalt = _inhalt(html)
    assert FUELLUNG not in _kopf(html) and inhalt.count(FUELLUNG) == 1
    knopf = inhalt[: inhalt.index(FUELLUNG)]
    assert knopf.rindex("<button") > inhalt.index('data-testid="jahresplanung-vorschau"')
    for name in ("berichte", "jahresplanung", "sitzungsgelder_jahr"):
        html = _inhalt(client.get(welt["seiten"][name]).content.decode())
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple", "emerald"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in html, (name, farbe)
            assert f"bg-{farbe}-100" not in html, (name, farbe)


def test_neue_vorlagen_ohne_12_px_karten_und_chips() -> None:
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for verboten in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]", "c-ui.card", "c-ui.badge",
                         "kpi-tile", "rounded-xl", "rounded-full", "onclick=", "style="):  # fmt: skip
            assert verboten not in text, f"{datei}: {verboten}"
        # Mehrzeilige Kommentare nur als {% comment %}
        for zeile in text.splitlines():
            assert zeile.count("{#") == zeile.count("#}"), f"{datei}: {zeile}"
