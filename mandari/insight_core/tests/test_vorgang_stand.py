# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorgangsdetail im Bürgerportal: Stand-Satz, Zeitstrahl und Dokumentzeile (Insight-Navigation, Stufe 2a).

Der Stand-Satz beantwortet „wo steht die Sache?“ aus dem Beratungsverlauf: letzte Beratung mit Datum,
Gremium und Ergebnis, dazu die Fälle „vertagt“, „ohne Ergebnis“ und „noch nicht beraten“.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.template import engines
from django.test import Client
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_core.services.paper_status import (
    CANCELLED,
    DECIDED,
    DEFERRED,
    LATEST,
    NO_RESULT,
    NONE,
    PAST,
    SCHEDULED,
    UNDATED,
    UPCOMING,
    in_committee,
    paper_status,
    result_as_participle,
    timeline,
)

BERLIN = ZoneInfo("Europe/Berlin")
JETZT = datetime(2026, 10, 3, 12, 0, tzinfo=BERLIN)


def tag(jahr: int, monat: int, t: int) -> datetime:
    return datetime(jahr, monat, t, 17, 0, tzinfo=BERLIN)


def eintrag(
    datum: datetime | None,
    gremium: str = "Rat der Stadt",
    ergebnis: str | None = None,
    *,
    public: bool = True,
    abgesagt: bool = False,
    top: str | None = None,
    rolle: str | None = None,
    entscheidung: bool = False,
) -> dict[str, Any]:
    """Eintrag wie aus ``PaperDetailView._get_consultations_with_meetings``."""
    return {
        "consultation": None,
        "meeting": SimpleNamespace(cancelled=abgesagt, id=uuid.uuid4()),
        "agenda_item": None,
        "date": datum,
        "organization_name": gremium,
        "agenda_number": top,
        "result": ergebnis,
        "public": public,
        "role": rolle,
        "authoritative": entscheidung,
    }


class TestStandSatz:
    def test_letzte_beratung_mit_ergebnis(self) -> None:
        verlauf = [
            eintrag(tag(2009, 9, 1), "Bezirksvertretung Mitte", "vertagt"),
            eintrag(tag(2011, 3, 22), "Bezirksvertretung Mitte", "einstimmig beschlossen"),
            eintrag(tag(2011, 12, 6), "Bezirksvertretung Mitte", "zur Kenntnis genommen"),
        ]
        stand = paper_status(verlauf, JETZT)
        assert stand.kind == DECIDED
        assert stand.text == "Am 06.12.2011 in der Bezirksvertretung Mitte zur Kenntnis genommen."
        assert stand.last is verlauf[2]

    def test_reihenfolge_der_eintraege_egal(self) -> None:
        verlauf = [
            eintrag(tag(2026, 5, 4), "Rat der Stadt", "beschlossen"),
            eintrag(tag(2026, 4, 20), "Ausschuss für Umwelt", "empfohlen"),
        ]
        assert paper_status(verlauf, JETZT).text == "Am 04.05.2026 im Rat der Stadt beschlossen."

    def test_naechste_beratung_wird_genannt(self) -> None:
        verlauf = [
            eintrag(tag(2026, 9, 10), "Ausschuss für Umwelt", "Einstimmig empfohlen"),
            eintrag(tag(2026, 10, 29), "Rat der Stadt"),
        ]
        stand = paper_status(verlauf, JETZT)
        assert stand.kind == DECIDED
        assert stand.text == (
            "Am 10.09.2026 im Ausschuss für Umwelt einstimmig empfohlen. Nächste Beratung am 29.10.2026 im Rat der Stadt."
        )
        assert stand.upcoming is verlauf[1]

    def test_vertagt_ohne_neuen_termin(self) -> None:
        stand = paper_status([eintrag(tag(2026, 6, 2), "Hauptausschuss", "vertagt")], JETZT)
        assert stand.kind == DEFERRED
        assert stand.text == "Am 02.06.2026 im Hauptausschuss vertagt. Ein neuer Termin ist nicht bekannt."

    def test_vertagt_mit_neuem_termin(self) -> None:
        verlauf = [
            eintrag(tag(2026, 6, 2), "Hauptausschuss", "Von der Tagesordnung abgesetzt"),
            eintrag(tag(2026, 11, 3), "Hauptausschuss"),
        ]
        stand = paper_status(verlauf, JETZT)
        assert stand.kind == DEFERRED
        assert stand.text == (
            "Am 02.06.2026 im Hauptausschuss von der Tagesordnung abgesetzt. Nächste Beratung am 03.11.2026 im Hauptausschuss."
        )

    def test_ohne_ergebnis_kurz_nach_der_sitzung(self) -> None:
        stand = paper_status([eintrag(tag(2026, 9, 30), "Bauausschuss")], JETZT)
        assert stand.kind == NO_RESULT
        assert stand.text == "Am 30.09.2026 im Bauausschuss beraten. Das Ergebnis ist noch nicht veröffentlicht."

    def test_ohne_ergebnis_lange_her(self) -> None:
        stand = paper_status([eintrag(tag(2019, 2, 14), "Bauausschuss", "  ")], JETZT)
        assert stand.kind == NO_RESULT
        assert stand.text == "Am 14.02.2019 im Bauausschuss beraten. Ein Ergebnis ist nicht angegeben."

    def test_nichtoeffentlich_ohne_ergebnis(self) -> None:
        stand = paper_status([eintrag(tag(2026, 9, 1), "Rat der Stadt", public=False)], JETZT)
        assert stand.text == "Am 01.09.2026 im Rat der Stadt nichtöffentlich beraten."

    def test_ergebnis_als_substantiv(self) -> None:
        stand = paper_status([eintrag(tag(2026, 9, 1), "Rat der Stadt", "Kenntnisnahme")], JETZT)
        assert stand.text == "Am 01.09.2026 im Rat der Stadt beraten. Ergebnis: Kenntnisnahme."

    def test_noch_nicht_beraten_mit_termin(self) -> None:
        stand = paper_status([eintrag(tag(2026, 10, 14), "Ausschuss für Umwelt")], JETZT)
        assert stand.kind == SCHEDULED
        assert stand.text == "Noch nicht beraten. Erste Beratung am 14.10.2026 im Ausschuss für Umwelt."

    def test_noch_keine_beratung(self) -> None:
        stand = paper_status([], JETZT)
        assert stand.kind == NONE
        assert stand.text == "Noch keine Beratung bekannt."
        assert stand.last is None and stand.upcoming is None

    def test_beratung_ohne_termin(self) -> None:
        stand = paper_status([eintrag(None)], JETZT)
        assert stand.kind == NONE
        assert stand.text == "Noch keine Beratung mit Termin bekannt."

    def test_abgesagte_sitzungen_zaehlen_nicht(self) -> None:
        verlauf = [
            eintrag(tag(2026, 5, 4), "Rat der Stadt", "beschlossen"),
            eintrag(tag(2026, 9, 1), "Rat der Stadt", abgesagt=True),
            eintrag(tag(2026, 12, 1), "Rat der Stadt", abgesagt=True),
        ]
        stand = paper_status(verlauf, JETZT)
        assert stand.text == "Am 04.05.2026 im Rat der Stadt beschlossen."
        assert stand.upcoming is None

    def test_sitzung_ohne_gremium(self) -> None:
        stand = paper_status([eintrag(tag(2026, 5, 4), "Sitzung", "beschlossen")], JETZT)
        assert stand.text == "Am 04.05.2026 beschlossen."


class TestGremiumUndErgebnis:
    @pytest.mark.parametrize(
        ("name", "erwartet"),
        [
            ("Rat der Stadt", "im Rat der Stadt"),
            ("Bezirksvertretung Nord", "in der Bezirksvertretung Nord"),
            ("Haupt- und Finanzausschuss", "im Haupt- und Finanzausschuss"),
            ("Kreistag", "im Kreistag"),
            ("Verbandsversammlung", "in der Verbandsversammlung"),
            ("Gleichstellungskommission", "in der Gleichstellungskommission"),
            ("Jugendparlament", "im Jugendparlament"),
            ("BV Süd", "im Gremium „BV Süd“"),
            ("Rat, Hauptausschuss", "im Gremium „Rat, Hauptausschuss“"),
            ("Sitzung", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_ortsangabe(self, name: str | None, erwartet: str) -> None:
        assert in_committee(name) == erwartet

    @pytest.mark.parametrize(
        ("ergebnis", "erwartet"),
        [
            ("zur Kenntnis genommen", "zur Kenntnis genommen"),
            ("Zur Kenntnis genommen.", "zur Kenntnis genommen"),
            ("Beschlossen", "beschlossen"),
            ("ungeändert beschlossen", "ungeändert beschlossen"),
            ("mit Änderungen beschlossen", "mit Änderungen beschlossen"),
            ("abgelehnt", "abgelehnt"),
            ("Kenntnisnahme", None),
            ("Bericht", None),
            ("Beschluss gemäß Vorlage", None),
            ("einstimmig", None),
            ("abgelehnt (10 Ja, 20 Nein)", None),
            ("", None),
        ],
    )
    def test_ergebnis_als_satzende(self, ergebnis: str, erwartet: str | None) -> None:
        assert result_as_participle(ergebnis) == erwartet


class TestZeitstrahl:
    def test_zustaende_und_detailzeile(self) -> None:
        verlauf = [
            eintrag(tag(2026, 3, 1), "Bauausschuss", "vertagt", top="5.4", rolle="Vorberatung"),
            eintrag(tag(2026, 4, 1), "Bauausschuss", abgesagt=True),
            eintrag(tag(2026, 5, 4), "Rat der Stadt", "beschlossen", top="8", entscheidung=True, public=False),
            eintrag(tag(2026, 11, 3), "Rat der Stadt", rolle="https://ris.example/oparl/role/1"),
            eintrag(None, "Bezirksvertretung Nord"),
        ]
        stand = paper_status(verlauf, JETZT)
        eintraege = timeline(verlauf, stand, JETZT)
        assert [e["state"] for e in eintraege] == [PAST, CANCELLED, LATEST, UPCOMING, UNDATED]
        assert eintraege[0]["detail"] == "TOP 5.4, Vorberatung"
        assert eintraege[2]["detail"] == "TOP 8, Entscheidung, nichtöffentlich"
        assert eintraege[3]["detail"] == ""
        assert eintraege[0]["result"] == "vertagt"
        # Die Eingabe bleibt unverändert
        assert "state" not in verlauf[0]

    def test_einzelne_beratung_ohne_zuletzt(self) -> None:
        verlauf = [eintrag(tag(2026, 5, 4), "Rat der Stadt", "beschlossen")]
        assert [e["state"] for e in timeline(verlauf, paper_status(verlauf, JETZT), JETZT)] == [PAST]


def render(source: str, **context: object) -> str:
    """Rendert einen Template-String inklusive Cotton-Kompilierung (wie der Loader)."""
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


class TestKomponenten:
    def _datei(self, **felder: Any) -> SimpleNamespace:
        werte: dict[str, Any] = {
            "id": uuid.uuid4(),
            "name": "Antrag",
            "file_name": "antrag.pdf",
            "mime_type": "application/pdf",
            "size": 120_000,
            "size_human": "117,2 KB",
            "text_content": "Der Rat möge beschließen …",
            "download_url": "https://ris.example/antrag.pdf",
            "access_url": None,
        }
        werte.update(felder)
        return SimpleNamespace(**werte)

    def test_dokumentzeile_mit_allen_aktionen(self) -> None:
        datei = self._datei()
        html = render('<ul><c-insight.document-row :file="datei" meta="Rat" /></ul>', datei=datei)
        assert html.index(">Ansehen<") < html.index(">Text<") < html.index(">Herunterladen<")
        assert f'href="/insight/dokumente/{datei.id}/preview/?download=1"' in html
        assert f'data-doc-url="/insight/dokumente/{datei.id}/preview/"' in html
        assert f'aria-controls="text-{datei.id}"' in html and f'id="text-{datei.id}"' in html
        assert 'aria-expanded="false"' in html
        assert 'aria-label="Text: Antrag"' in html
        assert "Automatisch aus dem PDF ausgelesen, Lesefehler sind möglich." in html
        assert "Der Rat möge beschließen" in html

    def test_nicht_barrierefreie_anlage_zeigt_text_zuerst(self) -> None:
        datei = self._datei(name="Stellungnahme (nicht barrierefrei)")
        html = render('<ul><c-insight.document-row :file="datei" /></ul>', datei=datei)
        assert html.index(">Text<") < html.index(">Ansehen<")
        assert html.count(">Text<") == 1

    def test_ohne_text_und_ohne_adresse(self) -> None:
        datei = self._datei(text_content="  \n", download_url=None, mime_type="image/png")
        html = render('<ul><c-insight.document-row :file="datei" /></ul>', datei=datei)
        assert ">Text<" not in html and ">Ansehen<" not in html and ">Herunterladen<" not in html
        assert "<span>Dokument</span>" in html

    def test_zeitstrahl_eintraege(self) -> None:
        html = render(
            "<c-insight.timeline>"
            '<c-insight.timeline-entry date="06.12.2011" title="Rat" href="/x/" state="latest">TOP 5</c-insight.timeline-entry>'
            '<c-insight.timeline-entry date="03.11.2026" title="Hauptausschuss" state="upcoming">TOP 2</c-insight.timeline-entry>'
            '<c-insight.timeline-entry date="01.04.2026" title="Bauausschuss" state="cancelled"></c-insight.timeline-entry>'
            "</c-insight.timeline>"
        )
        assert html.strip().startswith("<ol")
        assert 'aria-current="step"' in html and "zuletzt" in html
        assert "vorgesehen" in html and "abgesagt" in html
        assert '<a href="/x/"' in html and "Hauptausschuss" in html


@pytest.mark.django_db
class TestVorgangsseite:
    @pytest.fixture
    def vorgang(self) -> OParlPaper:
        basis = "https://ris.beispielstadt.example/oparl/"
        source = OParlSource.objects.create(name="Beispielquelle", url=basis + "system")
        body = OParlBody.objects.create(external_id=basis + "body/1", source=source, name="Beispielstadt")
        paper = OParlPaper.objects.create(
            external_id=basis + "paper/1", body=body, name="Fuß- und Radweg an der Hauptstraße", reference="A/0011/2009"
        )
        for nummer, (datum, ergebnis) in enumerate(
            [(tag(2009, 9, 1), "vertagt"), (tag(2011, 12, 6), "zur Kenntnis genommen")], start=1
        ):
            sitzung = OParlMeeting.objects.create(
                external_id=f"{basis}meeting/{nummer}", body=body, name="Bezirksvertretung Mitte", start=datum
            )
            top = OParlAgendaItem.objects.create(
                external_id=f"{basis}agendaitem/{nummer}", meeting=sitzung, number=f"5.{nummer}", result=ergebnis
            )
            OParlConsultation.objects.create(
                external_id=f"{basis}consultation/{nummer}",
                body=body,
                paper=paper,
                meeting_external_id=sitzung.external_id,
                agenda_item_external_id=top.external_id,
                role="Entscheidung",
            )
        OParlFile.objects.create(
            external_id=basis + "file/1",
            body=body,
            paper=paper,
            name="Stellungnahme zum Antrag (nicht barrierefrei)",
            mime_type="application/pdf",
            download_url="https://ris.beispielstadt.example/datei/1.pdf",
            text_content="Die Verwaltung nimmt wie folgt Stellung.",
        )
        return paper

    def test_stand_zeitstrahl_und_dokumente(self, vorgang: OParlPaper) -> None:
        seite = Client().get(f"/insight/vorgaenge/{vorgang.id}/").content.decode()
        assert "Am 06.12.2011 in der Bezirksvertretung Mitte zur Kenntnis genommen." in seite
        assert seite.index("Dokumente") < seite.index("Beratungsverlauf")
        assert "zuletzt" in seite and "TOP 5.1, Entscheidung." in seite
        assert "Herunterladen" in seite and "?download=1" in seite
        assert "Die Verwaltung nimmt wie folgt Stellung." in seite
        assert "Rohtext" not in seite
        # Die Dokumentansicht nennt das Gremium der letzten Beratung
        assert 'data-doc-meta="Bezirksvertretung Mitte · A/0011/2009"' in seite

    def test_noch_nicht_beraten(self, vorgang: OParlPaper) -> None:
        vorgang.consultations.all().delete()
        seite = Client().get(f"/insight/vorgaenge/{vorgang.id}/").content.decode()
        assert "Noch keine Beratung bekannt." in seite
        assert "Beratungsverlauf" not in seite

    def test_quelle_nur_als_http_adresse(self, vorgang: OParlPaper) -> None:
        vorgang.raw_json = {"web": "javascript:alert(1)"}
        vorgang.save()
        assert "javascript:alert" not in Client().get(f"/insight/vorgaenge/{vorgang.id}/").content.decode()
        vorgang.raw_json = {"web": "https://ris.beispielstadt.example/vo020.asp?VOLFDNR=11"}
        vorgang.save()
        seite = Client().get(f"/insight/vorgaenge/{vorgang.id}/").content.decode()
        assert 'href="https://ris.beispielstadt.example/vo020.asp?VOLFDNR=11"' in seite
