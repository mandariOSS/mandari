# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitemaps des Bürgerportals (Issue #914): alle öffentlichen Vorgänge und Sitzungen der gelisteten Kommunen.

Je Kommune gibt es eine Grund-Sitemap (Stadtseite, Gremien, Personen mit laufender Mitgliedschaft, Ratsfragen)
und für Vorgänge und Sitzungen nummerierte Dateien mit höchstens ``EINTRAEGE_JE_DATEI`` Adressen:
``/sitemap-insight-<slug>-vorgaenge-<n>.xml``. Alle Pfade beginnen mit ``/sitemap-insight-``; nur dieser Präfix
geht in der Produktion an Django.

Die Dateien teilen den Bestand in der Reihenfolge des Eingangs (``created_at``, dann ``id``) auf: Neue Einträge
landen in der letzten Datei, die älteren Dateien bleiben stabil. Der Container hat wenig Speicher, deshalb lesen
alle Abfragen nur Kennung und Änderungszeitpunkt (``values_list``) und laufen über ``iterator``; Seitenzahl, Anzahl
und letzte Änderung je Datei rechnet die Datenbank in einer Abfrage je Art aus. Diese Aufteilung liest den ganzen
Bestand einer Kommune; der Index hält sie deshalb ``DATEIEN_CACHE_SEKUNDEN`` im Cache.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from django.core.cache import cache
from django.db import connection
from django.db.models import Field, ForeignKey, Model, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

#: Höchstens so viele Adressen je Datei (das Protokoll erlaubt 50.000; kleinere Dateien antworten schneller)
EINTRAEGE_JE_DATEI = 10_000
#: So lange gilt die Aufteilung je Kommune und Art im Cache: Neue Dateien erscheinen spätestens dann im Index
#: (die Antworten selbst dürfen Suchmaschinen und Zwischenspeicher 24 Stunden halten)
DATEIEN_CACHE_SEKUNDEN = 6 * 3600
#: Gremien, Personen und Ratsfragen der Grund-Sitemap: je höchstens so viele
GRUND_HOECHSTENS = 5_000
#: Reihenfolge der Aufteilung: Eingang, dann Kennung (eindeutig, stabil)
REIHENFOLGE = ("created_at", "id")


@dataclass(frozen=True)
class Art:
    """Eine aufgeteilte Art: Vorgänge oder Sitzungen."""

    kennung: str
    pfad: str
    changefreq: str
    priority: str

    def modell(self) -> type[Model]:
        from ..models import OParlMeeting, OParlPaper

        return OParlPaper if self.kennung == "vorgaenge" else OParlMeeting


ARTEN: dict[str, Art] = {
    "vorgaenge": Art("vorgaenge", "/insight/vorgaenge/", "monthly", "0.6"),
    "sitzungen": Art("sitzungen", "/insight/termine/", "weekly", "0.7"),
}


@dataclass(frozen=True)
class Datei:
    """Eine nummerierte Sitemap-Datei im Index."""

    art: Art
    seite: int
    eintraege: int
    lastmod: datetime | None


def _bestand(art: Art, body_id: Any) -> QuerySet[Any]:
    return art.modell()._default_manager.filter(body_id=body_id, deleted=False)


def _zeitpunkt(wert: Any) -> datetime | None:
    """Ergebnis von ``MAX`` als Zeitpunkt: Postgres liefert ``datetime``, SQLite (Tests) Text."""
    if wert is None:
        return None
    zeitpunkt = wert if isinstance(wert, datetime) else parse_datetime(str(wert))
    if zeitpunkt is not None and timezone.is_naive(zeitpunkt):
        zeitpunkt = zeitpunkt.replace(tzinfo=UTC)
    return zeitpunkt


def _cache_schluessel(art: Art, body_id: Any) -> str:
    # Mit der Dateigröße: Ändert sie sich, gilt eine zwischengespeicherte Aufteilung nicht mehr
    return f"insight:sitemap:dateien:{art.kennung}:{EINTRAEGE_JE_DATEI}:{body_id}"


def dateien(art: Art, body_id: Any) -> list[Datei]:
    """
    Nummerierte Dateien einer Art für den Sitemap-Index: Seite, Zahl der Einträge, letzte Änderung.

    Aus dem Cache, sonst aus der Datenbank (``_dateien_berechnen``); zwischengespeichert werden nur Zahlen und
    Zeitpunkte, je Datei ein kleines Tupel.
    """
    schluessel = _cache_schluessel(art, body_id)
    gespeichert = cache.get(schluessel)
    if gespeichert is None:
        gespeichert = [(d.seite, d.eintraege, d.lastmod) for d in _dateien_berechnen(art, body_id)]
        cache.set(schluessel, gespeichert, DATEIEN_CACHE_SEKUNDEN)
    return [Datei(art=art, seite=seite, eintraege=anzahl, lastmod=lastmod) for seite, anzahl, lastmod in gespeichert]


def _dateien_berechnen(art: Art, body_id: Any) -> list[Datei]:
    """
    Eine Abfrage: Die Datenbank nummeriert die Einträge in der Reihenfolge der Aufteilung (``ROW_NUMBER``),
    teilt sie in Seiten und bildet je Seite Anzahl und jüngsten Änderungszeitpunkt. Zurück kommen nur so viele
    Zeilen, wie es Dateien gibt. Tabellen- und Spaltennamen stammen aus dem Modell, Werte gehen als Parameter.
    """
    modell = art.modell()
    meta = modell._meta
    q = connection.ops.quote_name

    def spalte(name: str) -> str:
        return q(str(cast("Field[Any, Any]", meta.get_field(name)).column))

    body_feld = cast("ForeignKey[Any, Any]", meta.get_field("body"))
    body_wert = body_feld.target_field.get_db_prep_value(body_id, connection)
    reihenfolge = ", ".join(f"{spalte(name)} ASC" for name in REIHENFOLGE)
    sql = (
        f"SELECT seite, COUNT(*), MAX(geaendert) FROM ("  # noqa: S608 – nur Namen aus dem Modell, Werte als Parameter
        f"SELECT (ROW_NUMBER() OVER (ORDER BY {reihenfolge}) - 1) / %s AS seite, "
        f"COALESCE({spalte('oparl_modified')}, {spalte('updated_at')}) AS geaendert "
        f"FROM {q(meta.db_table)} WHERE {spalte('body')} = %s AND {spalte('deleted')} = %s"
        ") nummeriert GROUP BY seite ORDER BY seite"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [EINTRAEGE_JE_DATEI, body_wert, False])
        zeilen = cursor.fetchall()
    return [
        Datei(art=art, seite=int(seite) + 1, eintraege=int(anzahl), lastmod=_zeitpunkt(geaendert))
        for seite, anzahl, geaendert in zeilen
    ]


def eintraege(art: Art, body_id: Any, seite: int) -> Iterator[tuple[Any, datetime | None]]:
    """Kennung und Änderungszeitpunkt der Einträge einer Datei (``seite`` ab 1), gestreamt."""
    start = (seite - 1) * EINTRAEGE_JE_DATEI
    werte = (
        _bestand(art, body_id)
        .order_by(*REIHENFOLGE)
        .values_list("id", Coalesce("oparl_modified", "updated_at"))[start : start + EINTRAEGE_JE_DATEI]
    )
    return werte.iterator(chunk_size=2_000)


def gremien(body_id: Any) -> Iterator[tuple[Any, datetime | None]]:
    from ..models import OParlOrganization

    return (
        OParlOrganization.objects.filter(body_id=body_id, deleted=False)
        .order_by("name")
        .values_list("id", Coalesce("oparl_modified", "updated_at"))[:GRUND_HOECHSTENS]
        .iterator(chunk_size=2_000)
    )


def personen(body_id: Any) -> Iterator[tuple[Any, datetime | None]]:
    """Nur Personen mit laufender Mitgliedschaft (Issue #914): Die übrigen Personenseiten tragen ``noindex``."""
    from ..models import OParlPerson
    from .indexierung import mit_laufender_mitgliedschaft

    bestand = OParlPerson.objects.filter(body_id=body_id, deleted=False)
    return (
        mit_laufender_mitgliedschaft(bestand, timezone.localdate())
        .order_by("family_name", "given_name", "id")
        .values_list("id", Coalesce("oparl_modified", "updated_at"))[:GRUND_HOECHSTENS]
        .iterator(chunk_size=2_000)
    )


def ratsfragen(body_id: Any) -> Iterator[tuple[Any, datetime | None]]:
    from ..models import PublicQuestion

    return (
        PublicQuestion.objects.filter(body_id=body_id, status="published")
        .order_by("-published_at")
        .values_list("id", "updated_at")[:GRUND_HOECHSTENS]
        .iterator(chunk_size=2_000)
    )
