# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``live_abschnitte_neu_zuordnen`` (Teil von #47): gespeicherte TOP-Abschnitte anhand der protokollierten
Lesungen mit der Titelprüfung neu zuordnen. Probelauf ändert nichts, Ausführung korrigiert, führt zusammen, ergänzt
und hängt Wortmeldungen um, ohne Ereignisse; ein zweiter Lauf findet nichts mehr.

Ausgangslage wie vor der Korrektur (erfundene Tagesordnung ``TAGESORDNUNG``): Die Texterkennung las „TOP 1.1“ als
„11“, die alte Zuordnung nahm TOP 11. Später las sie den Punkt mit und legte einen zweiten Abschnitt für 1.1 an.
„12“ und „1.2“ wechselten sich ab, sodass der Wechsel zu 1.2 nie bestätigt wurde.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.events.models import Event
from hub.live.models import (
    Broadcast,
    BroadcastLog,
    BroadcastSection,
    BroadcastSpeech,
    BroadcastStatus,
    LogKind,
    SectionConfidence,
    SectionOrigin,
)
from hub.live.tests.conftest import TAGESORDNUNG, Tagesordnung, Welt

pytestmark = pytest.mark.django_db

#: Lesungen (Sekunde, gelesene Nummer, Titel von TOP …): so liest die Texterkennung die Einblendung
LESUNGEN: list[tuple[int, str, str]] = [
    (0, "11", "1.1"),
    (10, "11", "1.1"),
    (20, "11", "1.1"),
    (30, "1.1", "1.1"),
    (40, "1.1", "1.1"),
    (50, "1.1", "1.1"),
    (60, "12", "1.2"),
    (70, "1.2", "1.2"),
    (80, "12", "1.2"),
    (90, "2", "2"),
    (100, "2", "2"),
]


@dataclass
class Alt:
    """Stand vor der Korrektur: Abschnitte und Wortmeldungen."""

    beginn: datetime
    top11: BroadcastSection
    top11_punkt: BroadcastSection
    top2: BroadcastSection
    wortmeldungen: list[BroadcastSpeech]


@pytest.fixture
def alt(welt: Welt, tagesordnung: Tagesordnung) -> Alt:
    t = tagesordnung
    beginn = welt.jetzt - timedelta(hours=1)

    def zeit(sekunde: float) -> datetime:
        return beginn + timedelta(seconds=sekunde)

    for sekunde, top, titel_von in LESUNGEN:
        # das Protokoll entsteht nach der Verarbeitung, ein paar Millisekunden nach dem Abschnitt
        BroadcastLog.objects.create(
            source=welt.quelle,
            broadcast=t.broadcast,
            at=zeit(sekunde + 0.3),
            kind=LogKind.LESUNG,
            data={"balken": True, "top": top, "titel": TAGESORDNUNG[titel_von], "name": None},
        )
    BroadcastLog.objects.create(
        source=welt.quelle, broadcast=t.broadcast, at=zeit(95), kind=LogKind.LESUNG, data={"balken": False}
    )

    def abschnitt(sekunde: int, ende: int | None, nummer: str) -> BroadcastSection:
        return BroadcastSection.objects.create(
            broadcast=t.broadcast,
            agenda_item=t.punkte[nummer],
            number=nummer,
            title_read=TAGESORDNUNG["1.1"] if nummer != "2" else TAGESORDNUNG["2"],
            title_similarity=0.3 if nummer == "11" else 1.0,
            started_at=zeit(sekunde),
            ended_at=zeit(ende) if ende is not None else None,
        )

    top11 = abschnitt(10, 40, "11")
    top11_punkt = abschnitt(40, 100, "1.1")
    top2 = abschnitt(100, None, "2")
    wortmeldungen = [
        BroadcastSpeech.objects.create(
            broadcast=t.broadcast, section=bereich, name_read=name, started_at=zeit(sekunde), readings=2
        )
        for sekunde, bereich, name in (
            (10, top11, "Erika Muster"),
            (50, top11_punkt, "Max Beispiel"),
            (85, top11_punkt, "Gisela Gast"),
            (100, top2, "Paula Probe"),
        )
    ]
    t.broadcast.current_section = top2
    t.broadcast.save()
    return Alt(beginn, top11, top11_punkt, top2, wortmeldungen)


def _befehl(broadcast: Broadcast, *zusatz: str) -> str:
    ausgabe = StringIO()
    call_command("live_abschnitte_neu_zuordnen", "--uebertragung", str(broadcast.pk), *zusatz, stdout=ausgabe)
    return ausgabe.getvalue()


def _stand(broadcast: Broadcast) -> list[tuple[str | None, str, datetime, datetime | None]]:
    return [
        (a.agenda_item.number if a.agenda_item else None, a.number, a.started_at, a.ended_at)
        for a in BroadcastSection.objects.filter(broadcast=broadcast).select_related("agenda_item")
    ]


def test_probelauf_aendert_nichts(tagesordnung: Tagesordnung, alt: Alt) -> None:
    vorher = _stand(tagesordnung.broadcast)
    ausgabe = _befehl(tagesordnung.broadcast, "--probelauf")
    assert "Probelauf, nichts gespeichert: 11 Lesungen" in ausgabe
    # TOP 11 → 1.1 korrigiert; TOP 2 war richtig und bekommt nur gelesene Nummer und Sicherheit (ergänzt)
    assert "1 Abschnitte korrigiert, 1 ergänzt, 1 zusammengeführt, 1 neu, 2 Wortmeldungen umgehängt" in ausgabe
    assert "TOP 11 → TOP 1.1 (gelesen 11), nummer_titel" in ausgabe
    assert "TOP 1.1 geht in" in ausgabe and "neu: " in ausgabe and "TOP 1.2, nummer_titel, Titel 1.00" in ausgabe
    assert _stand(tagesordnung.broadcast) == vorher
    assert [w.section_id for w in BroadcastSpeech.objects.order_by("started_at")] == [
        w.section_id for w in alt.wortmeldungen
    ]
    assert not BroadcastLog.objects.filter(kind=LogKind.ZUSTAND).exists()


def test_neuzuordnung_korrigiert_fuehrt_zusammen_und_haengt_wortmeldungen_um(
    tagesordnung: Tagesordnung, alt: Alt
) -> None:
    t = tagesordnung
    ereignisse = Event.objects.count()
    ausgabe = _befehl(t.broadcast)
    assert ausgabe.startswith("Gespeichert: 11 Lesungen")

    abschnitte = list(BroadcastSection.objects.filter(broadcast=t.broadcast).order_by("started_at"))
    assert [(a.agenda_item, a.number) for a in abschnitte] == [
        (t.punkte["1.1"], "1.1"),
        (t.punkte["1.2"], "1.2"),
        (t.punkte["2"], "2"),
    ]
    erster, zweiter, dritter = abschnitte
    assert erster.pk == alt.top11.pk and erster.started_at == alt.beginn + timedelta(seconds=10), "Beginn bleibt"
    assert (erster.number_read, erster.confidence) == ("11", SectionConfidence.NUMMER_UND_TITEL)
    assert (erster.title_similarity or 0) >= 0.95
    assert erster.ended_at == zweiter.started_at == alt.beginn + timedelta(seconds=70.3)
    assert zweiter.ended_at == dritter.started_at and dritter.pk == alt.top2.pk and dritter.ended_at is None
    assert (dritter.number_read, dritter.confidence) == ("2", SectionConfidence.NUMMER_UND_TITEL), "ergänzt"
    assert (zweiter.origin, zweiter.number_read) == (SectionOrigin.EINBLENDUNG, "1.2")
    assert not BroadcastSection.objects.filter(pk=alt.top11_punkt.pk).exists(), "in TOP 1.1 aufgegangen"

    zuordnung = {w.name_read: w.section_id for w in BroadcastSpeech.objects.all()}
    assert zuordnung == {
        "Erika Muster": erster.pk,
        "Max Beispiel": erster.pk,
        "Gisela Gast": zweiter.pk,
        "Paula Probe": dritter.pk,
    }
    t.broadcast.refresh_from_db()
    assert t.broadcast.current_section_id == dritter.pk
    assert Event.objects.count() == ereignisse, "keine Ereignisse beim Neuzuordnen"
    vorher = BroadcastLog.objects.get(kind=LogKind.ZUSTAND, data__grund="neuzuordnung")
    assert len(vorher.data["abschnitte_vorher"]) == 3, "alter Stand im Protokoll"
    alt_top11 = next(a for a in vorher.data["abschnitte_vorher"] if a["id"] == str(alt.top11.pk))
    assert (alt_top11["nummer"], alt_top11["tagesordnungspunkt"]) == ("11", str(t.punkte["11"].pk))
    assert (alt_top11["titel_gelesen"], alt_top11["titel_aehnlichkeit"]) == (TAGESORDNUNG["1.1"], 0.3)
    assert vorher.data["wortmeldungen_vorher"][str(alt.wortmeldungen[1].pk)] == str(alt.top11_punkt.pk)


def test_neuzuordnung_ist_idempotent(tagesordnung: Tagesordnung, alt: Alt) -> None:
    _befehl(tagesordnung.broadcast)
    stand = _stand(tagesordnung.broadcast)
    zweite = _befehl(tagesordnung.broadcast)
    assert "0 Abschnitte korrigiert, 0 ergänzt, 0 zusammengeführt, 0 neu, 0 Wortmeldungen umgehängt, 0 Enden" in zweite
    assert "Keine Änderungen." in zweite
    assert _stand(tagesordnung.broadcast) == stand
    assert BroadcastLog.objects.filter(kind=LogKind.ZUSTAND).count() == 1


def test_beendete_uebertragung_und_hand_abschnitt_bleiben(welt: Welt, tagesordnung: Tagesordnung, alt: Alt) -> None:
    """Von Hand gesetzte Abschnitte bleiben; das Ende der Übertragung bleibt das Ende des letzten Abschnitts."""
    t = tagesordnung
    ende = alt.beginn + timedelta(seconds=120)
    t.broadcast.status, t.broadcast.ended_at = BroadcastStatus.BEENDET, ende
    t.broadcast.save()
    BroadcastSection.objects.filter(pk=alt.top2.pk).update(ended_at=ende)
    hand = BroadcastSection.objects.create(
        broadcast=t.broadcast,
        agenda_item=t.punkte["11"],
        number="11",
        origin=SectionOrigin.HAND,
        started_at=alt.beginn + timedelta(seconds=5),
        ended_at=alt.beginn + timedelta(seconds=10),
    )
    # Lesungen nach dem Ende verarbeitet im Betrieb niemand mehr: Sie legen keinen Abschnitt an
    for sekunde in (125, 130):
        BroadcastLog.objects.create(
            source=welt.quelle,
            broadcast=t.broadcast,
            at=alt.beginn + timedelta(seconds=sekunde),
            kind=LogKind.LESUNG,
            data={"balken": True, "top": "11", "titel": TAGESORDNUNG["11"]},
        )
    _befehl(t.broadcast)
    assert not BroadcastSection.objects.filter(broadcast=t.broadcast, started_at__gt=ende).exists()
    hand.refresh_from_db()
    assert (hand.agenda_item, hand.number, hand.origin) == (t.punkte["11"], "11", SectionOrigin.HAND)
    letzter = BroadcastSection.objects.filter(broadcast=t.broadcast).order_by("started_at").last()
    assert letzter is not None and letzter.ended_at == ende


def test_ohne_lesungen_und_fehler(tagesordnung: Tagesordnung) -> None:
    ausgabe = _befehl(tagesordnung.broadcast, "--probelauf")
    assert "Keine protokollierten Lesungen" in ausgabe and "Keine Änderungen." in ausgabe
    with pytest.raises(CommandError, match="UUID"):
        call_command("live_abschnitte_neu_zuordnen", "--uebertragung", "keine-uuid")
    with pytest.raises(CommandError, match="nicht gefunden"):
        call_command("live_abschnitte_neu_zuordnen", "--uebertragung", "00000000-0000-0000-0000-000000000000")
