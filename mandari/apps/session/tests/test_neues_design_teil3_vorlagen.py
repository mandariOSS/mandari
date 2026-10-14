# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 3: Zu prüfen, Meine Mitzeichnungen und Suche (Issue #944).

Geprüft: Die drei Seiten haben mit eingeschaltetem Schalter eine eigene neue Vorlage, ohne Schalter bleiben die
bisherigen. Keine Funktion entfällt und keine kommt hinzu: Jede Aktion der bisherigen Seite (Links, Formulare mit
Methode, Feldern und Optionen, Rückfragen, Sperren) steht auch auf der neuen, für jede Standardrolle und in jedem
Zustand des Bestands: Vorlagen in Prüfung (freigabefähig, in Vertretung, durch das Vier-Augen-Prinzip gesperrt,
nichtöffentlich in einer zweiten Körperschaft), mit und ohne Körperschaftsfilter und leer; Mitzeichnungsstationen
(dran, wartend, in Vertretung, mit finanziellen Auswirkungen), leer und ohne Amt; die Suche ohne Begriff, mit Treffern
aller Arten über der Höchstzahl, mit allen Filtern und ohne Treffer. Vertretungen, Vier-Augen-Gründe und Leerzustände
stehen als Satz statt als Hinweiskasten, gefüllt ist höchstens die Hauptaktion im Kopfband.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionBody,
    SessionCosignature,
    SessionDelegation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _volle_welt, _vorlagen

pytestmark = pytest.mark.django_db

#: Seiten dieses Teils: Name im Bestand → neue Vorlage
SEITEN = {
    "zu_pruefen": "session/neu/zu_pruefen.html",
    "mitzeichnungen": "session/neu/mitzeichnungen.html",
    "suche": "session/neu/suche.html",
}
#: Neue Vorlagen dieses Teils (für die Prüfung der Schriftgrößen)
DATEIEN = ("zu_pruefen.html", "mitzeichnungen.html", "suche.html")
#: Alle Seiten des Bestands für den Gleichstand (Name → Schlüssel in der Welt); „_leer“ setzt die Vorlagen vorher frei
VERGLEICH = (
    "zu_pruefen",
    "zu_pruefen_koerperschaft",
    "zu_pruefen_leer",
    "mitzeichnungen",
    "mitzeichnungen_koerperschaft",
    "mitzeichnungen_leer",
    "suche_ohne_begriff",
    "suche",
    "suche_gefiltert",
    "suche_ohne_treffer",
)


def _anlage(tenant: SessionTenant, name: str, oeffentlich: bool = True, **ziel: Any) -> SessionFile:
    datei = SessionFile(tenant=tenant, name=name, mime_type="text/plain", is_public=oeffentlich, **ziel)
    file_version_service.attach_upload(datei, SimpleUploadedFile(name, b"Radweg", content_type="text/plain"), user=None)
    return datei


def _welt_3(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand auf der Grundlage von ``_volle_welt`` (Vorlage „Neue Brücke“ mit Mitzeichnungslauf: Kämmerei mitgezeichnet,
    Bauamt dran, Rechtsamt wartet), dazu:

    - Vier-Augen-Prinzip für Vorlagen immer; eine Leitung (Administrator, Amt Ordnungsamt), die alle Konten des Tests
      im Umfang „Freigaben“ und „Arbeitsvorrat“ vertreten (so sehen auch Rollen ohne Prüfrecht die Seite „Zu prüfen“,
      in Vertretung)
    - vier Vorlagen in Prüfung: „Neue Brücke“ (mit finanziellen Auswirkungen), „Eigene Vorlage“ der Leitung (in
      Vertretung gesperrt, Station Ordnungsamt dran), „Selbst erstellt“ (``vorher`` macht das Konto der Rolle zum
      Ersteller: für alle gesperrt) und „Grundstücksankauf“ (nichtöffentlich, zweite Körperschaft)
    - für die Suche nach „Radweg“: Vorlagen über der Höchstzahl je Art (auch nichtöffentlich), Sitzungen (auch
      nichtöffentlich), ein TOP mit Beschlussnummer und Ergebnis, ein Protokoll, Anlagen an Vorlage, Sitzung, TOP und
      Antrag (auch nichtöffentlich), ein Antrag und eine Wahlperiode
    """
    welt = _volle_welt(tenant)
    slug = tenant.slug
    heute = timezone.localdate()
    SessionTenant.objects.filter(pk=tenant.pk).update(four_eyes_papers="always")
    tenant.refresh_from_db()
    sitzung = welt["sitzung_obj"]
    rat = sitzung.organization

    bruecke = welt["vorlage_obj"]
    SessionPaper.objects.filter(pk=bruecke.pk).update(status="review", has_financial_impact=True)
    stationen = {s.department.name: s for s in SessionCosignature.objects.filter(paper=bruecke)}

    ordnungsamt = SessionOrganization.objects.create(tenant=tenant, name="Ordnungsamt", organization_type="department")
    leitung = SessionUser.objects.create(user=cast(Any, UserFactory)(email="leitung@example.org"), tenant=tenant)
    leitung.roles.add(SessionRole.objects.get(tenant=tenant, name="Administrator"))
    leitung.departments.add(ordnungsamt)

    eigene = SessionPaper.objects.create(
        tenant=tenant, name="Eigene Vorlage", reference="V/2026/051", status="review", main_organization=rat
    )
    SessionPaper.objects.filter(pk=eigene.pk).update(created_by=leitung)
    station_eigene = SessionCosignature.objects.create(paper=eigene, department=ordnungsamt, order=1)
    selbst = SessionPaper.objects.create(
        tenant=tenant, name="Selbst erstellt", reference="V/2026/052", status="review", main_organization=rat
    )
    nord = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Nord", short_name="GN", slug="nord", body_type="mitgliedsgemeinde"
    )
    grundstueck = SessionPaper.objects.create(
        tenant=tenant, name="Grundstücksankauf", reference="V/2026/053", status="review", is_public=False, body=nord
    )
    SessionCosignature.objects.create(paper=grundstueck, department=ordnungsamt, order=1)

    # Suche nach „Radweg“: von jeder Art etwas, Vorlagen über der Höchstzahl je Art
    radweg = SessionPaper.objects.create(
        tenant=tenant, name="Radweg Nord", reference="V/2026/060", status="approved", main_organization=rat
    )
    SessionPaper.objects.create(tenant=tenant, name="Radweg Süd", reference="V/2026/061", is_public=False)
    for nummer in range(24):
        SessionPaper.objects.create(tenant=tenant, name=f"Radweg Abschnitt {nummer}", reference=f"V/2026/1{nummer:02d}")
    sondersitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=rat,
        name="Sondersitzung Radweg",
        start=timezone.now() + timedelta(days=5),
        location="Rathaus, Saal 1",
    )
    SessionMeeting.objects.create(
        tenant=tenant,
        organization=rat,
        name="Radweg Grundstücke",
        start=timezone.now() + timedelta(days=6),
        is_public=False,
    )
    radweg_top = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="5",
        order=7,
        name="Radweg Planung",
        resolution_number="B/2026/7",
        vote_result="approved",
    )
    ausschuss = SessionMeeting.objects.create(
        tenant=tenant, organization=rat, name="Bauausschuss Mai", start=timezone.now() - timedelta(days=40)
    )
    SessionProtocol.objects.create(meeting=ausschuss, content="Der Radweg wurde beraten.", status="review")
    antrag = SessionApplication.objects.create(
        tenant=tenant,
        title="Radweg am Kanal",
        reference="A/2026/0009",
        submitter_name="Fraktion",
        submitter_email="f@example.org",
        status="in_review",
    )
    anlagen = [
        _anlage(tenant, "radweg-plan.txt", paper=radweg),
        _anlage(tenant, "radweg-vermerk.txt", False, paper=radweg),
        _anlage(tenant, "radweg-einladung.txt", meeting=sondersitzung),
        _anlage(tenant, "radweg-top.txt", agenda_item=radweg_top),
        _anlage(tenant, "radweg-antrag.txt", application=antrag),
    ]
    periode = SessionLegislativeTerm.objects.create(
        tenant=tenant,
        name="Wahlperiode 2025–2030",
        start_date=heute - timedelta(days=365),
        end_date=heute + timedelta(days=4 * 365),
    )

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    pruefen, mitzeichnen, suche = adresse("papers_review"), adresse("my_cosignatures"), adresse("search")
    filter_suche = f"organization={rat.pk}&year={timezone.localtime(sondersitzung.start).year}&term={periode.pk}"
    welt.update(
        {
            "zu_pruefen": pruefen,
            "zu_pruefen_koerperschaft": f"{pruefen}?body=nord",
            "zu_pruefen_leer": pruefen,
            "mitzeichnungen": mitzeichnen,
            "mitzeichnungen_koerperschaft": f"{mitzeichnen}?body=nord",
            "mitzeichnungen_leer": mitzeichnen,
            "suche_ohne_begriff": suche,
            "suche": f"{suche}?q=Radweg",
            "suche_gefiltert": f"{suche}?q=Radweg&kind=meetings&{filter_suche}",
            "suche_ohne_treffer": f"{suche}?q=Gibtsnicht",
            "leitung_obj": leitung,
            "selbst_obj": selbst,
            "pruefung_ids": [bruecke.pk, eigene.pk, selbst.pk, grundstueck.pk],
        }
    )

    def workflow(paper: SessionPaper, aktion: str) -> str:
        return adresse("paper_workflow", paper_id=paper.pk, action=aktion)

    def station(cosign: SessionCosignature, aktion: str) -> str:
        return adresse("cosignature_action", cosign_id=cosign.pk, action=aktion)

    welt["erwartet_3"] = {
        ("POST", workflow(bruecke, "approve")),
        ("POST", workflow(bruecke, "reject")),
        ("POST", workflow(eigene, "reject")),
        ("POST", workflow(selbst, "reject")),
        ("POST", workflow(grundstueck, "approve")),
        ("GET", adresse("paper_detail", paper_id=bruecke.pk)),
        ("GET", adresse("paper_detail", paper_id=grundstueck.pk)),
        ("POST", station(stationen["Bauamt"], "sign")),
        ("POST", station(stationen["Bauamt"], "reject")),
        ("POST", station(station_eigene, "sign")),
        ("POST", station(station_eigene, "reject")),
        ("GET", ""),  # Körperschaftsfilter und Suchformular senden an die eigene Adresse
        ("GET", adresse("paper_detail", paper_id=radweg.pk)),
        ("GET", adresse("meeting_detail", meeting_id=sondersitzung.pk)),
        ("GET", adresse("meeting_detail", meeting_id=sitzung.pk)),
        ("GET", adresse("meeting_protocol", meeting_id=ausschuss.pk)),
        ("GET", adresse("application_detail", application_id=antrag.pk)),
        *{("GET", adresse("file_download", file_id=anlage.pk)) for anlage in anlagen},
    }
    # Nie angeboten: Freigabe der selbst erstellten Vorlage (Vier-Augen-Prinzip), die wartende Station
    welt["nie_3"] = {
        ("POST", workflow(selbst, "approve")),
        ("POST", station(stationen["Rechtsamt"], "sign")),
    }
    return welt


def _konten(
    tenant: SessionTenant, leitung: SessionUser
) -> tuple[Callable[[SessionTenant, str], Client], dict[str, SessionUser]]:
    """
    ``client_fuer`` für ``vergleichen``: Konto mit der Rolle, zugeordnet zu Bauamt und Rechtsamt, vertritt die Leitung
    (Umfang „Freigaben“ und „Arbeitsvorrat“). ``konten`` merkt sich das Konto je Rolle für ``vorher``.
    """
    konten: dict[str, SessionUser] = {}
    aemter = list(SessionOrganization.objects.filter(tenant=tenant, name__in=("Bauamt", "Rechtsamt")))
    heute = timezone.localdate()

    def client_fuer(raum: SessionTenant, rolle: str) -> Client:
        user = cast(Any, UserFactory)()
        konto = SessionUser.objects.create(user=user, tenant=raum)
        konto.roles.add(SessionRole.objects.get(tenant=raum, name=rolle))
        konto.departments.add(*aemter)
        SessionDelegation.objects.create(
            tenant=raum,
            principal=leitung,
            deputy=konto,
            start_date=heute - timedelta(days=1),
            end_date=heute + timedelta(days=30),
        )
        konten[rolle] = konto
        client = Client()
        client.force_login(user)
        return client

    return client_fuer, konten


def _vorher(welt: dict[str, Any], konten: dict[str, SessionUser]) -> Callable[[str, str], None]:
    """Das Konto der Rolle hat „Selbst erstellt“ angelegt; auf den Seiten „…_leer“ ist keine Vorlage mehr in Prüfung."""

    def vorher(rolle: str, name: str) -> None:
        SessionPaper.objects.filter(pk=welt["selbst_obj"].pk).update(created_by=konten.get(rolle))
        status = "approved" if name.endswith("_leer") else "review"
        SessionPaper.objects.filter(pk__in=welt["pruefung_ids"]).update(status=status)

    return vorher


def _abruf(tenant: SessionTenant, welt: dict[str, Any], rolle: str, name: str, *, neu: bool = True) -> str:
    """Seite ``name`` für ein Konto mit ``rolle`` (Ämter und Vertretung wie im Gleichstand) abrufen."""
    client_fuer, konten = _konten(tenant, welt["leitung_obj"])
    client = client_fuer(tenant, rolle)
    _vorher(welt, konten)(rolle, name)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=neu)
    antwort = client.get(welt[name])
    assert antwort.status_code == 200, (rolle, name, antwort.status_code)
    return antwort.content.decode()


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("vorlagen3", neu=False)
    welt = _welt_3(tenant)
    client_fuer, _ = _konten(tenant, welt["leitung_obj"])
    client = client_fuer(tenant, "Administrator")
    for name, vorlage in SEITEN.items():
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt[name])
        assert alt.status_code == 200, name
        assert vorlage not in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt[name])
        assert neu.status_code == 200, name
        assert {vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_fuer_alle_rollen() -> None:
    """
    Jede Aktion der bisherigen Seiten steht mit denselben Merkmalen auf der neuen, für alle sechs Standardrollen und
    jeden Zustand des Bestands; die neue Seite bietet nichts an, was die bisherige nicht hatte. Einzige Ausnahme wie in
    Teil 1, 2a und 2b: die vier Seiten, die die bisherige Seitenleiste direkt verlinkte und die jetzt als Reiter über
    ihrer Liste stehen – eine Änderung des Rahmens, nicht der Seiten.
    """
    tenant = _mandant("gleich3", neu=False)
    welt = _welt_3(tenant)
    client_fuer, konten = _konten(tenant, welt["leitung_obj"])
    fehlt, gesehen = vergleichen(
        tenant,
        {name: welt[name] for name in VERGLEICH},
        ausnahmen=reiter_ausnahmen(tenant),
        client_fuer=client_fuer,
        vorher=_vorher(welt, konten),
    )
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet_3"] <= gezeigt(gesehen), sorted(welt["erwartet_3"] - gezeigt(gesehen))
    assert not welt["nie_3"] & gezeigt(gesehen), sorted(welt["nie_3"] & gezeigt(gesehen))
    # Die Optionen der Suchfilter (Trefferart, Gremium, Wahlperiode) gehören zum Formular
    suche = [felder for (methode, adresse, felder) in gesehen if (methode, adresse) == ("GET", "") and "q" in felder]
    assert any(any(f.startswith("kind∈") and "|papers|" in f for f in felder) for felder in suche), suche
    assert any(any(f.startswith("term∈") for f in felder) for felder in suche), suche


def test_mitzeichnungen_ohne_amt_gleich_und_als_satz() -> None:
    """Ohne Amt und ohne Vertretung bleibt die Liste leer, mit dem Satz dazu – wie bisher, ohne Hinweiskasten."""
    tenant = _mandant("ohneamt3", neu=False)
    welt = _welt_3(tenant)
    seiten = {"mitzeichnungen_ohne_amt": welt["mitzeichnungen"]}
    fehlt, _ = vergleichen(
        tenant, seiten, rollen_liste=["Lesezugriff", "Sachbearbeiter"], ausnahmen=reiter_ausnahmen(tenant)
    )
    assert not fehlt, "\n".join(fehlt)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    html = _client(tenant, "Lesezugriff").get(welt["mitzeichnungen"]).content.decode()
    assert "Keine offenen Mitzeichnungen. Sie sind bisher keinem Amt/Fachbereich zugeordnet." in html
    assert 'data-testid="meine-vertretungen"' not in html


# =============================================================================
# Darstellung
# =============================================================================


def test_zu_pruefen_vertretung_und_vier_augen_als_satz() -> None:
    tenant = _mandant("pruefen3", neu=True)
    welt = _welt_3(tenant)
    html = _abruf(tenant, welt, "Lesezugriff", "zu_pruefen")
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    # Lesezugriff sieht nur die öffentlichen Vorlagen, das Prüfrecht stammt aus der Vertretung der Leitung
    assert "3 Vorlagen in Prüfung, die ältesten zuerst." in html
    assert "Grundstücksankauf" not in inhalt
    assert 'data-testid="meine-vertretungen"' in inhalt and "Sie vertreten derzeit leitung@example.org" in inhalt
    assert "Freigabe in Vertretung für leitung@example.org" in inhalt
    assert "Vier-Augen-Prinzip: Die vertretene Person (leitung@example.org)" in inhalt
    assert "Vier-Augen-Prinzip: Sie haben" in inhalt
    assert "Eingereicht von leitung@example.org" in inhalt
    # Sätze statt Hinweiskästen, keine Karte
    assert 'data-testid="vertretung-hinweis"' not in inhalt and "rounded-xl" not in inhalt
    assert "Nichtöffentlich" in _abruf(tenant, welt, "Administrator", "zu_pruefen")

    leer = _abruf(tenant, welt, "Administrator", "zu_pruefen_leer")
    assert "Keine Vorlagen zur Prüfung. Der Arbeitsvorrat ist leer." in leer


def test_mitzeichnungen_stand_als_punkt_mit_text() -> None:
    tenant = _mandant("mitzeichnen3", neu=True)
    welt = _welt_3(tenant)
    html = _abruf(tenant, welt, "Sachbearbeiter", "mitzeichnungen")
    assert "4 offene Mitzeichnungsstationen Ihrer Ämter: " in html and "Bauamt" in html and "Rechtsamt" in html
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    assert "Wartet auf die vorherige Station" in inhalt and "Mit finanziellen Auswirkungen" in inhalt
    assert "In Vertretung für leitung@example.org" in inhalt
    # Zurückweisen klappt das Feld für den Grund auf (Pflicht wie bisher)
    assert 'x-data="{ rejectOpen: false }"' in inhalt and 'name="comment" required' in inhalt
    # Körperschaft gewählt: nur die nichtöffentliche Vorlage der zweiten Körperschaft
    gefiltert = _abruf(tenant, welt, "Sachbearbeiter", "mitzeichnungen_koerperschaft")
    assert "Grundstücksankauf" in gefiltert and "Neue Brücke" not in gefiltert and "Gemeinde Nord" in gefiltert
    assert "Keine offenen Mitzeichnungen." in _abruf(tenant, welt, "Sachbearbeiter", "mitzeichnungen_leer")


def test_suche_listen_statt_karten() -> None:
    tenant = _mandant("suche3", neu=True)
    welt = _welt_3(tenant)
    html = _abruf(tenant, welt, "Sachbearbeiter", "suche")
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    assert "Je Art höchstens 25 Treffer." in inhalt
    for titel in ("Vorlagen", "Tagesordnungspunkte und Beschlüsse", "Sitzungen", "Protokolle", "Anlagen", "Anträge"):
        assert f">{titel} <span" in inhalt, titel
    assert "B/2026/7 · TOP 5: Radweg Planung" in inhalt and "Niederschrift: Bauausschuss Mai" in inhalt
    assert "Antrag A/2026/0009" in inhalt and "radweg-vermerk.txt" in inhalt
    assert "<table" not in inhalt and "rounded-xl" not in inhalt and "text-amber-500" not in inhalt
    # Ohne Sichtrecht keine nichtöffentlichen Treffer
    lesen = _abruf(tenant, welt, "Lesezugriff", "suche")
    assert "Radweg Süd" not in lesen and "Radweg Grundstücke" not in lesen and "radweg-vermerk.txt" not in lesen

    gefiltert = _abruf(tenant, welt, "Sachbearbeiter", "suche_gefiltert")
    assert "Sondersitzung Radweg" in gefiltert and "Je Art höchstens" not in gefiltert
    assert "Radweg Nord" not in gefiltert.split('class="work-page-content"')[1]

    keine = _abruf(tenant, welt, "Sachbearbeiter", "suche_ohne_treffer")
    assert "Keine Treffer für „Gibtsnicht“. Verschlüsselte nichtöffentliche Inhalte sind nicht durchsuchbar." in keine
    ohne = _abruf(tenant, welt, "Sachbearbeiter", "suche_ohne_begriff")
    assert 'data-testid="suche-formular"' in ohne and "Treffer für" not in ohne


def test_hoechstens_die_hauptaktion_im_kopfband_ist_gefuellt() -> None:
    """Gefüllt ist höchstens „Suchen“ im Kopfband; im Inhalt kein gefüllter und kein farbiger Knopf."""
    tenant = _mandant("knoepfe3", neu=True)
    welt = _welt_3(tenant)
    for name in VERGLEICH:
        html = _abruf(tenant, welt, "Administrator", name)
        seite = html[html.index('data-gestaltung="neu"') : html.index("</main>")]
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        assert seite.count("bg-primary-600 hover:bg-primary-700") <= 1, f"{name}: mehr als ein gefüllter Knopf"
        assert "bg-primary-600 hover:bg-primary-700" not in inhalt, f"{name}: gefüllter Knopf im Inhalt"
        for farbe in ("green", "blue", "indigo", "amber", "red", "teal", "purple"):
            # Punkte des Stands (session/neu/_status.html) sind gefüllt, aber kein Knopf: nur Knöpfe mit Zeigefarbe
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in seite, f"{name}: Knopf in {farbe}"


def test_listen_nicht_in_12_px() -> None:
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        for baustein in ("c-ui.kpi-tile", "c-ui.badge", "c-ui.card", "c-ui.alert", "rounded-full bg-"):
            assert baustein not in text, f"{datei}: {baustein}"


def test_aktionen_wirken_wie_bisher() -> None:
    """Die neuen Seiten senden an dieselben Ziele: Freigeben, Zurückweisen mit Anmerkung und Mitzeichnen wirken."""
    tenant = _mandant("wirkung3", neu=True)
    welt = _welt_3(tenant)
    client_fuer, konten = _konten(tenant, welt["leitung_obj"])
    client = client_fuer(tenant, "Administrator")
    _vorher(welt, konten)("Administrator", "zu_pruefen")
    bruecke = welt["vorlage_obj"]
    station = SessionCosignature.objects.get(paper=bruecke, department__name="Bauamt")
    antwort = client.post(
        reverse(
            "session:cosignature_action", kwargs={"tenant_slug": tenant.slug, "cosign_id": station.pk, "action": "sign"}
        ),
        {"next": welt["mitzeichnungen"]},
    )
    assert antwort.status_code == 302 and antwort["Location"] == welt["mitzeichnungen"]
    station.refresh_from_db()
    assert station.status == "signed"
    selbst = welt["selbst_obj"]
    ziel = reverse(
        "session:paper_workflow", kwargs={"tenant_slug": tenant.slug, "paper_id": selbst.pk, "action": "reject"}
    )
    antwort = client.post(ziel, {"next": welt["zu_pruefen"], "comment": "Bitte Kosten ergänzen"})
    assert antwort.status_code == 302 and antwort["Location"] == welt["zu_pruefen"]
    assert SessionPaper.objects.get(pk=selbst.pk).status == "draft"
    assert (
        "Selbst erstellt" not in client.get(welt["zu_pruefen"]).content.decode().split('class="work-page-content"')[1]
    )


def test_vertretung_ohne_umfang_arbeitsvorrat() -> None:
    """Gleichstand auch, wenn die Vertretung nur Freigaben umfasst: keine Stationen der Leitung im Arbeitsvorrat."""
    tenant = _mandant("umfang3", neu=False)
    welt = _welt_3(tenant)
    client_fuer, konten = _konten(tenant, welt["leitung_obj"])

    def nur_freigaben(raum: SessionTenant, rolle: str) -> Client:
        client = client_fuer(raum, rolle)
        SessionDelegation.objects.filter(deputy=konten[rolle]).update(scope_worklist=False)
        return client

    fehlt, gesehen = vergleichen(
        tenant,
        {"mitzeichnungen": welt["mitzeichnungen"], "zu_pruefen": welt["zu_pruefen"]},
        rollen_liste=["Lesezugriff", "Revision"],
        ausnahmen=reiter_ausnahmen(tenant),
        client_fuer=nur_freigaben,
        vorher=_vorher(welt, konten),
    )
    assert not fehlt, "\n".join(fehlt)
    eigene = SessionPaper.objects.get(tenant=tenant, name="Eigene Vorlage")
    station = SessionCosignature.objects.get(paper=eigene)
    signieren = reverse(
        "session:cosignature_action", kwargs={"tenant_slug": tenant.slug, "cosign_id": station.pk, "action": "sign"}
    )
    assert ("POST", signieren) not in gezeigt(gesehen)
    # Die Freigaberechte aus der Vertretung bleiben: Revision sieht die Seite „Zu prüfen“ (nur Öffentliches)
    assert (
        "GET",
        reverse("session:paper_detail", kwargs={"tenant_slug": tenant.slug, "paper_id": eigene.pk}),
    ) in gezeigt(gesehen)
