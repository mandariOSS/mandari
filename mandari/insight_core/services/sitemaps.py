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

``lastmod`` ist nur ein glaubhafter Änderungszeitpunkt (``aenderungszeitpunkt``, Issue #939): Manche Quellen tragen
statt des echten Werts einen Ersatzwert ein (Bonn: 1999-12-31 bei fast allen Vorgängen), und solche Angaben schwächen
bei Suchmaschinen das Vertrauen in alle ``lastmod`` der Domain. Ohne glaubhaften Zeitpunkt fehlt ``lastmod``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.cache import cache
from django.db import connection
from django.db.models import Case, DateTimeField, F, Model, Q, QuerySet, When, Window
from django.db.models.functions import RowNumber
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

#: Frühester glaubhafter Änderungszeitpunkt. Ältere Werte sind Ersatzwerte der Quelle, keine Änderungen: Bonn trägt
#: 1999-12-31 bei fast allen Vorgängen ein (Issue #939). Grenze in UTC, also liegt auch ein Ersatzwert
#: „2000-01-01 00:00“ deutscher Zeit davor.
FRUEHESTE_AENDERUNG = datetime(2000, 1, 1, tzinfo=UTC)
#: So weit darf ein Änderungszeitpunkt in der Zukunft liegen (abweichende Uhren und Zeitzonen der Quellen); was
#: später liegt, ist kein glaubhafter Änderungszeitpunkt
ZUKUNFT_SPIELRAUM = timedelta(days=1)
#: Änderungszeitpunkt der RIS-Objekte laut Quelle, in dieser Reihenfolge. Nicht ``updated_at``: Der Abgleich setzt
#: es bei jedem Lauf neu, auch wenn sich nichts geändert hat (Upsert im Ingestor mit ``updated_at = now()``).
RIS_ZEITPUNKTE = ("oparl_modified", "oparl_created")
#: Änderungszeitpunkt der Ratsfragen: ``updated_at`` ändert sich nur mit der Seite (Freischalten, Antwort,
#: Freischalten der Antwort, Bearbeitung); die Erinnerung an das Ratsmitglied schreibt es nicht.
FRAGE_ZEITPUNKTE = ("updated_at",)


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


def aenderungszeitpunkt(felder: tuple[str, ...] = RIS_ZEITPUNKTE, *, jetzt: datetime | None = None) -> Case:
    """
    Glaubhafter Änderungszeitpunkt als Datenbank-Ausdruck (Issue #939): das erste der ``felder``, dessen Wert
    zwischen ``FRUEHESTE_AENDERUNG`` und ``jetzt`` plus ``ZUKUNFT_SPIELRAUM`` liegt, sonst ``NULL`` – dann trägt
    der Eintrag kein ``lastmod``.

    Die Datenbank wertet ihn je Zeile aus: Die Abfragen lesen weiter nur Kennung und Zeitpunkt, und die Aufteilung
    bildet daraus die letzte Änderung je Datei.
    """
    obergrenze = (jetzt or timezone.now()) + ZUKUNFT_SPIELRAUM
    return Case(
        *(
            When(Q(**{f"{feld}__gte": FRUEHESTE_AENDERUNG, f"{feld}__lte": obergrenze}), then=F(feld))
            for feld in felder
        ),
        default=None,
        output_field=DateTimeField(),
    )


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
    # Mit der Dateigröße: Ändert sie sich, gilt eine zwischengespeicherte Aufteilung nicht mehr. „lastmod2“: Seit
    # Issue #939 zählen nur glaubhafte Änderungszeitpunkte; Aufteilungen mit den alten Werten gelten nicht weiter.
    return f"insight:sitemap:dateien:lastmod2:{art.kennung}:{EINTRAEGE_JE_DATEI}:{body_id}"


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
    teilt sie in Seiten und bildet je Seite Anzahl und jüngsten glaubhaften Änderungszeitpunkt. Zurück kommen nur
    so viele Zeilen, wie es Dateien gibt.

    Die innere Abfrage baut das ORM, mit demselben Ausdruck für den Änderungszeitpunkt wie ``eintraege``; die
    äußere fasst ihre Zeilen je Seite zusammen. Werte gehen als Parameter.
    """
    nummeriert = (
        _bestand(art, body_id)
        .order_by()
        .annotate(
            seite=(Window(RowNumber(), order_by=[F(name).asc() for name in REIHENFOLGE]) - 1) / EINTRAEGE_JE_DATEI,
            geaendert=aenderungszeitpunkt(),
        )
        .values_list("seite", "geaendert")
    )
    innen, parameter = nummeriert.query.sql_with_params()
    sql = f"SELECT seite, COUNT(*), MAX(geaendert) FROM ({innen}) nummeriert GROUP BY seite ORDER BY seite"  # noqa: S608 – innere Abfrage vom ORM, Werte als Parameter
    with connection.cursor() as cursor:
        cursor.execute(sql, parameter)
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
        .values_list("id", aenderungszeitpunkt())[start : start + EINTRAEGE_JE_DATEI]
    )
    return werte.iterator(chunk_size=2_000)


def gremien(body_id: Any) -> Iterator[tuple[Any, datetime | None]]:
    from ..models import OParlOrganization

    return (
        OParlOrganization.objects.filter(body_id=body_id, deleted=False)
        .order_by("name")
        .values_list("id", aenderungszeitpunkt())[:GRUND_HOECHSTENS]
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
        .values_list("id", aenderungszeitpunkt())[:GRUND_HOECHSTENS]
        .iterator(chunk_size=2_000)
    )


def ratsfragen(body_id: Any) -> Iterator[tuple[Any, datetime | None]]:
    from ..models import PublicQuestion

    return (
        PublicQuestion.objects.filter(body_id=body_id, status="published")
        .order_by("-published_at")
        .values_list("id", aenderungszeitpunkt(FRAGE_ZEITPUNKTE))[:GRUND_HOECHSTENS]
        .iterator(chunk_size=2_000)
    )
