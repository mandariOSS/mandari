# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Seiten von Work im neuen Erscheinungsbild (Issues #852, #853).

Hinter dem Schalter ``Organization.work_new_design`` zeigen Übersicht, Sitzungen der Gremien (Liste und Sitzung),
Vorgänge (Liste und Vorgang), Gremien und Personen die Bausteine von Insight (Stand-Satz, Zeitstrahl, Dokumentzeile)
und „Für die Fraktion“: Positionen mit Begründung und Ergebnis, Notizen, Dokumente, eigene Gremien, Mitglieder der
Organisation. Ohne Schalter bleiben die bisherigen Seiten. Keine RIS-Funktion entfällt (Wege der bisherigen Seiten
stehen weiter auf der Seite); Positionen, Notizen und Mitglieder nur mit den Rechten, mit denen sie auch sonst
sichtbar sind, und nie aus einer anderen Organisation.

Die Seiten nutzen dieselben Bausteine wie Insight statt eigener Nachbauten: Listen und Seitenwahl (c-liste.*, Ziele in
Work), Kopfband und Band (c-rahmen.*, dicht), Zusammenfassung, Niederschrift, Übertragung, Sitzungsdateien und die
Fraktion an Personen (Issue #853).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests import factories
from apps.tenants.models import Membership, Organization
from apps.work.meetings.models import AgendaItemNote, AgendaItemPosition
from apps.work.motions.models import Motion
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    PersonFraktion,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.neu.example/oparl"
LESER_RECHTE = ["ris.view"]


def _kennung(art: str) -> str:
    return f"{RIS}/{art}/{uuid.uuid4()}"


@dataclass
class Welt:
    org: Organization
    vorsitz: Membership
    leser: Membership
    body: OParlBody
    ausschuss: OParlOrganization
    rat: OParlOrganization
    sitzung: OParlMeeting
    ratssitzung: OParlMeeting
    top_a: OParlAgendaItem
    top_b: OParlAgendaItem
    top_rat: OParlAgendaItem
    vorlage: OParlPaper
    zweite: OParlPaper
    person: OParlPerson


def _beratung(sitzung: OParlMeeting, top: OParlAgendaItem, vorlage: OParlPaper, **felder: Any) -> None:
    OParlConsultation.objects.create(
        external_id=_kennung("consultations"),
        body=vorlage.body,
        paper=vorlage,
        paper_external_id=vorlage.external_id,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=top.external_id,
        **felder,
    )


def _position(org: Organization, top: OParlAgendaItem, position: str, **felder: Any) -> AgendaItemPosition:
    begruendung = felder.pop("begruendung", "")
    eintrag = AgendaItemPosition(organization=org, agenda_item=top, position=position, **felder)
    if begruendung:
        cast(Any, eintrag).set_reasoning_encrypted(begruendung)
    eintrag.save()
    return eintrag


@pytest.fixture
def welt(org: Organization, make_member: Any) -> Welt:
    source = OParlSource.objects.create(name="Neu-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Musterstadt")
    org.body = body
    org.work_new_design = True
    org.save(update_fields=["body", "work_new_design"])
    ausschuss = OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name="Hauptausschuss")
    rat = OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name="Rat")
    jetzt = timezone.now()
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"),
        body=body,
        name="Sitzung",
        start=jetzt - timedelta(days=10),
        location_name="Rathaus, Saal 2",
    )
    sitzung.organizations.add(ausschuss)
    ratssitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, name="Sitzung", start=jetzt + timedelta(days=5)
    )
    ratssitzung.organizations.add(rat)
    top_a = OParlAgendaItem.objects.create(
        external_id=_kennung("items"), meeting=sitzung, number="10", order=2, name="Feuerwehr", result="vertagt"
    )
    top_b = OParlAgendaItem.objects.create(
        external_id=_kennung("items"), meeting=sitzung, number="2", order=1, name="Radwege"
    )
    top_rat = OParlAgendaItem.objects.create(
        external_id=_kennung("items"), meeting=ratssitzung, number="4", order=1, name="Feuerwehr im Rat"
    )
    vorlage = OParlPaper.objects.create(
        external_id=_kennung("papers"),
        body=body,
        name="Feuerwehrbedarfsplan",
        reference="V/1",
        paper_type="Beschlussvorlage",
        date=date.today() - timedelta(days=20),
        locations=[{"lat": 51.9, "lon": 7.6, "name": "Feuerwache Nord"}],
    )
    zweite = OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name="Radwegekonzept", reference="V/2", date=date.today()
    )
    _beratung(sitzung, top_a, vorlage, role="Vorberatung")
    _beratung(ratssitzung, top_rat, vorlage, role="Entscheidung", authoritative=True)
    _beratung(sitzung, top_b, zweite)
    _beratung(sitzung, top_a, zweite)  # zwei Vorlagen unter einem Punkt
    OParlFile.objects.create(
        external_id=_kennung("files"),
        body=body,
        paper=vorlage,
        name="Anlage Bedarfsplan",
        access_url="https://ris.neu.example/datei.pdf",
        mime_type="application/pdf",
    )
    person = OParlPerson.objects.create(external_id=_kennung("persons"), body=body, name="Erika Beispiel")
    OParlMembership.objects.create(
        external_id=_kennung("memberships"), person=person, organization=ausschuss, role="Vorsitz"
    )
    vorsitz = make_member(org, [], email="vorsitz-neu@example.org", is_admin=True)
    vorsitz.followed_organizations.add(ausschuss)
    vorsitz.oparl_person = person
    vorsitz.save(update_fields=["oparl_person"])
    leser = make_member(org, LESER_RECHTE, email="leser-neu@example.org")
    return Welt(
        org, vorsitz, leser, body, ausschuss, rat, sitzung, ratssitzung, top_a, top_b, top_rat, vorlage, zweite, person
    )


def _seite(client_for: Any, mitglied: Membership, name: str, query: dict[str, str] | None = None, **kwargs: Any) -> Any:
    url = reverse(name, kwargs={"org_slug": mitglied.organization.slug, **kwargs})
    antwort = client_for(mitglied.user).get(url, query or {})
    assert antwort.status_code == 200
    return antwort


SEITEN = [
    ("work:ris_overview", {}, "work/ris/neu/uebersicht.html", "work/ris/overview.html"),
    ("work:ris_meetings", {}, "work/ris/neu/sitzungen.html", "work/ris/meetings.html"),
    ("work:ris_meeting_detail", {"meeting_id": "sitzung"}, "work/ris/neu/sitzung.html", "work/ris/meeting_detail.html"),
    ("work:ris_papers", {}, "work/ris/neu/vorgaenge.html", "work/ris/papers.html"),
    ("work:ris_paper_detail", {"paper_id": "vorlage"}, "work/ris/neu/vorgang.html", "work/ris/paper_detail.html"),
    ("work:ris_organizations", {}, "work/ris/neu/gremien.html", "work/ris/organizations.html"),
    (
        "work:ris_organization_detail",
        {"org_id": "ausschuss"},
        "work/ris/neu/gremium.html",
        "work/ris/organization_detail.html",
    ),
    ("work:ris_persons", {}, "work/ris/neu/personen.html", "work/ris/persons.html"),
    ("work:ris_person_detail", {"person_id": "person"}, "work/ris/neu/person.html", "work/ris/person_detail.html"),
]


@pytest.mark.parametrize("neu", [True, False], ids=["neu", "bisher"])
@pytest.mark.parametrize(("name", "kwargs", "neue_vorlage", "alte_vorlage"), SEITEN, ids=[s[0] for s in SEITEN])
def test_vorlage_folgt_dem_schalter(
    welt: Welt, client_for: Any, name: str, kwargs: dict[str, str], neue_vorlage: str, alte_vorlage: str, neu: bool
) -> None:
    welt.org.work_new_design = neu
    welt.org.save(update_fields=["work_new_design"])
    werte: dict[str, Any] = {schluessel: getattr(welt, wert).pk for schluessel, wert in kwargs.items()}

    for mitglied in (welt.vorsitz, welt.leser):
        vorlagen = [t.name for t in _seite(client_for, mitglied, name, **werte).templates]
        assert (neue_vorlage if neu else alte_vorlage) in vorlagen
        assert (alte_vorlage if neu else neue_vorlage) not in vorlagen


def test_vorgang_mit_stand_verlauf_dokumentzeile_und_fuer_die_fraktion(welt: Welt, client_for: Any) -> None:
    _position(welt.org, welt.top_a, "amended", is_final=True, outcome="deferred", begruendung="Erst Kosten klären")
    _position(welt.org, welt.top_rat, "for")
    AgendaItemNote.objects.create(organization=welt.org, agenda_item=welt.top_a, author=welt.vorsitz)
    Motion.objects.create(
        organization=welt.org,
        author=welt.vorsitz,
        title="Änderungsantrag Feuerwache",
        status="draft",
        parent_paper=welt.vorlage,
    )

    html = _seite(client_for, welt.vorsitz, "work:ris_paper_detail", paper_id=welt.vorlage.pk).content.decode()

    # Stand-Satz und Zeitstrahl wie in Insight, Verlauf verlinkt die Sitzungen in Work
    assert "Stand:</span> Am " in html and "vertagt" in html and "Nächste Beratung am" in html
    assert 'data-testid="vorgang-verlauf"' in html
    assert (
        reverse("work:ris_meeting_detail", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.ratssitzung.pk})
        in html
    )
    # Dokumentzeile von Insight: Ansehen und Herunterladen über den Dateiabruf von mandari
    assert 'x-data="documentText"' in html and "Anlage Bedarfsplan" in html and "?download=1" in html
    # Für die Fraktion: Positionen je Beratung mit Ergebnis und Begründung, Notiz, Dokument, Vorbereitung
    assert "Mit Änderungsantrag" in html and "endgültig" in html and "Ergebnis: Vertagt" in html
    assert "Erst Kosten klären" in html and "Zustimmung" in html
    assert "1 Notiz zu den Tagesordnungspunkten" in html
    assert "Änderungsantrag Feuerwache" in html and "Änderungsantrag zu dieser Vorlage" in html
    assert (
        reverse("work:meeting_prepare", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.ratssitzung.pk}) in html
    )
    # Bisherige Wege bleiben: Karte für die Orte, neues Dokument, Angaben aus dem RIS
    assert "Feuerwache Nord" in html and reverse("work:ris_map", kwargs={"org_slug": welt.org.slug}) in html
    assert reverse("work:document_create", kwargs={"org_slug": welt.org.slug}) in html
    assert "Vorlagen-Nr." in html and "Beschlussvorlage" in html


def test_vorgang_ohne_vorbereitungsrecht_ohne_positionen_und_notizen(welt: Welt, client_for: Any) -> None:
    _position(welt.org, welt.top_a, "against", begruendung="Vertraulich in der Fraktion")

    html = _seite(client_for, welt.leser, "work:ris_paper_detail", paper_id=welt.vorlage.pk).content.decode()

    assert "Stand:</span>" in html and "Für die Fraktion" in html
    assert "Vertraulich in der Fraktion" not in html and "Ablehnung" not in html
    assert (
        reverse("work:meeting_prepare", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.ratssitzung.pk})
        not in html
    )


def test_positionen_anderer_organisationen_bleiben_fremd(welt: Welt, client_for: Any) -> None:
    andere = cast(Organization, cast(Any, factories.OrganizationFactory)(name="Andere Fraktion"))
    _position(andere, welt.top_a, "against", begruendung="Fremde Begründung")

    seiten: list[tuple[str, dict[str, Any]]] = [
        ("work:ris_paper_detail", {"paper_id": welt.vorlage.pk}),
        ("work:ris_meeting_detail", {"meeting_id": welt.sitzung.pk}),
        ("work:ris_papers", {}),
    ]
    for name, kwargs in seiten:
        html = _seite(client_for, welt.vorsitz, name, **kwargs).content.decode()
        assert "Fremde Begründung" not in html and "Ablehnung" not in html


def test_sitzung_mit_allen_vorlagen_je_punkt_und_position(welt: Welt, client_for: Any) -> None:
    _position(welt.org, welt.top_a, "for", is_final=True)
    AgendaItemNote.objects.create(organization=welt.org, agenda_item=welt.top_a, author=welt.vorsitz)
    AgendaItemNote.objects.create(organization=welt.org, agenda_item=welt.top_a, author=welt.vorsitz)

    html = _seite(client_for, welt.vorsitz, "work:ris_meeting_detail", meeting_id=welt.sitzung.pk).content.decode()

    # Natürliche Reihenfolge (2 vor 10), beide Vorlagen unter TOP 10
    assert html.index("Radwege") < html.index("Feuerwehr")
    assert (
        html.count(reverse("work:ris_paper_detail", kwargs={"org_slug": welt.org.slug, "paper_id": welt.zweite.pk}))
        >= 2
    )
    assert "Ergebnis: vertagt" in html
    assert "Für die Fraktion: " in html and "Zustimmung" in html and "2 Notizen" in html
    assert "Positionen zu 1 von 2 Vorlagen" in html
    # Bisherige Wege: Vorbereitung, Kalender, Gremien, Stand der Quelle
    assert reverse("work:meeting_prepare", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.sitzung.pk}) in html
    assert reverse("work:meetings_calendar", kwargs={"org_slug": welt.org.slug}) in html
    assert (
        reverse("work:ris_organization_detail", kwargs={"org_slug": welt.org.slug, "org_id": welt.ausschuss.pk}) in html
    )
    assert "Rathaus, Saal 2" in html


def test_listen_mit_stand_und_fuer_die_fraktion(welt: Welt, client_for: Any) -> None:
    _position(welt.org, welt.top_rat, "abstain")
    Motion.objects.create(
        organization=welt.org,
        author=welt.vorsitz,
        title="Antrag Radwege",
        status="submitted",
        related_paper=welt.zweite,
    )

    vorgaenge = _seite(client_for, welt.vorsitz, "work:ris_papers").content.decode()
    assert "Enthaltung" in vorgaenge and "Antrag Radwege" in vorgaenge
    assert "Nächste Beratung am" in vorgaenge  # Stand-Satz je Vorgang

    sitzungen = _seite(client_for, welt.vorsitz, "work:ris_meetings", {"view": "past"}).content.decode()
    assert "Positionen zu 0 von 2 Vorlagen" in sitzungen
    assert (
        reverse("work:meeting_prepare", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.sitzung.pk}) in sitzungen
    )
    assert "view=upcoming" in sitzungen and "view=all" in sitzungen

    gremien = _seite(client_for, welt.vorsitz, "work:ris_organizations", {"tab": "all"}).content.decode()
    assert "Ihr Gremium" in gremien

    personen = _seite(client_for, welt.vorsitz, "work:ris_persons").content.decode()
    assert "Ihre Organisation" in personen
    # Ohne Recht auf die Mitgliederliste kein Hinweis auf verknüpfte Konten
    assert "Ihre Organisation" not in _seite(client_for, welt.leser, "work:ris_persons").content.decode()

    uebersicht = _seite(client_for, welt.vorsitz, "work:ris_overview").content.decode()
    assert "2 Vorgänge" in uebersicht and "Hauptausschuss" in uebersicht and "Antrag Radwege" in uebersicht


def test_gremium_und_person_zeigen_die_eigene_organisation(welt: Welt, client_for: Any) -> None:
    gremium = _seite(
        client_for, welt.vorsitz, "work:ris_organization_detail", org_id=welt.ausschuss.pk
    ).content.decode()
    assert "Eines Ihrer Gremien." in gremium and "Aus Ihrer Organisation:" in gremium
    assert "Erika Beispiel" in gremium and "Vorsitz" in gremium

    person = _seite(client_for, welt.vorsitz, "work:ris_person_detail", person_id=welt.person.pk).content.decode()
    assert "Mitglied Ihrer Organisation" in person and "Hauptausschuss" in person

    leser_gremium = _seite(client_for, welt.leser, "work:ris_organization_detail", org_id=welt.ausschuss.pk)
    assert "Aus Ihrer Organisation:" not in leser_gremium.content.decode()


def test_vorgangsliste_ohne_abfrage_je_vorgang(welt: Welt, client_for: Any, django_assert_max_num_queries: Any) -> None:
    for nummer in range(20):
        vorgang = OParlPaper.objects.create(
            external_id=_kennung("papers"), body=welt.body, name=f"Vorgang {nummer}", date=date.today()
        )
        _beratung(welt.sitzung, welt.top_b, vorgang)
    client = client_for(welt.vorsitz.user)
    url = reverse("work:ris_papers", kwargs={"org_slug": welt.org.slug})
    client.get(url)  # Sitzung, Rahmen und Caches anwärmen

    with django_assert_max_num_queries(45):
        assert client.get(url).status_code == 200


# ---------------------------------------------------------------------------
# Gemeinsame Bausteine mit Insight (Issue #853)
# ---------------------------------------------------------------------------

#: Klassen der früheren Nachbauten für die RIS-Seiten (static/css/input.css), ersetzt durch die gemeinsamen Bausteine
EIGENE_KLASSEN = re.compile(
    r'class="[^"]*(\bwork-page-header\b|\bris-(h2|meta|link|titel|zeile|fraktion|feld|angaben)\b)'
)


@pytest.mark.parametrize(("name", "kwargs", "neue_vorlage", "alte_vorlage"), SEITEN, ids=[s[0] for s in SEITEN])
def test_kopfband_und_brotkrumen_aus_den_gemeinsamen_bausteinen(
    welt: Welt, client_for: Any, name: str, kwargs: dict[str, str], neue_vorlage: str, alte_vorlage: str
) -> None:
    werte: dict[str, Any] = {schluessel: getattr(welt, wert).pk for schluessel, wert in kwargs.items()}

    html = _seite(client_for, welt.vorsitz, name, **werte).content.decode()

    # Kopfband von Insight in der dichten Fassung (Titel 24 px), keine eigenen Klassen mehr
    assert '<h1 class="m-0 text-2xl font-bold' in html
    assert not EIGENE_KLASSEN.search(html)
    # Die Brotkrumen enden auf jeder Seite mit der aktuellen Seite
    krumen = html[html.index('aria-label="Brotkrumen"') :]
    krumen = krumen[: krumen.index("</nav>")]
    assert krumen.count('aria-current="page"') == 1


def test_listen_sind_die_listen_von_insight_mit_zielen_in_work(welt: Welt, client_for: Any) -> None:
    slug = welt.org.slug
    PersonFraktion.objects.create(
        person=welt.person, body=welt.body, bezeichnung="Fraktion Mitte", quelle=PersonFraktion.QUELLE_EINBLENDUNG
    )
    faelle = [
        ("work:ris_papers", {}, "vorgangsliste", "work:ris_paper_detail", {"paper_id": welt.vorlage.pk}),
        (
            "work:ris_meetings",
            {"view": "all"},
            "sitzungsliste",
            "work:ris_meeting_detail",
            {"meeting_id": welt.sitzung.pk},
        ),
        (
            "work:ris_organizations",
            {"tab": "all"},
            "gremienliste",
            "work:ris_organization_detail",
            {"org_id": welt.ausschuss.pk},
        ),
        ("work:ris_persons", {}, "personenliste", "work:ris_person_detail", {"person_id": welt.person.pk}),
    ]
    for name, query, testid, ziel, ziel_kwargs in faelle:
        html = _seite(client_for, welt.vorsitz, name, query).content.decode()
        liste = html[html.index(f'data-testid="{testid}"') :]
        liste = liste[: liste.index("</table>")]
        # Zeile wie in Insight (ganze Zeile klickbar), das Ziel liegt in Work, ohne Merkliste von Insight
        assert f'data-href="{reverse(ziel, kwargs={"org_slug": slug, **ziel_kwargs})}"' in liste, name
        assert "/insight/" not in liste and "bookmarks" not in liste, name
        assert "Für die Fraktion</th>" in liste, name

    # Fraktion an Personen wie in Insight: auch aus der bestätigten Zuordnung, mit Quelle
    personen = _seite(client_for, welt.vorsitz, "work:ris_persons").content.decode()
    assert "Fraktion Mitte" in personen and "laut Einblendung der Live-Übertragung" in personen
    person = _seite(client_for, welt.vorsitz, "work:ris_person_detail", person_id=welt.person.pk).content.decode()
    assert "Fraktion Mitte" in person and "Vorsitz, Hauptausschuss" in person
    gremium = _seite(client_for, welt.vorsitz, "work:ris_organization_detail", org_id=welt.ausschuss.pk)
    assert "Fraktion Mitte" in gremium.content.decode()


def test_seitenwahl_von_insight_behaelt_die_filter(welt: Welt, client_for: Any) -> None:
    for nummer in range(30):
        OParlPaper.objects.create(
            external_id=_kennung("papers"), body=welt.body, name=f"Antrag {nummer}", paper_type="Antrag"
        )

    html = _seite(client_for, welt.vorsitz, "work:ris_papers", {"type": "Antrag"}).content.decode()

    assert 'aria-label="Seiten"' in html and 'aria-label="Nächste Seite"' in html
    assert "?type=Antrag&amp;page=2" in html and "min-w-8 h-8" in html


def test_sitzung_mit_niederschrift_uebertragung_und_sitzungsdateien_wie_insight(welt: Welt, client_for: Any) -> None:
    niederschrift = OParlFile.objects.create(
        external_id=_kennung("files"),
        body=welt.body,
        meeting=welt.sitzung,
        name="Niederschrift öffentlicher Teil",
        access_url="https://ris.neu.example/niederschrift.pdf",
        mime_type="application/pdf",
    )
    einladung = OParlFile.objects.create(
        external_id=_kennung("files"),
        body=welt.body,
        meeting=welt.sitzung,
        name="Einladung",
        access_url="https://ris.neu.example/einladung.pdf",
        mime_type="application/pdf",
        text_content="Einladung zur Sitzung",
    )
    OParlFile.objects.create(
        external_id=_kennung("files"), body=welt.body, meeting=welt.sitzung, name="Gelöschte Anlage", deleted=True
    )
    welt.sitzung.raw_json = {
        "resultsProtocol": niederschrift.external_id,
        "mandari:meetingFormat": "hybrid",
        "mandari:publicAccess": {"url": "https://stream.example/sitzung", "hint": "Ohne Anmeldung."},
    }
    welt.sitzung.save(update_fields=["raw_json"])

    html = _seite(client_for, welt.vorsitz, "work:ris_meeting_detail", meeting_id=welt.sitzung.pk).content.decode()

    # Niederschrift und Teilnahme der Öffentlichkeit aus denselben Bausteinen wie Insight
    assert 'data-testid="oeffentliche-niederschrift"' in html
    assert reverse("insight_core:insight:file_proxy", args=[niederschrift.pk]) in html
    assert (
        'data-testid="sitzungsformat"' in html
        and "Hybride Sitzung" in html
        and "https://stream.example/sitzung" in html
    )
    # Übrige Sitzungsdateien als Dokumentzeilen; die Niederschrift steht nicht doppelt, Gelöschtes fehlt
    dateien = html[html.index('data-testid="sitzungsdateien"') :]
    dateien = dateien[: dateien.index("</ul>")]
    assert "Einladung" in dateien and reverse("insight_core:insight:file_proxy", args=[einladung.pk]) in dateien
    assert 'x-data="documentText"' in dateien and "Niederschrift öffentlicher Teil" not in dateien
    assert "Gelöschte Anlage" not in html


def test_vorgang_mit_zusammenfassung_aus_dem_baustein_von_insight(welt: Welt, client_for: Any) -> None:
    welt.vorlage.summary = "Kurz gesagt: eine neue Feuerwache im Norden."
    welt.vorlage.save(update_fields=["summary"])

    html = _seite(client_for, welt.vorsitz, "work:ris_paper_detail", paper_id=welt.vorlage.pk).content.decode()

    zusammenfassung = html[html.index('data-testid="vorgang-zusammenfassung"') :]
    assert "Kurz gesagt: eine neue Feuerwache" in zusammenfassung
    assert "Die Zusammenfassung kann Fehler enthalten." in zusammenfassung
    assert '<h2 class="text-base font-semibold' in zusammenfassung
