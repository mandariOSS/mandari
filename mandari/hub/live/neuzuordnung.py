# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP-Abschnitte einer Übertragung neu zuordnen (Teil von Issue #47, Befehl ``live_abschnitte_neu_zuordnen``).

Grundlage sind die protokollierten Lesungen der Übertragung (``BroadcastLog`` der Art ``lesung`` mit ``top`` und
``titel``; Lesungen ohne Einblendung zählen nicht). Sie laufen in zeitlicher Folge noch einmal durch Zuordnung und
Entprellung wie im Betrieb (``zuordnung.top_zuordnen``, ``services.top_wechsel``; Bestätigungen und Schwellen aus
dem Profil der Quelle). Das ergibt die Soll-Abschnitte. Von Hand gesetzte Abschnitte bleiben fest und setzen wie im
Betrieb den laufenden TOP.

Abgleich mit den gespeicherten Abschnitten aus der Einblendung im Zeitraum der Lesungen:

- **Korrigieren:** Ein gespeicherter Abschnitt, der höchstens ``TOLERANZ`` neben einem Soll-Abschnitt beginnt,
  übernimmt dessen Zuordnung (Tagesordnungspunkt, Nummer, gelesene Nummer und Titel, Ähnlichkeit, Sicherheit). Sein
  Beginn bleibt. Ändern sich nur gelesene Nummer, Titel, Ähnlichkeit oder Sicherheit (Abschnitte von vor der
  Titelprüfung), zählt das als **ergänzt**, nicht als korrigiert.
- **Zusammenführen:** Ein gespeicherter Abschnitt ohne Soll-Abschnitt (etwa „11“ und danach „1.1“, die jetzt
  derselbe TOP sind) geht im vorigen Abschnitt auf: Der endet, wo jener endete, die Wortmeldungen wandern mit.
  Danach wird der leere Abschnitt gelöscht.
- **Ergänzen:** Ein Soll-Abschnitt ohne gespeicherten beginnt neu und teilt den laufenden.
- **Wortmeldungen** gehören danach zum Abschnitt, der bei ihrem Beginn lief. ``TOLERANZ`` gilt auch hier, weil
  Abschnitt und Wortmeldung derselben Lesung ein paar Millisekunden auseinander liegen können; zwei Lesungen liegen
  mindestens einen Takt (5 Sekunden) auseinander.
- **Enden** folgen dem nächsten Abschnitt; Pausen (Abschnitt endete vor dem nächsten) bleiben erhalten.

Abschnitte vor oder nach dem Zeitraum der Lesungen (Protokoll schon aufgeräumt) bleiben, wie sie sind. Lesungen
nach dem Ende einer beendeten Übertragung zählen nicht (im Betrieb verarbeitet sie niemand mehr). **Keine
Ereignisse:** Die Neuzuordnung korrigiert Gespeichertes; Abonnenten haben auf die ursprünglichen Ereignisse
reagiert und sollen es nicht noch einmal tun. Vor einer Änderung schreibt sie den bisherigen Stand der Abschnitte
und Wortmeldungen ins Protokoll (Art ``zustand``, Grund ``neuzuordnung``). **Idempotent:** Ein zweiter Lauf findet
nichts mehr. ``probelauf`` rechnet alles in der Transaktion durch, nennt die Änderungen und nimmt sie zurück.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from django.db import transaction
from django.utils import timezone

from .models import (
    Broadcast,
    BroadcastLog,
    BroadcastSection,
    BroadcastSpeech,
    BroadcastStatus,
    LogKind,
    SectionOrigin,
)
from .profil import TITEL_ALLEIN, TITEL_ZUR_NUMMER, ProfilError, lade_profil
from .services import abschnitt_schluessel, protokollieren, top_wechsel
from .zuordnung import TopTreffer, kandidaten, top_zuordnen

#: Abstand, bis zu dem ein gespeicherter Abschnitt als derselbe wie ein Soll-Abschnitt gilt
TOLERANZ: Final = timedelta(seconds=2)
#: Bestätigungen, wenn das Profil der Quelle ungültig ist (wie die Vorgabe des Profils)
_BESTAETIGUNGEN: Final = 2


class NeuzuordnungError(ValueError):
    """Neuzuordnung nicht möglich; die Meldung ist ein fester Text."""


@dataclass(frozen=True)
class Soll:
    """Abschnitt laut Lesungen: Beginn (Zeit der bestätigenden Lesung), Zuordnung, gelesener Titel."""

    beginn: datetime
    treffer: TopTreffer
    titel: str


@dataclass
class Ergebnis:
    """Was die Neuzuordnung geändert hat (bzw. im Probelauf ändern würde)."""

    lesungen: int = 0
    korrigiert: list[str] = field(default_factory=list)
    #: Abschnitte, bei denen nur gelesene Nummer, Titel, Ähnlichkeit oder Sicherheit nachgetragen werden
    ergaenzt: int = 0
    zusammengefuehrt: list[str] = field(default_factory=list)
    neu: list[str] = field(default_factory=list)
    wortmeldungen: int = 0
    enden: int = 0
    hinweise: list[str] = field(default_factory=list)

    @property
    def aenderungen(self) -> int:
        return (
            len(self.korrigiert)
            + self.ergaenzt
            + len(self.zusammengefuehrt)
            + len(self.neu)
            + self.wortmeldungen
            + self.enden
        )


@dataclass
class _Platz:
    """Abschnitt der neuen Folge: vorhanden (``abschnitt``) oder neu (nur ``soll``)."""

    beginn: datetime
    abschnitt: BroadcastSection | None
    soll: Soll | None
    #: Ende vor dem Abgleich mit dem nächsten Abschnitt; ``None`` = offen
    ende: datetime | None

    @property
    def nummer(self) -> str:
        if self.soll is not None:
            return self.soll.treffer.nummer
        return self.abschnitt.number if self.abschnitt is not None else ""


def _uhrzeit(zeit: datetime) -> str:
    return timezone.localtime(zeit).strftime("%H:%M:%S")


def _spaeter(a: datetime | None, b: datetime | None) -> datetime | None:
    """Späteres Ende; ``None`` (offen) ist später als jede Zeit."""
    return None if a is None or b is None else max(a, b)


def _frueher(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _zuordnung_abweichend(abschnitt: BroadcastSection, soll: Soll) -> bool:
    """Anderer Tagesordnungspunkt oder andere Nummer (korrigiert)."""
    t = soll.treffer
    return abschnitt.agenda_item_id != (t.punkt.pk if t.punkt is not None else None) or abschnitt.number != t.nummer


def _abweichend(abschnitt: BroadcastSection, soll: Soll) -> bool:
    """Irgendeine Angabe der Zuordnung weicht ab (korrigiert oder ergänzt)."""
    t = soll.treffer
    return (
        _zuordnung_abweichend(abschnitt, soll)
        or abschnitt.number_read != t.gelesen
        or abschnitt.title_read != soll.titel
        or abschnitt.title_similarity != t.titel_aehnlichkeit
        or abschnitt.confidence != t.sicherheit
    )


def _beschreibung(soll: Soll) -> str:
    t = soll.treffer
    titel = f", Titel {t.titel_aehnlichkeit:.2f}" if t.titel_aehnlichkeit is not None else ""
    gelesen = f" (gelesen {t.gelesen})" if t.gelesen != t.nummer else ""
    return f"TOP {t.nummer}{gelesen}, {t.sicherheit}{titel}"


def _soll_abschnitte(
    broadcast: Broadcast,
    lesungen: list[tuple[datetime, dict[str, Any]]],
    abschnitte: list[BroadcastSection],
    von: datetime,
    ergebnis: Ergebnis,
) -> list[Soll]:
    """Lesungen noch einmal durch Zuordnung und Entprellung (wie ``services.lesung_verarbeiten``)."""
    noetig, zur_nummer, allein = _BESTAETIGUNGEN, TITEL_ZUR_NUMMER, TITEL_ALLEIN
    try:
        profil = lade_profil(broadcast.source.overlay_profile)
        noetig, zur_nummer, allein = profil.bestaetigungen, profil.titel_zur_nummer, profil.titel_allein
    except ProfilError:
        ergebnis.hinweise.append("Profil der Quelle ungültig: Vorgaben für Bestätigungen und Schwellen")
    punkte = kandidaten(broadcast.meeting_id)
    vorher = [a for a in abschnitte if a.started_at < von]
    aktuell = abschnitt_schluessel(vorher[-1]) if vorher else None
    hand = [a for a in abschnitte if a.origin == SectionOrigin.HAND and a.started_at >= von]
    entprellung: dict[str, Any] = {}
    soll: list[Soll] = []
    # Dieselbe Lesung kommt oft viele Male vor; die Titelprüfung je Lesart nur einmal rechnen
    bekannt: dict[tuple[str, str | None], TopTreffer | None] = {}
    for zeit, daten in lesungen:
        while hand and hand[0].started_at <= zeit:
            # von Hand gesetzt: gilt wie im Betrieb als laufender TOP
            aktuell = abschnitt_schluessel(hand.pop(0))
            entprellung.pop("top", None)
        titel = daten.get("titel") if isinstance(daten.get("titel"), str) else None
        schluessel = (str(daten.get("top") or ""), titel)
        if schluessel not in bekannt:
            bekannt[schluessel] = top_zuordnen(
                broadcast.meeting_id,
                schluessel[0],
                titel,
                titel_zur_nummer=zur_nummer,
                titel_allein=allein,
                punkte=punkte,
            )
        treffer = bekannt[schluessel]
        if treffer is not None and top_wechsel(entprellung, aktuell, treffer, noetig):
            soll.append(Soll(zeit, treffer, (titel or "")[:500]))
            aktuell = treffer.schluessel
    return soll


def _vorher_protokollieren(
    broadcast: Broadcast, abschnitte: list[BroadcastSection], sprecher: list[BroadcastSpeech]
) -> None:
    protokollieren(
        broadcast.source_id,
        LogKind.ZUSTAND,
        {
            "von": broadcast.status,
            "nach": broadcast.status,
            "grund": "neuzuordnung",
            "abschnitte_vorher": [
                {
                    "id": str(a.pk),
                    "beginn": a.started_at.isoformat(),
                    "ende": a.ended_at.isoformat() if a.ended_at else None,
                    "nummer": a.number,
                    "nummer_gelesen": a.number_read,
                    "tagesordnungspunkt": str(a.agenda_item_id) if a.agenda_item_id else None,
                    "titel_gelesen": a.title_read,
                    "titel_aehnlichkeit": a.title_similarity,
                    "sicherheit": a.confidence,
                    "herkunft": a.origin,
                }
                for a in abschnitte
            ],
            "wortmeldungen_vorher": {str(s.pk): str(s.section_id) if s.section_id else None for s in sprecher},
        },
        broadcast_id=broadcast.pk,
    )


def _neu_zuordnen(broadcast: Broadcast) -> Ergebnis:
    ergebnis = Ergebnis()
    protokoll = BroadcastLog.objects.filter(broadcast=broadcast, kind=LogKind.LESUNG)
    if broadcast.status != BroadcastStatus.LIVE and broadcast.ended_at is not None:
        protokoll = protokoll.filter(at__lte=broadcast.ended_at)
    lesungen = [
        (zeit, daten)
        for zeit, daten in protokoll.order_by("at", "id").values_list("at", "data")
        if isinstance(daten, dict) and daten.get("balken", True) is not False
    ]
    ergebnis.lesungen = len(lesungen)
    if not lesungen:
        ergebnis.hinweise.append("Keine protokollierten Lesungen mit Einblendung: nichts zu tun")
        return ergebnis
    von, bis = lesungen[0][0] - TOLERANZ, lesungen[-1][0] + TOLERANZ
    abschnitte = list(broadcast.sections.order_by("started_at", "id"))
    soll = _soll_abschnitte(broadcast, lesungen, abschnitte, von, ergebnis)

    # Abgleich: je Soll-Abschnitt der nächstgelegene gespeicherte aus der Einblendung im Zeitraum
    frei = [a for a in abschnitte if a.origin != SectionOrigin.HAND and von <= a.started_at <= bis]
    zugeordnet: dict[uuid.UUID, Soll] = {}
    neue: list[Soll] = []
    for ziel_soll in soll:
        nahe = [a for a in frei if abs(a.started_at - ziel_soll.beginn) <= TOLERANZ]
        if not nahe:
            neue.append(ziel_soll)
            continue
        gewaehlt = min(nahe, key=lambda a: abs(a.started_at - ziel_soll.beginn))
        frei.remove(gewaehlt)
        zugeordnet[gewaehlt.pk] = ziel_soll
    entfernt = frei
    entfernt_ids = {a.pk for a in entfernt}

    # Neue Folge der Abschnitte mit vorläufigen Enden
    plaetze = [
        _Platz(a.started_at, a, zugeordnet.get(a.pk), a.ended_at) for a in abschnitte if a.pk not in entfernt_ids
    ]
    for ziel_soll in neue:
        enthaltend = [a for a in abschnitte if a.started_at <= ziel_soll.beginn]
        ende: datetime | None = None
        if enthaltend and (enthaltend[-1].ended_at is None or enthaltend[-1].ended_at > ziel_soll.beginn):
            ende = enthaltend[-1].ended_at  # teilt den laufenden Abschnitt
        elif broadcast.status != BroadcastStatus.LIVE:
            ende = broadcast.ended_at
        plaetze.append(_Platz(ziel_soll.beginn, None, ziel_soll, ende))
    plaetze.sort(key=lambda p: p.beginn)
    for a in entfernt:
        aufnehmend = [p for p in plaetze if p.beginn <= a.started_at]
        if aufnehmend:
            platz = aufnehmend[-1]
            platz.ende = _spaeter(platz.ende, a.ended_at)
            ergebnis.zusammengefuehrt.append(
                f"{_uhrzeit(a.started_at)} TOP {a.number} geht in {_uhrzeit(platz.beginn)} TOP {platz.nummer} auf"
            )
        else:
            ergebnis.zusammengefuehrt.append(f"{_uhrzeit(a.started_at)} TOP {a.number} entfällt (vor dem ersten TOP)")
    enden = [_frueher(p.ende, plaetze[i + 1].beginn) if i + 1 < len(plaetze) else p.ende for i, p in enumerate(plaetze)]

    for a in abschnitte:
        passend = zugeordnet.get(a.pk)
        if passend is None or not _abweichend(a, passend):
            continue
        if _zuordnung_abweichend(a, passend):
            ergebnis.korrigiert.append(f"{_uhrzeit(a.started_at)} TOP {a.number} → {_beschreibung(passend)}")
        else:
            ergebnis.ergaenzt += 1
    for ziel_soll in neue:
        ergebnis.neu.append(f"{_uhrzeit(ziel_soll.beginn)} {_beschreibung(ziel_soll)}")
    ergebnis.enden = sum(
        1 for p, ende in zip(plaetze, enden, strict=True) if p.abschnitt is not None and p.abschnitt.ended_at != ende
    )

    # Wortmeldungen: zum Abschnitt, der bei ihrem Beginn lief
    sprecher = list(broadcast.speeches.order_by("started_at", "id"))

    def ziel_von(beginn: datetime) -> int | None:
        laufend = [i for i, p in enumerate(plaetze) if p.beginn - TOLERANZ <= beginn]
        return laufend[-1] if laufend else None

    ziele = [ziel_von(w.started_at) for w in sprecher]

    def ist_ziel(w: BroadcastSpeech, index: int | None) -> bool:
        if index is None:
            return w.section_id is None
        abschnitt = plaetze[index].abschnitt
        return abschnitt is not None and w.section_id == abschnitt.pk

    ergebnis.wortmeldungen = sum(1 for w, i in zip(sprecher, ziele, strict=True) if not ist_ziel(w, i))
    if not ergebnis.aenderungen:
        return ergebnis

    # Anwenden (ohne Ereignisse), vorher den alten Stand ins Protokoll
    _vorher_protokollieren(broadcast, abschnitte, sprecher)
    for a in abschnitte:
        passend = zugeordnet.get(a.pk)
        if passend is None or not _abweichend(a, passend):
            continue
        t = passend.treffer
        a.agenda_item = t.punkt
        a.number, a.number_read, a.title_read = t.nummer, t.gelesen, passend.titel
        a.title_similarity, a.confidence = t.titel_aehnlichkeit, t.sicherheit
        a.save(update_fields=["agenda_item", "number", "number_read", "title_read", "title_similarity", "confidence"])
    for p in plaetze:
        if p.abschnitt is None and p.soll is not None:
            t = p.soll.treffer
            p.abschnitt = BroadcastSection.objects.create(
                broadcast=broadcast,
                agenda_item=t.punkt,
                number=t.nummer,
                number_read=t.gelesen,
                title_read=p.soll.titel,
                title_similarity=t.titel_aehnlichkeit,
                confidence=t.sicherheit,
                origin=SectionOrigin.EINBLENDUNG,
                started_at=p.beginn,
            )
    for w, i in zip(sprecher, ziele, strict=True):
        ziel = plaetze[i].abschnitt if i is not None else None
        ziel_id = ziel.pk if ziel is not None else None
        if w.section_id != ziel_id:
            BroadcastSpeech.objects.filter(pk=w.pk).update(section=ziel)
    BroadcastSection.objects.filter(pk__in=entfernt_ids).delete()
    for p, ende in zip(plaetze, enden, strict=True):
        if p.abschnitt is not None and p.abschnitt.ended_at != ende:
            BroadcastSection.objects.filter(pk=p.abschnitt.pk).update(ended_at=ende)
    # Laufender TOP ist wie im Betrieb der zuletzt begonnene Abschnitt
    letzter = plaetze[-1].abschnitt if plaetze else None
    if letzter is not None and broadcast.current_section_id != letzter.pk:
        Broadcast.objects.filter(pk=broadcast.pk).update(current_section=letzter)
    return ergebnis


def neu_zuordnen(broadcast_id: uuid.UUID | str, *, probelauf: bool = False) -> Ergebnis:
    """Ordnet die TOP-Abschnitte der Übertragung neu zu (siehe Moduldokumentation); wirft ``NeuzuordnungError``."""
    with transaction.atomic():
        broadcast = (
            Broadcast.objects.select_for_update(of=("self",))
            .select_related("source", "meeting")
            .filter(pk=broadcast_id)
            .first()
        )
        if broadcast is None:
            raise NeuzuordnungError("Übertragung nicht gefunden")
        ergebnis = _neu_zuordnen(broadcast)
        if probelauf:
            transaction.set_rollback(True)
    return ergebnis
