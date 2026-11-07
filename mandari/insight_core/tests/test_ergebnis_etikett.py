# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergebnis-Etiketten im Bürgerportal (Issue #1260).

Die Ratsinformationssysteme schreiben Ergebnisse frei; das Bürgerportal ordnet sie einem festen Vokabular zu
(``insight_core.services.ergebnis``) und zeigt sie als kleines Etikett mit Erklärung (``c-insight.ergebnis``) in der
Tagesordnung, in der Vorgangsliste, auf der Startseite und in der Suche. Was sich nicht sicher zuordnen lässt, bleibt
neutral. Die Tagesordnung der Sitzung ist flach: Nummer, Titel, Etikett, Links.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.template import engines
from django.test import Client
from django.utils import timezone
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_core.services import ergebnis as vokabular
from insight_core.services.ergebnis import einordnen, ergebnis
from insight_core.services.paper_status import DECIDED, NO_RESULT, NONE, SCHEDULED, PaperStatus, paper_status

BERLIN = ZoneInfo("Europe/Berlin")


def render(source: str, **context: object) -> str:
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


# =============================================================================
# Zuordnung der Rohtexte
# =============================================================================


@pytest.mark.parametrize(
    ("roh", "schluessel"),
    [
        # beschlossen
        ("beschlossen", vokabular.BESCHLOSSEN),
        ("Beschlossen", vokabular.BESCHLOSSEN),
        ("einstimmig beschlossen", vokabular.BESCHLOSSEN),
        ("mehrheitlich beschlossen", vokabular.BESCHLOSSEN),
        ("ungeändert beschlossen", vokabular.BESCHLOSSEN),
        ("Beschlossen mit Änderungen", vokabular.BESCHLOSSEN),
        ("Beschluss gemäß Vorlage", vokabular.BESCHLOSSEN),
        ("Mit sechzehn Zustimmungen und einer Enthaltung wird folgender Beschluss gefasst:", vokabular.BESCHLOSSEN),
        ("Angenommen", vokabular.BESCHLOSSEN),
        ("Zustimmung mit Änderungen", vokabular.BESCHLOSSEN),
        ("einstimmig empfohlen", vokabular.BESCHLOSSEN),
        ("Mit Änderungen empfohlen", vokabular.BESCHLOSSEN),
        ("mehrheitlich als erledigt beschlossen", vokabular.BESCHLOSSEN),
        ("beschlossen; Kenntnis der Stellungnahme", vokabular.BESCHLOSSEN),
        ("einstimmig", vokabular.BESCHLOSSEN),
        ("einstimmig bei einer Enthaltung", vokabular.BESCHLOSSEN),
        ("gewählt", vokabular.BESCHLOSSEN),
        # abgelehnt
        ("abgelehnt", vokabular.ABGELEHNT),
        ("Mehrheitlich abgelehnt", vokabular.ABGELEHNT),
        ("Ablehnung", vokabular.ABGELEHNT),
        ("nicht beschlossen", vokabular.ABGELEHNT),
        ("nicht angenommen", vokabular.ABGELEHNT),
        ("nicht empfohlen", vokabular.ABGELEHNT),
        ("keine Mehrheit", vokabular.ABGELEHNT),
        # zur Kenntnis genommen
        ("zur Kenntnis genommen", vokabular.KENNTNIS),
        ("Kenntnis genommen", vokabular.KENNTNIS),
        ("Kenntnisnahme", vokabular.KENNTNIS),
        ("einstimmig zur Kenntnis genommen", vokabular.KENNTNIS),
        ("beantwortet", vokabular.KENNTNIS),
        # vertagt
        ("vertagt", vokabular.VERTAGT),
        ("Vertagt in die nächste Sitzung", vokabular.VERTAGT),
        ("1. Lesung – vertagt", vokabular.VERTAGT),
        ("einstimmig vertagt", vokabular.VERTAGT),
        ("zurückgestellt", vokabular.VERTAGT),
        ("nicht behandelt", vokabular.VERTAGT),
        ("auf die nächste Sitzung verschoben", vokabular.VERTAGT),
        ("Vertagung beschlossen", vokabular.VERTAGT),
        # verwiesen
        ("verwiesen", vokabular.VERWIESEN),
        ("Überwiesen an den Haupt- und Finanzausschuss", vokabular.VERWIESEN),
        ("zur Beratung in den Bauausschuss verwiesen", vokabular.VERWIESEN),
        ("Verweisung in die Bezirksvertretung beschlossen", vokabular.VERWIESEN),
        # abgesetzt
        ("abgesetzt", vokabular.ABGESETZT),
        ("Von der Tagesordnung abgesetzt", vokabular.ABGESETZT),
        ("von der Tagesordnung genommen", vokabular.ABGESETZT),
        ("Absetzung beschlossen", vokabular.ABGESETZT),
        ("Absetzung von der Tagesordnung einstimmig beschlossen", vokabular.ABGESETZT),
        # neutral: nicht sicher zuzuordnen
        ("zurückgezogen", vokabular.NEUTRAL),
        ("erledigt", vokabular.NEUTRAL),
        ("1. Lesung", vokabular.NEUTRAL),
        ("ohne Beschluss", vokabular.NEUTRAL),
        ("Empfehlung", vokabular.NEUTRAL),
        ("Änderungsantrag abgelehnt, Vorlage beschlossen", vokabular.NEUTRAL),
        # neutral: ein abgelehnter oder verneinter Antrag zum Verfahren sagt nichts über die Sache
        ("Verweisung abgelehnt", vokabular.NEUTRAL),
        ("Antrag auf Überweisung abgelehnt", vokabular.NEUTRAL),
        ("Der Verweisungsantrag wird mehrheitlich abgelehnt", vokabular.NEUTRAL),
        ("Antrag auf Absetzung abgelehnt", vokabular.NEUTRAL),
        ("Vertagung abgelehnt", vokabular.NEUTRAL),
        ("Antrag auf Vertagung fand keine Mehrheit", vokabular.NEUTRAL),
        ("nicht verwiesen", vokabular.NEUTRAL),
        ("nicht abgesetzt", vokabular.NEUTRAL),
        ("Die Verwaltung wird beauftragt, die Maßnahme auf das Jahr 2027 zu verschieben", vokabular.NEUTRAL),
        ("Die Maßnahme wird auf das Jahr 2027 verschoben, die Mittel bleiben im Haushalt", vokabular.NEUTRAL),
        ("", vokabular.NEUTRAL),
        (None, vokabular.NEUTRAL),
    ],
)
def test_rohtext_zum_vokabular(roh: str | None, schluessel: str) -> None:
    assert einordnen(roh) == schluessel


def test_angekuendigt_kommt_nie_aus_dem_rohtext() -> None:
    for roh in ("angekündigt", "Angekündigt", "Termin angekündigt"):
        assert einordnen(roh) != vokabular.ANGEKUENDIGT
    assert vokabular.angekuendigt().text == "angekündigt"
    assert vokabular.angekuendigt("Eigene Erklärung.").titel == "Eigene Erklärung."


def test_vokabular_ist_fest_und_hat_erklaerungen() -> None:
    begriffe = [text for text, _ in vokabular.VOKABULAR.values()]
    assert begriffe == [
        "beschlossen",
        "abgelehnt",
        "zur Kenntnis genommen",
        "vertagt",
        "verwiesen",
        "abgesetzt",
        "angekündigt",
    ]
    assert all(erklaerung.endswith(".") for _, erklaerung in vokabular.VOKABULAR.values())


class TestErgebnis:
    def test_ohne_text_kein_etikett(self) -> None:
        assert ergebnis(None) is None
        assert ergebnis("  ") is None

    def test_rohtext_nur_wenn_er_mehr_sagt(self) -> None:
        gleich = ergebnis("Beschlossen.")
        assert gleich is not None and gleich.text == "beschlossen" and gleich.roh_abweichend == ""
        assert gleich.titel == vokabular.VOKABULAR[vokabular.BESCHLOSSEN][1]

        mehr = ergebnis("einstimmig empfohlen")
        assert mehr is not None and mehr.bekannt and mehr.text == "beschlossen"
        assert mehr.roh_abweichend == "einstimmig empfohlen"
        assert mehr.titel.endswith("Im Ratsinformationssystem: „einstimmig empfohlen“")

    def test_neutral_zeigt_den_rohtext_gekuerzt(self) -> None:
        kurz = ergebnis("zurückgezogen")
        assert kurz is not None and not kurz.bekannt and kurz.schluessel == ""
        assert kurz.text == "zurückgezogen" and kurz.roh_abweichend == ""
        assert kurz.titel == "Ergebnis laut Ratsinformationssystem: zurückgezogen"

        lang = ergebnis("Die Sache wird in einer Sondersitzung im Frühjahr erneut aufgerufen werden")
        assert lang is not None and not lang.bekannt
        assert len(lang.text) <= 40 and lang.text.endswith("…")
        assert lang.roh_abweichend.startswith("Die Sache wird in einer Sondersitzung")


# =============================================================================
# Stand eines Vorgangs mit Etikett
# =============================================================================


def _eintrag(tage: int, ergebnis_text: str | None = None) -> dict[str, Any]:
    datum = datetime(2026, 10, 3, 17, 0, tzinfo=BERLIN) + timedelta(days=tage)
    return {
        "date": datum,
        "meeting": SimpleNamespace(cancelled=False),
        "organization_name": "Rat der Stadt",
        "result": ergebnis_text,
        "public": True,
    }


class TestStandMitEtikett:
    jetzt = datetime(2026, 10, 3, 12, 0, tzinfo=BERLIN)

    def test_beschluss(self) -> None:
        stand = paper_status([_eintrag(-5, "einstimmig beschlossen")], self.jetzt)
        assert stand.kind == DECIDED and stand.etikett is not None
        assert stand.etikett.schluessel == vokabular.BESCHLOSSEN

    def test_vertagt(self) -> None:
        stand = paper_status([_eintrag(-5, "vertagt")], self.jetzt)
        assert stand.etikett is not None and stand.etikett.schluessel == vokabular.VERTAGT

    def test_angesetzt_ist_angekuendigt(self) -> None:
        stand = paper_status([_eintrag(5)], self.jetzt)
        assert stand.kind == SCHEDULED and stand.etikett is not None
        assert stand.etikett.schluessel == vokabular.ANGEKUENDIGT

    def test_ohne_begriff_und_ohne_ergebnis_kein_etikett(self) -> None:
        assert paper_status([_eintrag(-5, "zurückgezogen")], self.jetzt).etikett is None
        assert paper_status([_eintrag(-5)], self.jetzt).kind == NO_RESULT
        assert paper_status([_eintrag(-5)], self.jetzt).etikett is None
        assert paper_status([], self.jetzt).kind == NONE and paper_status([], self.jetzt).etikett is None
        assert PaperStatus(DECIDED, "Satz").etikett is None


# =============================================================================
# Komponente
# =============================================================================


class TestKomponente:
    def test_begriff_farbe_und_erklaerung(self) -> None:
        html = render('<c-insight.ergebnis :ergebnis="e" klasse="shrink-0" />', e=ergebnis("einstimmig beschlossen"))
        assert 'data-ergebnis="beschlossen"' in html
        assert ">beschlossen</span>" in html
        assert 'title="Das Gremium hat zugestimmt' in html and "einstimmig beschlossen“" in html
        assert "bg-primary-50" in html and "shrink-0" in html and "whitespace-nowrap" in html
        # Flach: kleiner Radius, keine Pille, kein Rahmen um farbige Etiketten
        assert "rounded-full" not in html and "ring-1" not in html

    @pytest.mark.parametrize(
        ("roh", "farbe"),
        [
            ("abgelehnt", "bg-red-50"),
            ("vertagt", "bg-amber-50"),
            ("verwiesen", "bg-blue-50"),
            ("Kenntnisnahme", "bg-gray-100"),
            ("abgesetzt", "bg-gray-100"),
        ],
    )
    def test_farben_je_begriff(self, roh: str, farbe: str) -> None:
        assert farbe in render('<c-insight.ergebnis :ergebnis="e" />', e=ergebnis(roh))

    def test_neutral_und_angekuendigt_nur_umrandet(self) -> None:
        neutral = render('<c-insight.ergebnis :ergebnis="e" />', e=ergebnis("zurückgezogen"))
        assert 'data-ergebnis="neutral"' in neutral and ">zurückgezogen</span>" in neutral
        assert "ring-1" in neutral and "bg-" not in neutral and "whitespace-nowrap" not in neutral
        angekuendigt = render('<c-insight.ergebnis :ergebnis="e" />', e=vokabular.angekuendigt())
        assert 'data-ergebnis="angekuendigt"' in angekuendigt and "ring-1" in angekuendigt

    def test_ohne_ergebnis_nichts(self) -> None:
        assert render('<c-insight.ergebnis :ergebnis="e" />', e=None).strip() == ""

    def test_filter_im_template(self) -> None:
        html = render(
            '{% load insight_listen %}{% with e=r|ergebnis %}<c-insight.ergebnis :ergebnis="e" />{% endwith %}',
            r="vertagt",
        )
        assert 'data-ergebnis="vertagt"' in html

    def test_suchtreffer_mit_etikett_statt_punkt(self) -> None:
        treffer = {
            "kind": "vorgang",
            "url": "/v/",
            "title": "Radweg",
            "context": ["Antrag"],
            "status": "Am 01.10.2026 im Rat beschlossen.",
            "status_kind": "decided",
            "ergebnis": ergebnis("beschlossen"),
        }
        html = render('<ol><c-suche.treffer-vorgang :treffer="t" /></ol>', t=treffer)
        assert 'data-ergebnis="beschlossen"' in html and "rounded-full" not in html
        ohne = render('<ol><c-suche.treffer-vorgang :treffer="t" /></ol>', t={**treffer, "ergebnis": None})
        assert "data-ergebnis" not in ohne and "rounded-full" in ohne

    def test_vorgangsliste_mit_etikett(self) -> None:
        stand = PaperStatus(DECIDED, "Am 01.10.2026 im Rat abgelehnt.", last={"result": "abgelehnt"})
        vorgang = SimpleNamespace(id=uuid.uuid4(), name="Radweg", reference="", paper_type="", date=None, stand=stand)
        html = render('<c-liste.vorgaenge :vorgaenge="v" />', v=[vorgang])
        # unter dem Titel (bis 2xl) und vor dem Stand-Satz (ab 2xl)
        assert html.count('data-ergebnis="abgelehnt"') == 2
        dicht = render('<c-liste.vorgaenge :vorgaenge="v" dicht ohne_merken />', v=[vorgang])
        assert dicht.count('data-ergebnis="abgelehnt"') == 2 and "Am 01.10.2026 im Rat abgelehnt." in dicht


# =============================================================================
# Sitzungsseite: flache Tagesordnung
# =============================================================================

RIS = "https://ris.etikettstadt.example/oparl"


@pytest.fixture
def kommune(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="RIS Etikettstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Etikettstadt", slug="etikettstadt"
    )


def _sitzung(body: OParlBody, nummer: int, tage: int, **felder: Any) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nummer}",
        body=body,
        name=f"Ratssitzung {nummer}",
        start=timezone.now() + timedelta(days=tage),
        **felder,
    )


def _punkt(sitzung: OParlMeeting, nummer: str, name: str, **felder: Any) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agendaitem/{sitzung.pk}/{nummer}", meeting=sitzung, number=nummer, name=name, **felder
    )


def _tagesordnung(html: str) -> str:
    treffer = re.search(r'<section aria-labelledby="tagesordnung-titel">(.*?)</section>', html, re.S)
    assert treffer, "Tagesordnung fehlt"
    return treffer.group(1)


@pytest.mark.django_db
class TestSitzungsseite:
    def test_tagesordnung_flach_mit_etiketten(self, kommune: OParlBody) -> None:
        sitzung = _sitzung(kommune, 1, -3)
        beschlossen = _punkt(sitzung, "1", "Radweg am Kanal", result="einstimmig beschlossen")
        _punkt(sitzung, "2", "Bericht der Verwaltung", result="Kenntnisnahme")
        _punkt(sitzung, "2.1", "Anfrage zu Spielplätzen", result="zurückgezogen")
        _punkt(sitzung, "3", "Eröffnung")
        vorgang = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/1",
            body=kommune,
            name="Radweg am Kanal",
            reference="V/0459/2026",
            paper_type="Beschlussvorlage",
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/1",
            body=kommune,
            paper=vorgang,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=beschlossen.external_id,
        )

        html = Client().get(f"/insight/termine/{sitzung.pk}/").content.decode()
        teil = _tagesordnung(html)

        assert f'id="top-{beschlossen.pk}"' in teil
        assert 'data-ergebnis="beschlossen"' in teil and "einstimmig beschlossen" in teil
        assert 'data-ergebnis="kenntnis"' in teil and ">zur Kenntnis genommen</span>" in teil
        assert 'data-ergebnis="neutral"' in teil and ">zurückgezogen</span>" in teil
        assert teil.count("data-ergebnis=") == 3
        # Kein Kasten im Kasten: keine Karten, keine Nummernkästchen, kein „Ergebnis:“ als grauer Text
        assert "rounded-xl" not in teil and "rounded-lg" not in teil and "Ergebnis:" not in teil
        assert "Tagesordnung <span" in teil and "(4)" in teil
        # Link zum Vorgang mit Art und Nummer; der Titel steht schon am Punkt
        assert f'href="/insight/vorgaenge/{vorgang.pk}/"' in teil and ">Beschlussvorlage V/0459/2026</a>" in teil

    def test_kommende_sitzung_ohne_tagesordnung_ist_angekuendigt(self, kommune: OParlBody) -> None:
        sitzung = _sitzung(kommune, 2, 10)
        teil = _tagesordnung(Client().get(f"/insight/termine/{sitzung.pk}/").content.decode())
        assert 'data-ergebnis="angekuendigt"' in teil
        assert "Der Termin steht fest, die Tagesordnung ist noch nicht veröffentlicht." in teil

    def test_vergangene_sitzung_ohne_tagesordnung(self, kommune: OParlBody) -> None:
        sitzung = _sitzung(kommune, 3, -10)
        teil = _tagesordnung(Client().get(f"/insight/termine/{sitzung.pk}/").content.decode())
        assert "data-ergebnis" not in teil
        assert "Zu dieser Sitzung ist keine öffentliche Tagesordnung veröffentlicht." in teil
        abgesagt = _sitzung(kommune, 4, 10, cancelled=True)
        teil = _tagesordnung(Client().get(f"/insight/termine/{abgesagt.pk}/").content.decode())
        assert "angekuendigt" not in teil
