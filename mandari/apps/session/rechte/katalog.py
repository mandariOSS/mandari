# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtekatalog nach Objektart und Aktion (Issue #772, ADR „Rechte mit Geltungsbereich“).

Jedes Recht hat eine Kennung ``objektart.aktion`` und – solange es die Häkchen der Rolle gibt – eine **Herkunft**:
den bisherigen Rechtenamen (``approve_papers`` für ``can_approve_papers``), der heute erlaubt, was das Recht
beschreibt. Je Häkchen ist genau ein Recht das **Leitrecht**; das Häkchen gilt in der verträglichen Prüfschicht,
wenn sein Leitrecht mandantenweit gilt. Weitere Rechte derselben Herkunft bilden ab, was das Häkchen heute
zusätzlich erlaubt (``edit_meetings`` deckt Laden, Umlaufverfahren, Beschlusskontrolle und Terminieren).

Rechte ohne Herkunft gibt es heute noch nicht als Funktion; bis zur Rollenmatrix (#775) hat sie nur die
Administrator-Rolle. Kontrollrechte (#221) gehören nie zur Administrator-Vollmacht.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

#: Muster einer Kennung: ``objektart.aktion``, Kleinbuchstaben, Ziffern und einfache Unterstriche
KENNUNG_MUSTER: Final = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

#: Objektarten in der Reihenfolge der Rollenmatrix
OBJEKTARTEN: Final[dict[str, str]] = {
    "uebersicht": "Übersicht und Suche",
    "sitzung": "Sitzung",
    "tagesordnung": "Tagesordnung und TOP",
    "vorlage": "Vorlage",
    "antrag": "Antrag, Anfrage",
    "niederschrift": "Niederschrift",
    "beschluss": "Beschluss",
    "umlauf": "Umlaufverfahren",
    "person": "Person, Besetzung",
    "gremium": "Gremium",
    "koerperschaft": "Körperschaft",
    "sitzungsgeld": "Sitzungsgeld, Pauschalen",
    "endgeraet": "Endgeräte",
    "ablauf": "Abläufe",
    "konto": "Rollen, Konten, Vertretungen",
    "einstellung": "Einstellungen, Schnittstellen, Veröffentlichung",
    "pruefprotokoll": "Prüfprotokoll",
}


@dataclass(frozen=True)
class Recht:
    """Ein Recht des Katalogs."""

    kennung: str
    bezeichnung: str
    #: Bisheriger Rechtename (Häkchen ohne ``can_``), aus dem das Recht folgt; ``None`` = neu
    herkunft: str | None = None
    #: Leitrecht seiner Herkunft: steht in der verträglichen Prüfschicht für das Häkchen
    leit: bool = False
    #: Stationsrecht: braucht zusätzlich die Zuständigkeit für eine offene Station (#760)
    station: bool = False
    #: Kontrollrecht (#221): nicht in der Administrator-Vollmacht
    kontrolle: bool = False
    #: Ohne Wirkung, nur für die verlustfreie Abbildung eines Häkchens; entfällt mit den Häkchen
    ohne_wirkung: bool = False

    @property
    def objektart(self) -> str:
        return self.kennung.split(".", 1)[0]


def _r(kennung: str, bezeichnung: str, herkunft: str | None = None, **merkmale: Any) -> Recht:
    return Recht(kennung=kennung, bezeichnung=bezeichnung, herkunft=herkunft, **merkmale)


RECHTE: Final[tuple[Recht, ...]] = (
    # Übersicht
    _r("uebersicht.sehen", "Übersicht und Suche nutzen", "view_dashboard", leit=True),
    # Sitzung
    _r("sitzung.sehen", "Sitzungen sehen", "view_meetings", leit=True),
    _r("sitzung.noe_sehen", "Nichtöffentliche Sitzungen und TOPs sehen", "view_non_public_meetings", leit=True),
    _r("sitzung.anlegen", "Sitzungen anlegen", "create_meetings", leit=True),
    _r("sitzung.bearbeiten", "Sitzungen bearbeiten", "edit_meetings", leit=True),
    _r("sitzung.loeschen", "Sitzungen absagen und löschen", "delete_meetings", leit=True),
    _r("sitzung.laden", "Zur Sitzung laden", "edit_meetings"),
    _r("sitzung.leiten", "Sitzung leiten (Cockpit)", "conduct_meetings", leit=True),
    _r("sitzung.anwesenheit_fuehren", "Anwesenheit führen", "manage_attendance", leit=True),
    # Tagesordnung
    _r("tagesordnung.bearbeiten", "Tagesordnung bearbeiten", "edit_meetings"),
    _r("tagesordnung.benehmen_erklaeren", "Benehmen bzw. Einvernehmen zur Tagesordnung erklären", station=True),
    _r("tagesordnung.freigeben", "Tagesordnung freigeben", station=True),
    # Vorlage
    _r("vorlage.sehen", "Vorlagen sehen", "view_papers", leit=True),
    _r("vorlage.noe_sehen", "Nichtöffentliche Vorlagen sehen", "view_non_public_papers", leit=True),
    _r("vorlage.anlegen", "Vorlagen anlegen", "create_papers", leit=True),
    _r("vorlage.bearbeiten", "Vorlagen und Beratungsfolge bearbeiten", "edit_papers", leit=True),
    _r("vorlage.loeschen", "Vorlagen löschen", "delete_papers", leit=True),
    _r("vorlage.einreichen", "Vorlagen zur Freigabe einreichen", "edit_papers"),
    _r("vorlage.mitzeichnen", "Vorlagen mitzeichnen", "view_papers", station=True),
    _r("vorlage.freigeben", "Vorlagen freigeben", "approve_papers", leit=True, station=True),
    _r("vorlage.zurueckweisen", "Vorlagen zurückweisen", "approve_papers"),
    _r("vorlage.terminieren", "Vorlagen terminieren und weiterleiten", "edit_meetings"),
    _r("vorlage.zurueckziehen", "Vorlagen zurückziehen"),
    _r("vorlage.veroeffentlichen", "Vorlagen veröffentlichen"),
    # Antrag, Anfrage
    _r("antrag.sehen", "Anträge und Anfragen sehen", "view_applications", leit=True),
    _r("antrag.eingang_bearbeiten", "Eingang von Anträgen bearbeiten", "process_applications", leit=True),
    _r("antrag.zuweisen", "Anträge und Anfragen zuweisen", "process_applications"),
    _r("antrag.beantworten", "Anfragen beantworten", "process_applications", station=True),
    # Niederschrift
    _r("niederschrift.sehen", "Niederschriften sehen", "view_protocols", leit=True),
    _r("niederschrift.noe_sehen", "Nichtöffentliche Teile der Niederschrift sehen", "view_non_public_meetings"),
    _r("niederschrift.anlegen", "Niederschriften anlegen", "create_protocols", leit=True),
    _r("niederschrift.bearbeiten", "Niederschriften bearbeiten und zur Prüfung geben", "edit_protocols", leit=True),
    _r("niederschrift.unterzeichnen", "Niederschriften unterzeichnen", station=True),
    _r("niederschrift.genehmigen", "Niederschriften genehmigen bzw. feststellen", "approve_protocols", leit=True),
    _r("niederschrift.berichtigen", "Niederschriften berichtigen", "approve_protocols"),
    _r("niederschrift.veroeffentlichen", "Niederschriften veröffentlichen", "approve_protocols"),
    # Beschluss
    _r("beschluss.sehen", "Beschlüsse sehen", "view_meetings"),
    _r("beschluss.umsetzung_bearbeiten", "Umsetzung von Beschlüssen bearbeiten", "edit_meetings", station=True),
    _r("beschluss.auszug_uebergeben", "Beschlussauszüge übergeben", "edit_meetings"),
    # Umlaufverfahren
    _r("umlauf.sehen", "Umlaufverfahren sehen", "view_meetings"),
    _r("umlauf.anlegen", "Umlaufverfahren anlegen", "edit_meetings"),
    _r("umlauf.stimmen_erfassen", "Stimmen im Umlaufverfahren erfassen", "edit_meetings"),
    _r("umlauf.feststellen", "Ergebnis des Umlaufverfahrens feststellen", "edit_meetings"),
    # Person, Besetzung
    _r("person.sehen", "Personen und Besetzungen sehen", "view_meetings"),
    _r("person.kontaktdaten_sehen", "Kontaktdaten von Personen sehen", "view_meetings"),
    _r("person.bankdaten_sehen", "Bankdaten von Personen sehen", "manage_allowances"),
    _r("person.bearbeiten", "Personen und Besetzungen bearbeiten", "manage_organizations"),
    # Gremium, Körperschaft
    _r("gremium.sehen", "Gremien sehen", "view_meetings"),
    _r("gremium.verwalten", "Gremien verwalten", "manage_organizations", leit=True),
    _r("koerperschaft.verwalten", "Körperschaften verwalten", "manage_settings"),
    # Sitzungsgeld, Pauschalen
    _r("sitzungsgeld.erfassen", "Sitzungsgeld und Pauschalen erfassen", "manage_allowances", leit=True),
    _r("sitzungsgeld.saetze_verwalten", "Sätze und Zahlungsempfänger verwalten", "manage_allowances"),
    _r("sitzungsgeld.pruefen", "Sitzungsgeld prüfen", "manage_allowances", station=True),
    _r("sitzungsgeld.anordnen", "Sitzungsgeld anordnen", "manage_allowances"),
    _r("sitzungsgeld.exportieren", "Sitzungsgeld exportieren", "manage_allowances"),
    # Endgeräte
    _r("endgeraet.verwalten", "Endgeräte verwalten", "manage_devices", leit=True),
    # Abläufe
    _r("ablauf.verwalten", "Abläufe (Mitzeichnung, Vier-Augen-Prinzip, Genehmigungsweg) verwalten", "manage_settings"),
    _r("ablauf.station_uebersteuern", "Station eines Ablaufs übersteuern"),
    # Rollen, Konten, Vertretungen
    _r("konto.verwalten", "Konten und Einladungen verwalten", "manage_users", leit=True),
    _r("konto.rollen_verwalten", "Rollen verwalten und zuweisen", "manage_users"),
    _r("konto.vertretungen_verwalten", "Vertretungen verwalten", "manage_users"),
    _r("konto.rechteauskunft", "Rechteauskunft"),
    # Einstellungen, Schnittstellen, Veröffentlichung
    _r("einstellung.verwalten", "Einstellungen verwalten", "manage_settings", leit=True),
    _r("einstellung.schnittstellen_verwalten", "Schnittstellen und API-Schlüssel verwalten", "manage_settings"),
    _r("einstellung.veroeffentlichung_verwalten", "Veröffentlichung verwalten", "manage_settings"),
    _r("einstellung.api_nutzen", "Session-API nutzen", "access_api", leit=True),
    _r(
        "einstellung.oparl_lesen",
        "OParl-Schnittstelle (ohne Wirkung)",
        "access_oparl_api",
        leit=True,
        ohne_wirkung=True,
    ),
    # Prüfprotokoll (Kontrollrechte)
    _r("pruefprotokoll.sehen", "Prüfprotokoll sehen", "view_audit_log", leit=True, kontrolle=True),
    _r(
        "pruefprotokoll.exportieren",
        "Prüfprotokoll exportieren und prüfen",
        "export_audit_log",
        leit=True,
        kontrolle=True,
    ),
)

#: Kennung → Recht
KATALOG: Final[dict[str, Recht]] = {recht.kennung: recht for recht in RECHTE}

#: Bisheriger Rechtename → Kennung seines Leitrechts
LEITRECHTE: Final[dict[str, str]] = {recht.herkunft: recht.kennung for recht in RECHTE if recht.leit and recht.herkunft}

#: Bisheriger Rechtename → Kennungen aller Rechte, die aus ihm folgen
_AUS_HAEKCHEN: Final[dict[str, frozenset[str]]] = {
    herkunft: frozenset(recht.kennung for recht in RECHTE if recht.herkunft == herkunft) for herkunft in LEITRECHTE
}

#: Rechte der Administrator-Vollmacht: alle außer Kontrollrechten und Rechten ohne Wirkung
ADMIN_RECHTE: Final[frozenset[str]] = frozenset(
    recht.kennung for recht in RECHTE if not recht.kontrolle and not recht.ohne_wirkung
)


def rechte_aus_haekchen(namen: Iterable[str]) -> frozenset[str]:
    """Rechte, die aus bisherigen Rechtenamen folgen (unbekannte Namen gewähren nichts)."""
    ergebnis: set[str] = set()
    for name in namen:
        ergebnis |= _AUS_HAEKCHEN.get(name, frozenset())
    return frozenset(ergebnis)


def haekchen_aus_rechten(rechte: Iterable[str]) -> set[str]:
    """Bisherige Rechtenamen, deren Leitrecht in ``rechte`` liegt."""
    vorhanden = set(rechte)
    return {name for name, leitrecht in LEITRECHTE.items() if leitrecht in vorhanden}


def haekchen_der_rolle(rolle: Any) -> set[str]:
    """Gesetzte Häkchen einer Rolle als bisherige Rechtenamen (``can_approve_papers`` → ``approve_papers``)."""
    return {
        field.name[4:]
        for field in rolle._meta.concrete_fields
        if field.name.startswith("can_") and getattr(rolle, field.name, False)
    }


def rechte_der_rolle(rolle: Any) -> frozenset[str]:
    """Rechte einer Rolle: aus ihren Häkchen, bei Administrator-Rollen zusätzlich die Vollmacht."""
    rechte = rechte_aus_haekchen(haekchen_der_rolle(rolle))
    if rolle.is_admin:
        rechte |= ADMIN_RECHTE
    return rechte
