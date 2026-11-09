# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Feeds des Bürgerportals (Issue #1274): Abonnieren ohne Konto und ohne E-Mail-Adresse.

Jede gefilterte Vorgangsliste, jede Suche, jeder Vorgang („Neues zu diesem Vorgang“), jedes Gremium und die
Beschlüsse einer Kommune gibt es als Feed im Format Atom 1.0 (RFC 4287). Feed-Reader lesen Atom wie RSS. Dieser
Baustein kennt nur das Format, den Abruf und die Grenzen; welche Ereignisse ein Feed enthält, steht in
``feed_eintraege``, die Adressen in ``views/feeds.py``.

Regeln, damit Feed-Reader nichts doppelt zeigen und nichts verlieren:

- **Feste Kennungen.** Feed und Eintrag tragen eine tag-URI (RFC 4151) mit fester Behörde ``mandari.de,2026``, gebildet
  aus den kanonischen Kennungen (UUID) von Vorgang, Sitzung, Tagesordnungspunkt bzw. Dokument und der Art des
  Ereignisses, nie aus der Position in einer Liste. Dasselbe Ereignis hat in allen Feeds dieselbe Kennung; ein Umzug
  der Domain ändert nichts.
- **Datum des Ereignisses.** ``updated`` eines Eintrags ist der Tag der Vorlage bzw. der Beginn der Sitzung, der Feed
  trägt das Datum seines jüngsten Eintrags. Ohne Änderung im Bestand bleibt der Feed Byte für Byte gleich (ETag).
- **Grenzen.** Höchstens ``EINTRAEGE_HOECHSTENS`` Einträge, neueste zuerst, Zusammenfassungen gekürzt; Steuerzeichen
  aus der Quelle fallen weg (sie machten das XML ungültig).
- **Last.** Ein gebauter Feed liegt ``ZWISCHENSPEICHER_SEKUNDEN`` im Cache; nur das Bauen zählt gegen die Grenze je
  IP-Adresse (``INSIGHT_FEEDS_PER_IP_MINUTE``). Bedingte Abrufe (``If-None-Match``, ``If-Modified-Since``) bekommen
  304. Feed-Adressen tragen ``X-Robots-Tag: noindex``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from io import StringIO
from typing import Final
from xml.sax.xmlreader import AttributesImpl

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.templatetags.static import static
from django.utils import timezone
from django.utils.cache import get_conditional_response
from django.utils.http import http_date
from django.utils.xmlutils import SimplerXMLGenerator

from .. import throttle

ATOM_NS: Final = "http://www.w3.org/2005/Atom"
#: Medientyp für ``<link rel="alternate" type=…>`` und die Antwort
MEDIENTYP: Final = "application/atom+xml"
#: Anfang jeder Kennung (tag-URI nach RFC 4151); bleibt für immer gleich, sonst zeigen Reader alles doppelt
KENNUNG_PRAEFIX: Final = "tag:mandari.de,2026:"
#: Höchstens so viele Einträge je Feed
EINTRAEGE_HOECHSTENS: Final = 100
#: Längste Zusammenfassung eines Eintrags (Zeichen)
ZUSAMMENFASSUNG_HOECHSTENS: Final = 600
#: Längster Titel eines Eintrags (Zeichen)
TITEL_HOECHSTENS: Final = 300
#: So lange bleibt ein gebauter Feed im Cache (Sekunden); ebenso lange dürfen Reader und Proxys ihn halten
ZWISCHENSPEICHER_SEKUNDEN: Final = 900
#: Datum eines Feeds ohne Einträge (fest, damit die Antwort gleich bleibt)
LEER_STAND: Final = datetime(2026, 1, 1, tzinfo=UTC)
#: Zeichen, die XML 1.0 nicht erlaubt (Steuerzeichen außer Tab, Zeilenumbruch, Wagenrücklauf)
_UNGUELTIG: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass(frozen=True)
class Eintrag:
    """Ein Ereignis im Feed: neue Vorlage, Beratung mit Ergebnis, angesetzte Sitzung, neues Dokument …"""

    #: Feste Kennung (``kennung(…)``)
    kennung: str
    titel: str
    #: Zeitpunkt des Ereignisses (``updated``)
    zeitpunkt: datetime
    #: Absolute Adresse der Seite im Bürgerportal
    link: str
    zusammenfassung: str = ""
    #: Absolute Adresse des Originals im Ratsinformationssystem (``rel="related"``), leer ohne
    original: str = ""


@dataclass(frozen=True)
class Feed:
    """Kopf und Einträge eines Feeds; ``selbst`` und ``seite`` sind absolute Adressen."""

    kennung: str
    titel: str
    untertitel: str
    #: Adresse des Feeds (``rel="self"``)
    selbst: str
    #: Seite im Bürgerportal, die dieselbe Auswahl zeigt (``rel="alternate"``)
    seite: str
    eintraege: Sequence[Eintrag]
    #: Herausgeber (``author``); im Portal einer Körperschaft deren Name
    herausgeber: str = "mandari Insight"
    #: Basis der Website (``author/uri``, Symbol)
    basis: str = ""
    #: Herkunft der Angaben (``rights``)
    rechte: str = ""


def kennung(*teile: object) -> str:
    """Feste Kennung aus Art und Kennungen: ``kennung("vorgang", pk, "beratung", sitzung_pk)``."""
    return KENNUNG_PRAEFIX + "/".join(str(teil) for teil in teile)


def tagesbeginn(tag: date | datetime | None) -> datetime | None:
    """Zeitpunkt eines Tages (Mitternacht Ortszeit); Zeitpunkte bleiben, wie sie sind."""
    if tag is None:
        return None
    if isinstance(tag, datetime):
        return tag if timezone.is_aware(tag) else timezone.make_aware(tag)
    return timezone.make_aware(datetime.combine(tag, time.min))


def text(wert: object, grenze: int) -> str:
    """Text für den Feed: ohne Steuerzeichen, Leerraum zusammengefasst, an einer Wortgrenze gekürzt."""
    sauber = " ".join(_UNGUELTIG.sub(" ", str(wert or "")).split())
    if len(sauber) <= grenze:
        return sauber
    schnitt = sauber[: grenze - 1]
    if " " in schnitt:
        schnitt = schnitt[: schnitt.rindex(" ")]
    return schnitt.rstrip(" ,;:-–") + "…"


def _zeit(zeitpunkt: datetime) -> str:
    """RFC 3339 in UTC."""
    if timezone.is_naive(zeitpunkt):
        zeitpunkt = timezone.make_aware(zeitpunkt)
    return zeitpunkt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def auswahl(eintraege: Iterable[Eintrag], grenze: int = EINTRAEGE_HOECHSTENS) -> list[Eintrag]:
    """Neueste zuerst, jede Kennung einmal, höchstens ``grenze`` Einträge."""
    sortiert = sorted(eintraege, key=lambda e: (e.zeitpunkt, e.kennung), reverse=True)
    gesehen: set[str] = set()
    ergebnis: list[Eintrag] = []
    for eintrag in sortiert:
        if eintrag.kennung in gesehen:
            continue
        gesehen.add(eintrag.kennung)
        ergebnis.append(eintrag)
        if len(ergebnis) >= grenze:
            break
    return ergebnis


def stand(feed: Feed) -> datetime:
    """Datum des Feeds: jüngster Eintrag, ohne Einträge ``LEER_STAND``."""
    return max((e.zeitpunkt for e in feed.eintraege), default=LEER_STAND)


def atom(feed: Feed) -> bytes:
    """Feed als Atom 1.0 (UTF-8). Einträge in der Reihenfolge von ``auswahl``."""
    eintraege = auswahl(feed.eintraege)
    puffer = StringIO()
    xml = SimplerXMLGenerator(puffer, "utf-8", short_empty_elements=True)
    xml.startDocument()
    xml.startElement("feed", AttributesImpl({"xmlns": ATOM_NS, "xml:lang": "de"}))
    xml.addQuickElement("id", feed.kennung)
    xml.addQuickElement("title", text(feed.titel, TITEL_HOECHSTENS), {"type": "text"})
    if feed.untertitel:
        xml.addQuickElement("subtitle", text(feed.untertitel, ZUSAMMENFASSUNG_HOECHSTENS), {"type": "text"})
    xml.addQuickElement("updated", _zeit(stand(feed)))
    xml.addQuickElement("link", attrs={"rel": "self", "type": MEDIENTYP, "href": feed.selbst})
    xml.addQuickElement("link", attrs={"rel": "alternate", "type": "text/html", "href": feed.seite})
    xml.startElement("author", AttributesImpl({}))
    xml.addQuickElement("name", text(feed.herausgeber, TITEL_HOECHSTENS))
    if feed.basis:
        xml.addQuickElement("uri", f"{feed.basis}/insight/")
    xml.endElement("author")
    if feed.basis:
        xml.addQuickElement("icon", f"{feed.basis}{static('brand/favicon-192.png')}")
    if feed.rechte:
        xml.addQuickElement("rights", text(feed.rechte, ZUSAMMENFASSUNG_HOECHSTENS), {"type": "text"})
    for eintrag in eintraege:
        xml.startElement("entry", AttributesImpl({}))
        xml.addQuickElement("id", eintrag.kennung)
        xml.addQuickElement("title", text(eintrag.titel, TITEL_HOECHSTENS), {"type": "text"})
        xml.addQuickElement("updated", _zeit(eintrag.zeitpunkt))
        xml.addQuickElement("link", attrs={"rel": "alternate", "type": "text/html", "href": eintrag.link})
        if eintrag.original:
            xml.addQuickElement("link", attrs={"rel": "related", "type": "text/html", "href": eintrag.original})
        if eintrag.zusammenfassung:
            xml.addQuickElement("summary", text(eintrag.zusammenfassung, ZUSAMMENFASSUNG_HOECHSTENS), {"type": "text"})
        xml.endElement("entry")
    xml.endElement("feed")
    xml.endDocument()
    return puffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Abruf: Cache, Drosselung, bedingte Abrufe
# ---------------------------------------------------------------------------


def _cache_schluessel(schluessel: str) -> str:
    return "insight:feed:v1:" + hashlib.sha256(schluessel.encode("utf-8")).hexdigest()[:40]


def zu_viele_abrufe() -> HttpResponse:
    """429 mit ``Retry-After``: Die Grenze je IP-Adresse ist erreicht."""
    antwort = HttpResponse(
        "Zu viele Abrufe in kurzer Zeit. Bitte versuchen Sie es in einer Minute erneut.\n",
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    antwort["Retry-After"] = "60"
    antwort["X-Robots-Tag"] = "noindex"
    return antwort


def gedrosselt(request: HttpRequest) -> bool:
    """Einen Neubau zählen; ``True``, wenn die Grenze je IP-Adresse und Minute damit überschritten ist."""
    return throttle.hit(
        "feed-ip",
        throttle.client_ip(request),
        limit=throttle.setting("INSIGHT_FEEDS_PER_IP_MINUTE"),
        window=throttle.MINUTE,
    )


def antwort(request: HttpRequest, schluessel: str, bauen: Callable[[], Feed]) -> HttpResponse:
    """
    Feed aus dem Cache oder neu gebaut (``bauen``), mit ETag und Last-Modified.

    ``schluessel`` beschreibt die Auswahl vollständig (Art, Kommune, bereinigte Filter): Dieselbe Auswahl teilt sich
    einen Eintrag im Cache, unbekannte Parameter ändern ihn nicht. Gedrosselt wird nur der Neubau.
    """
    cache_schluessel = _cache_schluessel(schluessel)
    gespeichert = cache.get(cache_schluessel)
    if gespeichert is None:
        if gedrosselt(request):
            return zu_viele_abrufe()
        feed = bauen()
        inhalt = atom(feed)
        etag = '"' + hashlib.sha256(inhalt).hexdigest()[:32] + '"'
        gespeichert = (inhalt, etag, stand(feed).timestamp())
        cache.set(cache_schluessel, gespeichert, ZWISCHENSPEICHER_SEKUNDEN)
    inhalt, etag, zeitstempel = gespeichert
    zuletzt = int(zeitstempel)
    response = HttpResponse(inhalt, content_type=f"{MEDIENTYP}; charset=utf-8")
    response["ETag"] = etag
    response["Last-Modified"] = http_date(zuletzt)
    response["Cache-Control"] = f"public, max-age={ZWISCHENSPEICHER_SEKUNDEN}"
    response["X-Robots-Tag"] = "noindex"
    bedingt = get_conditional_response(request, etag=etag, last_modified=zuletzt, response=response)
    return bedingt if bedingt is not None else response
