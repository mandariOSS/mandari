# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuveröffentlichung erhält die Verknüpfungen (Issue #547, ADR ``docs/adr/20260929-kanonisches-modell.md``).

Fraktionsnotizen, Positionen, private Notizen, Redebeiträge, Dokumente und Aufgaben an Tagesordnungspunkten sowie
Kommentare, Dokumente und Anträge an Vorlagen dürfen nicht verloren gehen, wenn ein RIS seinen Stand neu
veröffentlicht – und dürfen nie still am falschen Punkt hängen.

Die Tests spielen den Betrieb nach: Der Abgleich hat den Stand bestätigt (``uhr.lauf()``), dann ruft der Ingestor
die neu veröffentlichte Sitzung ab (``uhr.abruf``), und nach der Ruhezeit läuft der Abgleich erneut.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from io import StringIO
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    PaperComment,
)
from apps.work.motions.models import Motion
from apps.work.ris import verknuepfungen
from apps.work.ris.models import RisAnker, RisNeuzuordnung
from apps.work.ris.verknuepfungen import Bericht, abgleichen, hinweise_fuer_tops
from apps.work.tasks.models import Task
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)

RIS = "https://ris.example.org"
CONFIG_RE = re.compile(r'<script[^>]*id="prepare-config"[^>]*>(.*?)</script>', re.S)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def eingeschaltet(settings: Any) -> None:
    """Wie nach dem Einschalten im Betrieb: Das Signal hält den Anker beim Verknüpfen fest."""
    settings.WORK_RIS_RELINK = "aktiv"


def _spaeter() -> Any:
    """Zeitpunkt des Abgleichs: nach der Ruhezeit aller Änderungen im Test."""
    return timezone.now() + verknuepfungen.RUHEZEIT + timedelta(minutes=1)


class Uhr:
    """Zeit der Abgleichläufe; jeder Lauf kommt nach der Ruhezeit der letzten Änderung."""

    def __init__(self) -> None:
        self.zeit = timezone.now()

    def lauf(self, modus: str = "aktiv", **kwargs: Any) -> Bericht:
        self.zeit += verknuepfungen.RUHEZEIT + timedelta(minutes=1)
        return abgleichen(modus=modus, jetzt=self.zeit, **kwargs)

    def abruf(self, *objekte: OParlMeeting | OParlPaper) -> None:
        """Der Ingestor hat Sitzungen bzw. Vorlagen abgerufen (``updated_at`` wie bei jedem Upsert)."""
        self.zeit += timedelta(minutes=1)
        for model in (OParlMeeting, OParlPaper):
            model.objects.filter(pk__in=[o.pk for o in objekte if isinstance(o, model)]).update(updated_at=self.zeit)

    def danach(self) -> datetime:
        """Ein Zeitpunkt nach dem letzten Lauf (für Datensätze, die erst danach entstehen)."""
        self.zeit += timedelta(seconds=30)
        return self.zeit


@pytest.fixture
def uhr() -> Uhr:
    return Uhr()


class Ris:
    """Kleiner Nachbau eines RIS-Bestands einer Kommune."""

    def __init__(self, body: OParlBody) -> None:
        self.body = body

    def sitzung(self, nummer: int = 1, tagesordnung: list[str] | None = None) -> OParlMeeting:
        raw = {"agendaItem": [{"id": f"{RIS}/{a}"} for a in tagesordnung]} if tagesordnung is not None else {}
        return OParlMeeting.objects.create(
            external_id=f"{RIS}/{self.body.pk}/meeting/{nummer}",
            body=self.body,
            name="Rat",
            start=timezone.now() + timedelta(days=3),
            raw_json=raw,
        )

    def tagesordnung(self, sitzung: OParlMeeting, adressen: list[str]) -> None:
        sitzung.raw_json = {"agendaItem": [{"id": f"{RIS}/{a}"} for a in adressen]}
        sitzung.save(update_fields=["raw_json", "updated_at"])

    def vorlage(self, adresse: str, nummer: str, name: str = "Vorlage") -> OParlPaper:
        return OParlPaper.objects.create(external_id=f"{RIS}/{adresse}", body=self.body, name=name, reference=nummer)

    def top(
        self,
        sitzung: OParlMeeting,
        adresse: str,
        nummer: str,
        name: str,
        *,
        public: bool = True,
        vorlage: OParlPaper | None = None,
    ) -> OParlAgendaItem:
        item = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/{adresse}", meeting=sitzung, number=nummer, name=name, public=public
        )
        if vorlage is not None:
            self.beraten(vorlage, item)
        return item

    def beraten(self, vorlage: OParlPaper, item: OParlAgendaItem, *, je_punkt: bool = False) -> None:
        """
        Beratung einer Vorlage. Wie beim Abruf aus Sitzungsseiten eine Beratung je Vorlage und Sitzung, die auf
        den heutigen Punkt zeigt; ``je_punkt`` für Quellen mit einer Beratung je Punkt (dieselbe Vorlage an
        mehreren Punkten).
        """
        ziel = item.external_id if je_punkt else item.meeting_id
        OParlConsultation.objects.update_or_create(
            external_id=f"{vorlage.external_id}#consultation/{ziel}",
            defaults={
                "body": self.body,
                "paper": vorlage,
                "agenda_item_external_id": item.external_id,
                "meeting_external_id": item.meeting.external_id,
            },
        )


@pytest.fixture
def body(org: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.fixture
def ris(body: OParlBody) -> Ris:
    return Ris(body)


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.prepare", "meetings.view"], email="vorbereiter@example.org")


def _verschluesselt(model: type[models.Model], **felder: Any) -> Any:
    objekt = model(**felder)
    objekt.set_content_encrypted("Inhalt der Fraktion")  # type: ignore[attr-defined]
    objekt.save()
    return objekt


def _arbeitsdaten(org: Any, mitglied: Any, item: OParlAgendaItem) -> dict[str, Any]:
    """Alle Arten von Work-Daten an einem Tagesordnungspunkt."""
    sitzung = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=1), created_by=mitglied
    )
    return {
        "notiz": _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied),
        "privat": _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=item, author=mitglied),
        "rede": _verschluesselt(
            AgendaSpeechNote, organization=org, agenda_item=item, author=mitglied, meeting=item.meeting
        ),
        "position": AgendaItemPosition.objects.create(organization=org, agenda_item=item, position="against"),
        "dokument": AgendaSupplementaryDocument.objects.create(
            organization=org, agenda_item=item, title="Link", url="https://example.org/a"
        ),
        "aufgabe": Task.objects.create(organization=org, title="Nachfragen", related_agenda_item=item),
        "fraktions_top": FactionAgendaItem.objects.create(
            meeting=sitzung, number="1", title="Rat vorbereiten", visibility="public", related_agenda_item=item
        ),
    }


_FELD = {"aufgabe": "related_agenda_item_id", "fraktions_top": "related_agenda_item_id"}


def _haengt_an(daten: dict[str, Any], item: OParlAgendaItem) -> None:
    for name, objekt in daten.items():
        frisch = type(objekt)._base_manager.get(pk=objekt.pk)
        assert getattr(frisch, _FELD.get(name, "agenda_item_id")) == item.id, name


def _republiziert(ris: Ris, alt: OParlAgendaItem, adresse: str, **felder: Any) -> OParlAgendaItem:
    """Löschmarkierung des alten Punkts und Neuanlage unter neuer Adresse."""
    alt.mark_deleted()
    return ris.top(alt.meeting, adresse, felder.pop("nummer", alt.number), felder.pop("name", alt.name), **felder)


def _ruhig(uhr: Uhr, sitzung: OParlMeeting, **kwargs: Any) -> Bericht:
    """Noch ein Abgleich nach einem weiteren Abruf ohne inhaltliche Änderung."""
    uhr.abruf(sitzung)
    return uhr.lauf(**kwargs)


# =============================================================================
# Akzeptanz: Neuveröffentlichung erhält die Verknüpfungen
# =============================================================================


def test_loeschmarkierung_und_neuanlage_mit_vorlage(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/2026/0001")
    alt = ris.top(sitzung, "top/1", "3", "Spielplatz Nord", vorlage=vorlage)
    daten = _arbeitsdaten(org, mitglied, alt)
    vorher = {n: (o.content_encrypted, o.updated_at) for n, o in daten.items() if hasattr(o, "content_encrypted")}
    uhr.lauf()

    # Neu veröffentlicht: anderer Titel, andere Nummer, dieselbe Vorlage
    neu = _republiziert(ris, alt, "top/1-neu", nummer="4", name="Spielplatz Nord (geändert)", vorlage=vorlage)
    uhr.abruf(sitzung)
    bericht = uhr.lauf()

    _haengt_an(daten, neu)
    for name, (inhalt, geaendert) in vorher.items():
        frisch = type(daten[name]).objects.get(pk=daten[name].pk)
        assert bytes(frisch.content_encrypted) == bytes(inhalt), "Inhalt bleibt unverändert verschlüsselt"
        assert frisch.updated_at == geaendert, "Umhängen ist keine inhaltliche Änderung"
        assert frisch.get_content_decrypted() == "Inhalt der Fraktion"
    assert bericht.umgehaengt == 1 and bericht.datensaetze == 7 and bericht.konflikte == 0
    protokoll = RisNeuzuordnung.objects.get(von=alt.id)
    assert protokoll.nach == neu.id and protokoll.ergebnis == "nachfolger"
    assert protokoll.verschoben["work.AgendaItemNote.agenda_item"] == [str(daten["notiz"].pk)]
    anker = RisAnker.objects.get(art=RisAnker.ART_TOP)
    assert anker.objekt == neu.id and anker.status == RisAnker.AKTUELL
    assert anker.kennung["papers"] == [str(vorlage.id)]

    assert _ruhig(uhr, sitzung, alle=True).umgehaengt == 0
    assert RisNeuzuordnung.objects.count() == 1


def test_neue_adressen_ohne_loeschmeldung_ohne_vorlage(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung(tagesordnung=["top/1", "top/2"])
    ris.top(sitzung, "top/1", "1", "Eröffnung")
    alt = ris.top(sitzung, "top/2", "2", "Bericht der Verwaltung")
    daten = _arbeitsdaten(org, mitglied, alt)
    uhr.lauf()

    # Die Quelle listet die Tagesordnung unter neuen Adressen; die alten Punkte bleiben ohne Löschmeldung stehen
    ris.top(sitzung, "top/1b", "1", "Eröffnung")
    neu = ris.top(sitzung, "top/2b", "2", "Bericht der Verwaltung")
    ris.tagesordnung(sitzung, ["top/1b", "top/2b"])
    uhr.abruf(sitzung)
    uhr.lauf()

    _haengt_an(daten, neu)
    assert not OParlAgendaItem.objects.get(pk=alt.pk).deleted, "Der Bestand wird nicht verändert"


def test_umnummerierung_mit_stabiler_kennung_aendert_nichts(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/7", "7", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, item)
    uhr.lauf()
    ris.top(sitzung, "top/dringlich", "7", "Dringlichkeitsantrag")
    OParlAgendaItem.objects.filter(pk=item.pk).update(number="8")
    uhr.abruf(sitzung)

    bericht = uhr.lauf()

    _haengt_an(daten, item)
    assert bericht.umgehaengt == 0 and not RisNeuzuordnung.objects.exists()
    assert RisAnker.objects.get(objekt=item.id).kennung["number"] == "8", "Kennung folgt dem heutigen Stand"


def test_umnummerierung_bei_nummer_in_der_adresse(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Eine Einfügung verschiebt die Inhalte über die Zeilen; die Daten wandern mit (Kette, eine Person)."""
    sitzung = ris.sitzung()
    vorlagen = [ris.vorlage(f"paper/{n}", f"V/{n}") for n in (1, 2, 3)]
    tops = [
        ris.top(sitzung, f"top/{n}", str(n), f"Punkt {n}", vorlage=v) for n, v in zip((5, 6), vorlagen[:2], strict=True)
    ]
    an_5 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=tops[0], author=mitglied)
    an_6 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=tops[1], author=mitglied)
    uhr.lauf()

    # Neuer TOP 5 vorne eingefügt: Zeile /5 trägt die neue Vorlage, /6 den bisherigen TOP 5, /7 den bisherigen TOP 6
    OParlAgendaItem.objects.filter(pk=tops[0].pk).update(name="Neuer Punkt")
    ris.beraten(vorlagen[2], tops[0])
    OParlAgendaItem.objects.filter(pk=tops[1].pk).update(name="Punkt 5")
    ris.beraten(vorlagen[0], tops[1])
    neu_7 = ris.top(sitzung, "top/7", "7", "Punkt 6", vorlage=vorlagen[1])
    uhr.abruf(sitzung)

    uhr.lauf()

    assert AgendaPrivateNote.objects.get(pk=an_5.pk).agenda_item_id == tops[1].id
    assert AgendaPrivateNote.objects.get(pk=an_6.pk).agenda_item_id == neu_7.id
    assert not RisAnker.objects.filter(objekt=tops[0].id).exists(), "Am neuen TOP 5 hängt nichts mehr"
    assert _ruhig(uhr, sitzung).umgehaengt == 0


def _tausch(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> dict[str, Any]:
    """TOP 1 und TOP 2 tauschen den Inhalt; dieselbe Person hat an beiden eine private Notiz."""
    sitzung = ris.sitzung()
    a, b = ris.vorlage("paper/a", "V/A"), ris.vorlage("paper/b", "V/B")
    top_1 = ris.top(sitzung, "top/1", "1", "A", vorlage=a)
    top_2 = ris.top(sitzung, "top/2", "2", "B", vorlage=b)
    daten = {
        "sitzung": sitzung,
        "top_1": top_1,
        "top_2": top_2,
        "pos_1": AgendaItemPosition.objects.create(organization=org, agenda_item=top_1, position="for"),
        "pos_2": AgendaItemPosition.objects.create(organization=org, agenda_item=top_2, position="against"),
        "notiz_1": _verschluesselt(AgendaItemNote, organization=org, agenda_item=top_1, author=mitglied),
        "privat_1": _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=top_1, author=mitglied),
        "privat_2": _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=top_2, author=mitglied),
    }
    uhr.lauf()
    OParlAgendaItem.objects.filter(pk=top_1.pk).update(name="B")
    OParlAgendaItem.objects.filter(pk=top_2.pk).update(name="A")
    ris.beraten(b, top_1)
    ris.beraten(a, top_2)
    uhr.abruf(sitzung)
    daten["bericht"] = uhr.lauf()
    return daten


def _tausch_pruefen(d: dict[str, Any]) -> None:
    assert AgendaItemPosition.objects.get(pk=d["pos_1"].pk).agenda_item_id == d["top_2"].id
    assert AgendaItemPosition.objects.get(pk=d["pos_2"].pk).agenda_item_id == d["top_1"].id
    assert AgendaItemNote.objects.get(pk=d["notiz_1"].pk).agenda_item_id == d["top_2"].id
    assert AgendaPrivateNote.objects.get(pk=d["privat_1"].pk).agenda_item_id == d["top_1"].id
    assert AgendaPrivateNote.objects.get(pk=d["privat_2"].pk).agenda_item_id == d["top_2"].id


def test_tausch_zweier_punkte(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Positionen und Notizen tauschen mit; private Notizen derselben Person an beiden bleiben – mit Hinweis."""
    d = _tausch(org, ris, mitglied, uhr)

    _tausch_pruefen(d)
    assert d["bericht"].konflikte == 2
    anker_1 = RisAnker.objects.get(objekt=d["top_1"].id)
    assert anker_1.kennung["name"] == "b", "beschreibt, was heute am Punkt hängt"
    assert anker_1.zurueckgelassen == {"work.AgendaPrivateNote.agenda_item": [str(d["privat_1"].pk)]}
    hinweis = hinweise_fuer_tops(org, [d["top_1"].id])[d["top_1"].id]
    assert hinweis.titel == "Nicht zugeordnet" and "„A“" in hinweis.text


@pytest.mark.parametrize("alle", [False, True])
def test_tausch_mit_konflikt_bewegt_beim_naechsten_lauf_nichts(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, alle: bool
) -> None:
    d = _tausch(org, ris, mitglied, uhr)
    protokolle = RisNeuzuordnung.objects.count()

    for _ in range(2):
        bericht = _ruhig(uhr, d["sitzung"], alle=alle)
        assert bericht.umgehaengt == 0 and bericht.konflikte == 0
        _tausch_pruefen(d)
    assert RisNeuzuordnung.objects.count() == protokolle


def test_tausch_von_positionen_trotz_bedingung_und_rueckweg(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """
    Positionen sind je Organisation und TOP eindeutig, als Bedingung in der Datenbank (#926). Beim Tausch würde jeder
    Zwischenstand sie verletzen; die Positionen ziehen über einen Zwischenplatz um, hin und auf dem Rückweg.
    """
    d = _tausch(org, ris, mitglied, uhr)
    with pytest.raises(IntegrityError), transaction.atomic():
        AgendaItemPosition.objects.create(organization=org, agenda_item=d["top_1"])

    _tausch_pruefen(d)
    assert d["bericht"].fehler == 0
    assert RisNeuzuordnung.objects.get(von=d["top_1"].id).verschoben["work.AgendaItemPosition.agenda_item"] == [
        str(d["pos_1"].pk)
    ]
    # Inhalte und Organisation bleiben, nur der TOP wechselt
    pos_1 = AgendaItemPosition.objects.get(pk=d["pos_1"].pk)
    assert (pos_1.organization_id, pos_1.position) == (org.id, "for")

    rueck = verknuepfungen.zurueckdrehen(RisNeuzuordnung.objects.all())

    assert rueck.nicht_moeglich == [] and rueck.datensaetze == 3
    assert AgendaItemPosition.objects.get(pk=d["pos_1"].pk).agenda_item_id == d["top_1"].id
    assert AgendaItemPosition.objects.get(pk=d["pos_2"].pk).agenda_item_id == d["top_2"].id
    assert AgendaItemNote.objects.get(pk=d["notiz_1"].pk).agenda_item_id == d["top_1"].id
    assert not AgendaItemPosition.objects.filter(organization__isnull=True).exists(), "nichts bleibt geparkt"


def test_ring_mit_bleibender_position_haengt_nichts_doppelt(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """
    Drei Punkte rotieren; am Ziel der ersten Position steht eine jüngere, die bleibt. Die erste Position bleibt
    deshalb am alten Punkt und belegt ihn – die Position, die dorthin ziehen wollte, bleibt auch. Nie zwei
    Positionen einer Organisation am selben Punkt (#926).
    """
    sitzung = ris.sitzung()
    a, b, c = ris.vorlage("paper/a", "V/A"), ris.vorlage("paper/b", "V/B"), ris.vorlage("paper/c", "V/C")
    top_1 = ris.top(sitzung, "top/1", "1", "A", vorlage=a)
    top_2 = ris.top(sitzung, "top/2", "2", "B", vorlage=b)
    top_3 = ris.top(sitzung, "top/3", "3", "C", vorlage=c)
    pos_1 = AgendaItemPosition.objects.create(organization=org, agenda_item=top_1, position="for")
    notiz_2 = _verschluesselt(AgendaItemNote, organization=org, agenda_item=top_2, author=mitglied)
    pos_3 = AgendaItemPosition.objects.create(organization=org, agenda_item=top_3, position="against")
    uhr.lauf()

    # Rotation: /1 trägt jetzt C, /2 trägt A, /3 trägt B
    for top, name, vorlage in ((top_1, "C", c), (top_2, "A", a), (top_3, "B", b)):
        OParlAgendaItem.objects.filter(pk=top.pk).update(name=name)
        ris.beraten(vorlage, top)
    uhr.abruf(sitzung)
    # Vor dem Abgleich legt jemand am neuen Inhalt von /2 eine Position an
    jung = AgendaItemPosition.objects.create(organization=org, agenda_item=top_2, position="abstain")
    AgendaItemPosition.objects.filter(pk=jung.pk).update(created_at=uhr.danach())

    bericht = uhr.lauf()

    assert bericht.fehler == 0
    assert AgendaItemNote.objects.get(pk=notiz_2.pk).agenda_item_id == top_3.id
    assert AgendaItemPosition.objects.get(pk=jung.pk).agenda_item_id == top_2.id
    assert AgendaItemPosition.objects.get(pk=pos_1.pk).agenda_item_id == top_1.id
    assert AgendaItemPosition.objects.get(pk=pos_3.pk).agenda_item_id == top_3.id
    assert bericht.konflikte == 2 and bericht.juenger == 1
    assert _ruhig(uhr, sitzung, alle=True).umgehaengt == 0


POSITIONEN = "work.AgendaItemPosition.agenda_item"


def _umzug(org: Any, von: OParlAgendaItem, nach: OParlAgendaItem, *positionen: Any) -> RisNeuzuordnung:
    """Protokolleintrag eines Umzugs, wie ihn der Abgleich schreibt."""
    return RisNeuzuordnung.objects.create(
        organization=org,
        art=RisAnker.ART_TOP,
        von=von.id,
        nach=nach.id,
        ergebnis="nachfolger",
        verschoben={POSITIONEN: [str(p.pk) for p in positionen]},
    )


def _an(position: Any) -> Any:
    return AgendaItemPosition.objects.get(pk=position.pk).agenda_item_id


def test_rueckweg_tausch_geht_auf_obwohl_eine_kette_blockiert(org: Any, ris: Ris, mitglied: Any) -> None:
    """
    Rückweg (#926): Ein Tausch A↔B geht zurück, auch wenn in derselben Organisation eine Kette C→D, E→C festhängt,
    weil an E inzwischen eine neue Position steht. Die Kette bleibt, ihre Einträge bleiben offen; eine zweite
    Organisation mit eigenem Tausch an denselben Punkten geht ebenso zurück.
    """
    sitzung = ris.sitzung()
    a, b, c, d, e = (ris.top(sitzung, f"top/{n}", n, n) for n in "ABCDE")
    # Stand nach dem Abgleich: pos_1 A→B, pos_2 B→A, pos_3 C→D, pos_4 E→C; danach entstand S an E
    pos_1 = AgendaItemPosition.objects.create(organization=org, agenda_item=b, position="for")
    pos_2 = AgendaItemPosition.objects.create(organization=org, agenda_item=a, position="against")
    pos_3 = AgendaItemPosition.objects.create(organization=org, agenda_item=d, position="for")
    pos_4 = AgendaItemPosition.objects.create(organization=org, agenda_item=c, position="against")
    s_neu = AgendaItemPosition.objects.create(organization=org, agenda_item=e, position="abstain")
    andere = cast(Any, OrganizationFactory)(name="Fraktion Anders", slug="fraktion-anders")
    q_1 = AgendaItemPosition.objects.create(organization=andere, agenda_item=b, position="for")
    q_2 = AgendaItemPosition.objects.create(organization=andere, agenda_item=a, position="against")
    tausch = [_umzug(org, a, b, pos_1), _umzug(org, b, a, pos_2), _umzug(andere, a, b, q_1), _umzug(andere, b, a, q_2)]
    kette = [_umzug(org, c, d, pos_3), _umzug(org, e, c, pos_4)]

    rueck = verknuepfungen.zurueckdrehen(RisNeuzuordnung.objects.all())

    assert [_an(p) for p in (pos_1, pos_2, q_1, q_2)] == [a.id, b.id, a.id, b.id]
    assert [_an(p) for p in (pos_3, pos_4, s_neu)] == [d.id, c.id, e.id]
    assert rueck.datensaetze == 4
    assert {pk for _e, _n, pk in rueck.nicht_moeglich} == {str(pos_3.pk), str(pos_4.pk)}
    assert set(rueck.offen) == {str(k.pk) for k in kette}
    assert all(RisNeuzuordnung.objects.get(pk=t.pk).zurueckgedreht_am for t in tausch)
    assert not any(RisNeuzuordnung.objects.get(pk=k.pk).zurueckgedreht_am for k in kette)
    assert not AgendaItemPosition.objects.filter(organization__isnull=True).exists(), "nichts bleibt geparkt"

    # Ist der Weg frei, holt ein späterer Rückweg die Kette nach
    s_neu.delete()
    rueck = verknuepfungen.zurueckdrehen(RisNeuzuordnung.objects.all())
    assert [_an(p) for p in (pos_3, pos_4)] == [c.id, e.id]
    assert rueck.offen == [] and rueck.datensaetze == 2
    assert all(RisNeuzuordnung.objects.get(pk=k.pk).zurueckgedreht_am for k in kette)


def test_position_entsteht_waehrend_des_abgleichs(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, monkeypatch: Any
) -> None:
    """
    Legt jemand zwischen dem Lesen und dem Sperren eine Position am Ziel an (#926), entscheidet der Abgleich am
    frischen Stand: Die betroffene Rotation bleibt mit Konflikt, ein unabhängiger Tausch derselben Organisation geht
    trotzdem durch.
    """
    sitzung = ris.sitzung()
    vorlagen = {n: ris.vorlage(f"paper/{n}", f"V/{n}") for n in "ABCDE"}
    tops = {n: ris.top(sitzung, f"top/{n}", n, n, vorlage=vorlagen[n]) for n in "ABCDE"}
    pos_1 = AgendaItemPosition.objects.create(organization=org, agenda_item=tops["A"], position="for")
    notiz = _verschluesselt(AgendaItemNote, organization=org, agenda_item=tops["B"], author=mitglied)
    pos_3 = AgendaItemPosition.objects.create(organization=org, agenda_item=tops["C"], position="against")
    pos_4 = AgendaItemPosition.objects.create(organization=org, agenda_item=tops["D"], position="for")
    pos_5 = AgendaItemPosition.objects.create(organization=org, agenda_item=tops["E"], position="against")
    uhr.lauf()

    # Rotation A→B→C→A und Tausch D↔E
    for top, inhalt in (("A", "C"), ("B", "A"), ("C", "B"), ("D", "E"), ("E", "D")):
        OParlAgendaItem.objects.filter(pk=tops[top].pk).update(name=inhalt)
        ris.beraten(vorlagen[inhalt], tops[top])
    uhr.abruf(sitzung)

    from django.db.models.query import QuerySet

    echt = QuerySet.select_for_update
    dazwischen: list[AgendaItemPosition] = []

    def mit_rennen(self: QuerySet[Any], *args: Any, **kwargs: Any) -> Any:
        if self.model is AgendaItemPosition and not dazwischen:
            # Speichern am neuen Inhalt von B, nachdem der Abgleich gelesen, aber bevor er gesperrt hat
            dazwischen.append(
                AgendaItemPosition.objects.create(organization=org, agenda_item=tops["B"], position="abstain")
            )
        return echt(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", mit_rennen)
    bericht = uhr.lauf()

    assert dazwischen and bericht.fehler == 0
    assert [_an(p) for p in (pos_4, pos_5)] == [tops["E"].id, tops["D"].id], "Tausch geht durch"
    assert [_an(p) for p in (pos_1, pos_3, dazwischen[0])] == [tops["A"].id, tops["C"].id, tops["B"].id]
    assert AgendaItemNote.objects.get(pk=notiz.pk).agenda_item_id == tops["C"].id
    assert bericht.konflikte == 2
    assert not AgendaItemPosition.objects.filter(organization__isnull=True).exists()


@pytest.mark.parametrize("fall", ["zurueck", "alles_zuruecksetzen"])
def test_fehlerzweige_beim_parken(org: Any, ris: Ris, monkeypatch: Any, fall: str) -> None:
    """
    Fehlerzweige (#926), erzwungen durch einen veralteten Plan, der alle ziehen lässt: Trifft eine Zeile am Ziel auf
    eine andere, kommt sie zurück; geht auch das nicht, wird alles zurückgesetzt. Nie bleibt etwas geparkt, und als
    geblieben zählen nur Zeilen, die geparkt waren.
    """
    monkeypatch.setattr(verknuepfungen, "_endzustand", lambda stehend, zuege, pruefen: (list(range(len(zuege))), []))
    verknuepfung = verknuepfungen._NACH_NAME[(RisAnker.ART_TOP, POSITIONEN)]
    sitzung = ris.sitzung()
    t1, t2, t3, t4, t5 = (ris.top(sitzung, f"top/{n}", str(n), str(n)) for n in range(1, 6))
    x = AgendaItemPosition.objects.create(organization=org, agenda_item=t1, position="for")
    AgendaItemPosition.objects.create(organization=org, agenda_item=t2, position="abstain")
    y = AgendaItemPosition.objects.create(organization=org, agenda_item=t3, position="against")
    z = AgendaItemPosition.objects.create(organization=org, agenda_item=t5, position="for")
    zuege = [(x.pk, t1.id, t2.id)]
    if fall == "alles_zuruecksetzen":
        # y zieht in den Platz von x; x kann dann nicht zurück. z hängt nicht mehr dort, wo der Plan ihn vermutet.
        zuege += [(y.pk, t3.id, t1.id), (z.pk, t4.id, t3.id)]

    umgezogen, geblieben = verknuepfungen._geparkt_umhaengen(verknuepfung, org.id, zuege)

    assert umgezogen == set()
    assert geblieben == ({0} if fall == "zurueck" else {0, 1})
    assert [_an(p) for p in (x, y, z)] == [t1.id, t3.id, t5.id]
    assert set(AgendaItemPosition.objects.values_list("organization_id", flat=True)) == {org.id}


def test_anderer_punkt_unter_alter_kennung_wird_nicht_zugeordnet(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, client_for: Any
) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/9", "9", "Bericht Klimaschutz")
    daten = _arbeitsdaten(org, mitglied, item)
    uhr.lauf()
    # Punkt entfällt, die Zeile trägt jetzt einen anderen Punkt
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Verschiedenes")
    uhr.abruf(sitzung)

    bericht = uhr.lauf()

    _haengt_an(daten, item)
    assert bericht.nicht_zugeordnet == 1
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.NICHT_ZUGEORDNET
    antwort = client_for(mitglied.user).get(f"/work/{org.slug}/meetings/{sitzung.id}/prepare/")
    treffer = CONFIG_RE.search(antwort.content.decode())
    assert treffer
    eintrag = json.loads(treffer.group(1))["items"][0]
    assert eintrag["withdrawn"] == "Nicht zugeordnet"
    assert "„Bericht Klimaschutz“" in eintrag["withdrawnHint"]


def test_korrektur_des_namens_bleibt_derselbe_punkt(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/4", "4", "Sanierung der Grundschule Nord")
    daten = _arbeitsdaten(org, mitglied, item)
    uhr.lauf()
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Sanierung der Grundschule Nord, 2. Bauabschnitt")
    uhr.abruf(sitzung)

    uhr.lauf()

    _haengt_an(daten, item)
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.AKTUELL


@pytest.mark.parametrize(("nummer", "eindeutig"), [("2", True), ("5", False)])
def test_mehrere_moegliche_nachfolger(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, nummer: str, eindeutig: bool
) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/m", "2", "Mitteilungen")
    daten = _arbeitsdaten(org, mitglied, alt)
    uhr.lauf()
    alt.mark_deleted()
    erster = ris.top(sitzung, "top/m1", nummer, "Mitteilungen")
    ris.top(sitzung, "top/m2", "6", "Mitteilungen")
    ris.top(sitzung, "top/m3", "2", "Mitteilungen", public=False)
    uhr.abruf(sitzung)

    bericht = uhr.lauf()

    if eindeutig:
        _haengt_an(daten, erster)
    else:
        _haengt_an(daten, alt)
        assert bericht.mehrdeutig == 1
        assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.MEHRDEUTIG


def test_gegenstueck_am_nachfolger_bleibt_unangetastet(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    privat_alt = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=alt, author=mitglied)
    notiz_alt = _verschluesselt(AgendaItemNote, organization=org, agenda_item=alt, author=mitglied)
    uhr.lauf()
    neu = _republiziert(ris, alt, "top/1b")
    privat_neu = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=neu, author=mitglied)
    uhr.abruf(sitzung)

    bericht = uhr.lauf()

    assert AgendaPrivateNote.objects.get(pk=privat_alt.pk).agenda_item_id == alt.id, "nichts zusammenführen"
    assert AgendaPrivateNote.objects.get(pk=privat_neu.pk).agenda_item_id == neu.id
    assert AgendaItemNote.objects.get(pk=notiz_alt.pk).agenda_item_id == neu.id
    assert bericht.konflikte == 1
    assert RisNeuzuordnung.objects.get(von=alt.id).konflikte == {
        "work.AgendaPrivateNote.agenda_item": [str(privat_alt.pk)]
    }
    assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.ENTFALLEN

    # Jede weitere Prüfung lässt es dabei: kein neues Protokoll, kein Umzug
    for alle in (False, True):
        bericht = _ruhig(uhr, sitzung, alle=alle)
        assert bericht.umgehaengt == 0 and bericht.konflikte == 0
    assert RisNeuzuordnung.objects.filter(von=alt.id).count() == 1


def test_ohne_nachfolger_bleibt_alles_am_geloeschten_punkt(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/1")
    item = ris.top(sitzung, "top/1", "1", "Abgesetzt", vorlage=vorlage)
    daten = _arbeitsdaten(org, mitglied, item)
    uhr.lauf()
    item.mark_deleted()
    ris.top(sitzung, "top/2", "1", "Anderer Punkt", vorlage=ris.vorlage("paper/2", "V/2"))
    uhr.abruf(sitzung)

    uhr.lauf()

    _haengt_an(daten, item)
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.ENTFALLEN


def _einbringung_und_beschluss(ris: Ris, *, public_beschluss: bool = True) -> tuple[Any, Any, Any, Any]:
    """Dieselbe Vorlage an zwei Punkten einer Sitzung (Quelle mit einer Beratung je Punkt)."""
    sitzung = ris.sitzung(tagesordnung=["top/5", "top/9"])
    vorlage = ris.vorlage("paper/1", "V/1", "Radweg")
    einbringung = ris.top(sitzung, "top/5", "5", "Radweg – Einbringung")
    beschluss = ris.top(sitzung, "top/9", "9", "Radweg – Beschluss", public=public_beschluss)
    ris.beraten(vorlage, einbringung, je_punkt=True)
    ris.beraten(vorlage, beschluss, je_punkt=True)
    return sitzung, vorlage, einbringung, beschluss


@pytest.mark.parametrize("wie", ["geloescht", "nicht_mehr_gelistet", "nichtoeffentlicher_teil"])
def test_vorlage_an_zwei_punkten_einer_wird_abgesetzt(org: Any, ris: Ris, mitglied: Any, uhr: Uhr, wie: str) -> None:
    """Der andere Punkt derselben Vorlage ist kein Nachfolger: Die Daten bleiben am abgesetzten Punkt."""
    sitzung, _, einbringung, beschluss = _einbringung_und_beschluss(
        ris, public_beschluss=wie != "nichtoeffentlicher_teil"
    )
    daten = _arbeitsdaten(org, mitglied, einbringung)
    uhr.lauf()

    if wie == "nicht_mehr_gelistet":
        ris.tagesordnung(sitzung, ["top/9"])
    else:
        einbringung.mark_deleted()
    uhr.abruf(sitzung)
    uhr.lauf()

    _haengt_an(daten, einbringung)
    assert not AgendaItemNote.objects.filter(agenda_item=beschluss).exists()
    assert RisAnker.objects.get(objekt=einbringung.id).status == RisAnker.ENTFALLEN
    assert not RisNeuzuordnung.objects.filter(ergebnis="nachfolger").exists()


def test_vorlage_an_zwei_punkten_neu_veroeffentlicht(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung, vorlage, einbringung, beschluss = _einbringung_und_beschluss(ris)
    daten = _arbeitsdaten(org, mitglied, einbringung)
    uhr.lauf()

    einbringung.mark_deleted()
    neu = ris.top(sitzung, "top/5b", "5", "Radweg – Einbringung")
    ris.beraten(vorlage, neu, je_punkt=True)
    ris.tagesordnung(sitzung, ["top/5b", "top/9"])
    uhr.abruf(sitzung)
    uhr.lauf()

    _haengt_an(daten, neu)
    assert not AgendaItemNote.objects.filter(agenda_item=beschluss).exists()


@pytest.mark.parametrize("titel", [None, "Radweg Ostring"])
def test_rueckwirkend_ohne_bekannte_geschwister(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, settings: Any, titel: str | None
) -> None:
    """
    Erster Lauf nach dem Einschalten: Der Punkt war schon vorher abgesetzt, seine Geschwister sind unbekannt – auch
    wenn beide Punkte den Titel der Vorlage tragen, ist der andere kein Nachfolger.
    """
    settings.WORK_RIS_RELINK = "aus"  # Verknüpfungen aus der Zeit vor dem Einschalten: ohne Anker
    _, _, einbringung, beschluss = _einbringung_und_beschluss(ris)
    if titel:
        OParlAgendaItem.objects.filter(pk__in=[einbringung.pk, beschluss.pk]).update(name=titel)
    daten = _arbeitsdaten(org, mitglied, einbringung)
    einbringung.mark_deleted()
    assert not RisAnker.objects.exists()

    settings.WORK_RIS_RELINK = "aktiv"
    bericht = uhr.lauf()

    _haengt_an(daten, einbringung)
    assert not AgendaItemNote.objects.filter(agenda_item=beschluss).exists()
    assert bericht.umgehaengt == 0
    anker = RisAnker.objects.get(objekt=einbringung.id)
    assert anker.kennung["geschwister"] is None and anker.status == RisAnker.ENTFALLEN


def test_rueckwirkend_neu_veroeffentlicht_unter_derselben_nummer(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, settings: Any
) -> None:
    """Vor dem Einschalten neu veröffentlicht: Der Punkt unter derselben Nummer ist der Nachfolger, nicht das Geschwister."""
    settings.WORK_RIS_RELINK = "aus"
    sitzung, vorlage, einbringung, beschluss = _einbringung_und_beschluss(ris)
    OParlAgendaItem.objects.filter(pk__in=[einbringung.pk, beschluss.pk]).update(name="Radweg Ostring")
    daten = _arbeitsdaten(org, mitglied, einbringung)
    einbringung.mark_deleted()
    neu = ris.top(sitzung, "top/5b", "5", "Radweg Ostring")
    ris.beraten(vorlage, neu, je_punkt=True)
    ris.tagesordnung(sitzung, ["top/5b", "top/9"])

    settings.WORK_RIS_RELINK = "aktiv"
    uhr.lauf()

    _haengt_an(daten, neu)
    assert not AgendaItemNote.objects.filter(agenda_item=beschluss).exists()


def test_juengere_datensaetze_bleiben_mit_hinweis(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Nummer in der Adresse: Was nach der letzten Bestätigung an der Zeile entstand, kann schon den neuen Punkt meinen."""
    sitzung = ris.sitzung()
    v5, v_neu = ris.vorlage("paper/5", "V/5"), ris.vorlage("paper/neu", "V/9")
    zeile_5 = ris.top(sitzung, "top/5", "5", "Punkt 5", vorlage=v5)
    alt = _verschluesselt(AgendaItemNote, organization=org, agenda_item=zeile_5, author=mitglied)
    uhr.lauf()

    # Einfügung: /5 trägt den neuen Punkt, /6 den bisherigen TOP 5
    OParlAgendaItem.objects.filter(pk=zeile_5.pk).update(name="Neuer Punkt")
    ris.beraten(v_neu, zeile_5)
    zeile_6 = ris.top(sitzung, "top/6", "6", "Punkt 5", vorlage=v5)
    uhr.abruf(sitzung)
    # Vor dem nächsten Abgleich schreibt jemand zum neuen Inhalt der Zeile /5
    jung = _verschluesselt(AgendaItemNote, organization=org, agenda_item=zeile_5, author=mitglied)
    AgendaItemNote.objects.filter(pk=jung.pk).update(created_at=uhr.danach())

    bericht = uhr.lauf()

    assert AgendaItemNote.objects.get(pk=alt.pk).agenda_item_id == zeile_6.id
    assert AgendaItemNote.objects.get(pk=jung.pk).agenda_item_id == zeile_5.id
    assert bericht.juenger == 1
    assert RisNeuzuordnung.objects.get(von=zeile_5.id).juenger == {"work.AgendaItemNote.agenda_item": [str(jung.pk)]}
    assert hinweise_fuer_tops(org, [zeile_5.id])[zeile_5.id].titel == "Nicht zugeordnet"
    assert _ruhig(uhr, sitzung, alle=True).umgehaengt == 0
    assert AgendaItemNote.objects.get(pk=jung.pk).agenda_item_id == zeile_5.id


@pytest.mark.parametrize("wie", ["geloescht", "nicht_mehr_gelistet"])
def test_juengere_datensaetze_an_abgesetzter_zeile_ziehen_mit(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, wie: str
) -> None:
    """Ohne neuen Inhalt an der alten Zeile gehört auch kurz vor der Neuveröffentlichung Geschriebenes zum Punkt."""
    sitzung = ris.sitzung(tagesordnung=["top/1"])
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    frueh = _verschluesselt(AgendaItemNote, organization=org, agenda_item=alt, author=mitglied)
    uhr.lauf()
    jung = _verschluesselt(AgendaItemNote, organization=org, agenda_item=alt, author=mitglied)
    AgendaItemNote.objects.filter(pk=jung.pk).update(created_at=uhr.danach())

    # gelöscht und neu angelegt bzw. ohne Löschmeldung unter neuer Adresse gelistet
    neu = _republiziert(ris, alt, "top/1b") if wie == "geloescht" else ris.top(sitzung, "top/1b", "1", "Haushalt")
    ris.tagesordnung(sitzung, ["top/1b"])
    uhr.abruf(sitzung)
    bericht = uhr.lauf()

    assert AgendaItemNote.objects.get(pk=frueh.pk).agenda_item_id == neu.id
    assert AgendaItemNote.objects.get(pk=jung.pk).agenda_item_id == neu.id
    assert bericht.juenger == 0 and bericht.datensaetze == 2
    assert not RisAnker.objects.filter(objekt=alt.id).exists(), "am alten Punkt hängt nichts mehr"
    assert hinweise_fuer_tops(org, [alt.id, neu.id]) == {}


def test_nach_nicht_zugeordnet_angelegtes_bleibt(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Notizen zum heutigen Inhalt einer nicht zugeordneten Zeile ziehen nicht mit, wenn der alte Punkt auftaucht."""
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/3", "3", "Bericht Klimaschutz")
    alt = _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    uhr.lauf()
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Verschiedenes")
    uhr.abruf(sitzung)
    uhr.lauf()
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.NICHT_ZUGEORDNET

    neu_hier = _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    AgendaItemNote.objects.filter(pk=neu_hier.pk).update(created_at=uhr.danach())
    wieder = ris.top(sitzung, "top/8", "8", "Bericht Klimaschutz")
    uhr.abruf(sitzung)
    uhr.lauf()

    assert AgendaItemNote.objects.get(pk=alt.pk).agenda_item_id == wieder.id
    assert AgendaItemNote.objects.get(pk=neu_hier.pk).agenda_item_id == item.id


def test_unveraenderte_sitzung_schreibt_bestaetigung_fort(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Ohne Abruf gilt der bestätigte Stand weiter: Später Angelegtes wandert bei einer Neuveröffentlichung mit."""
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=alt, author=mitglied)
    uhr.lauf()
    spaeter = AgendaItemPosition.objects.create(organization=org, agenda_item=alt, position="for")
    AgendaItemPosition.objects.filter(pk=spaeter.pk).update(created_at=uhr.danach())
    assert uhr.lauf().geprueft == 0, "unveränderte Sitzung"

    neu = _republiziert(ris, alt, "top/1b")
    uhr.abruf(sitzung)
    uhr.lauf()

    assert AgendaItemPosition.objects.get(pk=spaeter.pk).agenda_item_id == neu.id


def test_mandanten_bleiben_getrennt(org: Any, ris: Ris, mitglied: Any, make_member: Any, uhr: Uhr) -> None:
    """Jede Organisation behält ihre Datensätze; ein gleichnamiger Punkt einer anderen Sitzung zählt nicht."""
    andere_org = cast(Any, OrganizationFactory)(name="Fraktion Andere", slug="fraktion-andere")
    andere_org.body = ris.body
    andere_org.save(update_fields=["body"])
    anderes_mitglied = make_member(andere_org, ["meetings.prepare"], email="andere@example.org")
    sitzung, andere_sitzung = ris.sitzung(1), ris.sitzung(2)
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    fremd = ris.top(andere_sitzung, "top/x", "1", "Haushalt")
    eigene = AgendaItemPosition.objects.create(organization=org, agenda_item=alt, position="for")
    andere = AgendaItemPosition.objects.create(organization=andere_org, agenda_item=alt, position="against")
    andere_notiz = _verschluesselt(AgendaItemNote, organization=andere_org, agenda_item=fremd, author=anderes_mitglied)
    uhr.lauf()
    neu = _republiziert(ris, alt, "top/1b")
    uhr.abruf(sitzung, andere_sitzung)

    uhr.lauf()

    for position, organisation in ((eigene, org), (andere, andere_org)):
        frisch = AgendaItemPosition.objects.get(pk=position.pk)
        assert frisch.agenda_item_id == neu.id and frisch.organization_id == organisation.id
        protokoll = RisNeuzuordnung.objects.get(organization=organisation, von=alt.id)
        assert protokoll.verschoben == {"work.AgendaItemPosition.agenda_item": [str(position.pk)]}
    assert AgendaItemNote.objects.get(pk=andere_notiz.pk).agenda_item_id == fremd.id


def test_hinweis_nur_fuer_die_eigene_organisation(
    org: Any, ris: Ris, mitglied: Any, make_member: Any, uhr: Uhr
) -> None:
    """Ob eine andere Organisation am Punkt arbeitet, verrät der Hinweis nicht; ihre Kennung ist ihre eigene."""
    andere_org = cast(Any, OrganizationFactory)(name="Fraktion Andere", slug="fraktion-andere")
    anderes_mitglied = make_member(andere_org, ["meetings.prepare"], email="andere@example.org")
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/9", "9", "Bericht Klimaschutz")
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    uhr.lauf()
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Verschiedenes")
    # Die andere Organisation arbeitet erst nach der Änderung am Punkt: Ihr Anker trägt den neuen Stand
    _verschluesselt(AgendaItemNote, organization=andere_org, agenda_item=item, author=anderes_mitglied)
    uhr.abruf(sitzung)

    uhr.lauf()

    assert hinweise_fuer_tops(org, [item.id])[item.id].titel == "Nicht zugeordnet"
    assert hinweise_fuer_tops(andere_org, [item.id]) == {}
    assert RisAnker.objects.get(organization=andere_org).status == RisAnker.AKTUELL


def test_vorlage_neu_veroeffentlicht(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    alt = ris.vorlage("paper/1", "V/2026/0042", "Radweg")
    item = ris.top(sitzung, "top/1", "1", "Radweg", vorlage=alt)
    kommentar = _verschluesselt(PaperComment, organization=org, paper=alt, author=mitglied)
    dokument = AgendaSupplementaryDocument.objects.create(
        organization=org, agenda_item=item, paper=alt, title="Karte", url="https://example.org/k"
    )
    antrag = Motion.objects.create(organization=org, author=mitglied, title="Änderung", related_paper=alt)
    fraktion = FactionMeeting.objects.create(organization=org, title="F", start=timezone.now(), created_by=mitglied)
    fraktions_top = FactionAgendaItem.objects.create(meeting=fraktion, number="1", title="Radweg")
    fraktions_top.related_papers.add(alt)
    # Gleiche Nummer in einer anderen Kommune darf nie gewählt werden
    andere_kommune = OParlBody.objects.create(external_id=f"{RIS}/body/2", source=ris.body.source, name="Nachbar")
    OParlPaper.objects.create(
        external_id=f"{RIS}/body2/paper/1", body=andere_kommune, reference="V/2026/0042", name="Radweg"
    )

    alt.mark_deleted()
    neu = ris.vorlage("paper/1-neu", " v/2026/0042 ", "Radweg")
    abgleichen(modus="aktiv", jetzt=_spaeter())

    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == neu.id
    assert AgendaSupplementaryDocument.objects.get(pk=dokument.pk).paper_id == neu.id
    assert Motion.objects.get(pk=antrag.pk).related_paper_id == neu.id
    assert list(fraktions_top.related_papers.all()) == [neu]
    assert RisNeuzuordnung.objects.get(art=RisAnker.ART_VORLAGE).nach == neu.id


def test_vorlage_gleiche_nummer_anderer_gegenstand(org: Any, ris: Ris, mitglied: Any) -> None:
    alt = ris.vorlage("paper/1", "123", "Bebauungsplan Nord")
    kommentar = _verschluesselt(PaperComment, organization=org, paper=alt, author=mitglied)
    alt.mark_deleted()
    ris.vorlage("paper/2", "123", "Haushaltssatzung 2027")

    abgleichen(modus="aktiv", jetzt=_spaeter())

    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == alt.id
    assert RisAnker.objects.get(art=RisAnker.ART_VORLAGE).status == RisAnker.ENTFALLEN


# =============================================================================
# Betrieb
# =============================================================================


def test_standard_ist_aus(org: Any, ris: Ris, mitglied: Any, settings: Any) -> None:
    from mandari import settings_test

    if "WORK_RIS_RELINK" not in os.environ:
        assert settings_test.WORK_RIS_RELINK == "aus", "neuer, datenverändernder Weg: Standard aus"
    settings.WORK_RIS_RELINK = "aus"
    alt = ris.top(ris.sitzung(), "top/1", "1", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, alt)
    _republiziert(ris, alt, "top/1b")

    bericht = abgleichen(jetzt=_spaeter())

    assert bericht.geprueft == 0 and not RisAnker.objects.exists(), "kein Signal, kein Abgleich"
    _haengt_an(daten, alt)


def test_anker_beim_verknuepfen_traegt_den_stand_von_damals(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/1", "1", "Haushalt", vorlage=ris.vorlage("paper/1", "V/1"))
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)

    anker = RisAnker.objects.get(art=RisAnker.ART_TOP, objekt=item.id)
    assert anker.kennung["name"] == "haushalt" and anker.kennung["references"] == ["v/1"]
    assert anker.kennung["geschwister"] == []
    assert anker.geprueft_am is None and anker.bestaetigt_am is not None


def test_probe_meldet_nur(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, alt)
    uhr.lauf(modus="probe")
    neu = _republiziert(ris, alt, "top/1b")
    uhr.abruf(sitzung)

    bericht = uhr.lauf(modus="probe")

    _haengt_an(daten, alt)
    assert bericht.geplant == [("top", str(alt.id), "nachfolger", str(neu.id))]
    assert not RisNeuzuordnung.objects.exists()
    assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.AKTUELL
    assert abgleichen(modus="aus").geprueft == 0


def test_nur_geaenderte_und_ruhende_sitzungen(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, alt)
    jetzt = _spaeter()
    assert abgleichen(modus="aktiv", jetzt=jetzt).geprueft == 1
    assert abgleichen(modus="aktiv", jetzt=jetzt + timedelta(minutes=15)).geprueft == 0, "unverändert"

    neu = _republiziert(ris, alt, "top/1b")
    zeitpunkt = jetzt + timedelta(minutes=20)
    OParlAgendaItem.objects.filter(meeting=sitzung).update(updated_at=zeitpunkt)
    bericht = abgleichen(modus="aktiv", jetzt=zeitpunkt + timedelta(minutes=5))
    assert bericht.geprueft == 0, "mitten im Abruf: erst nach der Ruhezeit"
    _haengt_an(daten, alt)

    bericht = abgleichen(modus="aktiv", jetzt=zeitpunkt + verknuepfungen.RUHEZEIT + timedelta(minutes=1))
    assert bericht.umgehaengt == 1
    _haengt_an(daten, neu)


def test_entfallene_vorlage_nur_mit_abstand(org: Any, ris: Ris, mitglied: Any) -> None:
    alt = ris.vorlage("paper/1", "V/1", "Radweg")
    kommentar = _verschluesselt(PaperComment, organization=org, paper=alt, author=mitglied)
    alt.mark_deleted()
    jetzt = _spaeter()
    assert abgleichen(modus="aktiv", jetzt=jetzt).entfallen == 1
    assert abgleichen(modus="aktiv", jetzt=jetzt + timedelta(minutes=15)).geprueft == 0

    neu = ris.vorlage("paper/1-neu", "V/1", "Radweg")
    assert abgleichen(modus="aktiv", jetzt=jetzt + timedelta(hours=1)).geprueft == 0, "erst nach der Neuprüfung"
    bericht = abgleichen(modus="aktiv", jetzt=jetzt + verknuepfungen.NEUPRUEFUNG + timedelta(minutes=1))
    assert bericht.umgehaengt == 1
    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == neu.id


def test_fehler_in_einer_sitzung_bricht_den_lauf_nicht_ab(
    org: Any, ris: Ris, mitglied: Any, uhr: Uhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    erste, zweite = ris.sitzung(1), ris.sitzung(2)
    alt_1 = ris.top(erste, "top/1", "1", "Haushalt")
    alt_2 = ris.top(zweite, "top/2", "1", "Haushalt")
    daten_1 = _arbeitsdaten(org, mitglied, alt_1)
    position_2 = AgendaItemPosition.objects.create(organization=org, agenda_item=alt_2, position="for")
    uhr.lauf()
    neu_1, neu_2 = _republiziert(ris, alt_1, "top/1b"), _republiziert(ris, alt_2, "top/2b")
    uhr.abruf(erste, zweite)

    original = verknuepfungen._anwenden

    def anwenden(art: str, organisation: Any, entscheidungen: list[Any], *args: Any) -> None:
        original(art, organisation, entscheidungen, *args)
        if any(anker.objekt == alt_1.id for anker, _ in entscheidungen):
            raise IntegrityError("zeitgleich angelegt")

    monkeypatch.setattr(verknuepfungen, "_anwenden", anwenden)
    bericht = uhr.lauf()

    assert bericht.fehler == 1 and bericht.umgehaengt == 1
    _haengt_an(daten_1, alt_1)  # zurückgerollt, der nächste Lauf versucht es erneut
    assert AgendaItemPosition.objects.get(pk=position_2.pk).agenda_item_id == neu_2.id

    monkeypatch.setattr(verknuepfungen, "_anwenden", original)
    uhr.lauf()
    _haengt_an(daten_1, neu_1)


def test_anker_ohne_verknuepfung_wird_entfernt(org: Any, ris: Ris, mitglied: Any) -> None:
    item = ris.top(ris.sitzung(), "top/1", "1", "Haushalt")
    notiz = _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    notiz.delete()

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    assert bericht.anker_entfernt == 1 and not RisAnker.objects.exists()


def test_zurueckdrehen(org: Any, ris: Ris, mitglied: Any, uhr: Uhr) -> None:
    """Rückweg: Umzüge kommen an den früheren Punkt zurück und ziehen von dort nie wieder automatisch um."""
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/1", "Radweg")
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, alt)
    kommentar = _verschluesselt(PaperComment, organization=org, paper=vorlage, author=mitglied)
    uhr.lauf()
    eingeschaltet = timezone.now()  # Protokoll mit echter Uhrzeit
    neu = _republiziert(ris, alt, "top/1b")
    vorlage.mark_deleted()
    neue_vorlage = ris.vorlage("paper/1b", "V/1", "Radweg")
    uhr.abruf(sitzung, vorlage)
    uhr.lauf()
    _haengt_an(daten, neu)
    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == neue_vorlage.id

    ausgabe = StringIO()
    call_command("ris_neuzuordnung_zurueckdrehen", "--seit", eingeschaltet.isoformat(), "--dry-run", stdout=ausgabe)
    assert "Würde zurückdrehen: 2 Umzüge, 8 Datensätze" in ausgabe.getvalue()
    _haengt_an(daten, neu)

    call_command("ris_neuzuordnung_zurueckdrehen", "--seit", eingeschaltet.isoformat(), stdout=ausgabe)

    _haengt_an(daten, alt)
    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == vorlage.id
    assert not RisNeuzuordnung.objects.filter(ergebnis="nachfolger", zurueckgedreht_am__isnull=True).exists()
    assert len(RisAnker.objects.get(objekt=alt.id).zurueckgelassen) == 7

    # Der Abgleich läuft weiter und hängt nichts mehr um
    for alle in (False, True):
        assert _ruhig(uhr, sitzung, alle=alle).umgehaengt == 0
    _haengt_an(daten, alt)
    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == vorlage.id
    assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.ENTFALLEN


def test_alle_verknuepfungen_erfasst() -> None:
    """Jedes Feld aus Work auf einen Tagesordnungspunkt oder eine Vorlage folgt einer Neuveröffentlichung."""
    from django.apps import apps

    ziele = {OParlAgendaItem: RisAnker.ART_TOP, OParlPaper: RisAnker.ART_VORLAGE}
    gefunden = set()
    for model in apps.get_app_config("work").get_models():
        for feld in model._meta.get_fields():
            ziel = getattr(feld, "related_model", None)
            direkt = (feld.many_to_one and feld.concrete) or (feld.many_to_many and not feld.auto_created)
            if direkt and ziel in ziele:
                gefunden.add((ziele[ziel], model._meta.label, feld.name))
    erfasst = {(art, v.modell, v.feld) for art, liste in verknuepfungen.VERKNUEPFUNGEN.items() for v in liste}
    assert gefunden == erfasst


def test_bedingung_in_der_datenbank_ist_beim_umhaengen_bekannt() -> None:
    """Steht die Eindeutigkeit als Bedingung in der Datenbank, muss das Umhängen sie kennen (sonst IntegrityError)."""
    for liste in verknuepfungen.VERKNUEPFUNGEN.values():
        for v in liste:
            if v.m2m:
                continue
            model = v.tabelle()[0]
            feld = model._meta.get_field(v.feld).name
            bedingungen = [
                set(c.fields)
                for c in model._meta.constraints
                if isinstance(c, models.UniqueConstraint) and c.fields and c.condition is None
            ] + [set(felder) for felder in model._meta.unique_together]
            if any(feld in felder for felder in bedingungen):
                assert v.db_eindeutig or v.parken, v.name
            if v.parken:
                assert v.parken in v.eindeutig and model._meta.get_field(v.parken.removesuffix("_id")).null, v.name


def test_zeitplan_ist_registriert() -> None:
    from apps.events.schedule import Every, autodiscover, registry

    autodiscover()
    eintrag = registry.get("apps.work.schedules.ris_verknuepfungen_abgleichen")
    assert eintrag is not None and eintrag.trigger == Every(timedelta(minutes=15))


def test_erster_lauf_erfasst_den_bestand(org: Any, ris: Ris, mitglied: Any, uhr: Uhr, settings: Any) -> None:
    """
    Nach dem Einschalten bekommen Verknüpfungen aus der Zeit davor ihren Anker mit dem heutigen Stand (keine
    Datenmigration: Mit ``aus`` schreibt das Update nichts außer zwei leeren Tabellen).
    """
    settings.WORK_RIS_RELINK = "aus"
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/1")
    item = ris.top(sitzung, "top/1", "1", "Haushalt", vorlage=vorlage)
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    _verschluesselt(PaperComment, organization=org, paper=vorlage, author=mitglied)
    ohne = ris.top(sitzung, "top/2", "2", "Ohne Arbeitsdaten")
    assert not RisAnker.objects.exists()

    settings.WORK_RIS_RELINK = "probe"
    bericht = uhr.lauf(modus="probe")

    assert bericht.anker_neu == 2 and bericht.umgehaengt == 0
    assert sorted(RisAnker.objects.values_list("art", "objekt")) == [
        (RisAnker.ART_TOP, item.id),
        (RisAnker.ART_VORLAGE, vorlage.id),
    ]
    assert not RisAnker.objects.filter(objekt=ohne.id).exists()
    assert RisAnker.objects.get(objekt=item.id).kennung["references"] == ["v/1"]
    assert RisAnker.objects.get(objekt=vorlage.id).kennung["reference"] == "V/1"
    assert uhr.lauf(modus="probe").anker_neu == 0, "wiederholbar"
