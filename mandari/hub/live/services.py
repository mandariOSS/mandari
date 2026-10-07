# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ablauf der Live-Übertragungen (Issue #915): Zeitfenster, Zustände, Lesungen, Entprellung, Protokoll.

**Zeitfenster aus OParl.** Für jede aktive Quelle zählen die Sitzungen ihres Gremiums mit Beginn im Fenster
[jetzt − 10 h, jetzt + 45 min] (``VORLAUF``, ``FENSTER``). Für jede entsteht eine Übertragung im Zustand
``geplant``. Abgefragt wird je Quelle einmal je Minute (Zeitplan ``live_status_abfragen``) und nur für die aktuelle
Übertragung: eine laufende, sonst die zuletzt begonnene geplante, sonst eine eben beendete (Wiederaufnahme).

**Zustände.**

- ``geplant`` → ``live``, sobald der Anbieter „live“ meldet (Ereignis ``ris.broadcast.started``).
- ``live`` → ``beendet``, wenn der Anbieter das Ende meldet (``nachher``; bei 3Q die Tafel ``post`` – das Standbild
  nach dem Ende gilt damit nicht als Sitzung), wenn ``OHNE_SIGNAL`` lang keine Übertragung kommt (Ende = Beginn des
  Aussetzers) oder am Ende des Zeitfensters (Ereignis ``ris.broadcast.ended``).
- ``beendet`` → ``live``, wenn der Anbieter binnen ``WIEDERAUFNAHME`` wieder „live“ meldet (lange Pause).
- ``geplant`` → ``nicht_uebertragen``, wenn bis ``NICHT_UEBERTRAGEN_NACH`` nach dem Beginn nichts lief.

**Lesungen.** Läuft eine Übertragung, reiht der Zeitplan einen Auftrag ``live_bilder_lesen`` in die Warteschlange
``live`` ein (höchstens einen je Übertragung). Er liest ``LESEDAUER`` Sekunden lang im Takt der Quelle Einzelbilder
(nur im Speicher) und verwirft sie nach der Lesung; eine Lease verhindert, dass zwei Aufträge dieselbe Übertragung
lesen. Ein Wechsel von TOP oder Person gilt erst nach ``bestaetigungen`` gleichen Lesungen in Folge (Profil); dann
entsteht ein TOP-Abschnitt (``ris.broadcast.agenda_item_started``) bzw. eine Wortmeldung
(``ris.broadcast.speaker_changed``). Wortmeldungen haben nur einen Beginn: Redezeiten werden nicht erfasst.

**TOP-Zuordnung.** Jede Lesung wird mit Nummer und Titel einem Tagesordnungspunkt der Sitzung der Übertragung
zugeordnet (``zuordnung.top_zuordnen``: Lesarten der Nummer mit Titelprüfung). Entprellt wird über den so
bestimmten Tagesordnungspunkt (``TopTreffer.schluessel``), nicht über die gelesene Nummer: „11“ mit dem Titel von
TOP 1.1 und „1.1“ sind derselbe TOP.

**Schalter.** ``LIVE_UEBERTRAGUNG_AKTIV`` (Standard aus): aus = keine Anfragen nach außen, keine Aufträge.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from PIL import Image

from apps.events import leases
from insight_core.models import OParlMeeting

from . import ereignisse
from .anbieter import Anbieter, AnbieterError, Phase, StreamInfo, StreamStatus, anbieter
from .bezeichnungen import bekannte_fraktionen, kanonisch
from .einzelbild import einzelbild
from .lesung import Lesung, lies_bild, vereinfacht
from .models import (
    Broadcast,
    BroadcastLog,
    BroadcastSection,
    BroadcastSource,
    BroadcastSpeech,
    BroadcastStatus,
    LogKind,
    SectionOrigin,
)
from .ocr import Erkenner, OcrError, tesseract
from .profil import Einblendungsprofil, ProfilError, lade_profil
from .zuordnung import TopTreffer, abschnittsschluessel, normalisiere_nummer, person_zuordnen, top_zuordnen

logger = logging.getLogger(__name__)

VORLAUF: Final = timedelta(minutes=45)
FENSTER: Final = timedelta(hours=10)
NICHT_UEBERTRAGEN_NACH: Final = timedelta(hours=3)
OHNE_SIGNAL: Final = timedelta(minutes=15)
WIEDERAUFNAHME: Final = timedelta(hours=3)
#: So lange liest ein Auftrag (Sekunden); der Zeitplan reiht jede Minute den nächsten ein
LESEDAUER: Final = 50.0
#: Importpfad des Leseauftrags (Prüfung auf wartende Aufträge)
LESEAUFTRAG: Final = "hub.live.auftraege.live_bilder_lesen"
WARTESCHLANGE: Final = "live"
#: Länge gespeicherter Fehlertexte
_MAX_FEHLER: Final = 300

Bildquelle = Callable[[str], Image.Image]


def aktiv() -> bool:
    """Ist die Erkennung von Live-Übertragungen eingeschaltet (``LIVE_UEBERTRAGUNG_AKTIV``)?"""
    return bool(getattr(settings, "LIVE_UEBERTRAGUNG_AKTIV", False))


def protokollieren(
    source_id: uuid.UUID,
    art: str,
    daten: dict[str, Any],
    *,
    broadcast_id: uuid.UUID | None = None,
    zeit: datetime | None = None,
) -> None:
    """Ein Protokolleintrag (ohne Bilddaten)."""
    BroadcastLog.objects.create(
        source_id=source_id, broadcast_id=broadcast_id, at=zeit or timezone.now(), kind=art, data=daten
    )


def protokoll_aufraeumen(jetzt: datetime | None = None) -> int:
    """Löscht Protokolleinträge nach ``LIVE_PROTOKOLL_TAGE`` (Standard 90); liefert ihre Anzahl."""
    tage = int(getattr(settings, "LIVE_PROTOKOLL_TAGE", 90))
    grenze = (jetzt or timezone.now()) - timedelta(days=max(1, tage))
    geloescht, _ = BroadcastLog.objects.filter(at__lt=grenze).delete()
    return geloescht


# =============================================================================
# Zeitfenster und Zustände
# =============================================================================


def sitzungen_im_fenster(quelle: BroadcastSource, jetzt: datetime) -> list[OParlMeeting]:
    """Nicht abgesagte Sitzungen des Gremiums mit Beginn in [jetzt − FENSTER, jetzt + VORLAUF]."""
    return list(
        OParlMeeting.objects.filter(
            body_id=quelle.body_id,
            organizations=quelle.organization_id,
            deleted=False,
            cancelled=False,
            start__gte=jetzt - FENSTER,
            start__lte=jetzt + VORLAUF,
        )
        .order_by("start")
        .distinct()
    )


def _status_daten(status: StreamStatus) -> dict[str, Any]:
    return {
        "online": status.online,
        "phase": str(status.phase),
        "tafeltext": status.tafeltext,
        "zuschauer": status.zuschauer,
    }


def _zustand(broadcast: Broadcast, neu: str, jetzt: datetime, grund: str) -> None:
    alt = broadcast.status
    broadcast.status = neu
    protokollieren(
        broadcast.source_id,
        LogKind.ZUSTAND,
        {"von": alt, "nach": neu, "grund": grund},
        broadcast_id=broadcast.pk,
        zeit=jetzt,
    )


def _beenden(broadcast: Broadcast, ende: datetime, jetzt: datetime, grund: str) -> None:
    """Übertragung beenden: offenen Abschnitt schließen, Ereignis ``ended`` (in der laufenden Transaktion)."""
    _zustand(broadcast, BroadcastStatus.BEENDET, jetzt, grund)
    broadcast.ended_at = ende
    broadcast.offline_since = None
    broadcast.debounce = {}
    BroadcastSection.objects.filter(broadcast=broadcast, ended_at__isnull=True).update(ended_at=ende)
    ereignisse.melden(
        ereignisse.ENDED,
        broadcast_id=broadcast.pk,
        body_id=broadcast.source.body_id,
        payload={"broadcast": str(broadcast.pk), "meeting": str(broadcast.meeting_id), "reason": grund},
    )


def _beginnen(broadcast: Broadcast, jetzt: datetime, grund: str) -> None:
    _zustand(broadcast, BroadcastStatus.LIVE, jetzt, grund)
    if broadcast.started_at is None:
        broadcast.started_at = jetzt
    broadcast.ended_at = None
    broadcast.offline_since = None
    ereignisse.melden(
        ereignisse.STARTED,
        broadcast_id=broadcast.pk,
        body_id=broadcast.source.body_id,
        payload={
            "broadcast": str(broadcast.pk),
            "meeting": str(broadcast.meeting_id),
            "organization": str(broadcast.source.organization_id),
        },
    )


def uebergang(broadcast: Broadcast, status: StreamStatus, jetzt: datetime) -> None:
    """Wendet einen Status des Anbieters auf die Übertragung an (in einer Transaktion aufrufen, speichert)."""
    broadcast.last_checked_at = jetzt
    broadcast.last_status = _status_daten(status)
    start = broadcast.meeting.start or jetzt
    if status.phase == Phase.LIVE:
        if broadcast.status == BroadcastStatus.GEPLANT:
            _beginnen(broadcast, jetzt, "anbieter_live")
        elif broadcast.status == BroadcastStatus.BEENDET and (
            broadcast.ended_at is None or jetzt - broadcast.ended_at <= WIEDERAUFNAHME
        ):
            _beginnen(broadcast, jetzt, "wiederaufnahme")
        broadcast.offline_since = None
    elif status.phase == Phase.NACHHER:
        if broadcast.status == BroadcastStatus.LIVE:
            _beenden(broadcast, broadcast.offline_since or jetzt, jetzt, "anbieter")
    elif broadcast.status == BroadcastStatus.LIVE:
        # vorher/unbekannt während der Übertragung: Aussetzer oder Pause, erst nach OHNE_SIGNAL beenden
        if broadcast.offline_since is None:
            broadcast.offline_since = jetzt
        elif jetzt - broadcast.offline_since >= OHNE_SIGNAL:
            _beenden(broadcast, broadcast.offline_since, jetzt, "ohne_signal")
    if broadcast.status == BroadcastStatus.GEPLANT and jetzt - start > NICHT_UEBERTRAGEN_NACH:
        _zustand(broadcast, BroadcastStatus.NICHT_UEBERTRAGEN, jetzt, "kein_beginn")
    broadcast.save()


def _ausserhalb_abschliessen(quelle: BroadcastSource, jetzt: datetime) -> None:
    """Übertragungen, die das Zeitfenster verlassen haben: live → beendet, geplant → nicht übertragen."""
    for broadcast in Broadcast.objects.filter(
        source=quelle, status=BroadcastStatus.LIVE, meeting__start__lt=jetzt - FENSTER
    ).select_related("source", "meeting"):
        with transaction.atomic():
            gesperrt = (
                Broadcast.objects.select_for_update(of=("self",))
                .select_related("source", "meeting")
                .get(pk=broadcast.pk)
            )
            if gesperrt.status == BroadcastStatus.LIVE:
                _beenden(gesperrt, jetzt, jetzt, "zeitfenster")
                gesperrt.save()
    for broadcast in Broadcast.objects.filter(
        source=quelle, status=BroadcastStatus.GEPLANT, meeting__start__lt=jetzt - NICHT_UEBERTRAGEN_NACH
    ):
        with transaction.atomic():
            _zustand(broadcast, BroadcastStatus.NICHT_UEBERTRAGEN, jetzt, "kein_beginn")
            broadcast.save(update_fields=["status", "updated_at"])


def aktuelle_uebertragung(quelle: BroadcastSource, jetzt: datetime) -> Broadcast | None:
    """Die Übertragung, deren Status abgefragt wird (siehe Moduldokumentation)."""
    im_fenster = Broadcast.objects.filter(
        source=quelle, meeting__start__gte=jetzt - FENSTER, meeting__start__lte=jetzt + VORLAUF
    ).select_related("source", "meeting")
    laufend = im_fenster.filter(status=BroadcastStatus.LIVE).order_by("-meeting__start").first()
    if laufend is not None:
        return laufend
    geplant = im_fenster.filter(status=BroadcastStatus.GEPLANT).order_by("-meeting__start").first()
    if geplant is not None:
        return geplant
    return (
        im_fenster.filter(status=BroadcastStatus.BEENDET, ended_at__gte=jetzt - WIEDERAUFNAHME)
        .order_by("-ended_at")
        .first()
    )


def _stream(broadcast: Broadcast, adapter: Anbieter) -> StreamInfo:
    """Aufgelöster Stream der Übertragung; ohne HLS-Adresse erneut auflösen (der Anbieter nennt sie evtl. erst live)."""
    info = StreamInfo.aus_dict(broadcast.stream)
    if info is None or not info.hls_url:
        info = adapter.aufloesen(broadcast.source.identifier)
        broadcast.stream = info.als_dict()
        Broadcast.objects.filter(pk=broadcast.pk).update(stream=broadcast.stream)
    return info


def abfragen(
    quelle: BroadcastSource, jetzt: datetime | None = None, adapter: Anbieter | None = None
) -> Broadcast | None:
    """Ein Schritt des Zeitplans für eine Quelle: Übertragungen anlegen, Status abfragen, Zustand führen."""
    jetzt = jetzt or timezone.now()
    adapter = adapter or anbieter(quelle.provider)
    for sitzung in sitzungen_im_fenster(quelle, jetzt):
        Broadcast.objects.get_or_create(source=quelle, meeting=sitzung)
    _ausserhalb_abschliessen(quelle, jetzt)
    broadcast = aktuelle_uebertragung(quelle, jetzt)
    if broadcast is None:
        return None
    try:
        info = _stream(broadcast, adapter)
        status = adapter.status(info)
    except AnbieterError as fehler:
        protokollieren(
            quelle.pk,
            LogKind.FEHLER,
            {"wo": "status", "fehler": str(fehler)[:_MAX_FEHLER]},
            broadcast_id=broadcast.pk,
            zeit=jetzt,
        )
        return broadcast
    protokollieren(
        quelle.pk,
        LogKind.ABFRAGE,
        _status_daten(status) | {"roh": dict(status.roh)},
        broadcast_id=broadcast.pk,
        zeit=jetzt,
    )
    with transaction.atomic():
        gesperrt = (
            Broadcast.objects.select_for_update(of=("self",)).select_related("source", "meeting").get(pk=broadcast.pk)
        )
        uebergang(gesperrt, status, jetzt)
    if gesperrt.status == BroadcastStatus.LIVE and info.hls_url:
        einreihen(gesperrt)
    return gesperrt


def alle_abfragen(jetzt: datetime | None = None) -> int:
    """Zeitplan: jede aktive Quelle abfragen (nur mit ``LIVE_UEBERTRAGUNG_AKTIV``); Rückgabe: Zahl der Quellen."""
    if not aktiv():
        return 0
    anzahl = 0
    for quelle in BroadcastSource.objects.filter(active=True).order_by("created_at"):
        try:
            abfragen(quelle, jetzt)
        except Exception:  # noqa: BLE001 – eine Quelle darf die anderen nicht aufhalten; Fehler steht im Log
            logger.exception("Live-Übertragung: Abfrage der Quelle %s gescheitert", quelle.pk)
        anzahl += 1
    return anzahl


# =============================================================================
# Leseaufträge
# =============================================================================


def wartende_auftraege(broadcast_id: uuid.UUID) -> int:
    """Wartende oder laufende Leseaufträge dieser Übertragung."""
    from apps.events.models import Task, TaskStatus

    anzahl = 0
    for args in Task.objects.filter(
        queue=WARTESCHLANGE, task_path=LESEAUFTRAG, status__in=[TaskStatus.WARTEND, TaskStatus.LAEUFT]
    ).values_list("args", flat=True):
        werte = args.get("args") if isinstance(args, dict) else None
        if werte and str(werte[0]) == str(broadcast_id):
            anzahl += 1
    return anzahl


def einreihen(broadcast: Broadcast) -> bool:
    """Reiht einen Leseauftrag ein, wenn für die Übertragung keiner wartet oder läuft."""
    if not aktiv() or wartende_auftraege(broadcast.pk):
        return False
    from apps.events.tasks_backend import journal_backend

    from .auftraege import live_bilder_lesen

    # Immer ins Journal (Warteschlange live), auch wenn die Anwendung Aufträge sonst sofort ausführt
    journal_backend().enqueue(live_bilder_lesen, [str(broadcast.pk)], {})
    return True


@dataclass(frozen=True)
class Verarbeitung:
    """Ergebnis einer Lesung: läuft die Übertragung noch, wurde gewechselt?"""

    live: bool
    neuer_abschnitt: BroadcastSection | None = None
    neue_wortmeldung: BroadcastSpeech | None = None


def _kandidat(entprellung: dict[str, Any], art: str, wert: Any, noetig: int) -> bool:
    """Zählt gleiche Lesungen in Folge; ``True``, sobald ``noetig`` erreicht ist (Kandidat wird dann geleert)."""
    bisher = entprellung.get(art)
    anzahl = int(bisher.get("anzahl", 0)) + 1 if isinstance(bisher, dict) and bisher.get("wert") == wert else 1
    if anzahl >= noetig:
        entprellung.pop(art, None)
        return True
    entprellung[art] = {"wert": wert, "anzahl": anzahl}
    return False


def abschnitt_schluessel(abschnitt: BroadcastSection | None) -> str | None:
    """Schlüssel eines Abschnitts für die Entprellung (wie ``TopTreffer.schluessel``)."""
    if abschnitt is None:
        return None
    return abschnittsschluessel(abschnitt.agenda_item_id, abschnitt.number)


def top_wechsel(entprellung: dict[str, Any], aktuell: str | None, treffer: TopTreffer | None, noetig: int) -> bool:
    """
    Entprellung des TOP über den Schlüssel des Treffers (``aktuell``: Schlüssel des laufenden Abschnitts);
    ``True``, wenn nach ``noetig`` gleichen Lesungen ein neuer Abschnitt beginnt. Auch die Neuzuordnung nutzt das.
    """
    if treffer is None:
        return False
    if treffer.schluessel == aktuell:
        entprellung.pop("top", None)
        return False
    return _kandidat(entprellung, "top", treffer.schluessel, noetig)


def _neuer_abschnitt(
    broadcast: Broadcast,
    treffer: TopTreffer,
    titel: str | None,
    jetzt: datetime,
    origin: str = SectionOrigin.EINBLENDUNG,
) -> BroadcastSection:
    BroadcastSection.objects.filter(broadcast=broadcast, ended_at__isnull=True).update(ended_at=jetzt)
    punkt = treffer.punkt
    abschnitt = BroadcastSection.objects.create(
        broadcast=broadcast,
        agenda_item=punkt,
        number=treffer.nummer,
        number_read=treffer.gelesen,
        title_read=(titel or "")[:500],
        title_similarity=treffer.titel_aehnlichkeit,
        confidence=treffer.sicherheit,
        origin=origin,
        started_at=jetzt,
    )
    broadcast.current_section = abschnitt
    ereignisse.melden(
        ereignisse.AGENDA_ITEM_STARTED,
        broadcast_id=broadcast.pk,
        body_id=broadcast.source.body_id,
        payload={
            "broadcast": str(broadcast.pk),
            "meeting": str(broadcast.meeting_id),
            "section": str(abschnitt.pk),
            "agenda_item": str(punkt.pk) if punkt else None,
            "number": treffer.nummer,
            "origin": str(origin),
        },
    )
    return abschnitt


def _neue_wortmeldung(broadcast: Broadcast, lesung: Lesung, anzahl: int, jetzt: datetime) -> BroadcastSpeech:
    treffer = person_zuordnen(
        lesung.name or "", meeting=broadcast.meeting, organization_id=broadcast.source.organization_id
    )
    wortmeldung = BroadcastSpeech.objects.create(
        broadcast=broadcast,
        section=broadcast.current_section,
        person=treffer.person,
        name_read=(lesung.name or "")[:200],
        faction_read=(lesung.fraktion or "")[:200],
        function_read=(lesung.funktion or "")[:200],
        started_at=jetzt,
        assignment=treffer.zuordnung,
        readings=anzahl,
    )
    broadcast.current_speech = wortmeldung
    ereignisse.melden(
        ereignisse.SPEAKER_CHANGED,
        broadcast_id=broadcast.pk,
        body_id=broadcast.source.body_id,
        payload={
            "broadcast": str(broadcast.pk),
            "meeting": str(broadcast.meeting_id),
            "speech": str(wortmeldung.pk),
            "section": str(broadcast.current_section_id) if broadcast.current_section_id else None,
            "person": str(treffer.person.pk) if treffer.person else None,
            "assignment": str(treffer.zuordnung),
        },
    )
    return wortmeldung


def _personenschluessel(name: str | None, fraktion: str | None, funktion: str | None) -> list[str]:
    """Schlüssel der Entprellung in Vergleichsform: Lesarten mit und ohne Umlautzeichen gelten als gleich."""
    return [vereinfacht(name or ""), vereinfacht(fraktion or ""), vereinfacht(funktion or "")]


def lesung_verarbeiten(
    broadcast_id: uuid.UUID | str, lesung: Lesung, jetzt: datetime, profil: Einblendungsprofil
) -> Verarbeitung:
    """Entprellt eine Lesung und führt TOP-Abschnitt und Wortmeldung (eine Transaktion)."""
    # Zuordnung vor der Sperre: Die Titelprüfung vergleicht mit allen Punkten der Sitzung und dauert bei langen
    # Tagesordnungen spürbar; die Sitzung einer Übertragung ändert sich nicht
    treffer: TopTreffer | None = None
    if lesung.balken:
        meeting_id = Broadcast.objects.filter(pk=broadcast_id).values_list("meeting_id", flat=True).first()
        if meeting_id is not None:
            treffer = top_zuordnen(
                meeting_id,
                lesung.top,
                lesung.titel,
                titel_zur_nummer=profil.titel_zur_nummer,
                titel_allein=profil.titel_allein,
            )
    with transaction.atomic():
        broadcast = (
            Broadcast.objects.select_for_update(of=("self",))
            .select_related("source", "meeting", "current_section", "current_speech")
            .filter(pk=broadcast_id)
            .first()
        )
        if broadcast is None or broadcast.status != BroadcastStatus.LIVE:
            return Verarbeitung(live=False)
        broadcast.frames_read += 1
        if not lesung.balken:
            broadcast.frames_without_overlay += 1
            broadcast.save(update_fields=["frames_read", "frames_without_overlay", "updated_at"])
            return Verarbeitung(live=True)

        entprellung: dict[str, Any] = dict(broadcast.debounce or {})
        abschnitt: BroadcastSection | None = None
        wortmeldung: BroadcastSpeech | None = None

        aktuell = abschnitt_schluessel(broadcast.current_section)
        if treffer is not None and top_wechsel(entprellung, aktuell, treffer, profil.bestaetigungen):
            abschnitt = _neuer_abschnitt(broadcast, treffer, lesung.titel, jetzt)

        if lesung.name:
            if lesung.fraktion:
                # Verlesene und abgeschnittene Bezeichnungen auf bekannte abbilden, sonst entstünden Dubletten
                bekannte = bekannte_fraktionen(broadcast.source.body_id, profil.fraktionen)
                lesung = replace(lesung, fraktion=kanonisch(lesung.fraktion, bekannte))
            schluessel = _personenschluessel(lesung.name, lesung.fraktion, lesung.funktion)
            jetzt_am_wort = broadcast.current_speech
            if (
                jetzt_am_wort is not None
                and _personenschluessel(
                    jetzt_am_wort.name_read, jetzt_am_wort.faction_read, jetzt_am_wort.function_read
                )
                == schluessel
            ):
                BroadcastSpeech.objects.filter(pk=jetzt_am_wort.pk).update(readings=jetzt_am_wort.readings + 1)
                entprellung.pop("person", None)
            elif _kandidat(entprellung, "person", schluessel, profil.bestaetigungen):
                wortmeldung = _neue_wortmeldung(broadcast, lesung, profil.bestaetigungen, jetzt)

        broadcast.debounce = entprellung
        broadcast.save()
        return Verarbeitung(live=True, neuer_abschnitt=abschnitt, neue_wortmeldung=wortmeldung)


def abschnitt_von_hand(broadcast: Broadcast, nummer: str, jetzt: datetime | None = None) -> BroadcastSection:
    """TOP von Hand setzen (Verwaltung), z. B. wenn die Einblendung fehlt."""
    gesucht = normalisiere_nummer(nummer)
    if gesucht is None:
        raise ValueError("TOP-Nummer ohne Ziffern")
    with transaction.atomic():
        gesperrt = (
            Broadcast.objects.select_for_update(of=("self",)).select_related("source", "meeting").get(pk=broadcast.pk)
        )
        # Von Hand gilt die Nummer der Verwaltung: keine Lesarten, keine Titelprüfung
        treffer = top_zuordnen(gesperrt.meeting_id, gesucht, None, varianten=False)
        if treffer is None:  # nicht erreichbar: die Nummer hat Ziffern (oben geprüft)
            raise ValueError("TOP-Nummer ohne Ziffern")
        abschnitt = _neuer_abschnitt(gesperrt, treffer, None, jetzt or timezone.now(), SectionOrigin.HAND)
        gesperrt.debounce = {k: v for k, v in (gesperrt.debounce or {}).items() if k != "top"}
        gesperrt.save()
    return abschnitt


def lesen(
    broadcast_id: uuid.UUID | str,
    *,
    dauer: float = LESEDAUER,
    bildquelle: Bildquelle = einzelbild,
    erkenner: Erkenner = tesseract,
    schlafen: Callable[[float], None] = time.sleep,
    uhr: Callable[[], float] = time.monotonic,
) -> int:
    """
    Leseauftrag: ``dauer`` Sekunden lang im Takt der Quelle Einzelbilder lesen; Rückgabe: Zahl der Lesungen.

    Das Bild bleibt im Speicher und wird nach der Lesung verworfen. Ins Protokoll gehen nur Lesung, Zeiten und
    Bildgröße.
    """
    if not aktiv():
        return 0
    broadcast = Broadcast.objects.select_related("source").filter(pk=broadcast_id).first()
    if broadcast is None or broadcast.status != BroadcastStatus.LIVE:
        return 0
    info = StreamInfo.aus_dict(broadcast.stream)
    quelle = broadcast.source
    if info is None or not info.hls_url:
        return 0
    try:
        profil = lade_profil(quelle.overlay_profile)
    except ProfilError:
        protokollieren(
            quelle.pk, LogKind.FEHLER, {"wo": "profil", "fehler": "Profil ungültig"}, broadcast_id=broadcast.pk
        )
        return 0

    sperre = f"live.lesen.{broadcast.pk}"
    halter = leases.new_holder_id()
    if not leases.acquire(sperre, halter, ttl=timedelta(seconds=dauer + 30)):
        return 0
    gelesen = 0
    try:
        ende = uhr() + dauer
        takt = float(max(5, quelle.interval_seconds))
        while True:
            beginn = uhr()
            lesung: Lesung | None = None
            daten: dict[str, Any] = {}
            try:
                bild = bildquelle(info.hls_url)
                daten["ms_bild"] = int((uhr() - beginn) * 1000)
                daten["breite"], daten["hoehe"] = bild.size
                try:
                    lesung = lies_bild(bild, profil, erkenner)
                finally:
                    bild.close()
                    del bild
            except (AnbieterError, OcrError) as fehler:
                protokollieren(
                    quelle.pk,
                    LogKind.FEHLER,
                    {"wo": "bild", "fehler": str(fehler)[:_MAX_FEHLER]},
                    broadcast_id=broadcast.pk,
                )
            except Exception as fehler:  # noqa: BLE001 – ein Bild darf den Auftrag nicht beenden; Fehler im Log
                logger.exception("Live-Übertragung %s: Lesung gescheitert", broadcast.pk)
                protokollieren(
                    quelle.pk,
                    LogKind.FEHLER,
                    {"wo": "lesung", "fehler": type(fehler).__name__},
                    broadcast_id=broadcast.pk,
                )
            if lesung is not None:
                ergebnis = lesung_verarbeiten(broadcast.pk, lesung, timezone.now(), profil)
                daten["ms_gesamt"] = int((uhr() - beginn) * 1000)
                protokollieren(quelle.pk, LogKind.LESUNG, lesung.als_dict() | daten, broadcast_id=broadcast.pk)
                gelesen += 1
                if not ergebnis.live:
                    break
            naechster = beginn + takt
            if naechster >= ende:
                break
            schlafen(max(0.0, naechster - uhr()))
    finally:
        leases.release(sperre, halter)
    return gelesen
