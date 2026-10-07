# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionszuordnung für Personen ohne OParl-Fraktion (Issue #916, Modell ``PersonFraktion``).

Viele Kommunen liefern über OParl keine Fraktionsmitgliedschaften. Insight ordnet Personen deshalb selbst eine
Fraktion zu: aus der Einblendung einer Live-Übertragung (``fraktion_aus_einblendung``, aufgerufen von der
Live-Erkennung, Issue #915), von Hand im Admin (``speichern``, ``bestaetigen``, ``ablehnen``) oder aus OParl.

Regeln der automatischen Übernahme (``fraktion_aus_einblendung``):

- **Gleiche bestätigte Zuordnung:** zählt eine Lesung mehr (``belege``) und merkt sich den Zeitpunkt.
- **Keine bestätigte Zuordnung:** Ist die Person eindeutig zugeordnet und mehrfach gleich gelesen (``eindeutig``),
  entsteht eine bestätigte Zuordnung mit Quelle ``einblendung``; sonst ein Vorschlag. Ein vorhandener Vorschlag
  gleicher Bezeichnung zählt hoch bzw. wird bestätigt.
- **Abweichende Bezeichnung** bei bestehender bestätigter Zuordnung: nur ein Vorschlag, nie ein Überschreiben.
  Von Hand gepflegte Werte ändert der automatische Weg damit nie; sie gehen bei der Anzeige auch vor.
- **Entscheidungen bleiben:** Eine abgelehnte Bezeichnung oder eine beendete Zuordnung gleicher Bezeichnung
  wird nicht automatisch wieder bestätigt, die Lesung zählt nur mit.
- **Funktionsbezeichnungen** („Oberbürgermeisterin“, „Beigeordneter“) sind keine Fraktion; der Aufrufer filtert
  sie, ``ist_funktionsbezeichnung`` fängt die üblichen zur Sicherheit noch einmal ab.
- **Lesefehler** führen nicht zu Dubletten: Die gelesene Bezeichnung wird bereinigt und unscharf mit den
  vorhandenen Bezeichnungen derselben Körperschaft abgeglichen (``kanonische_bezeichnung``). Umlaute, die die
  Texterkennung mal richtig, mal ohne Punkte oder als Ersatzzeichen liest, zählen gleich; eine Ähnlichkeit ab 0,85
  oder ein abgeschnittener Anfang einer vorhandenen Bezeichnung ergibt deren Schreibweise. Maßgeblich ist die von
  Hand gepflegte, dann die bestätigte Schreibweise.

Nebenläufigkeit: Die Zuordnungen einer Person in ihrer Körperschaft werden gesperrt (``select_for_update``);
legen zwei Lesungen gleichzeitig die erste Zuordnung an, verhindert ein Unique-Constraint die doppelte, und die
zweite Lesung zählt nach einem neuen Versuch die erste hoch. Jede Änderung an einer bestätigten Zuordnung meldet
``ris.person.faction_assigned`` im selben ``transaction.atomic``-Block (``hub.ris.faction_assignment``).

Es werden nur Bezeichnung, Zeitpunkt und Zahl der Lesungen gespeichert, nie Bild- oder Tondaten.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from ..models import OParlBody, OParlPerson, PersonFraktion

#: Wortteile von Funktionsbezeichnungen in der Vergleichsform (``_normal``: klein, ohne Umlautpunkte). Einblendungen
#: zeigen die Funktion oft an der Stelle der Fraktion; ein Wort, das einen dieser Teile enthält, ist keine Fraktion.
_FUNKTION_TEILE = (
    "burgermeister",
    "beigeordnet",
    "dezernent",
    "stadtdirektor",
    "kreisdirektor",
    "kammerer",
    "kammerin",
    "kammerei",
    "landrat",
    "verwaltung",
    "schriftfuhr",
    "protokoll",
    "vorsitz",
    "ratsmitglied",
    "sachkundig",
)
#: Ganze Funktionsbezeichnungen (Vergleichsform), die auch verlesen erkannt werden („Oberbiirgermeister“,
#: „Stadtkämrnerin“). „Stadtrat“ nur als ganzes Wort: „Stadtratsfraktion“ ist eine Fraktion.
_FUNKTION_WOERTER = (
    "oberburgermeister",
    "oberburgermeisterin",
    "burgermeister",
    "burgermeisterin",
    "beigeordneter",
    "beigeordnete",
    "stadtkammerer",
    "stadtkammerin",
    "kammerer",
    "kammerin",
    "stadtdirektor",
    "stadtdirektorin",
    "kreisdirektor",
    "kreisdirektorin",
    "dezernent",
    "dezernentin",
    "stadtrat",
    "stadtratin",
    "landrat",
    "landratin",
    "schriftfuhrer",
    "schriftfuhrerin",
    "vorsitzender",
    "vorsitzende",
)
#: Zeichen, mit denen Einblendungen Felder trennen oder kürzen („Fraktion A |“, „– Fraktion A“, „Fraktion Ab…“)
_RAND = " \t\r\n|·•:;,.…–—-_/\\"

#: Ab dieser Ähnlichkeit (0 bis 1) gelten zwei gelesene Bezeichnungen als dieselbe Fraktion
SCHWELLE = 0.85
#: Ein abgeschnittener Anfang zählt erst ab so vielen Zeichen (normalisiert), sonst passt „Fraktion“ auf alles
PRAEFIX_MIN = 12
#: Ähnlichkeit eines abgeschnittenen Anfangs einer längeren Bezeichnung
_PRAEFIX_WERT = 0.95
#: Längste Abweichung am Stück, die als Lesefehler gilt („ü“ als „ii“, „rn“ als „m“). Ein ganzes anderes Wort
#: („Nord“ statt „Süd“) ist ein Unterschied, auch wenn der Rest lang und gleich ist.
_LESEFEHLER_MAX = 2
#: Ähnlichkeit zweier Bezeichnungen, die sich in einem ganzen Wort unterscheiden (unter ``SCHWELLE``)
_UNTERSCHIED = 0.5
#: Buchstaben, die die Texterkennung oft verliert; eine Schreibweise mit ihnen ist eher die richtige
_UMLAUTE = frozenset("äöüÄÖÜß")
#: Wörter ohne Unterscheidungskraft: Ohne sie wären „Fraktion A“ und „Fraktion B“ zu 90 % gleich
_ALLGEMEIN = frozenset(
    {"fraktion", "ratsfraktion", "kreistagsfraktion", "gruppe", "ratsgruppe", "die", "der", "im", "rat"}
)

#: Reihenfolge, wenn mehrere bestätigte Zuordnungen zugleich gelten: von Hand gepflegt geht vor
_VORRANG = {PersonFraktion.QUELLE_HAND: 0, PersonFraktion.QUELLE_OPARL: 1, PersonFraktion.QUELLE_EINBLENDUNG: 2}

#: Versuche, wenn eine gleichzeitige Lesung dieselbe Zuordnung schon angelegt hat
_VERSUCHE = 3


def bereinigen(bezeichnung: str | None) -> str:
    """
    Bezeichnung ohne Ersatz- und Steuerzeichen, Trennzeichen am Rand und doppelten Leerraum, höchstens 200 Zeichen.

    Getrennt gelesene Umlaute („u“ mit Punkten als eigenem Zeichen) werden wieder ein Zeichen (NFC).
    """
    text = unicodedata.normalize("NFC", bezeichnung or "")
    text = "".join(z for z in text if z != "\ufffd" and (z.isspace() or unicodedata.category(z)[0] != "C"))
    text = " ".join(text.split()).strip(_RAND)
    if not any(zeichen.isalpha() for zeichen in text):
        return ""
    return text[: PersonFraktion.BEZEICHNUNG_MAX_LENGTH].strip()


def ist_funktionsbezeichnung(bezeichnung: str) -> bool:
    """
    Ist das eine Funktion („Oberbürgermeister“, „Stadtkämmerin“, „Beigeordnete“) statt einer Fraktion?

    Unscharf wie die Texterkennung: ohne Umlautpunkte und Ersatzzeichen („Stadtkammerer“, „Oberbu?rgermeisterin“),
    und ein Wort, das einer Funktionsbezeichnung bis auf kleine Lesefehler gleicht („Oberbiirgermeister“), zählt auch.
    """
    for wort in _normal(bezeichnung)[0].split():
        if any(teil in wort for teil in _FUNKTION_TEILE):
            return True
        if any(_lesefehler(wort, funktion) for funktion in _FUNKTION_WOERTER if abs(len(wort) - len(funktion)) <= 2):
            return True
    return False


def _laengste_abweichung(a: str, b: str) -> int:
    """Längstes Stück am Stück, in dem sich zwei Vergleichsformen unterscheiden (in Zeichen)."""
    abschnitte = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return max((max(i2 - i1, j2 - j1) for art, i1, i2, j1, j2 in abschnitte if art != "equal"), default=0)


def _lesefehler(a: str, b: str) -> bool:
    """Unterscheiden sich zwei Vergleichsformen nur durch kleine Lesefehler (ähnlich, kein ganzes anderes Wort)?"""
    if a == b:
        return True
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio >= SCHWELLE and _laengste_abweichung(a, b) <= _LESEFEHLER_MAX


def _normal(bezeichnung: str) -> tuple[str, str]:
    """
    Vergleichsformen: (alle Wörter, Wörter ohne allgemeine wie „Fraktion“), klein, ohne Umlautpunkte und Zeichen.

    „Bürgerliste Süd“, „Buergerliste Sued“, „Burgerliste Sud“ und „Bu?rgerliste Su?d“ (mit Ersatzzeichen) ergeben
    dasselbe.
    """
    text = unicodedata.normalize("NFKD", bezeichnung.replace("\ufffd", "").casefold())
    text = "".join(z for z in text if not unicodedata.combining(z))
    for umschrift, buchstabe in (("ae", "a"), ("oe", "o"), ("ue", "u")):
        text = text.replace(umschrift, buchstabe)
    woerter = re.findall(r"[0-9a-z]+", text)
    kern = [wort for wort in woerter if wort not in _ALLGEMEIN]
    return " ".join(woerter), " ".join(kern or woerter)


def aehnlichkeit(a: str, b: str) -> float:
    """
    Wie sicher meinen zwei gelesene Bezeichnungen dieselbe Fraktion (0 bis 1)?

    Gleich ohne allgemeine Wörter und Umlautpunkte: 1. Sonst die Ähnlichkeit der unterscheidenden Wörter
    (``difflib``); weicht ein Stück von mehr als ``_LESEFEHLER_MAX`` Zeichen ab (ein anderes Wort wie „Nord“ statt
    „Süd“), bleibt sie unter ``SCHWELLE``. Mindestens 0,95, wenn die eine der abgeschnittene Anfang der anderen ist
    (ab ``PRAEFIX_MIN`` Zeichen, auch mit kleinen Lesefehlern).
    """
    voll_a, kern_a = _normal(a)
    voll_b, kern_b = _normal(b)
    if not kern_a or not kern_b:
        return 0.0
    if kern_a == kern_b:
        return 1.0
    wert = SequenceMatcher(None, kern_a, kern_b, autojunk=False).ratio()
    if wert >= SCHWELLE and _laengste_abweichung(kern_a, kern_b) > _LESEFEHLER_MAX:
        wert = _UNTERSCHIED
    if _abgeschnitten(voll_a, voll_b) or _abgeschnitten(voll_b, voll_a):
        wert = max(wert, _PRAEFIX_WERT)
    return wert


def _abgeschnitten(kurz: str, lang: str) -> bool:
    """Ist ``kurz`` (Vergleichsform) der abgeschnittene, ggf. verlesene Anfang von ``lang``?"""
    if len(kurz) < PRAEFIX_MIN or len(kurz) >= len(lang):
        return False
    return _lesefehler(kurz, lang[: len(kurz)])


def _vollstaendig(text: str, andere: Iterable[str]) -> bool:
    """Ist ``text`` nicht nur der abgeschnittene Anfang einer anderen Schreibweise derselben Fraktion?"""
    voll = _normal(text)[0]
    return not any(_abgeschnitten(voll, _normal(anders)[0]) for anders in andere if anders != text)


def gleiche_fraktion(a: str, b: str) -> bool:
    """Meinen zwei gelesene Bezeichnungen dieselbe Fraktion (Lesefehler, abgeschnitten)?"""
    return aehnlichkeit(a, b) >= SCHWELLE


def kanonische_bezeichnung(body: OParlBody, bezeichnung: str) -> str:
    """
    Schreibweise einer gelesenen Fraktion, wie sie in der Körperschaft schon vorkommt; sonst die bereinigte Lesung.

    Verglichen wird mit den bestätigten Zuordnungen und Vorschlägen derselben Körperschaft (``aehnlichkeit``). Unter
    den Schreibweisen derselben Fraktion gilt die von Hand gepflegte (auch an einem im Admin bearbeiteten Vorschlag),
    dann die aus OParl, dann die bestätigte. Gibt es nur gelesene Vorschläge, zählt auch die Lesung selbst mit: die vollständige vor einer abgeschnittenen, die mit Umlauten
    vor einer ohne („Bürgerliste“ vor „Burgerliste“), dann die häufigste. Vorschläge übernehmen diese Schreibweise
    beim Verbuchen. Passt eine Lesung gleich gut zu zwei verschiedenen Fraktionen (abgeschnitten vor dem
    Unterschied), bleibt sie, wie sie ist.
    """
    name = bereinigen(bezeichnung)
    if not name:
        return ""
    # Je Schreibweise: bester Rang der Quelle, bester Status (bestätigt vor Vorschlag), Lesungen insgesamt
    schreibweisen: dict[str, tuple[int, int, int]] = {}
    vorhanden = PersonFraktion.objects.filter(
        body_id=body.pk, status__in=[PersonFraktion.STATUS_BESTAETIGT, PersonFraktion.STATUS_VORSCHLAG]
    ).values_list("bezeichnung", "quelle", "status", "belege")
    for text, quelle, status, belege in vorhanden:
        rang = (_VORRANG.get(quelle, 9), 0 if status == PersonFraktion.STATUS_BESTAETIGT else 1, belege)
        bisher = schreibweisen.get(text)
        if bisher is not None:
            rang = (min(bisher[0], rang[0]), min(bisher[1], rang[1]), bisher[2] + belege)
        schreibweisen[text] = rang
    passend = {text: wert for text in schreibweisen if (wert := aehnlichkeit(name, text)) >= SCHWELLE}
    if not passend:
        return name
    bester_wert = max(passend.values())
    beste = [text for text, wert in passend.items() if wert == bester_wert]
    if any(not gleiche_fraktion(beste[0], text) for text in beste[1:]):
        return name
    gruppe = [text for text in passend if gleiche_fraktion(text, beste[0])]
    # Fest steht eine bestätigte Schreibweise oder eine von Hand gesetzte (auch an einem Vorschlag)
    hand = _VORRANG[PersonFraktion.QUELLE_HAND]
    fest = [text for text in gruppe if schreibweisen[text][1] == 0 or schreibweisen[text][0] == hand]
    if fest:
        return min(fest, key=lambda text: (schreibweisen[text][0], -schreibweisen[text][2], text))
    kandidaten = [*gruppe, name] if name not in schreibweisen else gruppe
    return min(
        kandidaten,
        key=lambda text: (
            not _vollstaendig(text, kandidaten),
            not (_UMLAUTE & set(text)),
            -schreibweisen.get(text, (9, 9, 0))[2],
            text,
        ),
    )


def _gleich(zuordnung: PersonFraktion, bezeichnung: str) -> bool:
    if zuordnung.bezeichnung.casefold() == bezeichnung.casefold():
        return True
    return gleiche_fraktion(zuordnung.bezeichnung, bezeichnung)


def _passend(zuordnungen: Iterable[PersonFraktion], bezeichnung: str) -> PersonFraktion | None:
    """Zuordnung derselben Fraktion: genaue Schreibweise vor ähnlicher, dann nach Vorrang."""

    def reihenfolge(zuordnung: PersonFraktion) -> tuple[bool, float, tuple[int, int, float]]:
        genau = zuordnung.bezeichnung.casefold() == bezeichnung.casefold()
        return (not genau, -aehnlichkeit(zuordnung.bezeichnung, bezeichnung), _vorrang(zuordnung))

    return min((z for z in zuordnungen if _gleich(z, bezeichnung)), key=reihenfolge, default=None)


def _vorrang(zuordnung: PersonFraktion) -> tuple[int, int, float]:
    """Sortierschlüssel: Quelle (Hand zuerst), späterer Beginn, zuletzt geändert."""
    beginn = zuordnung.gueltig_ab.toordinal() if zuordnung.gueltig_ab else 0
    geaendert = zuordnung.geaendert.timestamp() if zuordnung.geaendert else 0.0
    return (_VORRANG.get(zuordnung.quelle, 9), -beginn, -geaendert)


def _bestaetigt_am(stichtag: date) -> Q:
    return (
        Q(status=PersonFraktion.STATUS_BESTAETIGT)
        & (Q(gueltig_ab__isnull=True) | Q(gueltig_ab__lte=stichtag))
        & (Q(gueltig_bis__isnull=True) | Q(gueltig_bis__gte=stichtag))
    )


# =============================================================================
# Lesen
# =============================================================================


def aktuelle_fraktion(person: OParlPerson, body: OParlBody, *, stichtag: date | None = None) -> PersonFraktion | None:
    """Bestätigte Zuordnung der Person in der Körperschaft am Stichtag (Standard: heute); Hand geht vor."""
    stichtag = stichtag or timezone.localdate()
    kandidaten = PersonFraktion.objects.filter(_bestaetigt_am(stichtag), person_id=person.pk, body_id=body.pk)
    return min(kandidaten, key=_vorrang, default=None)


def aktuelle_fraktionen(personen: Iterable[OParlPerson], *, stichtag: date | None = None) -> dict[Any, PersonFraktion]:
    """``{person_id: PersonFraktion}`` für viele Personen in ihrer Körperschaft, eine Abfrage."""
    koerperschaft = {person.pk: person.body_id for person in personen}
    if not koerperschaft:
        return {}
    stichtag = stichtag or timezone.localdate()
    ergebnis: dict[Any, PersonFraktion] = {}
    for zuordnung in PersonFraktion.objects.filter(_bestaetigt_am(stichtag), person_id__in=list(koerperschaft)):
        if zuordnung.body_id != koerperschaft[zuordnung.person_id]:
            continue
        bisher = ergebnis.get(zuordnung.person_id)
        if bisher is None or _vorrang(zuordnung) < _vorrang(bisher):
            ergebnis[zuordnung.person_id] = zuordnung
    return ergebnis


# =============================================================================
# Automatische Übernahme aus der Einblendung (Schnittstelle für die Live-Erkennung)
# =============================================================================


def fraktion_aus_einblendung(
    *, person: OParlPerson, body: OParlBody, bezeichnung: str, zeitpunkt: datetime, eindeutig: bool
) -> PersonFraktion | None:
    """
    Verbucht eine gelesene Fraktion aus der Einblendung einer Live-Übertragung (Regeln: Moduldokumentation).

    - ``person``/``body``: zugeordnete Person und Körperschaft der Sitzung
    - ``bezeichnung``: gelesene Fraktion (ohne Funktionsbezeichnung); Lesefehler gleicht ``kanonische_bezeichnung`` aus
    - ``zeitpunkt``: Zeitpunkt der Lesung (mit Zeitzone)
    - ``eindeutig``: Die Person war eindeutig zugeordnet und die Fraktion mehrfach gleich gelesen

    Rückgabe: die betroffene Zuordnung (bestätigt, Vorschlag oder abgelehnt; maßgeblich ist ``status``) oder
    ``None``, wenn die Bezeichnung leer oder eine Funktionsbezeichnung ist.
    """
    if timezone.is_naive(zeitpunkt):
        raise ValueError("zeitpunkt braucht eine Zeitzone")
    name = bereinigen(bezeichnung)
    if not name or ist_funktionsbezeichnung(name):
        return None
    stichtag = timezone.localdate(zeitpunkt)
    for versuch in range(_VERSUCHE):
        try:
            with transaction.atomic():
                return _verbuchen(person, body, kanonische_bezeichnung(body, name), zeitpunkt, stichtag, eindeutig)
        except IntegrityError:
            # Eine gleichzeitige Lesung hat die Zuordnung eben angelegt; neu lesen und hochzählen
            if versuch == _VERSUCHE - 1:
                raise
    return None  # pragma: no cover – die Schleife endet mit return oder raise


def _verbuchen(
    person: OParlPerson, body: OParlBody, name: str, zeitpunkt: datetime, stichtag: date, eindeutig: bool
) -> PersonFraktion:
    zuordnungen = list(PersonFraktion.objects.select_for_update().filter(person_id=person.pk, body_id=body.pk))
    # Bestätigte Zuordnungen, die am Tag der Lesung nicht schon beendet sind (auch solche mit späterem Beginn)
    laufend = [
        z
        for z in zuordnungen
        if z.status == PersonFraktion.STATUS_BESTAETIGT and (z.gueltig_bis is None or z.gueltig_bis >= stichtag)
    ]
    treffer = _passend(laufend, name)
    if treffer is None:
        # Entscheidungen von Hand bleiben: abgelehnt bleibt abgelehnt
        treffer = _passend((z for z in zuordnungen if z.status == PersonFraktion.STATUS_ABGELEHNT), name)
    if treffer is not None:
        return _lesung_zaehlen(treffer, zeitpunkt)

    vorschlag = _passend((z for z in zuordnungen if z.status == PersonFraktion.STATUS_VORSCHLAG), name)
    belegt = {z.bezeichnung.casefold() for z in zuordnungen if z.status == PersonFraktion.STATUS_VORSCHLAG}
    if (
        vorschlag is not None
        and vorschlag.quelle != PersonFraktion.QUELLE_HAND
        and vorschlag.bezeichnung != name
        and name.casefold() not in belegt - {vorschlag.bezeichnung.casefold()}
    ):
        # Ein früher gelesener Vorschlag übernimmt die kanonische Schreibweise; eine im Admin gesetzte bleibt
        vorschlag.bezeichnung = name
        vorschlag.save(update_fields=["bezeichnung", "geaendert"])
    # Eine beendete Zuordnung gleicher Bezeichnung hat jemand beendet; sie kommt nicht von selbst zurück
    beendet = any(z.status == PersonFraktion.STATUS_BESTAETIGT and _gleich(z, name) for z in zuordnungen)
    if eindeutig and not laufend and not beendet:
        if vorschlag is not None:
            vorschlag.status = PersonFraktion.STATUS_BESTAETIGT
            vorschlag.gueltig_ab = vorschlag.gueltig_ab or stichtag
            vorschlag.save(update_fields=["status", "gueltig_ab", "geaendert"])
            zuordnung = _lesung_zaehlen(vorschlag, zeitpunkt)
        else:
            zuordnung = PersonFraktion.objects.create(
                person=person,
                body=body,
                bezeichnung=name,
                quelle=PersonFraktion.QUELLE_EINBLENDUNG,
                status=PersonFraktion.STATUS_BESTAETIGT,
                gueltig_ab=stichtag,
                belege=1,
                zuletzt_gesehen=zeitpunkt,
            )
        _melden(zuordnung)
        return zuordnung

    if vorschlag is not None:
        return _lesung_zaehlen(vorschlag, zeitpunkt)
    return PersonFraktion.objects.create(
        person=person,
        body=body,
        bezeichnung=name,
        quelle=PersonFraktion.QUELLE_EINBLENDUNG,
        status=PersonFraktion.STATUS_VORSCHLAG,
        gueltig_ab=stichtag,
        belege=1,
        zuletzt_gesehen=zeitpunkt,
    )


def _lesung_zaehlen(zuordnung: PersonFraktion, zeitpunkt: datetime) -> PersonFraktion:
    """Eine Lesung mehr; ``zuletzt_gesehen`` geht nie zurück (Nachverarbeitung älterer Aufzeichnungen)."""
    zuletzt = max(filter(None, (zuordnung.zuletzt_gesehen, zeitpunkt)))
    PersonFraktion.objects.filter(pk=zuordnung.pk).update(
        belege=F("belege") + 1, zuletzt_gesehen=zuletzt, geaendert=timezone.now()
    )
    zuordnung.refresh_from_db(fields=["belege", "zuletzt_gesehen", "geaendert"])
    return zuordnung


# =============================================================================
# Pflege im Admin
# =============================================================================


def speichern(zuordnung: PersonFraktion) -> list[PersonFraktion]:
    """
    Zuordnung aus dem Admin speichern (neu oder geändert); Rückgabe: dadurch beendete andere Zuordnungen.

    Eine laufende bestätigte Zuordnung beendet die bisherige laufende derselben Person in derselben Körperschaft
    (Ende am Vortag ihres Beginns). Jede Änderung an einer bestätigten Zuordnung und jede Ablehnung einer bisher
    bestätigten wird gemeldet.
    """
    with transaction.atomic():
        zuordnungen = list(
            PersonFraktion.objects.select_for_update().filter(person_id=zuordnung.person_id, body_id=zuordnung.body_id)
        )
        vorher = next((z.status for z in zuordnungen if z.pk == zuordnung.pk), None)
        if vorher is None and not zuordnung._state.adding:
            # Person oder Körperschaft im Formular geändert: alter Stand aus der Datenbank
            vorher = PersonFraktion.objects.filter(pk=zuordnung.pk).values_list("status", flat=True).first()
        beendet: list[PersonFraktion] = []
        if zuordnung.status == PersonFraktion.STATUS_BESTAETIGT and zuordnung.gueltig_bis is None:
            beginn = zuordnung.gueltig_ab or timezone.localdate()
            for andere in zuordnungen:
                if (
                    andere.pk != zuordnung.pk
                    and andere.status == PersonFraktion.STATUS_BESTAETIGT
                    and andere.gueltig_bis is None
                ):
                    ende = beginn - timedelta(days=1)
                    andere.gueltig_bis = max(ende, andere.gueltig_ab) if andere.gueltig_ab else ende
                    andere.save(update_fields=["gueltig_bis", "geaendert"])
                    _melden(andere)
                    beendet.append(andere)
        zuordnung.save()
        if zuordnung.status == PersonFraktion.STATUS_BESTAETIGT or vorher == PersonFraktion.STATUS_BESTAETIGT:
            _melden(zuordnung)
    return beendet


def bestaetigen(zuordnung: PersonFraktion) -> list[PersonFraktion]:
    """Vorschlag (oder abgelehnte Zuordnung) bestätigen; Rückgabe: dadurch beendete andere Zuordnungen."""
    if zuordnung.status == PersonFraktion.STATUS_BESTAETIGT:
        return []
    zuordnung.status = PersonFraktion.STATUS_BESTAETIGT
    zuordnung.gueltig_ab = zuordnung.gueltig_ab or timezone.localdate()
    return speichern(zuordnung)


def ablehnen(zuordnung: PersonFraktion) -> bool:
    """Zuordnung ablehnen (auch eine bestätigte, etwa nach falscher Personenzuordnung); ``False``, wenn schon abgelehnt."""
    if zuordnung.status == PersonFraktion.STATUS_ABGELEHNT:
        return False
    zuordnung.status = PersonFraktion.STATUS_ABGELEHNT
    speichern(zuordnung)
    return True


def _melden(zuordnung: PersonFraktion) -> None:
    """Stand einer bestätigten bzw. abgelehnten Zuordnung an die Datendrehscheibe (laufende Transaktion)."""
    from hub.ris.faction_assignment import report_faction_assigned

    if zuordnung.status == PersonFraktion.STATUS_VORSCHLAG:
        return
    report_faction_assigned(
        assignment_id=zuordnung.pk,
        person_id=zuordnung.person_id,
        body_id=zuordnung.body_id,
        source=zuordnung.quelle,
        status=zuordnung.status,
        organization_id=zuordnung.organisation_id,
        valid_from=zuordnung.gueltig_ab,
        valid_until=zuordnung.gueltig_bis,
    )
