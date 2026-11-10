# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitemaps des Bürgerportals (Issue #914): die öffentlichen Vorgänge und Sitzungen der gelisteten Kommunen, die etwas zu
bieten haben (Issue #1145, ``indexierung.vorgaenge_mit_inhalt``, ``indexierung.sitzungen_mit_inhalt``). Suchmaschinen
rufen je Tag nur wenige Hundert Seiten ab; die Sitemap nennt ihnen die, für die sich das lohnt.

Je Kommune gibt es eine Grund-Sitemap (Stadtseite und ihre Listen, Gremien, Personen mit laufender Mitgliedschaft,
Ratsfragen) und für Vorgänge und Sitzungen nummerierte Dateien mit höchstens ``EINTRAEGE_JE_DATEI`` Adressen:
``/sitemap-insight-<slug>-vorgaenge-<n>.xml``. Alle Pfade beginnen mit ``/sitemap-insight-``; nur dieser Präfix
geht in der Produktion an Django.

Die Dateien teilen den Bestand in der Reihenfolge des Eingangs (``created_at``, dann ``id``) auf: Neue Einträge
landen meist in der letzten Datei; bekommt ein älterer Vorgang Text, rückt er an seine Stelle. Der Container hat wenig Speicher, deshalb lesen
alle Abfragen nur Kennung und Änderungszeitpunkt (``values_list``) und laufen über ``iterator``; Seitenzahl, Anzahl
und letzte Änderung je Datei rechnet die Datenbank in einer Abfrage je Art aus.

Vorberechnet (Issue #1259): Diese Aufteilung liest den ganzen Bestand einer Kommune. Für alle gelisteten Kommunen
brauchte der Index ohne Cache über 40 Sekunden, Suchmaschinen brechen dann ab. Deshalb rechnet der Zeitplan
``insight_core.schedules.sitemap_aufteilen`` sie im Worker vor und speichert sie (``SitemapAufteilung``), neu, sobald
sie älter als ``ERNEUERN_NACH`` ist. Der Index liest nur die gespeicherten Werte, eine Abfrage für alle Kommunen, mit
oder ohne Cache. Fehlt eine Aufteilung (neu gelistete Kommune, geänderte Dateigröße) oder ist sie älter als
``HOECHSTENS_ALT`` (kein Worker), rechnet der Index sie selbst, aber höchstens ``INDEX_RECHENZEIT_SEKUNDEN`` je
Abruf; was dann noch fehlt, erscheint nach dem nächsten Lauf des Zeitplans.

``lastmod`` ist nur ein glaubhafter Änderungszeitpunkt (``aenderungszeitpunkt``, Issue #939): Manche Quellen tragen
statt des echten Werts einen Ersatzwert ein (Bonn: 1999-12-31 bei fast allen Vorgängen), und solche Angaben schwächen
bei Suchmaschinen das Vertrauen in alle ``lastmod`` der Domain. Ohne glaubhaften Zeitpunkt fehlt ``lastmod``.

Das gilt auch für die Stadtseite und die Grund-Sitemap im Index: Sie tragen die jüngste glaubhafte Änderung der
Vorgänge und Sitzungen der Kommune (``aenderung_der_kommune``, ``juengste_aenderung``), nicht den letzten Abgleich.
Der Abgleich läuft mehrmals am Tag, auch wenn sich nichts geändert hat; ein ``lastmod``, das jedes Mal mitläuft,
sagt Suchmaschinen nichts über den Inhalt.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import connection
from django.db.models import Case, DateTimeField, F, Model, Q, QuerySet, When, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from django.utils.dateparse import parse_datetime

#: Höchstens so viele Adressen je Datei (das Protokoll erlaubt 50.000; kleinere Dateien antworten schneller)
EINTRAEGE_JE_DATEI = 10_000
#: Älter als das rechnet der Zeitplan eine gespeicherte Aufteilung neu: Neue Dateien und ``lastmod`` erscheinen dann im
#: Index (die Antworten selbst dürfen Suchmaschinen und Zwischenspeicher 24 Stunden halten)
ERNEUERN_NACH = timedelta(hours=3)
#: Älter als das gilt eine Aufteilung als liegen geblieben (läuft kein Worker?): Der Index rechnet sie dann selbst
HOECHSTENS_ALT = timedelta(hours=24)
#: So lange darf der Index je Abruf selbst rechnen; den Rest übernimmt der Zeitplan
INDEX_RECHENZEIT_SEKUNDEN = 5.0
#: So lange rechnet ein Lauf des Zeitplans höchstens (Zeitgrenze der Warteschlange ``default``: 5 Minuten); was
#: offen bleibt, kommt im nächsten Lauf zuerst
ZEITPLAN_RECHENZEIT_SEKUNDEN = 240.0
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
    from .indexierung import sitzungen_mit_inhalt, vorgaenge_mit_inhalt

    bestand = art.modell()._default_manager.filter(body_id=body_id, deleted=False)
    auswahl = vorgaenge_mit_inhalt if art.kennung == "vorgaenge" else sitzungen_mit_inhalt
    return auswahl(bestand, timezone.now())


def _zeitpunkt(wert: Any) -> datetime | None:
    """Ergebnis von ``MAX`` als Zeitpunkt: Postgres liefert ``datetime``, SQLite (Tests) Text."""
    if wert is None:
        return None
    zeitpunkt = wert if isinstance(wert, datetime) else parse_datetime(str(wert))
    if zeitpunkt is not None and timezone.is_naive(zeitpunkt):
        zeitpunkt = zeitpunkt.replace(tzinfo=UTC)
    return zeitpunkt


def _gespeichert(dateien: list[Datei]) -> list[list[Any]]:
    """Dateien als JSON: je Datei Seite, Zahl der Einträge und letzte Änderung (ISO 8601 oder ``None``)."""
    return [[d.seite, d.eintraege, d.lastmod.isoformat() if d.lastmod else None] for d in dateien]


def _aus_gespeichertem(art: Art, werte: Any) -> list[Datei]:
    return [
        Datei(art=art, seite=int(seite), eintraege=int(anzahl), lastmod=_zeitpunkt(lastmod))
        for seite, anzahl, lastmod in (werte if isinstance(werte, list) else [])
    ]


def aufteilen(art: Art, body_id: Any) -> list[Datei]:
    """Aufteilung einer Kommune und Art aus dem Bestand berechnen (``_dateien_berechnen``) und speichern."""
    from ..models import SitemapAufteilung

    dateien = _dateien_berechnen(art, body_id)
    SitemapAufteilung.objects.update_or_create(
        body_id=body_id,
        art=art.kennung,
        defaults={
            "eintraege_je_datei": EINTRAEGE_JE_DATEI,
            "dateien": _gespeichert(dateien),
            "berechnet_am": timezone.now(),
        },
    )
    return dateien


def _gespeicherte(body_ids: list[Any]) -> dict[tuple[Any, str], tuple[datetime, list[Datei]]]:
    """Gespeicherte Aufteilungen dieser Kommunen mit der geltenden Dateigröße, in einer Abfrage."""
    from ..models import SitemapAufteilung

    zeilen = SitemapAufteilung.objects.filter(
        body_id__in=body_ids, art__in=list(ARTEN), eintraege_je_datei=EINTRAEGE_JE_DATEI
    ).values_list("body_id", "art", "berechnet_am", "dateien")
    return {
        (body_id, kennung): (berechnet, _aus_gespeichertem(ARTEN[kennung], werte))
        for body_id, kennung, berechnet, werte in zeilen
    }


def index_dateien(body_ids: Iterable[Any]) -> dict[tuple[Any, str], list[Datei]]:
    """
    Nummerierte Dateien je Kommune und Art (Schlüssel ``(body_id, art.kennung)``) für den Sitemap-Index.

    Aus der Vorberechnung, eine Abfrage für alle Kommunen. Fehlende und liegen gebliebene Aufteilungen (älter als
    ``HOECHSTENS_ALT``) rechnet der Index selbst, solange ``INDEX_RECHENZEIT_SEKUNDEN`` nicht verbraucht sind;
    danach gilt eine liegen gebliebene weiter, und eine fehlende fehlt im Index bis zum nächsten Lauf des Zeitplans.
    """
    kennungen = list(body_ids)
    gespeichert = _gespeicherte(kennungen)
    grenze = timezone.now() - HOECHSTENS_ALT
    frist = time.monotonic() + INDEX_RECHENZEIT_SEKUNDEN
    ergebnis: dict[tuple[Any, str], list[Datei]] = {}
    for body_id in kennungen:
        for art in ARTEN.values():
            eintrag = gespeichert.get((body_id, art.kennung))
            if (eintrag is None or eintrag[0] < grenze) and time.monotonic() < frist:
                ergebnis[(body_id, art.kennung)] = aufteilen(art, body_id)
            elif eintrag is not None:
                ergebnis[(body_id, art.kennung)] = eintrag[1]
    return ergebnis


def juengste_aenderung(dateien: Iterable[Datei]) -> datetime | None:
    """Jüngste glaubhafte Änderung über mehrere Dateien; ``None``, wenn keine von ihnen eine nennt."""
    return max((datei.lastmod for datei in dateien if datei.lastmod is not None), default=None)


def aenderung_der_kommune(body_id: Any) -> datetime | None:
    """
    Jüngste glaubhafte Änderung der Vorgänge und Sitzungen einer Kommune: ``lastmod`` ihrer Stadtseite, die die
    nächsten Sitzungen, die neuesten Vorgänge und die letzten Beschlüsse zeigt (Issue #939). ``None`` ohne
    glaubhaften Zeitpunkt.

    Aus der Vorberechnung wie im Index (``index_dateien``): eine Abfrage, und Stadtseite und Grund-Sitemap im Index
    nennen denselben Wert.
    """
    return juengste_aenderung(datei for dateien in index_dateien([body_id]).values() for datei in dateien)


def aufteilungen_erneuern(
    *, aelter_als: timedelta = ERNEUERN_NACH, rechenzeit: float = ZEITPLAN_RECHENZEIT_SEKUNDEN
) -> int:
    """
    Aufteilungen der gelisteten Kommunen vorberechnen (Zeitplan ``sitemap_aufteilen``): fehlende zuerst, dann die
    ältesten, jede, die älter als ``aelter_als`` ist. Nach ``rechenzeit`` Sekunden endet der Lauf, der nächste macht
    weiter. Aufteilungen von Kommunen, die nicht mehr gelistet sind oder nicht mehr bestehen, entfallen. Rückgabe:
    Zahl der neu berechneten Aufteilungen.
    """
    from ..models import OParlBody, SitemapAufteilung

    body_ids = list(OParlBody.objects.listed().order_by("id").values_list("id", flat=True))
    SitemapAufteilung.objects.exclude(body_id__in=body_ids).delete()
    stand = {
        (body_id, kennung): berechnet
        for body_id, kennung, berechnet in SitemapAufteilung.objects.filter(
            body_id__in=body_ids, eintraege_je_datei=EINTRAEGE_JE_DATEI
        ).values_list("body_id", "art", "berechnet_am")
    }
    grenze = timezone.now() - aelter_als
    faellig: list[tuple[datetime | None, Any, Art]] = []
    for body_id in body_ids:
        for art in ARTEN.values():
            berechnet = stand.get((body_id, art.kennung))
            if berechnet is None or berechnet <= grenze:
                faellig.append((berechnet, body_id, art))
    # Fehlende zuerst, dann die ältesten (stabil: sonst in der Reihenfolge der Kommunen)
    faellig.sort(key=lambda eintrag: (eintrag[0] is not None, eintrag[0] or grenze))
    frist = time.monotonic() + rechenzeit
    anzahl = 0
    for _berechnet, body_id, art in faellig:
        if time.monotonic() >= frist:
            break
        aufteilen(art, body_id)
        anzahl += 1
    return anzahl


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
    # Innere Abfrage vom ORM, Werte als Parameter
    sql = f"SELECT seite, COUNT(*), MAX(geaendert) FROM ({innen}) nummeriert GROUP BY seite ORDER BY seite"  # noqa: S608
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
