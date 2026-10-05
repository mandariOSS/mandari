# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuveröffentlichung erhält die Verknüpfungen (Issue #547, ADR ``docs/adr/20260929-kanonisches-modell.md``).

Fraktionsnotizen, Positionen, private Notizen, Redebeiträge, Dokumente und Aufgaben an Tagesordnungspunkten sowie
Kommentare, Dokumente und Anträge an Vorlagen dürfen nicht verloren gehen, wenn ein RIS seinen Stand neu
veröffentlicht – und dürfen nie still am falschen Punkt hängen.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
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
from apps.work.ris.verknuepfungen import abgleichen
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


def _spaeter() -> Any:
    """Zeitpunkt des Abgleichs: nach der Ruhezeit aller Änderungen im Test."""
    return timezone.now() + verknuepfungen.RUHEZEIT + timedelta(minutes=1)


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

    def beraten(self, vorlage: OParlPaper, item: OParlAgendaItem) -> None:
        """Beratung je Vorlage und Sitzung (wie beim Abruf aus Sitzungsseiten): zeigt auf den heutigen Punkt."""
        OParlConsultation.objects.update_or_create(
            external_id=f"{vorlage.external_id}#consultation/{item.meeting_id}",
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


# =============================================================================
# Akzeptanz: Neuveröffentlichung erhält die Verknüpfungen
# =============================================================================


def test_loeschmarkierung_und_neuanlage_mit_vorlage(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/2026/0001")
    alt = ris.top(sitzung, "top/1", "3", "Spielplatz Nord", vorlage=vorlage)
    daten = _arbeitsdaten(org, mitglied, alt)
    vorher = {n: (o.content_encrypted, o.updated_at) for n, o in daten.items() if hasattr(o, "content_encrypted")}

    # Neu veröffentlicht: anderer Titel, andere Nummer, dieselbe Vorlage
    neu = _republiziert(ris, alt, "top/1-neu", nummer="4", name="Spielplatz Nord (geändert)", vorlage=vorlage)
    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

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


def test_neue_adressen_ohne_loeschmeldung_ohne_vorlage(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung(tagesordnung=["top/1", "top/2"])
    ris.top(sitzung, "top/1", "1", "Eröffnung")
    alt = ris.top(sitzung, "top/2", "2", "Bericht der Verwaltung")
    daten = _arbeitsdaten(org, mitglied, alt)

    # Die Quelle listet die Tagesordnung unter neuen Adressen; die alten Punkte bleiben ohne Löschmeldung stehen
    ris.top(sitzung, "top/1b", "1", "Eröffnung")
    neu = ris.top(sitzung, "top/2b", "2", "Bericht der Verwaltung")
    ris.tagesordnung(sitzung, ["top/1b", "top/2b"])
    abgleichen(modus="aktiv", jetzt=_spaeter())

    _haengt_an(daten, neu)
    assert not OParlAgendaItem.objects.get(pk=alt.pk).deleted, "Der Bestand wird nicht verändert"


def test_umnummerierung_mit_stabiler_kennung_aendert_nichts(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/7", "7", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, item)
    ris.top(sitzung, "top/dringlich", "7", "Dringlichkeitsantrag")
    OParlAgendaItem.objects.filter(pk=item.pk).update(number="8")

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    _haengt_an(daten, item)
    assert bericht.umgehaengt == 0 and not RisNeuzuordnung.objects.exists()
    assert RisAnker.objects.get(objekt=item.id).kennung["number"] == "8", "Kennung folgt dem heutigen Stand"


def test_umnummerierung_bei_nummer_in_der_adresse(org: Any, ris: Ris, mitglied: Any) -> None:
    """Eine Einfügung verschiebt die Inhalte über die Zeilen; die Daten wandern mit (Kette, eine Person)."""
    sitzung = ris.sitzung()
    vorlagen = [ris.vorlage(f"paper/{n}", f"V/{n}") for n in (1, 2, 3)]
    tops = [
        ris.top(sitzung, f"top/{n}", str(n), f"Punkt {n}", vorlage=v) for n, v in zip((5, 6), vorlagen[:2], strict=True)
    ]
    an_5 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=tops[0], author=mitglied)
    an_6 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=tops[1], author=mitglied)

    # Neuer TOP 5 vorne eingefügt: Zeile /5 trägt die neue Vorlage, /6 den bisherigen TOP 5, /7 den bisherigen TOP 6
    OParlAgendaItem.objects.filter(pk=tops[0].pk).update(name="Neuer Punkt")
    ris.beraten(vorlagen[2], tops[0])
    OParlAgendaItem.objects.filter(pk=tops[1].pk).update(name="Punkt 5")
    ris.beraten(vorlagen[0], tops[1])
    neu_7 = ris.top(sitzung, "top/7", "7", "Punkt 6", vorlage=vorlagen[1])

    abgleichen(modus="aktiv", jetzt=_spaeter())

    assert AgendaPrivateNote.objects.get(pk=an_5.pk).agenda_item_id == tops[1].id
    assert AgendaPrivateNote.objects.get(pk=an_6.pk).agenda_item_id == neu_7.id
    assert not RisAnker.objects.filter(objekt=tops[0].id).exists(), "Am neuen TOP 5 hängt nichts mehr"


def test_tausch_zweier_punkte(org: Any, ris: Ris, mitglied: Any) -> None:
    """Positionen und Notizen tauschen mit; private Notizen derselben Person an beiden bleiben – mit Hinweis."""
    sitzung = ris.sitzung()
    a, b = ris.vorlage("paper/a", "V/A"), ris.vorlage("paper/b", "V/B")
    top_1 = ris.top(sitzung, "top/1", "1", "A", vorlage=a)
    top_2 = ris.top(sitzung, "top/2", "2", "B", vorlage=b)
    pos_1 = AgendaItemPosition.objects.create(organization=org, agenda_item=top_1, position="for")
    pos_2 = AgendaItemPosition.objects.create(organization=org, agenda_item=top_2, position="against")
    notiz_1 = _verschluesselt(AgendaItemNote, organization=org, agenda_item=top_1, author=mitglied)
    privat_1 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=top_1, author=mitglied)
    privat_2 = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=top_2, author=mitglied)

    OParlAgendaItem.objects.filter(pk=top_1.pk).update(name="B")
    OParlAgendaItem.objects.filter(pk=top_2.pk).update(name="A")
    ris.beraten(b, top_1)
    ris.beraten(a, top_2)
    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    assert AgendaItemPosition.objects.get(pk=pos_1.pk).agenda_item_id == top_2.id
    assert AgendaItemPosition.objects.get(pk=pos_2.pk).agenda_item_id == top_1.id
    assert AgendaItemNote.objects.get(pk=notiz_1.pk).agenda_item_id == top_2.id
    assert AgendaPrivateNote.objects.get(pk=privat_1.pk).agenda_item_id == top_1.id
    assert AgendaPrivateNote.objects.get(pk=privat_2.pk).agenda_item_id == top_2.id
    assert bericht.konflikte == 2
    assert set(RisAnker.objects.values_list("status", flat=True)) == {RisAnker.NICHT_ZUGEORDNET}


def test_anderer_punkt_unter_alter_kennung_wird_nicht_zugeordnet(
    org: Any, ris: Ris, mitglied: Any, client_for: Any
) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/9", "9", "Bericht Klimaschutz")
    daten = _arbeitsdaten(org, mitglied, item)
    # Punkt entfällt, die Zeile trägt jetzt einen anderen Punkt
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Verschiedenes")

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    _haengt_an(daten, item)
    assert bericht.nicht_zugeordnet == 1
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.NICHT_ZUGEORDNET
    antwort = client_for(mitglied.user).get(f"/work/{org.slug}/meetings/{sitzung.id}/prepare/")
    treffer = CONFIG_RE.search(antwort.content.decode())
    assert treffer
    eintrag = json.loads(treffer.group(1))["items"][0]
    assert eintrag["withdrawn"] == "Nicht zugeordnet"
    assert "„Bericht Klimaschutz“" in eintrag["withdrawnHint"]


def test_korrektur_des_namens_bleibt_derselbe_punkt(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/4", "4", "Sanierung der Grundschule Nord")
    daten = _arbeitsdaten(org, mitglied, item)
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Sanierung der Grundschule Nord, 2. Bauabschnitt")

    abgleichen(modus="aktiv", jetzt=_spaeter())

    _haengt_an(daten, item)
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.AKTUELL


@pytest.mark.parametrize(("nummer", "eindeutig"), [("2", True), ("5", False)])
def test_mehrere_moegliche_nachfolger(org: Any, ris: Ris, mitglied: Any, nummer: str, eindeutig: bool) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/m", "2", "Mitteilungen")
    daten = _arbeitsdaten(org, mitglied, alt)
    alt.mark_deleted()
    erster = ris.top(sitzung, "top/m1", nummer, "Mitteilungen")
    ris.top(sitzung, "top/m2", "6", "Mitteilungen")
    ris.top(sitzung, "top/m3", "2", "Mitteilungen", public=False)

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    if eindeutig:
        _haengt_an(daten, erster)
    else:
        _haengt_an(daten, alt)
        assert bericht.mehrdeutig == 1
        assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.MEHRDEUTIG


def test_gegenstueck_am_nachfolger_bleibt_unangetastet(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    privat_alt = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=alt, author=mitglied)
    notiz_alt = _verschluesselt(AgendaItemNote, organization=org, agenda_item=alt, author=mitglied)
    neu = _republiziert(ris, alt, "top/1b")
    privat_neu = _verschluesselt(AgendaPrivateNote, organization=org, agenda_item=neu, author=mitglied)

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    assert AgendaPrivateNote.objects.get(pk=privat_alt.pk).agenda_item_id == alt.id, "nichts zusammenführen"
    assert AgendaPrivateNote.objects.get(pk=privat_neu.pk).agenda_item_id == neu.id
    assert AgendaItemNote.objects.get(pk=notiz_alt.pk).agenda_item_id == neu.id
    assert bericht.konflikte == 1
    assert RisNeuzuordnung.objects.get(von=alt.id).konflikte == {
        "work.AgendaPrivateNote.agenda_item": [str(privat_alt.pk)]
    }
    assert RisAnker.objects.get(objekt=alt.id).status == RisAnker.ENTFALLEN


def test_ohne_nachfolger_bleibt_alles_am_geloeschten_punkt(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/1")
    item = ris.top(sitzung, "top/1", "1", "Abgesetzt", vorlage=vorlage)
    daten = _arbeitsdaten(org, mitglied, item)
    item.mark_deleted()
    ris.top(sitzung, "top/2", "1", "Anderer Punkt", vorlage=ris.vorlage("paper/2", "V/2"))

    abgleichen(modus="aktiv", jetzt=_spaeter())

    _haengt_an(daten, item)
    assert RisAnker.objects.get(objekt=item.id).status == RisAnker.ENTFALLEN


def test_mandanten_bleiben_getrennt(org: Any, ris: Ris, mitglied: Any, make_member: Any) -> None:
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
    neu = _republiziert(ris, alt, "top/1b")

    abgleichen(modus="aktiv", jetzt=_spaeter())

    for position, organisation in ((eigene, org), (andere, andere_org)):
        frisch = AgendaItemPosition.objects.get(pk=position.pk)
        assert frisch.agenda_item_id == neu.id and frisch.organization_id == organisation.id
        protokoll = RisNeuzuordnung.objects.get(organization=organisation, von=alt.id)
        assert protokoll.verschoben == {"work.AgendaItemPosition.agenda_item": [str(position.pk)]}
    assert AgendaItemNote.objects.get(pk=andere_notiz.pk).agenda_item_id == fremd.id


def test_hinweis_nur_fuer_die_eigene_organisation(org: Any, ris: Ris, mitglied: Any, make_member: Any) -> None:
    """Ob eine andere Organisation am Punkt arbeitet, verrät der Hinweis nicht; ihre Kennung ist ihre eigene."""
    from apps.work.ris.verknuepfungen import hinweise_fuer_tops

    andere_org = cast(Any, OrganizationFactory)(name="Fraktion Andere", slug="fraktion-andere")
    anderes_mitglied = make_member(andere_org, ["meetings.prepare"], email="andere@example.org")
    item = ris.top(ris.sitzung(), "top/9", "9", "Bericht Klimaschutz")
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    OParlAgendaItem.objects.filter(pk=item.pk).update(name="Verschiedenes")
    # Die andere Organisation arbeitet erst nach der Änderung am Punkt: Ihr Anker trägt den neuen Stand
    _verschluesselt(AgendaItemNote, organization=andere_org, agenda_item=item, author=anderes_mitglied)

    abgleichen(modus="aktiv", jetzt=_spaeter())

    assert hinweise_fuer_tops(org, [item.id])[item.id].titel == "Nicht zugeordnet"
    assert hinweise_fuer_tops(andere_org, [item.id]) == {}
    assert RisAnker.objects.get(organization=andere_org).status == RisAnker.AKTUELL


def test_vorlage_neu_veroeffentlicht(org: Any, ris: Ris, mitglied: Any) -> None:
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
    OParlPaper.objects.create(external_id=f"{RIS}/body2/paper/1", body=andere_kommune, reference="V/2026/0042")

    alt.mark_deleted()
    neu = ris.vorlage("paper/1-neu", " v/2026/0042 ", "Radweg")
    abgleichen(modus="aktiv", jetzt=_spaeter())

    assert PaperComment.objects.get(pk=kommentar.pk).paper_id == neu.id
    assert AgendaSupplementaryDocument.objects.get(pk=dokument.pk).paper_id == neu.id
    assert Motion.objects.get(pk=antrag.pk).related_paper_id == neu.id
    assert list(fraktions_top.related_papers.all()) == [neu]
    assert RisNeuzuordnung.objects.get(art=RisAnker.ART_VORLAGE).nach == neu.id


# =============================================================================
# Betrieb
# =============================================================================


def test_anker_beim_verknuepfen_traegt_den_stand_von_damals(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    item = ris.top(sitzung, "top/1", "1", "Haushalt", vorlage=ris.vorlage("paper/1", "V/1"))
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)

    anker = RisAnker.objects.get(art=RisAnker.ART_TOP, objekt=item.id)
    assert anker.kennung["name"] == "haushalt" and anker.kennung["references"] == ["v/1"]
    assert anker.geprueft_am is None


def test_probe_meldet_nur(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    alt = ris.top(sitzung, "top/1", "1", "Haushalt")
    daten = _arbeitsdaten(org, mitglied, alt)
    neu = _republiziert(ris, alt, "top/1b")

    bericht = abgleichen(modus="probe", jetzt=_spaeter())

    _haengt_an(daten, alt)
    assert bericht.geplant == [("top", str(alt.id), "nachfolger", str(neu.id))]
    assert not RisNeuzuordnung.objects.exists()
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


def test_anker_ohne_verknuepfung_wird_entfernt(org: Any, ris: Ris, mitglied: Any) -> None:
    item = ris.top(ris.sitzung(), "top/1", "1", "Haushalt")
    notiz = _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    notiz.delete()

    bericht = abgleichen(modus="aktiv", jetzt=_spaeter())

    assert bericht.anker_entfernt == 1 and not RisAnker.objects.exists()


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


def test_zeitplan_ist_registriert() -> None:
    from apps.events.schedule import Every, autodiscover, registry

    autodiscover()
    eintrag = registry.get("apps.work.schedules.ris_verknuepfungen_abgleichen")
    assert eintrag is not None and eintrag.trigger == Every(timedelta(minutes=15))


# =============================================================================
# Datenmigration
# =============================================================================

MIGRATION = importlib.import_module("apps.work.migrations.0070_ris_anker_erfassen")
NACHHER = ("work", "0070_ris_anker_erfassen")
VORHER = MIGRATION.Migration.dependencies[0]


@pytest.mark.django_db(transaction=True)
def test_migration_legt_anker_fuer_den_bestand_an(org: Any, ris: Ris, mitglied: Any) -> None:
    sitzung = ris.sitzung()
    vorlage = ris.vorlage("paper/1", "V/1")
    item = ris.top(sitzung, "top/1", "1", "Haushalt", vorlage=vorlage)
    _verschluesselt(AgendaItemNote, organization=org, agenda_item=item, author=mitglied)
    _verschluesselt(PaperComment, organization=org, paper=vorlage, author=mitglied)
    ohne = ris.top(sitzung, "top/2", "2", "Ohne Arbeitsdaten")

    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        RisAnker.objects.all().delete()  # Stand vor dem Update: Verknüpfungen ohne Anker
        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        assert sorted(RisAnker.objects.values_list("art", "objekt", "kennung")) == [
            (RisAnker.ART_TOP, item.id, {}),
            (RisAnker.ART_VORLAGE, vorlage.id, {}),
        ]
        assert not RisAnker.objects.filter(objekt=ohne.id).exists()

        # Wiederholbar
        MIGRATION.anker_anlegen(executor.loader.project_state([NACHHER]).apps, None)
        assert RisAnker.objects.count() == 2

        # Der erste Abgleich erfasst die Kennung
        abgleichen(modus="aktiv", jetzt=_spaeter())
        assert RisAnker.objects.get(objekt=item.id).kennung["references"] == ["v/1"]
        assert RisAnker.objects.get(objekt=vorlage.id).kennung["reference"] == "V/1"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
