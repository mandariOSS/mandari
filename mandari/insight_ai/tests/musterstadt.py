# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feste Ratsdaten der Musterstadt für die Tests des KI-Assistenten (Issue #899)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

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
)

BERLIN = ZoneInfo("Europe/Berlin")
#: Dienstag, 06.10.2026, 10:00 Uhr – „diese Woche“ ist Montag, 05.10., bis Sonntag, 11.10.
JETZT = datetime(2026, 10, 6, 10, 0, tzinfo=BERLIN)
BASIS = "https://ris.musterstadt.example/oparl"

#: Text der Begründung: vorn Allgemeines, in der Mitte der Abschnitt zum Radweg, hinten Kosten
BEGRUENDUNG = (
    "Allgemeine Vorbemerkung zur Verkehrsplanung der Musterstadt. "
    * 40
    + "\n\nDer Radweg an der Musterstraße wird auf 2,50 m verbreitert und vom Gehweg getrennt. "
    "Der Radweg erhält eine rote Markierung an allen Einmündungen.\n\n"
    + "Weitere Ausführungen zu Baumstandorten und Entwässerung. " * 40
    + "\n\nKosten: Die Maßnahme kostet 480.000 Euro und wird zu 75 Prozent gefördert."
)


def kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


def zeit(tag: int, stunde: int, monat: int = 10) -> datetime:
    return datetime(2026, monat, tag, stunde, 0, tzinfo=BERLIN)


@dataclass
class Musterstadt:
    body: OParlBody
    fremd: OParlBody
    rat: OParlOrganization
    ausschuss: OParlOrganization
    rat_sitzung: OParlMeeting
    ausschuss_sitzung: OParlMeeting
    vergangene_sitzung: OParlMeeting
    spaetere_sitzung: OParlMeeting
    abgesagte_sitzung: OParlMeeting
    geloeschte_sitzung: OParlMeeting
    fremde_sitzung: OParlMeeting
    top_eroeffnung: OParlAgendaItem
    top_radweg: OParlAgendaItem
    top_nichtoeffentlich: OParlAgendaItem
    vorlage: OParlPaper
    grundstueck: OParlPaper
    fremde_vorlage: OParlPaper
    datei: OParlFile
    datei_entfernt: OParlFile
    fremde_datei: OParlFile
    person: OParlPerson


def _gremium(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=kennung("organizations"), body=body, name=name, **felder)


def _sitzung(body: OParlBody, start: datetime, *gremien: OParlOrganization, **felder: Any) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(external_id=kennung("meetings"), body=body, start=start, **felder)
    sitzung.organizations.set(gremien)
    return sitzung


def _top(sitzung: OParlMeeting, nummer: str, name: str, **felder: Any) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=sitzung, number=nummer, order=int(nummer), name=name, **felder
    )


def _beratung(vorlage: OParlPaper, sitzung: OParlMeeting, top: OParlAgendaItem, **felder: Any) -> OParlConsultation:
    return OParlConsultation.objects.create(
        external_id=kennung("consultations"),
        body=vorlage.body,
        paper=vorlage,
        paper_external_id=vorlage.external_id,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=top.external_id,
        **felder,
    )


def baue_musterstadt() -> Musterstadt:
    source = OParlSource.objects.create(name="Muster-RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=kennung("bodies"), source=source, name="Stadt Musterstadt")
    fremd = OParlBody.objects.create(external_id=kennung("bodies"), source=source, name="Stadt Nachbarort")
    rat = _gremium(body, "Rat", classification="Rat")
    ausschuss = _gremium(body, "Ausschuss für Umwelt und Verkehr", classification="Ausschuss")
    _gremium(body, "Sonderausschuss Stadtfest", end_date=date(2025, 12, 31))
    fremder_rat = _gremium(fremd, "Rat")

    rat_sitzung = _sitzung(body, zeit(7, 17), rat, name="Sitzung des Rates", location_name="Rathaus, Ratssaal")
    ausschuss_sitzung = _sitzung(body, zeit(8, 16), ausschuss, name="Sitzung")
    vergangene_sitzung = _sitzung(body, zeit(15, 16, monat=9), ausschuss, name="Sitzung")
    spaetere_sitzung = _sitzung(body, zeit(20, 17), rat, name="Sitzung des Rates")
    abgesagte_sitzung = _sitzung(body, zeit(9, 9), ausschuss, name="Sondersitzung", cancelled=True)
    geloeschte_sitzung = _sitzung(body, zeit(9, 18), rat, name="Gelöschte Sitzung", deleted=True)
    fremde_sitzung = _sitzung(fremd, zeit(7, 18), fremder_rat, name="Sitzung des Rates im Nachbarort")

    top_eroeffnung = _top(rat_sitzung, "1", "Eröffnung der Sitzung")
    top_radweg = _top(rat_sitzung, "2", "Radweg an der Musterstraße")
    top_nichtoeffentlich = _top(
        rat_sitzung, "3", "Verkauf des Grundstücks Parzelle 7", public=False, result="beschlossen"
    )
    top_ausschuss = _top(
        vergangene_sitzung,
        "4",
        "Radweg an der Musterstraße",
        result="einstimmig empfohlen",
        resolution_text="Der Ausschuss empfiehlt dem Rat, den Radweg an der Musterstraße zu verbreitern.",
    )

    vorlage = OParlPaper.objects.create(
        external_id=kennung("papers"),
        body=body,
        name="Radweg an der Musterstraße",
        reference="V/2026/0123",
        paper_type="Beschlussvorlage",
        date=date(2026, 9, 1),
    )
    grundstueck = OParlPaper.objects.create(
        external_id=kennung("papers"),
        body=body,
        name="Verkauf des Grundstücks Parzelle 7",
        reference="V/2026/0200",
        paper_type="Beschlussvorlage",
        date=date(2026, 9, 10),
    )
    fremde_vorlage = OParlPaper.objects.create(
        external_id=kennung("papers"),
        body=fremd,
        name="Radweg im Nachbarort",
        reference="V/2026/0123",
        date=date(2026, 9, 2),
    )
    _beratung(vorlage, vergangene_sitzung, top_ausschuss, role="Vorberatung")
    _beratung(vorlage, rat_sitzung, top_radweg, role="Entscheidung", authoritative=True)
    _beratung(grundstueck, rat_sitzung, top_nichtoeffentlich, role="Entscheidung", authoritative=True)

    datei = OParlFile.objects.create(
        external_id=kennung("files"), body=body, paper=vorlage, name="Begründung", text_content=BEGRUENDUNG
    )
    datei_entfernt = OParlFile.objects.create(
        external_id=kennung("files"),
        body=body,
        paper=vorlage,
        name="Alte Anlage",
        text_content="Entfernter Text",
        source_missing_since=JETZT,
    )
    fremde_datei = OParlFile.objects.create(
        external_id=kennung("files"), body=fremd, paper=fremde_vorlage, name="Fremde Anlage", text_content="Fremd"
    )
    person = OParlPerson.objects.create(
        external_id=kennung("persons"),
        body=body,
        name="Erika Mustermann",
        given_name="Erika",
        family_name="Mustermann",
        email="erika.mustermann@musterstadt.example",
        phone="01234 56789",
    )
    OParlMembership.objects.create(external_id=kennung("memberships"), person=person, organization=rat, role="Vorsitz")
    OParlMembership.objects.create(
        external_id=kennung("memberships"),
        person=person,
        organization=ausschuss,
        role="Mitglied",
        end_date=date(2025, 1, 31),
    )
    return Musterstadt(
        body=body,
        fremd=fremd,
        rat=rat,
        ausschuss=ausschuss,
        rat_sitzung=rat_sitzung,
        ausschuss_sitzung=ausschuss_sitzung,
        vergangene_sitzung=vergangene_sitzung,
        spaetere_sitzung=spaetere_sitzung,
        abgesagte_sitzung=abgesagte_sitzung,
        geloeschte_sitzung=geloeschte_sitzung,
        fremde_sitzung=fremde_sitzung,
        top_eroeffnung=top_eroeffnung,
        top_radweg=top_radweg,
        top_nichtoeffentlich=top_nichtoeffentlich,
        vorlage=vorlage,
        grundstueck=grundstueck,
        fremde_vorlage=fremde_vorlage,
        datei=datei,
        datei_entfernt=datei_entfernt,
        fremde_datei=fremde_datei,
        person=person,
    )


class FakeSuche:
    """Ersatz für den Suchdienst (Elasticsearch): feste Treffer je Index, merkt sich die Aufrufe."""

    def __init__(self, treffer: dict[str, list[dict[str, Any]]] | None = None, *, fehler: bool = False) -> None:
        self.treffer = treffer or {}
        self.fehler = fehler
        self.aufrufe: list[dict[str, Any]] = []

    def search_all(self, **kwargs: Any) -> dict[str, Any]:
        self.aufrufe.append(kwargs)
        if self.fehler:
            raise ConnectionError("Suche nicht erreichbar")
        index = (kwargs.get("index_names") or ["alle"])[0]
        hits = self.treffer.get(index, [])[: kwargs.get("page_size", 20)]
        return {"results": hits, "total": len(hits)}
