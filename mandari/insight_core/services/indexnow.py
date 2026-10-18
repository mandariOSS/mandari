# SPDX-License-Identifier: AGPL-3.0-or-later
"""
IndexNow (https://www.indexnow.org/, Issue #939): meldet neue, geänderte und entfernte Seiten des Bürgerportals sofort
an Bing, Yandex, Seznam, Naver und Yep, statt auf den nächsten Besuch des Crawlers zu warten. Google nimmt nicht teil;
dort bleibt es bei den Sitemaps.

**Auslöser** sind die Ereignisse der Datendrehscheibe (Abonnement ``insight.indexnow``, ``insight_core.subscribers``):
Vorgänge, Sitzungen, Gremien und Personen über ihr eigenes Ereignis, dazu die Seite des Vorgangs bzw. der Sitzung,
wenn sich eine Datei, Beratung oder ein Tagesordnungspunkt daran ändert (``paper``/``meeting`` in der Nutzlast).
Rücknahmen (``ris.object.depublished``) melden die entfallene Adresse. Es zählen nur öffentliche Ereignisse gelisteter
Kommunen, deren Veröffentlichung weder pausiert noch beendet ist, wie in den Sitemaps; Personen nur mit laufender
Mitgliedschaft (sonst ``noindex``).

**Schlüssel** ``INDEXNOW_KEY`` (8–128 Zeichen aus Buchstaben, Ziffern und Bindestrich): Die Suchmaschine liest ihn
unter ``/insight/<schlüssel>.txt`` nach (``views.sitemap.indexnow_schluessel``). Die Datei liegt unter ``/insight/``,
damit die Anwendung ohne die Website auskommt; sie berechtigt damit genau die Adressen unter ``/insight/``. Ohne
Schlüssel ist alles aus: keine Datei, kein Abonnement.

**Fehler:** Die Meldung ist ein Hinweis, kein Auftrag; der Weg zu den Suchmaschinen bleiben die Sitemaps. Ist die
Suchmaschine nicht erreichbar, überlastet (429), gestört (5xx) oder lehnt sie ab (400, 403, 422), steht das im Log, und
das Abonnement geht weiter. Es wartet bewusst nicht (``TargetUnavailableError``): Ein Rückstand über fünf Minuten
meldet der Worker als Störung, und ein dauerhaftes 429 hielte das Abonnement sonst ohne Ende an.

**Doppelte Zustellung** (mindestens einmal) meldet dieselben Adressen noch einmal; das schadet nicht.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from typing import Any, Final

import httpx
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

NAME: Final = "insight.indexnow"
TYPES: Final = (
    "ris.paper.*",
    "ris.meeting.*",
    "ris.organization.*",
    "ris.person.changed",
    "ris.file.changed",
    "ris.consultation.*",
    "ris.agendaitem.*",
    "ris.object.depublished",
)
#: Ereignisse je Batch; eine Meldung trägt höchstens ``ADRESSEN_JE_MELDUNG`` Adressen
BATCH: Final = 1000
#: Warteschlange für Fremdsysteme (läuft im Dienst ``worker``)
QUEUE: Final = "adapter"

#: Erlaubte Schlüssel laut Protokoll
SCHLUESSEL_MUSTER: Final = re.compile(r"[A-Za-z0-9-]{8,128}")
#: Höchstens so viele Adressen je Meldung (Grenze des Protokolls)
ADRESSEN_JE_MELDUNG: Final = 10_000
USER_AGENT: Final = "mandari (+https://mandari.de)"
TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)

#: Seite je Objekttyp (``aggregate_type``), wie in den Sitemaps
PFADE: Final = {
    "Paper": "/insight/vorgaenge/",
    "Meeting": "/insight/termine/",
    "Organization": "/insight/gremien/",
    "Person": "/insight/personen/",
}
#: Nutzlastfelder, die auf die Seite eines Vorgangs bzw. einer Sitzung zeigen (Datei, Beratung, Tagesordnungspunkt)
BEZUEGE: Final = (("paper", "Paper"), ("meeting", "Meeting"), ("previous_meeting", "Meeting"))
_OEFFENTLICH: Final = "oeffentlich"


def schluessel() -> str:
    """Der Schlüssel aus ``INDEXNOW_KEY``, wenn er dem Protokoll genügt, sonst ``""`` (IndexNow aus)."""
    wert = (getattr(settings, "INDEXNOW_KEY", "") or "").strip()
    return wert if SCHLUESSEL_MUSTER.fullmatch(wert) else ""


def _basis() -> str:
    return str(getattr(settings, "SITE_URL", "") or "https://mandari.de").rstrip("/")


def schluessel_adresse() -> str:
    """Adresse der Schlüsseldatei (``keyLocation``)."""
    return f"{_basis()}/insight/{schluessel()}.txt"


def melden(adressen: Iterable[str]) -> int:
    """
    Meldet ``adressen`` (absolute Adressen unter ``SITE_URL/insight/``) an ``INDEXNOW_ENDPOINT``; gibt zurück, wie
    viele die Suchmaschine angenommen hat (Antwort 200 oder 202).

    Wirft nicht: Netzfehler und jede andere Antwort stehen im Log (siehe Moduldokumentation, „Fehler“).
    """
    aktuell = schluessel()
    bereich = f"{_basis()}/insight/"
    liste = list(dict.fromkeys(a for a in adressen if a.startswith(bereich)))
    if not aktuell or not liste:
        return 0
    angenommen = 0
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False) as client:
        for start in range(0, len(liste), ADRESSEN_JE_MELDUNG):
            teil = liste[start : start + ADRESSEN_JE_MELDUNG]
            daten = {
                "host": httpx.URL(_basis()).host,
                "key": aktuell,
                "keyLocation": schluessel_adresse(),
                "urlList": teil,
            }
            try:
                antwort = client.post(settings.INDEXNOW_ENDPOINT, json=daten)
            except httpx.HTTPError as fehler:
                logger.warning("IndexNow: %d Adressen nicht gemeldet (%s)", len(teil), type(fehler).__name__)
                continue
            if antwort.status_code in (200, 202):
                angenommen += len(teil)
                logger.info("IndexNow: %d Adressen gemeldet (Antwort %d)", len(teil), antwort.status_code)
            else:
                # 400 Format, 403 Schlüsseldatei nicht gefunden oder falsch, 422 fremde Adressen, 429 zu viele
                # Meldungen, 5xx Störung der Suchmaschine
                logger.warning("IndexNow: %d Adressen nicht angenommen (Antwort %d)", len(teil), antwort.status_code)
    return angenommen


def _uuid(wert: Any) -> uuid.UUID | None:
    if isinstance(wert, uuid.UUID):
        return wert
    try:
        return uuid.UUID(str(wert))
    except (TypeError, ValueError):
        return None


def _freigegebene_kommunen() -> set[str]:
    """Gelistete Kommunen, deren Veröffentlichung weder pausiert noch beendet ist (wie im Sitemap-Index)."""
    from .. import publication
    from ..models import OParlBody

    freigegeben = set()
    for body_id in OParlBody.objects.listed().values_list("id", flat=True):
        stand = publication.body_state(str(body_id))
        if stand is None or not (stand.paused or stand.withdrawn):
            freigegeben.add(str(body_id))
    return freigegeben


def seiten(ereignisse: Iterable[Any]) -> list[str]:
    """Adressen der Seiten, die sich mit ``ereignisse`` geändert haben, jede einmal (Reihenfolge der Ereignisse)."""
    kommunen: set[str] | None = None
    gefunden: dict[tuple[str, uuid.UUID], None] = {}
    for ereignis in ereignisse:
        if str(ereignis.visibility) != _OEFFENTLICH or ereignis.body_id is None:
            continue
        if kommunen is None:
            kommunen = _freigegebene_kommunen()
        if str(ereignis.body_id) not in kommunen:
            continue
        if ereignis.aggregate_type in PFADE and ereignis.aggregate_id is not None:
            gefunden.setdefault((ereignis.aggregate_type, ereignis.aggregate_id), None)
        nutzlast = ereignis.payload if isinstance(ereignis.payload, dict) else {}
        for feld, art in BEZUEGE:
            kennung = _uuid(nutzlast.get(feld))
            if kennung is not None:
                gefunden.setdefault((art, kennung), None)
    ohne_index = _personen_ohne_index([kennung for art, kennung in gefunden if art == "Person"])
    return [
        f"{_basis()}{PFADE[art]}{kennung}/"
        for art, kennung in gefunden
        if not (art == "Person" and kennung in ohne_index)
    ]


def _personen_ohne_index(kennungen: list[uuid.UUID]) -> set[uuid.UUID]:
    """Personen, deren Seite ``noindex`` trägt: vorhanden, aber ohne laufende Mitgliedschaft (``indexierung``)."""
    if not kennungen:
        return set()
    from ..models import OParlPerson
    from .indexierung import mit_laufender_mitgliedschaft

    bestand = OParlPerson.objects.filter(id__in=kennungen, deleted=False)
    laufend = set(mit_laufender_mitgliedschaft(bestand, timezone.localdate()).values_list("id", flat=True))
    return set(bestand.values_list("id", flat=True)) - laufend


def indexnow(events: list[Any], delivery: Any) -> None:
    """Handler des Abonnements ``insight.indexnow``: geänderte Seiten melden (wiederholbar, wirft nicht)."""
    adressen = seiten(events)
    if adressen:
        melden(adressen)
