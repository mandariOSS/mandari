# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auftrag ``zustellen``: wartende Meldungen einer Sitzung an den Protokolldienst senden (Warteschlange ``adapter``).

Der Auftrag bekommt nur die Kennung der Sitzung. Er sucht die Sitzung beim Dienst, setzt die Kennungen der TOPs
ein und sendet alle wartenden Meldungen in einem Aufruf, jede mit ``external_id`` ``mandari-session:<Kennung der
Meldung>``. Angenommene und schon bekannte Meldungen gelten als zugestellt, abgewiesene als fehlgeschlagen (Code
``abgelehnt``); ein Abstimmungsergebnis zu einem TOP, den der Dienst nicht kennt, ebenso (``top_unbekannt``).

**Wiederholung:** Ist der Dienst nicht erreichbar, überlastet oder kennt er die Sitzung noch nicht, wirft der
Auftrag; der Runner wiederholt ihn mit wachsender Wartezeit (``TASKS`` in ``settings.py``: zwölf Versuche, zusammen
etwa sechs Stunden). Nach dem letzten Versuch und bei dauerhaften Fehlern (Anmeldung abgelehnt) stehen die Zeilen auf
„fehlgeschlagen“. Ist die Anbindung inzwischen ausgeschaltet, gehen wartende Meldungen nicht mehr hinaus.

Zugestellte Meldungen einer Sitzung bleiben ``AUFBEWAHRUNG`` lang (Stand der Pause für spätere Meldungen), danach
löscht der Auftrag sie.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Final

from django.db.models import F
from django.tasks import TaskContext, task
from django.utils import timezone

from apps.session.models import SessionMeeting, SessionProtokolldienst, SessionProtokolldienstMeldung

from . import client, einstellungen

logger = logging.getLogger("apps.session.protokolldienst")

Meldung = SessionProtokolldienstMeldung

#: Meldungen je Aufruf (der Dienst nimmt bis zu 500)
BUENDEL: Final = 200
AUFBEWAHRUNG: Final = timedelta(days=30)
PREFIX: Final = "mandari-session:"


def external_id(zeile: Meldung) -> str:
    return f"{PREFIX}{zeile.pk}"


def _ereignis(zeile: Meldung, tops: dict[str, str]) -> dict[str, Any] | None:
    """Ereignis für den Dienst; ``None``, wenn ein Abstimmungsergebnis keinem TOP beim Dienst zuzuordnen ist."""
    payload = dict(zeile.payload or {})
    nummer = str(payload.get("number") or "").strip()
    if zeile.kind in (Meldung.TOP_AUFGERUFEN, Meldung.ABSTIMMUNG) and nummer in tops:
        payload["agenda_item"] = tops[nummer]
    if zeile.kind == Meldung.ABSTIMMUNG:
        if "agenda_item" not in payload:
            return None
        payload.pop("number", None)
    return {
        "type": zeile.kind,
        "occurred_at": zeile.occurred_at.isoformat(),
        "payload": payload,
        "source": client.QUELLE,
        "external_id": external_id(zeile),
    }


def _abschliessen(zeilen: list[Meldung], status: str, code: str = "") -> None:
    if not zeilen:
        return
    werte: dict[str, Any] = {"status": status, "error_code": code}
    if status == Meldung.ZUGESTELLT:
        werte["delivered_at"] = timezone.now()
    Meldung.objects.filter(pk__in=[z.pk for z in zeilen], status=Meldung.WARTEND).update(**werte)


def _stand_vermerken(konfiguration: SessionProtokolldienst, *, fehler: str = "") -> None:
    jetzt = timezone.now()
    if fehler:
        werte: dict[str, Any] = {"last_error": fehler, "last_error_at": jetzt}
    else:
        werte = {"last_delivery_at": jetzt, "last_error": ""}
    SessionProtokolldienst.objects.filter(pk=konfiguration.pk).update(**werte)


def _letzter_versuch(context: TaskContext[Any, Any]) -> bool:
    from apps.events.tasks_backend import journal_options

    optionen, _ = journal_options()
    return int(context.attempt) >= optionen.max_attempts_for(zustellen.module_path)


@task(queue_name="adapter", takes_context=True)
def zustellen(context: TaskContext[Any, Any], meeting_id: str) -> str:
    """Wartende Meldungen der Sitzung zustellen; liefert einen kurzen Code für das Ergebnis."""
    meeting = SessionMeeting.objects.select_related("tenant").filter(pk=meeting_id).first()
    if meeting is None:
        return "sitzung_fehlt"
    wartend = list(Meldung.objects.filter(meeting=meeting, status=Meldung.WARTEND).order_by("occurred_at")[:BUENDEL])
    if not wartend:
        return "nichts"
    konfiguration = einstellungen.aktiv(meeting.tenant)
    if konfiguration is None:
        _abschliessen(wartend, Meldung.FEHLGESCHLAGEN, "ausgeschaltet")
        return "ausgeschaltet"
    Meldung.objects.filter(pk__in=[z.pk for z in wartend]).update(attempts=F("attempts") + 1)
    try:
        with client.neuer_client(konfiguration) as http:
            dienst_id, tops = client.sitzung_finden(http, konfiguration, meeting)
            senden: list[tuple[Meldung, dict[str, Any]]] = []
            ohne_top: list[Meldung] = []
            for zeile in wartend:
                ereignis = _ereignis(zeile, tops)
                if ereignis is None:
                    ohne_top.append(zeile)
                else:
                    senden.append((zeile, ereignis))
            ergebnisse = (
                client.ereignisse_senden(http, konfiguration, dienst_id, [e for _, e in senden]) if senden else []
            )
    except client.ProtokolldienstError as fehler:
        _stand_vermerken(konfiguration, fehler=fehler.code)
        Meldung.objects.filter(pk__in=[z.pk for z in wartend]).update(error_code=fehler.code)
        if fehler.dauerhaft or _letzter_versuch(context):
            _abschliessen(wartend, Meldung.FEHLGESCHLAGEN, fehler.code)
            logger.warning("Protokolldienst: Meldungen der Sitzung %s nicht zugestellt (%s)", meeting.pk, fehler.code)
            if fehler.dauerhaft:
                from apps.events.tasks_backend import PermanentTaskError

                raise PermanentTaskError(fehler.code) from None
        raise

    nach_index = {int(eintrag.get("index", -1)): eintrag for eintrag in ergebnisse}
    zugestellt, abgelehnt = [], []
    for index, (zeile, _gesendet) in enumerate(senden):
        ergebnis = nach_index.get(index)
        if ergebnis is not None and not ergebnis.get("error"):
            zugestellt.append(zeile)
        else:
            abgelehnt.append(zeile)
    _abschliessen(zugestellt, Meldung.ZUGESTELLT)
    _abschliessen(abgelehnt, Meldung.FEHLGESCHLAGEN, "abgelehnt")
    _abschliessen(ohne_top, Meldung.FEHLGESCHLAGEN, "top_unbekannt")
    _stand_vermerken(konfiguration, fehler="abgelehnt" if abgelehnt else "")
    Meldung.objects.filter(
        meeting=meeting, status=Meldung.ZUGESTELLT, delivered_at__lt=timezone.now() - AUFBEWAHRUNG
    ).delete()
    # Ein Bündel voll: den Rest gleich danach
    if len(wartend) == BUENDEL:
        from .meldungen import einreihen

        einreihen(meeting.pk)
    return f"zugestellt:{len(zugestellt)}"
