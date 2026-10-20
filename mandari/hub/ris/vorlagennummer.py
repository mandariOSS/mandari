# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlagennummern erkennen und in jeder Schreibweise finden (Issue #1102).

Wer eine bestimmte Vorlage sucht, hat meist ihre Nummer, etwa ``V/0591/2026``. Eingegeben wird sie aber auch als
``V 0591 2026``, ``v0591/2026``, ``V/591/2026`` oder nur ``0591/2026``. Für den Vergleich bilden wir aus Eingabe und
gespeicherter Nummer denselben **Vergleichsschlüssel**:

- Großbuchstaben (nur A–Z; andere Zeichen gelten wie ein Trenner),
- ``/``, ``-``, ``.``, Leerzeichen oder gar kein Trenner sind gleichwertig: Buchstaben stehen zusammen (``A-R`` =
  ``AR``), Zahlen werden getrennt, auch der Übergang zwischen Buchstabe und Ziffer trennt (``V0591`` = ``V/0591``),
- führende Nullen je Zahlengruppe zählen nicht (``0591`` = ``591``),
- die Gruppen stehen mit ``/`` verbunden: ``V/0591/2026`` → ``V/591/2026``, ``A-R/0052/2026`` → ``AR/52/2026``.

Was passt (``Nummer.muster``):

- Beginnt die Eingabe mit Buchstaben (Kreis wie ``V``, ``AN``, ``VO``), muss die Nummer genau so beginnen: ``V/591/2026``
  trifft ``V/0591/2026`` und die Folgevorlagen ``V/0591/2026/1``, aber nie ``AN/0591/2026``.
- Beginnt sie mit einer Ziffer, genügt das Endstück ab einer Gruppengrenze: ``0591/2026`` trifft ``V/0591/2026`` und
  ``AN/0591/2026`` (nur innerhalb der abgefragten Kommunen) samt Folgevorlagen.

Rang je Treffer (``Nummer.rang``, kleiner steht weiter oben): 1 genau diese Nummer, 2 Endstück genau (nur bei Eingabe
ohne Kreis), 3 Folgevorlage oder sonst passend, 4 nur Treffer der gewöhnlichen Textsuche.

In der Datenbank rechnet ``Schluessel`` denselben Schlüssel: in PostgreSQL als Kette aus ``translate`` und
``regexp_replace`` (unveränderlich, deshalb mit Ausdrucksindex, ``insight_core/0060_vorgang_nummer_index``), in SQLite
(Tests, Entwicklung) über eine registrierte Python-Funktion. Abgefragt wird immer innerhalb der Vorgänge, die der
Aufrufer übergibt, also je Kommune. ``suchen`` ist die eine Funktion für Listen und Suche in Insight und Work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from django.db.models import Case, CharField, Func, IntegerField, Q, QuerySet, Value, When
from django.db.models.lookups import Contains, EndsWith, Exact, Regex, StartsWith

TRENNER: Final = "/"
#: Höchstlänge einer Eingabe, die noch als Nummer gilt (längere Texte sind Suchbegriffe)
MAX_LAENGE: Final = 40
#: Buchstabengruppen einer Nummer sind kurz (V, AN, VO, A-R, DRS); längere Wörter sind Suchbegriffe
MAX_BUCHSTABEN_JE_GRUPPE: Final = 3
#: Mindestens so viele Ziffern (mit führenden Nullen) hat eine Eingabe, die als Nummer gilt
MIN_ZIFFERN: Final = 4

_NUR_NUMMERNZEICHEN = re.compile(r"[A-Za-z0-9/\-._\s]+")
#: Datumsangaben sehen aus wie Nummern, sind aber keine (20.10.2026, 2026-10-20)
_DATUM = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{1,2}-\d{1,2}")

_KLEIN = "abcdefghijklmnopqrstuvwxyz"
#: Schritte nach den Großbuchstaben, je (regulärer Ausdruck, Ersatz), überall angewandt. Dieselben Ausdrücke rechnet
#: Python (``re``) wie PostgreSQL (``regexp_replace``); sie nutzen nur, was beide gleich verstehen.
_SCHRITTE: Final = (
    ("[^0-9A-Z]+", "/"),  # jede Folge anderer Zeichen ist ein Trenner
    ("([A-Z])/(?=[A-Z])", r"\1"),  # Buchstaben stehen zusammen (A-R = AR)
    ("([A-Z])([0-9])", r"\1/\2"),  # der Übergang zwischen Buchstabe und Ziffer trennt
    ("([0-9])([A-Z])", r"\1/\2"),
)
#: Führende Nullen je Zahlengruppe entfernen (nach den Schritten oben steht vor jeder Zahl ``/`` oder der Anfang)
_NULLEN: Final = ("(^|/)0+([0-9])", r"\1\2")


def _sql(schritte: tuple[tuple[str, str], ...]) -> str:
    ausdruck = f"translate(%(expressions)s, '{_KLEIN}', '{_KLEIN.upper()}')"
    for muster, ersatz in schritte:
        ausdruck = f"regexp_replace({ausdruck}, '{muster}', '{ersatz}', 'g')"
    return f"btrim({ausdruck}, '{TRENNER}')"


#: Derselbe Schlüssel in PostgreSQL; nur feste Zeichenketten, damit Abfrage und Ausdrucksindex gleich lauten
SQL_SCHLUESSEL: Final = _sql((*_SCHRITTE, _NULLEN))
#: Höchster Rang, der als „genau diese Nummer“ gilt und hervorgehoben wird (1 genau, 2 Endstück genau)
RANG_GENAU: Final = 2
#: Name der Python-Funktion in SQLite
SQLITE_FUNKTION: Final = "mandari_vorlagennummer"


def _anwenden(text: str, schritte: tuple[tuple[str, str], ...]) -> str:
    gross = "".join(zeichen.upper() if zeichen.isascii() else " " for zeichen in text)
    for muster, ersatz in schritte:
        gross = re.sub(muster, ersatz, gross)
    return gross.strip(TRENNER)


def _gruppen(text: str) -> list[str]:
    """Buchstaben- und Zifferngruppen in Großbuchstaben, wie eingegeben (Nullen bleiben)."""
    roh = _anwenden(text, _SCHRITTE)
    return roh.split(TRENNER) if roh else []


def schluessel(text: str | None) -> str:
    """Vergleichsschlüssel einer Nummer: ``"v 0591-2026"`` → ``"V/591/2026"``; leer ohne Buchstaben und Ziffern."""
    return _anwenden(text or "", (*_SCHRITTE, _NULLEN))


def sieht_wie_nummer_aus(text: str | None) -> bool:
    """
    Ist die Eingabe eine Vorlagennummer (``V/0591/2026``, ``0591/2026``, ``VO 455 22``, ``4305/IX``)?

    Mindestens zwei Gruppen, darunter Ziffern; nur Buchstaben, Ziffern und Trenner; Buchstabengruppen kurz; zwei
    Zahlengruppen oder eine mit mindestens drei Stellen, zusammen mindestens ``MIN_ZIFFERN`` Ziffern (kürzere Anfänge
    beim Tippen wie „12/2“ träfen zu viele Nummern). „Weg 12“, „Haushalt 2026“ und Daten sind keine Nummern.
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_LAENGE or not _NUR_NUMMERNZEICHEN.fullmatch(text) or _DATUM.fullmatch(text):
        return False
    gruppen = _gruppen(text)
    zahlen = [g for g in gruppen if g.isdigit()]
    woerter = [g for g in gruppen if not g.isdigit()]
    if len(gruppen) < 2 or not zahlen or any(len(w) > MAX_BUCHSTABEN_JE_GRUPPE for w in woerter):
        return False
    if sum(len(z) for z in zahlen) < MIN_ZIFFERN:
        return False
    return len(zahlen) >= 2 or any(len(z) >= 3 for z in zahlen)


class Schluessel(Func):
    """Vergleichsschlüssel einer Spalte in der Datenbank (wie ``schluessel``)."""

    template = SQL_SCHLUESSEL
    arity = 1
    output_field = CharField()

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(compiler, connection, template=f"{SQLITE_FUNKTION}(%(expressions)s)", **extra_context)


def _sqlite_schluessel(wert: Any) -> str | None:
    return None if wert is None else schluessel(str(wert))


def sqlite_anmelden(sender: Any, connection: Any, **kwargs: Any) -> None:
    """Signal ``connection_created``: ``Schluessel`` in SQLite als Python-Funktion bereitstellen."""
    if connection.vendor == "sqlite":
        connection.connection.create_function(SQLITE_FUNKTION, 1, _sqlite_schluessel, deterministic=True)


@dataclass(frozen=True)
class Nummer:
    """Eine als Vorlagennummer erkannte Eingabe."""

    #: wie eingegeben (ohne Leerraum am Rand), für Hinweise wie „Die Vorlage … liegt uns noch nicht vor.“
    eingabe: str
    schluessel: str

    @property
    def mit_kreis(self) -> bool:
        """Beginnt mit Buchstaben (``V/…``, ``AN/…``): dann nur Nummern mit genau diesem Anfang."""
        return not self.schluessel[:1].isdigit()

    @property
    def muster(self) -> str:
        """Regulärer Ausdruck auf den Schlüssel (nur ``A-Z0-9/``, gleich in PostgreSQL und Python)."""
        if self.mit_kreis:
            return f"^{self.schluessel}(/|$)"
        return f"(^|/){self.schluessel}(/|$)"

    def bedingungen(self, feld: str = "reference") -> list[Any]:
        """Filter für passende Nummern; der Vergleich per ``LIKE`` nutzt den Index, das Muster grenzt genau ab."""
        k = Schluessel(feld)
        vorauswahl = StartsWith(k, self.schluessel) if self.mit_kreis else Contains(k, self.schluessel)
        return [vorauswahl, Regex(k, self.muster)]

    def rang(self, feld: str = "reference") -> Case:
        """Rang je Zeile als Datenbankausdruck (1 genau, 2 Endstück genau, 3 sonst passend, 4 nur Textsuche)."""
        k = Schluessel(feld)
        faelle = [When(Exact(k, self.schluessel), then=Value(1))]
        if not self.mit_kreis:
            faelle.append(When(EndsWith(k, TRENNER + self.schluessel), then=Value(2)))
        faelle.append(When(Regex(k, self.muster), then=Value(3)))
        return Case(*faelle, default=Value(4), output_field=IntegerField())

    def rang_von(self, nummer: str | None) -> int:
        """Rang einer einzelnen Nummer (wie ``rang``)."""
        k = schluessel(nummer)
        if k == self.schluessel:
            return 1
        if not self.mit_kreis and k.endswith(TRENNER + self.schluessel):
            return 2
        return 3 if re.search(self.muster, k) else 4


def genau(rang: int | None) -> bool:
    """Gilt der Rang (``nummer_rang``) als genau passende Nummer, die hervorgehoben bzw. direkt geöffnet wird?"""
    return rang is not None and rang <= RANG_GENAU


def nummer(text: str | None) -> Nummer | None:
    """Die Eingabe als ``Nummer`` – oder ``None``, wenn sie nicht wie eine Vorlagennummer aussieht."""
    text = (text or "").strip()
    if not sieht_wie_nummer_aus(text):
        return None
    return Nummer(eingabe=text, schluessel=schluessel(text))


def suchen[T: QuerySet[Any]](
    papers: T,
    text: str,
    *,
    bedingung: Q | None = None,
    reihenfolge: tuple[str, ...] = ("-date", "-oparl_created"),
) -> T:
    """
    Vorgänge aus ``papers`` zu ``text``, sortiert: die eine Funktion für Listen und Suchen (Insight, Work, Drehscheibe).

    Gefunden wird, was ``bedingung`` erfüllt (ohne Angabe: Name oder Nummer enthält ``text``). Sieht ``text`` wie eine
    Vorlagennummer aus, kommen alle Vorgänge mit passender Nummer dazu und stehen vorn (Annotation ``nummer_rang``,
    siehe ``Nummer.rang``); sonst bleibt es bei ``reihenfolge``. Die Nummern werden in einer Unterabfrage auf denselben
    Vorgängen gesucht (gleiche Kommunen und Filter, Ausdrucksindex), nicht für jede Zeile der Textsuche.
    """
    text = text.strip()
    if bedingung is None:
        bedingung = Q(name__icontains=text) | Q(reference__icontains=text)
    nr = nummer(text)
    if nr is None:
        return papers.filter(bedingung).order_by(*reihenfolge)
    treffer = papers.filter(*nr.bedingungen()).values("pk")
    gefunden = papers.filter(bedingung | Q(pk__in=treffer)).annotate(nummer_rang=nr.rang())
    return gefunden.order_by("nummer_rang", *reihenfolge)


def passend[T: QuerySet[Any]](papers: T, text: str, *, reihenfolge: tuple[str, ...] = ("-date", "-oparl_created")) -> T:
    """Nur Vorgänge mit zu ``text`` passender Nummer, nach Rang und ``reihenfolge``; leer, wenn ``text`` keine ist."""
    nr = nummer(text)
    if nr is None:
        return papers.none()
    return papers.filter(*nr.bedingungen()).annotate(nummer_rang=nr.rang()).order_by("nummer_rang", *reihenfolge)
