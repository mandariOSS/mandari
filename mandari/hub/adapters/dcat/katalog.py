# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalogmodell nach DCAT-AP.de 3.0 und seine Datensätze je Kommune.

Das Modell kennt weder Datenbank noch RDF: Die Ausgaben (Aggregator, Session-Mandanten) beschreiben, was eine
Kommune anbietet (``Angebot``); ``datensaetze`` macht daraus die Datensätze mit ihren Distributionen,
``hub.adapters.dcat.rdf`` serialisiert den fertigen ``Katalog``.

Je Kommune gibt es drei Datensätze – so, wie Nutzerinnen und Portale Ratsinformationen suchen:

- **Sitzungen und Tagesordnungen** (``sitzungen``)
- **Vorlagen und Beschlüsse** (``vorlagen``)
- **Gremien und Mandate** (``gremien``)

Jeder Datensatz hat als Distribution die passende Liste der OParl-Schnittstelle, dazu – wenn die Installation
sie anbietet – Änderungsfeed und Snapshot der Kommune sowie vorhandene Exporte (Sitzungskalender).

**Keine Personendaten im Katalog:** Herausgeber, Urheber und Kontakt sind Stellen (Betreiber, Kommune,
Verwaltung), nie Personen. Die Ausgaben reichen nur Namen von Stellen und Funktionsadressen herein.

Kennungen sind Hash-Adressen im Dokument des Katalogs der Kommune (``<katalog>#sitzungen``,
``<katalog>#sitzungen-oparl``). Sie sind stabil, solange die Adresse des Katalogs gleich bleibt, und
gleich, egal in welchem Katalog ein Datensatz erscheint – Portale erkennen ihn so wieder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from django.utils import timezone

from hub.adapters.dcat import vokabular
from hub.adapters.dcat.vokabular import Lizenz, Raumbezug

#: Datensätze je Kommune: Schlüssel in der Kennung und in ``Angebot.kennzahlen``
SITZUNGEN: Final = "sitzungen"
VORLAGEN: Final = "vorlagen"
GREMIEN: Final = "gremien"
DATENSAETZE: Final = (SITZUNGEN, VORLAGEN, GREMIEN)

#: Listen der OParl-Schnittstelle, die ``Angebot.listen`` nennt
LISTE_SITZUNGEN: Final = "meetings"
LISTE_VORLAGEN: Final = "papers"
LISTE_GREMIEN: Final = "organizations"
LISTE_PERSONEN: Final = "people"


# =============================================================================
# Modell
# =============================================================================


@dataclass(frozen=True)
class Stelle:
    """Eine Stelle (``foaf:Agent``): Betreiber, Kommune oder Verwaltung – nie eine Person."""

    uri: str
    name: str
    homepage: str | None = None


@dataclass(frozen=True)
class Kontakt:
    """Kontaktstelle (``vcard:Organization``) mit Funktionsadresse und Webseite."""

    uri: str
    name: str
    email: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Zeitraum:
    """Zeitliche Abdeckung (``dct:PeriodOfTime``); mindestens eine Grenze ist gesetzt."""

    beginn: date | None = None
    ende: date | None = None


def _tag(wert: date | datetime | None) -> date | None:
    """Tag eines Zeitpunkts in der Zeitzone der Installation; ein Datum bleibt, wie es ist."""
    if isinstance(wert, datetime):
        return timezone.localtime(wert).date() if timezone.is_aware(wert) else wert.date()
    return wert


def zeitraum(beginn: date | datetime | None, ende: date | datetime | None = None) -> Zeitraum | None:
    """Zeitraum aus frühestem und spätestem Wert (etwa aus einer Aggregat-Abfrage); ``None`` ohne beide."""
    tag_beginn, tag_ende = _tag(beginn), _tag(ende)
    return Zeitraum(tag_beginn, tag_ende) if tag_beginn or tag_ende else None


@dataclass(frozen=True)
class Kennzahlen:
    """Was die Daten eines Datensatzes über ihn sagen: Zeitraum und letzte Änderung."""

    zeitraum: Zeitraum | None = None
    geaendert: datetime | None = None


@dataclass(frozen=True)
class Distribution:
    """Ein Zugang zu einem Datensatz (``dcat:Distribution``)."""

    uri: str
    titel: str
    beschreibung: str
    zugriff: str
    format: str
    medientyp: str | None = None
    download: str | None = None
    standard: str | None = None
    #: Kennung des Dienstes (``dcat:accessService``), über den die Distribution abrufbar ist
    dienst: str | None = None


@dataclass(frozen=True)
class Datensatz:
    """Ein Datensatz (``dcat:Dataset``) mit allem, was DCAT-AP.de an ihm erwartet."""

    uri: str
    titel: str
    beschreibung: str
    schlagworte: tuple[str, ...]
    herausgeber: Stelle
    lizenz: Lizenz
    distributionen: tuple[Distribution, ...]
    urheber: Stelle | None = None
    kontakt: Kontakt | None = None
    #: Text der Namensnennung, wenn die Lizenz sie verlangt
    namensnennung: str | None = None
    raum: Raumbezug | None = None
    zeitraum: Zeitraum | None = None
    veroeffentlicht: datetime | None = None
    geaendert: datetime | None = None
    webseite: str | None = None
    frequenz: str = vokabular.FREQUENZ_LAUFEND
    #: Kennung des Datenbereitstellers bei GovData (``dcatde:contributorID``)
    bereitsteller: str | None = None


@dataclass(frozen=True)
class Dienst:
    """Die Schnittstelle, über die die Distributionen abrufbar sind (``dcat:DataService``)."""

    uri: str
    titel: str
    beschreibung: str
    endpunkt: str
    herausgeber: Stelle
    datensaetze: tuple[str, ...]
    kontakt: Kontakt | None = None
    dokumentation: str = vokabular.OPARL_SPEZIFIKATION
    standard: str = vokabular.OPARL_STANDARD


@dataclass(frozen=True)
class Katalog:
    """Ein Katalog (``dcat:Catalog``)."""

    uri: str
    titel: str
    beschreibung: str
    herausgeber: Stelle
    datensaetze: tuple[Datensatz, ...]
    dienste: tuple[Dienst, ...] = ()
    homepage: str | None = None
    lizenz: Lizenz | None = None
    raum: Raumbezug | None = None
    veroeffentlicht: datetime | None = None

    @property
    def geaendert(self) -> datetime | None:
        """Letzte Änderung: die jüngste der Datensätze."""
        return max((d.geaendert for d in self.datensaetze if d.geaendert), default=None)


# =============================================================================
# Datensätze einer Kommune
# =============================================================================


@dataclass(frozen=True)
class Angebot:
    """
    Offene Daten einer Kommune, wie eine Ausgabe sie anbietet.

    ``basis`` ist die Adresse des Katalogs der Kommune; aus ihr entstehen die Kennungen der Datensätze.
    ``listen`` nennt die Adressen der OParl-Listen (``meetings``, ``papers``, ``organizations``,
    ``people``), ``kennzahlen`` je Datensatz Zeitraum und letzte Änderung.
    """

    basis: str
    kommune: str
    herausgeber: Stelle
    lizenz: Lizenz
    listen: Mapping[str, str]
    dienst: str
    urheber: Stelle | None = None
    kontakt: Kontakt | None = None
    raum: Raumbezug | None = None
    veroeffentlicht: datetime | None = None
    webseite: str | None = None
    feed: str | None = None
    snapshot: str | None = None
    kalender: str | None = None
    kennzahlen: Mapping[str, Kennzahlen] = field(default_factory=dict)
    frequenz: str = vokabular.FREQUENZ_LAUFEND
    bereitsteller: str | None = None


def namensnennung(angebot: Angebot) -> str | None:
    """Text der Namensnennung für Lizenzen, die sie verlangen: die Kommune als Datenbereitsteller."""
    return angebot.kommune if angebot.lizenz.namensnennung else None


def _liste(angebot: Angebot, datensatz: str, liste: str, titel: str, beschreibung: str) -> Distribution | None:
    adresse = angebot.listen.get(liste)
    if not adresse:
        return None
    return Distribution(
        uri=f"{angebot.basis}#{datensatz}-oparl-{liste}",
        titel=titel,
        beschreibung=beschreibung,
        zugriff=adresse,
        format=vokabular.FORMAT_JSON,
        medientyp=vokabular.MEDIENTYP_JSON,
        standard=vokabular.OPARL_STANDARD,
        dienst=angebot.dienst,
    )


def _aenderungen(angebot: Angebot, datensatz: str) -> tuple[Distribution, ...]:
    """Änderungsfeed und Snapshot: Sie gelten für alle Objekte der Kommune, also für jeden Datensatz."""
    zugaenge: list[Distribution] = []
    if angebot.feed:
        zugaenge.append(
            Distribution(
                uri=f"{angebot.basis}#{datensatz}-aenderungen",
                titel="Änderungsfeed der Kommune",
                beschreibung=(
                    "Neue, geänderte und entfernte Objekte der Kommune in fester Reihenfolge, seitenweise mit "
                    "Cursor; die Inhalte liefert die OParl-Schnittstelle. Kompatible Erweiterung von OParl 1.1."
                ),
                zugriff=angebot.feed,
                format=vokabular.FORMAT_JSON,
                medientyp=vokabular.MEDIENTYP_JSON,
                dienst=angebot.dienst,
            )
        )
    if angebot.snapshot:
        zugaenge.append(
            Distribution(
                uri=f"{angebot.basis}#{datensatz}-snapshot",
                titel="Gesamtstand der Kommune (Snapshot)",
                beschreibung=(
                    "Alle öffentlichen Objekte der Kommune in einer Datei, ein JSON-Objekt je Zeile (NDJSON), mit "
                    "dem Cursor, ab dem der Änderungsfeed fortsetzt."
                ),
                zugriff=angebot.snapshot,
                download=angebot.snapshot,
                format=vokabular.FORMAT_JSON,
                dienst=angebot.dienst,
            )
        )
    return tuple(zugaenge)


def _kalender(angebot: Angebot) -> tuple[Distribution, ...]:
    if not angebot.kalender:
        return ()
    return (
        Distribution(
            uri=f"{angebot.basis}#{SITZUNGEN}-kalender",
            titel="Sitzungskalender (iCalendar)",
            beschreibung=(
                "Öffentliche Sitzungen der Kommune als abonnierbarer Kalender, drei Monate zurück und gut ein Jahr "
                "voraus."
            ),
            zugriff=angebot.kalender,
            download=angebot.kalender,
            format=vokabular.FORMAT_ICS,
            medientyp=vokabular.MEDIENTYP_KALENDER,
        ),
    )


def _zugaenge(*teile: Distribution | tuple[Distribution, ...] | None) -> tuple[Distribution, ...]:
    ergebnis: list[Distribution] = []
    for teil in teile:
        if isinstance(teil, Distribution):
            ergebnis.append(teil)
        elif teil:
            ergebnis.extend(teil)
    return tuple(ergebnis)


def datensaetze(angebot: Angebot) -> tuple[Datensatz, ...]:
    """Die drei Datensätze einer Kommune; ohne eine Distribution entfällt ein Datensatz."""
    k = angebot.kommune
    beschreibungen = {
        SITZUNGEN: (
            f"Sitzungen und Tagesordnungen – {k}",
            (
                f"Sitzungen des Rats, der Ausschüsse und der übrigen Gremien von {k} mit Termin, Ort und "
                "Tagesordnung, dazu Niederschriften und Anlagen, soweit sie öffentlich sind."
            ),
            ("Sitzung", "Tagesordnung", "Niederschrift"),
            _zugaenge(
                _liste(
                    angebot,
                    SITZUNGEN,
                    LISTE_SITZUNGEN,
                    "Sitzungen (OParl-Liste)",
                    "Alle öffentlichen Sitzungen mit eingebetteten Tagesordnungspunkten und Dateien, seitenweise, "
                    "filterbar nach Änderungszeit.",
                ),
                _kalender(angebot),
                _aenderungen(angebot, SITZUNGEN),
            ),
        ),
        VORLAGEN: (
            f"Vorlagen und Beschlüsse – {k}",
            (
                f"Beschlussvorlagen, Anträge, Anfragen und Mitteilungen von {k} mit ihren Beratungen in den "
                "Gremien, den gefassten Beschlüssen und den zugehörigen Dokumenten, soweit sie öffentlich sind."
            ),
            ("Vorlage", "Beschluss", "Antrag", "Anfrage"),
            _zugaenge(
                _liste(
                    angebot,
                    VORLAGEN,
                    LISTE_VORLAGEN,
                    "Vorlagen (OParl-Liste)",
                    "Alle öffentlichen Vorlagen mit Beratungsfolge und Dateien, seitenweise, filterbar nach "
                    "Änderungszeit.",
                ),
                _aenderungen(angebot, VORLAGEN),
            ),
        ),
        GREMIEN: (
            f"Gremien und Mandate – {k}",
            (
                f"Rat, Ausschüsse, Beiräte und Fraktionen von {k} mit ihren Mitgliedschaften, Rollen und "
                "Wahlperioden sowie die Mandatsträgerinnen und Mandatsträger in ihrer öffentlichen Funktion."
            ),
            ("Gremium", "Ausschuss", "Fraktion", "Mandat", "Wahlperiode"),
            _zugaenge(
                _liste(
                    angebot,
                    GREMIEN,
                    LISTE_GREMIEN,
                    "Gremien (OParl-Liste)",
                    "Alle Gremien mit ihren Mitgliedschaften, seitenweise, filterbar nach Änderungszeit.",
                ),
                _liste(
                    angebot,
                    GREMIEN,
                    LISTE_PERSONEN,
                    "Mandatsträgerinnen und Mandatsträger (OParl-Liste)",
                    "Personen in ihrer öffentlichen Funktion mit ihren Mitgliedschaften, seitenweise, filterbar "
                    "nach Änderungszeit.",
                ),
                _aenderungen(angebot, GREMIEN),
            ),
        ),
    }
    ergebnis: list[Datensatz] = []
    for schluessel in DATENSAETZE:
        titel, beschreibung, worte, zugaenge = beschreibungen[schluessel]
        if not zugaenge:
            continue
        kennzahlen = angebot.kennzahlen.get(schluessel) or Kennzahlen()
        ergebnis.append(
            Datensatz(
                uri=f"{angebot.basis}#{schluessel}",
                titel=titel,
                beschreibung=beschreibung,
                schlagworte=("Ratsinformationen", "Kommunalpolitik", *worte, "OParl", k),
                herausgeber=angebot.herausgeber,
                lizenz=angebot.lizenz,
                distributionen=zugaenge,
                urheber=angebot.urheber,
                kontakt=angebot.kontakt,
                namensnennung=namensnennung(angebot),
                raum=angebot.raum,
                zeitraum=kennzahlen.zeitraum,
                veroeffentlicht=angebot.veroeffentlicht,
                geaendert=kennzahlen.geaendert,
                webseite=angebot.webseite,
                frequenz=angebot.frequenz,
                bereitsteller=angebot.bereitsteller,
            )
        )
    return tuple(ergebnis)
