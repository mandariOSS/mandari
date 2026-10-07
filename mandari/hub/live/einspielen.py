# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokoll des lokalen Prototyps einspielen (Issue #915, Befehl ``live_protokoll_einspielen``).

Der Prototyp (Live-Wächter) schreibt je Zeile ein JSON-Objekt mit ``zeit`` (ISO 8601) und ``art``:

- ``abfrage`` (``online``, ``playout``, ``zuschauer``), ``status`` (``online``, ``playout``, ``tafel``,
  ``metadaten``, ``rest``): Statusabfragen; ``status`` nur bei Änderung.
- ``top`` (``top``, ``titel_ocr``) und ``person`` (``name``, ``fraktion``): entprellte Wechsel.
- ``roh``: jede Lesung (``top``, ``titel_ocr``, ``name``, ``fraktion`` bzw. ``balken: false``, Zeiten, Bildgröße).
- ``fehler`` (``wo``, ``fehler``), ``lebenszeichen``, ``start``, ``wartet``, ``beginnt``, ``ende``.

Daraus entstehen Übertragung, TOP-Abschnitte, Wortmeldungen und Protokolleinträge wie im Betrieb, nur ohne
Ereignisse der Datendrehscheibe (die Sitzung ist vorbei; Abonnenten sollen nicht nachträglich reagieren).
**Idempotent:** Jede Zeile trägt im Protokoll ihren Prüfwert; Abschnitte und Wortmeldungen mit gleichem Beginn
werden nicht doppelt angelegt. Bild-Adressen aus ``status`` werden verworfen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from insight_core.models import OParlMeeting

from .anbieter import ohne_bilder
from .bezeichnungen import bekannte_fraktionen, kanonisch
from .lesung import nur_text, trenne_funktion
from .models import (
    Broadcast,
    BroadcastLog,
    BroadcastSection,
    BroadcastSource,
    BroadcastSpeech,
    BroadcastStatus,
    LogKind,
)
from .profil import STANDARD_FUNKTIONEN, ProfilError, lade_profil
from .zuordnung import aehnlichkeit, normalisiere_nummer, person_zuordnen, tagesordnungspunkt


@dataclass
class Bilanz:
    zeilen: int = 0
    neu_protokoll: int = 0
    abschnitte: int = 0
    wortmeldungen: int = 0
    uebersprungen: int = 0
    fehlerhaft: int = 0
    status: str = ""
    hinweise: list[str] = field(default_factory=list)


def _zeit(wert: object) -> datetime | None:
    if not isinstance(wert, str):
        return None
    try:
        zeit = datetime.fromisoformat(wert)
    except ValueError:
        return None
    return zeit if zeit.tzinfo else timezone.make_aware(zeit)


def _phase(playout: object, online: object) -> str:
    zustand = str(playout or "").lower()
    if zustand == "post":
        return "nachher"
    if zustand == "pre":
        return "vorher"
    return "live" if online is True else "unbekannt"


def _protokoll(
    broadcast: Broadcast, zeit: datetime, art: str, daten: dict[str, Any], pruefwert: str, bilanz: Bilanz
) -> None:
    if BroadcastLog.objects.filter(broadcast=broadcast, at=zeit, kind=art, data__zeile=pruefwert).exists():
        return
    BroadcastLog.objects.create(
        source_id=broadcast.source_id, broadcast=broadcast, at=zeit, kind=art, data={**daten, "zeile": pruefwert}
    )
    bilanz.neu_protokoll += 1


def einspielen(zeilen: Iterable[str], *, meeting: OParlMeeting, quelle: BroadcastSource) -> Bilanz:
    """Spielt die Zeilen ein (eine Transaktion); liefert die Bilanz."""
    bilanz = Bilanz()
    try:
        profil = lade_profil(quelle.overlay_profile)
        funktionen, profil_fraktionen = profil.funktionen, profil.fraktionen
    except ProfilError:
        funktionen, profil_fraktionen = STANDARD_FUNKTIONEN, ()
    with transaction.atomic():
        broadcast, _ = Broadcast.objects.select_for_update().get_or_create(source=quelle, meeting=meeting)
        abschnitt = broadcast.current_section
        sprecher = broadcast.current_speech
        letzte: datetime | None = None
        for roh in zeilen:
            if not roh.strip():
                continue
            bilanz.zeilen += 1
            pruefwert = hashlib.sha256(roh.strip().encode("utf-8")).hexdigest()[:16]
            try:
                zeile = json.loads(roh)
            except ValueError:
                bilanz.fehlerhaft += 1
                continue
            zeit = _zeit(zeile.get("zeit")) if isinstance(zeile, dict) else None
            if zeit is None or not isinstance(zeile, dict):
                bilanz.fehlerhaft += 1
                continue
            letzte = zeit
            art = zeile.get("art")
            if art in ("abfrage", "status"):
                phase = _phase(zeile.get("playout"), zeile.get("online"))
                daten: dict[str, Any] = {
                    "online": zeile.get("online") is True,
                    "phase": phase,
                    "zuschauer": zeile.get("zuschauer"),
                }
                if art == "status":
                    daten["tafeltext"] = str(zeile.get("tafel") or "")[:300]
                    if isinstance(zeile.get("metadaten"), dict):
                        daten["roh"] = {"Metadata": ohne_bilder(zeile["metadaten"])}
                    _status_anwenden(broadcast, phase, zeit)
                _protokoll(broadcast, zeit, LogKind.ABFRAGE, daten, pruefwert, bilanz)
            elif art == "top":
                nummer = normalisiere_nummer(str(zeile.get("top") or ""))
                if nummer is None:
                    bilanz.uebersprungen += 1
                    continue
                titel = str(zeile.get("titel_ocr") or "")[:500]
                vorhanden = BroadcastSection.objects.filter(broadcast=broadcast, number=nummer, started_at=zeit).first()
                if vorhanden is None:
                    BroadcastSection.objects.filter(broadcast=broadcast, ended_at__isnull=True).update(ended_at=zeit)
                    punkt = tagesordnungspunkt(meeting.pk, nummer)
                    vorhanden = BroadcastSection.objects.create(
                        broadcast=broadcast,
                        agenda_item=punkt,
                        number=nummer,
                        title_read=titel,
                        title_similarity=aehnlichkeit(titel, punkt.name if punkt else None),
                        started_at=zeit,
                    )
                    bilanz.abschnitte += 1
                abschnitt = vorhanden
            elif art == "person":
                name = nur_text(str(zeile.get("name") or ""))
                if not name:
                    bilanz.uebersprungen += 1
                    continue
                fraktion, funktion = trenne_funktion(nur_text(str(zeile.get("fraktion") or "")), funktionen)
                if fraktion:
                    fraktion = kanonisch(fraktion, bekannte_fraktionen(quelle.body_id, profil_fraktionen))
                vorhanden_sprecher = BroadcastSpeech.objects.filter(
                    broadcast=broadcast, name_read=name[:200], started_at=zeit
                ).first()
                if vorhanden_sprecher is None:
                    treffer = person_zuordnen(name, meeting=meeting, organization_id=quelle.organization_id)
                    vorhanden_sprecher = BroadcastSpeech.objects.create(
                        broadcast=broadcast,
                        section=abschnitt,
                        person=treffer.person,
                        name_read=name[:200],
                        faction_read=(fraktion or "")[:200],
                        function_read=(funktion or "")[:200],
                        started_at=zeit,
                        assignment=treffer.zuordnung,
                        readings=2,
                    )
                    bilanz.wortmeldungen += 1
                sprecher = vorhanden_sprecher
            elif art == "roh":
                daten = {
                    "balken": zeile.get("balken", True) is not False,
                    "top": zeile.get("top"),
                    "titel": zeile.get("titel_ocr"),
                    "name": zeile.get("name"),
                    "fraktion": zeile.get("fraktion"),
                    "ms_bild": zeile.get("ms_bild"),
                    "ms_gesamt": zeile.get("ms_gesamt"),
                }
                groesse = zeile.get("groesse")
                if isinstance(groesse, list) and len(groesse) == 2:
                    daten["breite"], daten["hoehe"] = groesse
                _protokoll(broadcast, zeit, LogKind.LESUNG, daten, pruefwert, bilanz)
            elif art == "fehler":
                daten = {"wo": str(zeile.get("wo") or "")[:40], "fehler": str(zeile.get("fehler") or "")[:300]}
                _protokoll(broadcast, zeit, LogKind.FEHLER, daten, pruefwert, bilanz)
            else:
                bilanz.uebersprungen += 1

        broadcast.current_section = abschnitt
        broadcast.current_speech = sprecher
        if (
            broadcast.status == BroadcastStatus.LIVE
            and letzte is not None
            and timezone.now() - letzte > timedelta(hours=1)
        ):
            broadcast.status = BroadcastStatus.BEENDET
            broadcast.ended_at = broadcast.ended_at or letzte
            BroadcastSection.objects.filter(broadcast=broadcast, ended_at__isnull=True).update(ended_at=letzte)
            bilanz.hinweise.append("Ende aus der letzten Zeile übernommen")
        broadcast.save()
        bilanz.status = broadcast.status
    return bilanz


def _status_anwenden(broadcast: Broadcast, phase: str, zeit: datetime) -> None:
    """Zustand aus einer Statusänderung des Protokolls (ohne Ereignisse)."""
    if phase == "live" and broadcast.status in (BroadcastStatus.GEPLANT, BroadcastStatus.NICHT_UEBERTRAGEN):
        broadcast.status = BroadcastStatus.LIVE
        if broadcast.started_at is None or zeit < broadcast.started_at:
            broadcast.started_at = zeit
    elif phase == "nachher" and broadcast.status == BroadcastStatus.LIVE:
        broadcast.status = BroadcastStatus.BEENDET
        broadcast.ended_at = zeit
        BroadcastSection.objects.filter(broadcast=broadcast, ended_at__isnull=True).update(ended_at=zeit)
