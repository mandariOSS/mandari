# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stammdaten-Import aus Bestandssystemen (Issue #762, Teil L11a; Bezug #42).

Übernimmt Wahlperioden, Gremien, Fraktionen, Ämter, Personen und Besetzungen aus Listen der Verwaltung
(je Objektart eine CSV- oder XLSX-Datei, Vorlagen über :func:`write_templates`) in einen Session-Mandanten,
z. B. beim Umstieg aus SessionNet. Anleitung: ``docs/SESSION_STAMMDATEN_IMPORT.md``.

Zwei Schritte:

1. **Planen** (:func:`plan_import`): Dateien lesen, jede Zeile prüfen und mit dem Bestand abgleichen. Ergebnis
   je Zeile „neu“, „geändert“ (mit den Feldern) oder „unverändert“, dazu Fehler und Hinweise mit Datei und
   Zeile. Planen schreibt nichts; das ist der Prüflauf.
2. **Ausführen** (:func:`apply_plan`): nur ohne Fehler, ganz oder gar nicht in einer Transaktion. Jede Anlage
   und Änderung steht über die Model-Signale im Prüfprotokoll des Mandanten.

**Idempotent** über fachliche Schlüssel (ohne zusätzliche Kennungsspalte in der Datenbank): Wahlperiode und
Gremium (auch Fraktion und Amt) über den Namen, Person über Vor- und Nachname (bei gleichen Namen zusätzlich
über die E-Mail), Besetzung über Gremium, Person und Beginn wie die Datenbankregel. Ein zweiter Import
derselben Dateien ändert nichts. Gibt es einen Namen im Mandanten mehrfach (z. B. „Verwaltungsausschuss“
zweier Körperschaften), ist das ein Fehler statt einer beliebigen Zuordnung. Leere Zellen ändern an
vorhandenen Datensätzen nichts. Die Spalte ``kennung`` der Personen (z. B. die Personennummer des Altsystems)
verknüpft nur die Dateien untereinander.

**Stimmrecht** ohne Angabe nach Funktion und Landesprofil (:func:`default_vote`): Hinzugewählte ohne
Stimmrecht (§ 71 Abs. 7 NKomVG), beratende Mitglieder und Gäste ebenso.

**Sicherheit:** Telefon, Adresse und Bankdaten gehen nur über die Verschlüsselungs-Accessoren in die Datenbank
und erscheinen nie in Bericht, Ausgabe oder Fehlermeldung.

**Gegenprobe** (:func:`cross_check`): laufende Besetzungen je Gremium gegen die öffentlichen Mitgliederlisten
im RIS-Bestand, z. B. aus dem SessionNet-Adapter. Abweichungen sind Hinweise, keine Fehler.

**Vorlagen** (:func:`write_templates`): leer oder vorbefüllt mit dem öffentlichen Bestand eines Bodys im
RIS-Bestand (Gremien, Personen, Besetzungen, Wahlperioden), z. B. aus dem SessionNet-Adapter. Die Verwaltung
ergänzt Kontakt- und Bankdaten und prüft Namen und Funktionen; danach Prüflauf und Import wie oben.

Noch nicht enthalten: Zuordnung zu Körperschaften (folgt mit #756).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models, transaction

from apps.common import csv_safety
from apps.session.models import (
    DELIVERY_CHANNEL_CHOICES,
    SessionLegislativeTerm,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import allowance_service
from apps.session.services.stammdaten_tabellen import Table, TableError, excel_date, read_table

if TYPE_CHECKING:
    from insight_core.services.public_members import PublicRoster

# ---------------------------------------------------------------------------
# Vorlagen: Dateien und Spalten
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    key: str
    description: str
    required: bool = False


KINDS = ("wahlperioden", "gremien", "fraktionen", "aemter", "personen", "besetzungen")
ORGANIZATION_KINDS = ("gremien", "fraktionen", "aemter")

LABELS = {
    "wahlperioden": "Wahlperioden",
    "gremien": "Gremien",
    "fraktionen": "Fraktionen",
    "aemter": "Ämter",
    "personen": "Personen",
    "besetzungen": "Besetzungen",
}

_DATE_HINT = "TT.MM.JJJJ"
_BOOL_HINT = "ja/nein"

COLUMNS: dict[str, tuple[Column, ...]] = {
    "wahlperioden": (
        Column("name", "Name, z. B. „Wahlperiode 2021–2026“", required=True),
        Column("nummer", "Nummer der Wahlperiode (Platzhalter {wp} in Nummernkreisen)"),
        Column("beginn", f"erster Tag ({_DATE_HINT})"),
        Column("ende", f"letzter Tag ({_DATE_HINT})"),
    ),
    "gremien": (
        Column("name", "Name des Gremiums", required=True),
        Column("kurzname", "Kurzname"),
        Column("art", "Rat, Ausschuss, Beirat, Kommission oder Sonstiges", required=True),
        Column(
            "ausschussart",
            "Hauptausschuss (auch Verwaltungs-, Kreis-, Regions-, Samtgemeindeausschuss), Finanzausschuss, "
            "Rechnungsprüfungsausschuss oder „anderer“",
        ),
        Column("uebergeordnet", "Name des übergeordneten Gremiums"),
        Column("ladungsfrist_tage", "Ladungsfrist in Tagen"),
        Column("sollstaerke", "Mitgliederzahl (Soll)"),
        Column("beginn", f"Beginn ({_DATE_HINT})"),
        Column("ende", f"Ende ({_DATE_HINT})"),
        Column("aktiv", _BOOL_HINT),
    ),
    "fraktionen": (
        Column("name", "Name der Fraktion oder Gruppe", required=True),
        Column("kurzname", "Kurzname"),
        Column("beginn", f"Beginn ({_DATE_HINT})"),
        Column("ende", f"Ende ({_DATE_HINT})"),
        Column("aktiv", _BOOL_HINT),
    ),
    "aemter": (
        Column("name", "Name des Amts oder Fachbereichs", required=True),
        Column("kurzname", "Kurzname"),
        Column("uebergeordnet", "Name des übergeordneten Amts"),
        Column("aktiv", _BOOL_HINT),
    ),
    "personen": (
        Column("kennung", "eindeutige Kennung in dieser Datei, z. B. Personennummer des Altsystems", required=True),
        Column("anrede", "z. B. Frau, Herr"),
        Column("titel", "z. B. Dr."),
        Column("vorname", "Vorname", required=True),
        Column("nachname", "Nachname", required=True),
        Column("email", "E-Mail-Adresse"),
        Column("telefon", "Telefon (wird verschlüsselt gespeichert)"),
        Column("adresse", "Anschrift (wird verschlüsselt gespeichert)"),
        Column("kontoinhaber", "Kontoinhaber/in (verschlüsselt)"),
        Column("iban", "IBAN (verschlüsselt, Prüfziffer wird geprüft)"),
        Column("bic", "BIC (verschlüsselt)"),
        Column("zustellweg", "E-Mail, Portal oder Brief"),
        Column("mandat_beginn", f"Mandatsbeginn ({_DATE_HINT})"),
        Column("mandat_ende", f"Mandatsende ({_DATE_HINT})"),
        Column("aktiv", _BOOL_HINT),
    ),
    "besetzungen": (
        Column("person", "Kennung der Person aus der Personendatei", required=True),
        Column("gremium", "Name des Gremiums, der Fraktion oder des Amts", required=True),
        Column(
            "funktion",
            "Mitglied, Vorsitz, stellv. Vorsitz, hinzugewählt, sachkundige/r Bürger/in, beratend "
            "(auch Grundmandat) oder Gast",
        ),
        Column(
            "stimmrecht",
            f"{_BOOL_HINT} (leer: nein bei beratend, hinzugewählt und Gast, in Niedersachsen auch bei "
            "sachkundigen Bürgern; sonst ja)",
        ),
        Column("beginn", f"erster Tag ({_DATE_HINT})"),
        Column("ende", f"letzter Tag ({_DATE_HINT})"),
        Column("wahlperiode", "Name der Wahlperiode (leer: die Wahlperiode, in die der Beginn fällt)"),
        Column("vertretung_fuer", "Kennung der vertretenen Person (persönliche Stellvertretung)"),
    ),
}

# Andere übliche Spaltenüberschriften (nach Vereinheitlichung, siehe _fold)
_HEADER_ALIASES = {
    "e_mail": "email",
    "mail": "email",
    "familienname": "nachname",
    "personennummer": "kennung",
    "von": "beginn",
    "bis": "ende",
    "mandatsbeginn": "mandat_beginn",
    "mandatsende": "mandat_ende",
    "rolle": "funktion",
    "kurzbezeichnung": "kurzname",
    "typ": "art",
    "ladungsfrist": "ladungsfrist_tage",
    "mitgliederzahl": "sollstaerke",
    "vertretung": "vertretung_fuer",
    "stellvertretung_fuer": "vertretung_fuer",
}


def _choices(model: type[models.Model], name: str) -> dict[str, str]:
    return {str(code): str(label) for code, label in cast(Any, model._meta.get_field(name)).choices or []}


_ORG_TYPE_LABELS = _choices(SessionOrganization, "organization_type")
_ORG_TYPE_BY_KIND = {"fraktionen": "faction", "aemter": "department"}

_ART = {
    "rat": "council",
    "gemeinderat": "council",
    "stadtrat": "council",
    "samtgemeinderat": "council",
    "kreistag": "council",
    "regionsversammlung": "council",
    "ortsrat": "council",
    "stadtbezirksrat": "council",
    "ausschuss": "committee",
    "fachausschuss": "committee",
    "hauptausschuss": "committee",
    "verwaltungsausschuss": "committee",
    "kreisausschuss": "committee",
    "regionsausschuss": "committee",
    "samtgemeindeausschuss": "committee",
    "finanzausschuss": "committee",
    "rechnungspruefungsausschuss": "committee",
    "beirat": "advisory",
    "kommission": "commission",
    "fraktion": "faction",
    "amt": "department",
    "fachbereich": "department",
    "sonstiges": "other",
    "sonstige": "other",
    **{code: code for code in _ORG_TYPE_LABELS},
}

_COMMITTEE_KIND = {
    "hauptausschuss": SessionOrganization.COMMITTEE_KIND_MAIN,
    "verwaltungsausschuss": SessionOrganization.COMMITTEE_KIND_MAIN,
    "kreisausschuss": SessionOrganization.COMMITTEE_KIND_MAIN,
    "regionsausschuss": SessionOrganization.COMMITTEE_KIND_MAIN,
    "samtgemeindeausschuss": SessionOrganization.COMMITTEE_KIND_MAIN,
    "finanzausschuss": SessionOrganization.COMMITTEE_KIND_FINANCE,
    "rechnungspruefungsausschuss": SessionOrganization.COMMITTEE_KIND_AUDIT,
    "anderer": SessionOrganization.COMMITTEE_KIND_ORDINARY,
    "anderer_ausschuss": SessionOrganization.COMMITTEE_KIND_ORDINARY,
    "keine": SessionOrganization.COMMITTEE_KIND_ORDINARY,
    "keine_besondere_art": SessionOrganization.COMMITTEE_KIND_ORDINARY,
    **{code: code for code, _ in SessionOrganization.COMMITTEE_KIND_CHOICES},
}

_ROLE = {
    "mitglied": "member",
    "mitglieder": "member",
    "ordentliches_mitglied": "member",
    "ordentliche_mitglieder": "member",
    "ratsmitglied": "member",
    "ratsherr": "member",
    "ratsfrau": "member",
    "abgeordnete": "member",
    "abgeordneter": "member",
    "vorsitz": "chair",
    "vorsitzende": "chair",
    "vorsitzender": "chair",
    "vorsitzende_r": "chair",
    "gast": "guest",
    "gaeste": "guest",
    **{code: code for code in _choices(SessionOrganizationMembership, "role")},
}
_ROLES_WITHOUT_VOTE = {"advisor", "guest"}
_DEPUTY_WORDS = {"stellvertreter", "stellvertreterin", "stellvertreter_in", "vertreter", "vertreterin", "vertreter_in"}
_ROLE_TEXT = {
    "member": "Mitglied",
    "chair": "Vorsitz",
    "deputy_chair": "stellv. Vorsitz",
    "expert_citizen": "sachkundige/r Bürger/in",
    "advisor": "beratend",
    "guest": "Gast",
}
DEPUTY_TEXT = "stellvertretendes Mitglied"


def resolve_role(value: str) -> tuple[str, bool] | None:
    """
    Funktion aus dem Altsystem -> (Rolle, stellvertretendes Mitglied); ``None``, wenn unbekannt.

    Stellvertretende Mitglieder sind Mitglieder mit Vertretungsregel (``substitute_for``). Ämter wie
    „stellv. Bürgermeister“ sind keine Stellvertretung im Gremium und bleiben unbekannt (die Verwaltung
    entscheidet, ob Vorsitz oder Mitglied).
    """
    folded = _fold(value)
    if folded in _ROLE:
        return _ROLE[folded], False
    tokens = set(folded.split("_"))
    if "vorsitz" in folded:
        return ("deputy_chair" if ("stell" in folded or "stv" in tokens) else "chair"), False
    if "beratend" in folded or "grundmandat" in folded:
        return "advisor", False
    if "sachkundig" in folded or "hinzugewaehlt" in folded or "buergerlich" in folded:
        return "expert_citizen", False
    deputy_marker = "stell" in folded or "stv" in tokens or "vertret" in folded or "ersatz" in folded
    if folded in _DEPUTY_WORDS or (deputy_marker and "mitglied" in folded):
        return "member", True
    if "mitglied" in folded:
        return "member", False
    return None


CO_OPTED_TEXT = "hinzugewähltes Mitglied"


def co_opted(value: str) -> bool:
    """„hinzugewählt“: Ausschussmitglied ohne Mandat in der Vertretung (§ 71 Abs. 7 NKomVG)."""
    return "hinzugewaehlt" in _fold(value)


def role_text(value: str) -> str:
    """Funktion für die Importvorlage: bekannte Angaben vereinheitlicht, unbekannte unverändert."""
    resolved = resolve_role(value) if value.strip() else None
    if resolved is None:
        return value.strip()
    role, deputy = resolved
    if role == "expert_citizen" and co_opted(value):
        # Bleibt erkennbar: Hinzugewählte haben ohne Angabe kein Stimmrecht (default_vote)
        return CO_OPTED_TEXT
    return DEPUTY_TEXT if deputy else _ROLE_TEXT[role]


# Hinweise zum Stimmrecht ohne Angabe in der Spalte stimmrecht (default_vote), je Zeile gesammelt
VOTE_CO_OPTED = "co_opted"
VOTE_UNCLEAR = "unclear"


def default_vote(role: str, function: str, state: str | None) -> tuple[bool, str | None]:
    """
    Stimmrecht einer neuen Besetzung ohne Angabe in der Spalte ``stimmrecht`` -> (Stimmrecht, Hinweisart).

    - beratend (auch Grundmandat, § 71 Abs. 4 NKomVG) und Gast: nein.
    - hinzugewählt: nein (§ 71 Abs. 7 NKomVG), mit Sammelhinweis. Im Landesprofil Niedersachsen (NI) gilt das
      auch, wenn die Datei die Funktion „sachkundige Bürger“ nennt. Ausschüsse nach § 73 NKomVG regelt das
      jeweilige Gesetz; dort die Spalte ausfüllen.
    - sachkundige Bürger im Landesprofil Nordrhein-Westfalen (NW): ja (§ 58 Abs. 3 GO NRW).
    - sachkundige Bürger ohne eines dieser Landesprofile: ja, mit Sammelhinweis (Landesrecht prüfen).
    - alle anderen: ja.

    Sonst zählte eine Person ohne Stimme in ``attendance_service.roster`` zur Beschlussfähigkeit.
    """
    if role in _ROLES_WITHOUT_VOTE:
        return False, None
    if role != "expert_citizen":
        return True, None
    if co_opted(function) or state == "NI":
        return False, VOTE_CO_OPTED
    if state == "NW":
        return True, None
    return True, VOTE_UNCLEAR


_VOTE_HINTS = {
    VOTE_CO_OPTED: "Hinzugewählte ohne Angabe in stimmrecht ohne Stimmrecht übernommen (§ 71 Abs. 7 NKomVG); "
    "wo ein Gesetz Stimmrecht vorsieht (Ausschüsse nach § 73 NKomVG), „ja“ angeben",
    VOTE_UNCLEAR: "Sachkundige Bürger/innen ohne Angabe in stimmrecht mit Stimmrecht übernommen; das Stimmrecht "
    "hängt vom Landesrecht ab – bitte prüfen und in stimmrecht angeben",
}


_DELIVERY = {
    "e_mail": "email",
    "mail": "email",
    "mandari_work": "portal",
    "work": "portal",
    "brief": "letter",
    "post": "letter",
    **{code: code for code, _ in DELIVERY_CHANNEL_CHOICES},
}

_TRUE = {"ja", "j", "x", "1", "wahr", "true", "yes", "y"}
_FALSE = {"nein", "n", "0", "falsch", "false", "no"}

_DATE_DE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_DATE_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SERIAL_RE = re.compile(r"^\d{1,6}(\.\d+)?$")


def _fold(value: str) -> str:
    """Vergleichsschlüssel: Kleinbuchstaben, Umlaute ausgeschrieben, nur Buchstaben/Ziffern mit „_“."""
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _name_key(value: str | None) -> str:
    """Schlüssel für Namen (Gremien, Wahlperioden, Personen): Groß-/Kleinschreibung und Leerraum egal."""
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _by_name[N: (SessionLegislativeTerm, SessionOrganization)](objects: Iterable[N]) -> tuple[dict[str, N], set[str]]:
    """
    Bestand nach Namensschlüssel -> (eindeutige Namen, mehrfach vorhandene Namen).

    Mehrfach vorhandene Namen lassen sich nicht zuordnen (z. B. „Verwaltungsausschuss“ zweier Körperschaften
    im selben Mandanten, bis zur Spalte ``koerperschaft`` mit #756); der Plan meldet sie als Fehler, statt
    einen beliebigen Datensatz zu nehmen.
    """
    unique: dict[str, N] = {}
    ambiguous: set[str] = set()
    for obj in objects:
        key = _name_key(obj.name)
        if key in ambiguous:
            continue
        if key in unique:
            del unique[key]
            ambiguous.add(key)
        else:
            unique[key] = obj
    return unique, ambiguous


def _ambiguous(name: str) -> str:
    return (
        f"„{name}“ ist im Mandanten mehrfach vorhanden und damit nicht eindeutig "
        "(Abgleich je Körperschaft folgt mit der Spalte koerperschaft)."
    )


# ---------------------------------------------------------------------------
# Plan und Bericht
# ---------------------------------------------------------------------------

ERROR = "Fehler"
HINT = "Hinweis"

NEW = "neu"
CHANGED = "geändert"
UNCHANGED = "unverändert"


@dataclass
class Finding:
    severity: str
    file: str
    line: int | None
    message: str

    def text(self) -> str:
        where = f"{self.file}, Zeile {self.line}" if self.line is not None else self.file
        return f"{where}: {self.message}"


@dataclass
class Tally:
    """Zählabgleich je Objektart: Zeilen der Datei = neu + geändert + unverändert + fehlerhaft."""

    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    before: int = 0
    after: int | None = None

    @property
    def balanced(self) -> bool:
        return self.rows == self.created + self.updated + self.unchanged + self.failed


@dataclass
class Item:
    """Eine geprüfte Zeile: neu, geändert oder unverändert (oder fehlerhaft)."""

    kind: str
    file: str
    line: int
    label: str
    instance: Any = None
    values: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    failed: bool = False

    @property
    def action(self) -> str:
        if self.instance is None:
            return NEW
        return CHANGED if self.changes else UNCHANGED


@dataclass
class ImportPlan:
    tenant: SessionTenant
    files: dict[str, str] = field(default_factory=dict)  # Objektart -> Dateiname
    findings: list[Finding] = field(default_factory=list)
    tallies: dict[str, Tally] = field(default_factory=dict)
    terms: dict[str, Item] = field(default_factory=dict)  # Namensschlüssel -> Zeile
    organizations: dict[str, Item] = field(default_factory=dict)  # Namensschlüssel -> Zeile
    persons: dict[str, Item] = field(default_factory=dict)  # Kennung -> Zeile
    memberships: list[Item] = field(default_factory=list)
    cross_check: list[dict[str, Any]] = field(default_factory=list)
    applied: bool = False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def hints(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == HINT]

    def error(self, file: str, line: int | None, message: str) -> None:
        self.findings.append(Finding(ERROR, file, line, message))

    def hint(self, file: str, line: int | None, message: str) -> None:
        self.findings.append(Finding(HINT, file, line, message))

    def fail(self, item: Item, message: str) -> None:
        """Eine bereits geplante Zeile nachträglich verwerfen (Folgeprüfung über Zeilen hinweg)."""
        self.error(item.file, item.line, message)
        if not item.failed:
            item.failed = True

    def items(self) -> Iterable[Item]:
        yield from self.terms.values()
        yield from self.organizations.values()
        yield from self.persons.values()
        yield from self.memberships

    def as_dict(self) -> dict[str, Any]:
        return {
            "mandant": self.tenant.slug,
            "ausgefuehrt": self.applied,
            "dateien": self.files,
            "zaehlabgleich": {
                LABELS[kind]: {
                    "datei": t.rows,
                    "neu": t.created,
                    "geaendert": t.updated,
                    "unveraendert": t.unchanged,
                    "fehlerhaft": t.failed,
                    "bestand_vorher": t.before,
                    "bestand_nachher": t.after,
                    "stimmig": t.balanced,
                }
                for kind, t in self.tallies.items()
            },
            "fehler": [vars(f) for f in self.errors],
            "hinweise": [vars(f) for f in self.hints],
            "aenderungen": [
                {"datei": i.file, "zeile": i.line, "objekt": i.label, "felder": i.changes}
                for i in self.items()
                if not i.failed and i.action == CHANGED
            ],
            "gegenprobe": self.cross_check,
        }

    def as_text(self) -> str:
        mode = "ausgeführt" if self.applied else "Prüflauf, nichts geschrieben"
        lines = [f"Stammdaten-Import für Mandant „{self.tenant.name}“ ({self.tenant.slug}) – {mode}"]
        if self.files:
            lines.append("Dateien: " + ", ".join(self.files[k] for k in KINDS if k in self.files))
        lines += ["", "Zählabgleich (Datei = neu + geändert + unverändert + fehlerhaft)"]
        lines.append(
            f"  {'Objektart':<13}{'Datei':>7}{'neu':>6}{'geändert':>10}{'unverändert':>13}{'fehlerhaft':>12}"
            f"{'Bestand vorher':>16}{'nachher':>9}"
        )
        for kind in KINDS:
            t = self.tallies.get(kind)
            if t is None:
                continue
            after = "–" if t.after is None else str(t.after)
            lines.append(
                f"  {LABELS[kind]:<13}{t.rows:>7}{t.created:>6}{t.updated:>10}{t.unchanged:>13}{t.failed:>12}"
                f"{t.before:>16}{after:>9}" + ("" if t.balanced else "  (nicht stimmig)")
            )
        for title, findings in (("Fehler", self.errors), ("Hinweise", self.hints)):
            if findings:
                lines += ["", f"{title} ({len(findings)})"]
                lines += [f"  {f.text()}" for f in findings]
        changed = [i for i in self.items() if not i.failed and i.action == CHANGED]
        if changed:
            lines += ["", f"Änderungen an vorhandenen Datensätzen ({len(changed)})"]
            lines += [f"  {i.file}, Zeile {i.line}: {i.label} – {', '.join(i.changes)}" for i in changed]
        if self.cross_check:
            lines += ["", "Gegenprobe mit den öffentlichen Mitgliederlisten"]
            for entry in self.cross_check:
                if entry["oeffentlich"] is None:
                    lines.append(f"  {entry['gremium']}: kein öffentliches Gremium gleichen Namens")
                    continue
                parts = [f"{entry['uebereinstimmend']} übereinstimmend"]
                if entry["nur_oeffentlich"]:
                    parts.append("nur öffentlich: " + ", ".join(entry["nur_oeffentlich"]))
                if entry["nur_import"]:
                    parts.append("nur im Import: " + ", ".join(entry["nur_import"]))
                lines.append(f"  {entry['gremium']}: " + "; ".join(parts))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Zellwerte
# ---------------------------------------------------------------------------


class CellValueError(Exception):
    """Ungültiger Zellwert; die Meldung nennt nie den Wert vertraulicher Spalten."""


@dataclass
class _Row:
    file: str
    line: int
    cells: dict[str, str]
    table: Table

    def get(self, key: str) -> str:
        return csv_safety.csv_unescape_cell(self.cells.get(key, "").strip())

    def date(self, key: str) -> date | None:
        value = self.get(key)
        if not value:
            return None
        match_de = _DATE_DE_RE.match(value)
        match_iso = _DATE_ISO_RE.match(value)
        try:
            if match_de:
                return date(int(match_de.group(3)), int(match_de.group(2)), int(match_de.group(1)))
            if match_iso:
                return date(int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3)))
            if self.table.spreadsheet and _SERIAL_RE.match(value):
                return excel_date(float(value), date1904=self.table.date1904)
        except (ValueError, OverflowError):
            pass
        raise CellValueError(f"Spalte {key}: „{value}“ ist kein Datum ({_DATE_HINT}).")

    def integer(self, key: str, *, minimum: int, maximum: int) -> int | None:
        value = self.get(key)
        if not value:
            return None
        try:
            number = int(value)
        except ValueError:
            raise CellValueError(f"Spalte {key}: „{value}“ ist keine ganze Zahl.") from None
        if not minimum <= number <= maximum:
            raise CellValueError(f"Spalte {key}: {number} liegt nicht zwischen {minimum} und {maximum}.")
        return number

    def boolean(self, key: str) -> bool | None:
        value = self.get(key)
        folded = _fold(value)
        if not folded:
            return None
        if folded in _TRUE:
            return True
        if folded in _FALSE:
            return False
        raise CellValueError(f"Spalte {key}: „{value}“ ist weder ja noch nein.")

    def choice(self, key: str, mapping: dict[str, str], allowed: str) -> str | None:
        value = self.get(key)
        folded = _fold(value)
        if not folded:
            return None
        if folded in mapping:
            return mapping[folded]
        raise CellValueError(f"Spalte {key}: „{value}“ ist nicht erlaubt (erlaubt: {allowed}).")

    def text(self, key: str, *, max_length: int) -> str:
        value = re.sub(r"\s+", " ", self.get(key))
        if len(value) > max_length:
            raise CellValueError(f"Spalte {key}: länger als {max_length} Zeichen.")
        return value

    def role(self) -> tuple[str | None, bool]:
        """(Rolle oder ``None`` bei leerer Zelle, stellvertretendes Mitglied)."""
        value = self.get("funktion")
        if not _fold(value):
            return None, False
        resolved = resolve_role(value)
        if resolved is None:
            raise CellValueError(
                f"Spalte funktion: „{value}“ ist unbekannt (erlaubt: Mitglied, stellvertretendes Mitglied, "
                "Vorsitz, stellv. Vorsitz, sachkundige/r Bürger/in, beratend, Gast)."
            )
        return resolved


def _overlaps(start_a: date | None, end_a: date | None, start_b: date | None, end_b: date | None) -> bool:
    """Zeiträume mit offenen Grenzen (None = unbegrenzt) berühren sich."""
    if end_a is not None and start_b is not None and end_a < start_b:
        return False
    return not (end_b is not None and start_a is not None and end_b < start_a)


# ---------------------------------------------------------------------------
# Dateien finden und lesen
# ---------------------------------------------------------------------------


def find_files(directory: Path) -> tuple[dict[str, Path], list[str]]:
    """Importdateien im Verzeichnis (``<objektart>.csv`` oder ``.xlsx``) und Probleme (doppelt, keine)."""
    found: dict[str, Path] = {}
    problems: list[str] = []
    for kind in KINDS:
        existing = [directory / f"{kind}{suffix}" for suffix in (".csv", ".xlsx")]
        existing = [path for path in existing if path.is_file()]
        if len(existing) > 1:
            problems.append(f"{kind}: CSV und XLSX zugleich vorhanden – bitte nur eine Datei.")
        elif existing:
            found[kind] = existing[0]
    if not found and not problems:
        problems.append("Keine Importdatei gefunden (erwartet z. B. personen.csv oder personen.xlsx).")
    return found, problems


def _read_rows(plan: ImportPlan, kind: str, path: Path) -> list[_Row] | None:
    name = path.name
    plan.files[kind] = name
    try:
        table = read_table(path)
    except TableError as exc:
        plan.error(name, None, str(exc))
        return None
    except OSError:
        plan.error(name, None, "Datei nicht lesbar.")
        return None
    known = {column.key for column in COLUMNS[kind]}
    keys = [_HEADER_ALIASES.get(_fold(cell), _fold(cell)) for cell in table.header]
    duplicates = sorted({key for key in keys if key and keys.count(key) > 1})
    if duplicates:
        plan.error(name, 1, "Spalten doppelt: " + ", ".join(duplicates) + ".")
        return None
    missing = [column.key for column in COLUMNS[kind] if column.required and column.key not in keys]
    if missing:
        plan.error(name, 1, "Pflichtspalten fehlen: " + ", ".join(missing) + ".")
        return None
    unknown = [cell for cell, key in zip(table.header, keys, strict=True) if cell and key not in known]
    if unknown:
        plan.hint(name, 1, "Spalten werden nicht übernommen: " + ", ".join(unknown) + ".")
    return [
        _Row(
            file=name,
            line=line,
            cells={key: values[i] if i < len(values) else "" for i, key in enumerate(keys) if key in known},
            table=table,
        )
        for line, values in table.rows
    ]


def _compare(item: Item, labels: dict[str, str]) -> None:
    """Geänderte Felder einer vorhandenen Zeile festhalten (leere Werte ändern nichts)."""
    if item.instance is None:
        return
    for field_name, value in item.values.items():
        if value is None or value == "":
            continue
        if getattr(item.instance, field_name) != value:
            item.changes.append(labels.get(field_name, field_name))


# ---------------------------------------------------------------------------
# Planen je Objektart
# ---------------------------------------------------------------------------


def _plan_terms(plan: ImportPlan, rows: list[_Row]) -> None:
    tally = plan.tallies.setdefault("wahlperioden", Tally())
    stock = list(SessionLegislativeTerm.objects.filter(tenant=plan.tenant))
    existing, ambiguous = _by_name(stock)
    tally.before = len(stock)
    for row in rows:
        tally.rows += 1
        try:
            name = row.text("name", max_length=255)
            number = row.integer("nummer", minimum=1, maximum=999)
            start, end = row.date("beginn"), row.date("ende")
        except CellValueError as exc:
            plan.error(row.file, row.line, str(exc))
            continue
        key = _name_key(name)
        if not name:
            plan.error(row.file, row.line, "Name fehlt.")
        elif key in plan.terms:
            plan.error(row.file, row.line, f"Wahlperiode „{name}“ doppelt (zuerst Zeile {plan.terms[key].line}).")
        elif key in ambiguous:
            plan.error(row.file, row.line, f"Wahlperiode {_ambiguous(name)}")
        elif start is not None and end is not None and end < start:
            plan.error(row.file, row.line, "Das Ende liegt vor dem Beginn.")
        else:
            instance = existing.get(key)
            item = Item(
                kind="wahlperioden",
                file=row.file,
                line=row.line,
                label=name,
                instance=instance,
                values={
                    "name": instance.name if instance is not None else name,
                    "number": number,
                    "start_date": start,
                    "end_date": end,
                },
            )
            _compare(item, {"number": "Nummer", "start_date": "Beginn", "end_date": "Ende"})
            plan.terms[key] = item

    # Zeiträume dürfen sich nicht überschneiden (Datei und übriger Bestand zusammen)
    matched = {item.instance.pk for item in plan.terms.values() if item.instance is not None}
    periods: list[tuple[str, date | None, date | None, Item | None]] = [
        (t.name, t.start_date, t.end_date, None) for t in stock if t.pk not in matched
    ]
    for item in plan.terms.values():
        start = item.values["start_date"] or (item.instance.start_date if item.instance is not None else None)
        end = item.values["end_date"] or (item.instance.end_date if item.instance is not None else None)
        periods.append((item.label, start, end, item))
    for index, (_name_a, start_a, end_a, item_a) in enumerate(periods):
        if item_a is None or item_a.failed or (start_a is None and end_a is None):
            continue
        for name_b, start_b, end_b, item_b in periods[:index] + periods[index + 1 :]:
            if item_b is not None and (item_b.failed or item_b.line > item_a.line):
                continue  # Paare aus der Datei meldet die spätere Zeile
            if (start_b is not None or end_b is not None) and _overlaps(start_a, end_a, start_b, end_b):
                plan.fail(item_a, f"Zeitraum überschneidet sich mit „{name_b}“.")
                break


_ORG_LABELS = {
    "short_name": "Kurzname",
    "committee_kind": "Ausschussart",
    "invitation_period_days": "Ladungsfrist",
    "target_member_count": "Mitgliederzahl",
    "start_date": "Beginn",
    "end_date": "Ende",
    "is_active": "Aktiv",
}


def _plan_organization_row(
    plan: ImportPlan, kind: str, row: _Row, existing: dict[str, SessionOrganization], ambiguous: set[str]
) -> None:
    try:
        name = row.text("name", max_length=500)
        short_name = row.text("kurzname", max_length=100)
        committee_kind: str | None = None
        invitation_days: int | None = None
        target: int | None = None
        if kind == "gremien":
            org_type = row.choice("art", _ART, "Rat, Ausschuss, Beirat, Kommission, Sonstiges")
            committee_kind = row.choice(
                "ausschussart", _COMMITTEE_KIND, "Hauptausschuss, Finanzausschuss, Rechnungsprüfungsausschuss, anderer"
            ) or _COMMITTEE_KIND.get(_fold(row.get("art")))
            invitation_days = row.integer("ladungsfrist_tage", minimum=0, maximum=365)
            target = row.integer("sollstaerke", minimum=1, maximum=1000)
        else:
            org_type = _ORG_TYPE_BY_KIND[kind]
        start, end = row.date("beginn"), row.date("ende")
        active = row.boolean("aktiv")
    except CellValueError as exc:
        plan.error(row.file, row.line, str(exc))
        return
    key = _name_key(name)
    instance = existing.get(key)
    if not name:
        plan.error(row.file, row.line, "Name fehlt.")
    elif org_type is None:
        plan.error(row.file, row.line, "Art fehlt.")
    elif key in plan.organizations:
        first = plan.organizations[key]
        plan.error(row.file, row.line, f"„{name}“ doppelt (zuerst {first.file}, Zeile {first.line}).")
    elif key in ambiguous:
        plan.error(row.file, row.line, _ambiguous(name))
    elif start is not None and end is not None and end < start:
        plan.error(row.file, row.line, "Das Ende liegt vor dem Beginn.")
    elif instance is not None and instance.organization_type != org_type:
        plan.error(
            row.file,
            row.line,
            f"„{name}“ gibt es schon als {_ORG_TYPE_LABELS.get(instance.organization_type, '?')}, "
            f"die Datei nennt {_ORG_TYPE_LABELS.get(org_type, org_type)}.",
        )
    else:
        item = Item(
            kind=kind,
            file=row.file,
            line=row.line,
            label=instance.name if instance is not None else name,
            instance=instance,
            values={
                "name": instance.name if instance is not None else name,
                "short_name": short_name,
                "organization_type": org_type,
                "committee_kind": committee_kind if org_type == "committee" else None,
                "invitation_period_days": invitation_days,
                "target_member_count": target,
                "start_date": start,
                "end_date": end,
                "is_active": active,
            },
            refs={"parent": _name_key(row.get("uebergeordnet")) or None, "parent_label": row.get("uebergeordnet")},
        )
        _compare(item, _ORG_LABELS)
        plan.organizations[key] = item


def _plan_organizations(plan: ImportPlan, rows_by_kind: dict[str, list[_Row]]) -> None:
    stock = list(SessionOrganization.objects.filter(tenant=plan.tenant).select_related("parent"))
    existing, ambiguous = _by_name(stock)
    for kind in ORGANIZATION_KINDS:
        if kind not in rows_by_kind:
            continue
        tally = plan.tallies.setdefault(kind, Tally())
        tally.before = sum(1 for o in stock if _org_kind(o.organization_type) == kind)
        for row in rows_by_kind[kind]:
            tally.rows += 1
            _plan_organization_row(plan, kind, row, existing, ambiguous)

    # Übergeordnetes Gremium: aus den Dateien oder dem Bestand
    for key, item in plan.organizations.items():
        parent_key = item.refs["parent"]
        if parent_key is None:
            continue
        parent_item = plan.organizations.get(parent_key)
        if parent_key == key:
            plan.fail(item, "Ein Gremium kann sich nicht selbst übergeordnet sein.")
        elif parent_item is None and parent_key in ambiguous:
            plan.fail(item, f"Übergeordnetes Gremium {_ambiguous(item.refs['parent_label'])}")
        elif parent_item is None and parent_key not in existing:
            plan.fail(item, "Übergeordnetes Gremium unbekannt (weder in den Dateien noch angelegt).")
        elif item.instance is not None and _name_key(getattr(item.instance.parent, "name", "")) != parent_key:
            item.changes.append("Übergeordnetes Gremium")
    _fail_parent_rings(plan, existing)
    # Fehlerhafte übergeordnete Gremien ziehen ihre Untergremien mit (auch über mehrere Stufen)
    changed = True
    while changed:
        changed = False
        for item in plan.organizations.values():
            parent_item = plan.organizations.get(item.refs["parent"] or "")
            if not item.failed and parent_item is not None and parent_item.failed:
                plan.fail(item, f"Übergeordnetes Gremium „{parent_item.label}“ hat Fehler.")
                changed = True


def _fail_parent_rings(plan: ImportPlan, existing: dict[str, SessionOrganization]) -> None:
    """Übergeordnete Gremien und Ämter dürfen keinen Ring bilden (A → B → A), aus Dateien und Bestand zusammen."""
    parents: dict[str, str] = {}
    names: dict[str, str] = {}
    for key, org in existing.items():
        names[key] = org.name
        if org.parent is not None:
            parents[key] = _name_key(org.parent.name)
    for key, item in plan.organizations.items():
        names[key] = item.label
        if item.refs["parent"] is not None:
            parents[key] = item.refs["parent"]  # die Datei ersetzt das übergeordnete Gremium, leer lässt es
    for key, item in plan.organizations.items():
        if item.failed or parents.get(key, key) == key:
            continue
        path = [key]
        current = parents[key]
        while current in parents and current not in path:
            path.append(current)
            current = parents[current]
        if current == key:
            ring = " → ".join(names.get(k, k) for k in [*path, key])
            plan.fail(item, f"Übergeordnete Gremien bilden einen Ring: {ring}.")


def _org_kind(org_type: str) -> str:
    return {"faction": "fraktionen", "department": "aemter"}.get(org_type, "gremien")


_PERSON_LABELS = {
    "form_of_address": "Anrede",
    "title": "Titel",
    "email": "E-Mail",
    "delivery_channel": "Zustellweg",
    "start_date": "Mandatsbeginn",
    "end_date": "Mandatsende",
    "is_active": "Aktiv",
}
_SECRET_LABELS = {
    "phone": "Telefon",
    "address": "Adresse",
    "bank_account_holder": "Kontoinhaber/in",
    "bank_iban": "IBAN",
    "bank_bic": "BIC",
}
_BANK_SECRETS = ("bank_account_holder", "bank_iban", "bank_bic")


def _person_problems(row: _Row, given: str, family: str, email: str, start: Any, end: Any) -> list[str]:
    problems = []
    if not given or not family:
        problems.append("Vor- und Nachname sind Pflicht.")
    if email:
        try:
            validate_email(email)
        except ValidationError:
            problems.append("E-Mail-Adresse ungültig.")
    if start is not None and end is not None and end < start:
        problems.append("Das Mandatsende liegt vor dem Mandatsbeginn.")
    iban = allowance_service.normalize_account_code(row.get("iban"))
    if iban:
        problem = allowance_service.iban_problem(iban)
        if problem:
            problems.append(f"Die IBAN ist ungültig: {problem}.")
    bic = allowance_service.normalize_account_code(row.get("bic"))
    if bic and not allowance_service.valid_bic(bic):
        problems.append("Die BIC ist ungültig (8 oder 11 Zeichen, z. B. COBADEFFXXX).")
    return problems


def _plan_persons(plan: ImportPlan, rows: list[_Row]) -> None:
    tally = plan.tallies.setdefault("personen", Tally())
    by_name: dict[tuple[str, str], list[SessionPerson]] = {}
    for person in SessionPerson.objects.filter(tenant=plan.tenant):
        by_name.setdefault((_name_key(person.given_name), _name_key(person.family_name)), []).append(person)
    tally.before = sum(len(group) for group in by_name.values())
    matched: dict[Any, Item] = {}

    for row in rows:
        tally.rows += 1
        kennung = row.get("kennung")
        placeholder = Item("personen", row.file, row.line, kennung, failed=True)
        try:
            given = row.text("vorname", max_length=100)
            family = row.text("nachname", max_length=100)
            title = row.text("titel", max_length=50)
            address_form = row.text("anrede", max_length=50)
            phone = row.text("telefon", max_length=100)
            holder = row.text("kontoinhaber", max_length=200)
            email = row.text("email", max_length=254)
            delivery = row.choice("zustellweg", _DELIVERY, "E-Mail, Portal, Brief")
            start, end = row.date("mandat_beginn"), row.date("mandat_ende")
            active = row.boolean("aktiv")
        except CellValueError as exc:
            plan.error(row.file, row.line, str(exc))
            plan.persons.setdefault(kennung, placeholder)
            continue
        label = f"{given} {family}".strip()
        placeholder.label = label or kennung
        if not kennung:
            plan.error(row.file, row.line, "Kennung fehlt.")
            continue
        if kennung in plan.persons:
            plan.error(row.file, row.line, f"Kennung „{kennung}“ doppelt (zuerst Zeile {plan.persons[kennung].line}).")
            continue
        problems = _person_problems(row, given, family, email, start, end)
        candidates = by_name.get((_name_key(given), _name_key(family)), [])
        if len(candidates) > 1 and email:
            candidates = [p for p in candidates if p.email.casefold() == email.casefold()] or candidates
        if len(candidates) > 1:
            problems.append("Mehrere Personen dieses Namens im Bestand – E-Mail-Adresse angeben, um zu unterscheiden.")
        instance = candidates[0] if len(candidates) == 1 else None
        identity = instance.pk if instance is not None else (_name_key(given), _name_key(family), email.casefold())
        if not problems and identity in matched:
            problems.append(f"Dieselbe Person wie Zeile {matched[identity].line}.")
        if problems:
            for message in problems:
                plan.error(row.file, row.line, f"{label}: {message}" if label else message)
            plan.persons[kennung] = placeholder
            continue
        item = Item(
            kind="personen",
            file=row.file,
            line=row.line,
            label=label,
            instance=instance,
            values={
                "given_name": instance.given_name if instance is not None else given,
                "family_name": instance.family_name if instance is not None else family,
                "title": title,
                "form_of_address": address_form,
                "email": email,
                "delivery_channel": delivery,
                "start_date": start,
                "end_date": end,
                "is_active": active,
            },
            secrets={
                key: value
                for key, value in {
                    "phone": phone,
                    "address": row.get("adresse"),
                    "bank_account_holder": holder,
                    "bank_iban": allowance_service.normalize_account_code(row.get("iban")),
                    "bank_bic": allowance_service.normalize_account_code(row.get("bic")),
                }.items()
                if value
            },
        )
        _compare(item, _PERSON_LABELS)
        if instance is not None:
            for secret, value in item.secrets.items():
                if getattr(instance, f"get_{secret}_decrypted")() != value:
                    item.changes.append(_SECRET_LABELS[secret])
            if email and instance.email and instance.email.casefold() != email.casefold():
                # Gleicher Name, andere E-Mail: vielleicht eine andere Person. Der Bericht zeigt die Werte nicht,
                # deshalb ausdrücklich darauf hinweisen (sonst ginge z. B. Sitzungsgeld auf ein fremdes Konto)
                bank = [_SECRET_LABELS[s] for s in _BANK_SECRETS if _SECRET_LABELS[s] in item.changes]
                plan.hint(
                    row.file,
                    row.line,
                    f"{label}: andere E-Mail-Adresse als bei der vorhandenen Person gleichen Namens – möglicherweise "
                    "eine andere Person"
                    + (f"; dabei ändern sich auch {', '.join(bank)}" if bank else "")
                    + ". Vor dem Import prüfen; eine andere Person gleichen Namens zuerst von Hand anlegen.",
                )
        matched[identity] = item
        plan.persons[kennung] = item


def _term_for_day(plan: ImportPlan, stock: list[SessionLegislativeTerm], day: date | None) -> str | None:
    """Wahlperiode (Namensschlüssel), die den Tag enthält – aus der Datei oder dem Bestand (auch mehrdeutige)."""
    if day is None:
        return None
    periods = [
        (_name_key(term.name), term.start_date, term.end_date)
        for term in stock
        if _name_key(term.name) not in plan.terms
    ]
    for key, item in plan.terms.items():
        if item.failed:
            continue
        instance = item.instance
        periods.append(
            (
                key,
                item.values["start_date"] or (instance.start_date if instance is not None else None),
                item.values["end_date"] or (instance.end_date if instance is not None else None),
            )
        )
    for key, start, end in periods:
        if (start or end) and (start is None or start <= day) and (end is None or day <= end):
            return key
    return None


def _lines_text(lines: list[int]) -> str:
    return ("Zeile " if len(lines) == 1 else "Zeilen ") + ", ".join(str(line) for line in lines)


_MEMBERSHIP_LABELS = {"role": "Funktion", "has_voting_rights": "Stimmrecht", "end_date": "Ende"}


def _plan_memberships(plan: ImportPlan, rows: list[_Row]) -> None:
    tally = plan.tallies.setdefault("besetzungen", Tally())
    tenant_orgs, ambiguous_orgs = _by_name(SessionOrganization.objects.filter(tenant=plan.tenant))
    term_stock = list(SessionLegislativeTerm.objects.filter(tenant=plan.tenant))
    terms, ambiguous_terms = _by_name(term_stock)
    term_names = {_name_key(t.name): t.name for t in term_stock}
    vote_hints: dict[str, list[int]] = {}
    by_pair: dict[tuple[Any, Any], list[SessionOrganizationMembership]] = {}
    for membership in SessionOrganizationMembership.objects.filter(organization__tenant=plan.tenant).select_related(
        "legislative_term"
    ):
        by_pair.setdefault((membership.organization_id, membership.person_id), []).append(membership)
    tally.before = sum(len(group) for group in by_pair.values())
    planned: dict[tuple[str, str], list[Item]] = {}

    for row in rows:
        tally.rows += 1
        try:
            role, deputy = row.role()
            vote = row.boolean("stimmrecht")
            start, end = row.date("beginn"), row.date("ende")
        except CellValueError as exc:
            plan.error(row.file, row.line, str(exc))
            continue
        person_key, substitute_key = row.get("person"), row.get("vertretung_fuer")
        org_name, term_name = row.get("gremium"), row.get("wahlperiode")
        org_key, term_key = _name_key(org_name), _name_key(term_name) or None
        person = plan.persons.get(person_key)
        substitute = plan.persons.get(substitute_key) if substitute_key else None
        org_item = plan.organizations.get(org_key)
        term_item = plan.terms.get(term_key or "")

        problems: list[str] = []
        if person is None:
            problems.append(f"Person „{person_key}“ fehlt in der Personendatei.")
        elif person.failed:
            problems.append(f"Person „{person_key}“ hat Fehler (siehe {person.file}, Zeile {person.line}).")
        if org_item is None and org_key in ambiguous_orgs:
            problems.append(f"Gremium {_ambiguous(org_name)}")
        elif org_item is None and org_key not in tenant_orgs:
            problems.append(f"Gremium „{org_name}“ unbekannt (weder in den Dateien noch angelegt).")
        elif org_item is not None and org_item.failed:
            problems.append(f"Gremium „{org_name}“ hat Fehler (siehe {org_item.file}, Zeile {org_item.line}).")
        if substitute_key and substitute is None:
            problems.append(f"Vertretene Person „{substitute_key}“ fehlt in der Personendatei.")
        elif substitute is not None and substitute.failed:
            problems.append(f"Vertretene Person „{substitute_key}“ hat Fehler.")
        elif substitute_key and substitute_key == person_key:
            problems.append("Eine Person kann sich nicht selbst vertreten.")
        if start is not None and end is not None and end < start:
            problems.append("Das Ende liegt vor dem Beginn.")
        if term_key and term_item is None and term_key in ambiguous_terms:
            problems.append(f"Wahlperiode {_ambiguous(term_name)}")
        elif term_key and term_item is None and term_key not in terms:
            problems.append(f"Wahlperiode „{term_name}“ unbekannt.")
        elif term_item is not None and term_item.failed:
            problems.append(f"Wahlperiode „{term_name}“ hat Fehler.")
        if problems:
            for message in problems:
                plan.error(row.file, row.line, message)
            continue
        assert person is not None

        org_instance = org_item.instance if org_item is not None else tenant_orgs[org_key]
        org_label = org_item.label if org_item is not None else tenant_orgs[org_key].name
        label = f"{person.label} – {org_label}"
        pair_existing: list[SessionOrganizationMembership] = []
        if org_instance is not None and person.instance is not None:
            pair_existing = by_pair.get((org_instance.pk, person.instance.pk), [])
        match = next((m for m in pair_existing if m.start_date == start), None)
        # Ohne Angabe: Wahlperiode nach dem Beginn, nur für neue Besetzungen
        term_ref = term_key or (_term_for_day(plan, term_stock, start) if match is None else None)
        if term_ref in ambiguous_terms:
            plan.error(
                row.file, row.line, f"{label}: Wahlperiode zum Beginn – {_ambiguous(term_names[term_ref or ''])}"
            )
            continue
        if match is None and deputy and not substitute_key and vote is None:
            # Ohne vertretene Person zählte die Stellvertretung sonst als stimmberechtigtes Mitglied mit
            # (Beschlussfähigkeit, attendance_service.roster)
            plan.hint(
                row.file,
                row.line,
                f"{label}: stellvertretendes Mitglied ohne vertretung_fuer – ohne Stimmrecht übernommen; "
                "die vertretene Person angeben, damit sie nachrücken kann.",
            )
            vote = False
        vote_hint = None
        if match is None:
            role = role or "member"
            if vote is None:
                vote, vote_hint = default_vote(role, row.get("funktion"), plan.tenant.state_profile_id)
        item = Item(
            kind="besetzungen",
            file=row.file,
            line=row.line,
            label=label,
            instance=match,
            values={"role": role, "has_voting_rights": vote, "start_date": start, "end_date": end},
            refs={
                "organization": org_key,
                "organization_label": org_label,
                "person": person_key,
                "person_label": person.label,
                "substitute": substitute_key or None,
                "term": term_ref,
            },
        )
        _compare(item, _MEMBERSHIP_LABELS)
        if match is not None:
            if term_key and _name_key(getattr(match.legislative_term, "name", "")) != term_key:
                item.changes.append("Wahlperiode")
            if substitute is not None and (
                substitute.instance is None or match.substitute_for_id != substitute.instance.pk
            ):
                item.changes.append("Vertretung für")
        if term_key and start is not None:
            term_start, term_end = (
                (term_item.values["start_date"], term_item.values["end_date"])
                if term_item is not None
                else (terms[term_key].start_date, terms[term_key].end_date)
            )
            if (term_start and start < term_start) or (term_end and start > term_end):
                plan.hint(row.file, row.line, f"Beginn liegt außerhalb der Wahlperiode „{term_name}“.")

        # Eine Person hat in einem Gremium keine zwei Besetzungen mit überlappendem Zeitraum
        new_end = end if end is not None else (match.end_date if match is not None else None)
        clash = None
        for other in planned.get((org_key, person_key), []):
            other_end = other.values["end_date"] or (other.instance.end_date if other.instance is not None else None)
            if _overlaps(start, new_end, other.values["start_date"], other_end):
                clash = f"überschneidet sich mit Zeile {other.line}"
                break
        for membership in pair_existing if clash is None else []:
            if membership is not match and _overlaps(start, new_end, membership.start_date, membership.end_date):
                since = f" ab {membership.start_date:%d.%m.%Y}" if membership.start_date else ""
                clash = f"überschneidet sich mit der vorhandenen Besetzung{since}"
                break
        if clash is not None:
            plan.error(row.file, row.line, f"{label}: Zeitraum {clash}.")
            continue
        planned.setdefault((org_key, person_key), []).append(item)
        plan.memberships.append(item)
        if vote_hint is not None:
            vote_hints.setdefault(vote_hint, []).append(row.line)

    for kind, lines in vote_hints.items():
        plan.hint(plan.files.get("besetzungen", "besetzungen"), None, f"{_VOTE_HINTS[kind]} ({_lines_text(lines)}).")


def _count(plan: ImportPlan) -> None:
    """Zählabgleich: jede Zeile genau einmal als neu, geändert, unverändert oder fehlerhaft."""
    groups: dict[str, list[Item]] = {kind: [] for kind in KINDS}
    for item in plan.items():
        groups[item.kind].append(item)
    for kind, tally in plan.tallies.items():
        ok = [item for item in groups[kind] if not item.failed]
        tally.created = sum(1 for item in ok if item.action == NEW)
        tally.updated = sum(1 for item in ok if item.action == CHANGED)
        tally.unchanged = sum(1 for item in ok if item.action == UNCHANGED)
        tally.failed = tally.rows - len(ok)
        tally.after = tally.before + tally.created if not plan.errors else None


def plan_import(tenant: SessionTenant, directory: Path) -> ImportPlan:
    """Dateien lesen, prüfen und mit dem Bestand abgleichen – ohne zu schreiben (Prüflauf)."""
    plan = ImportPlan(tenant=tenant)
    files, problems = find_files(directory)
    for message in problems:
        plan.error(directory.name or str(directory), None, message)
    rows_by_kind: dict[str, list[_Row]] = {}
    for kind, path in files.items():
        rows = _read_rows(plan, kind, path)
        if rows is not None:
            rows_by_kind[kind] = rows
    if "wahlperioden" in rows_by_kind:
        _plan_terms(plan, rows_by_kind["wahlperioden"])
    if any(kind in rows_by_kind for kind in ORGANIZATION_KINDS):
        _plan_organizations(plan, rows_by_kind)
    if "personen" in rows_by_kind:
        _plan_persons(plan, rows_by_kind["personen"])
    if "besetzungen" in rows_by_kind:
        if "personen" in rows_by_kind:
            _plan_memberships(plan, rows_by_kind["besetzungen"])
        else:
            plan.error(plan.files["besetzungen"], None, "Besetzungen brauchen die Personendatei (personen.csv).")
    _count(plan)
    return plan


# ---------------------------------------------------------------------------
# Ausführen
# ---------------------------------------------------------------------------


def _assign(instance: Any, values: dict[str, Any]) -> None:
    """Werte setzen; leere Werte lassen das Feld (bzw. den Standard des Modells) unverändert."""
    for field_name, value in values.items():
        if value is not None and value != "":
            setattr(instance, field_name, value)


def lock_tenant(tenant: SessionTenant) -> None:
    """
    Importläufe für denselben Mandanten nacheinander: in derselben Transaktion vor :func:`plan_import` aufrufen,
    damit zwischen Planen und :func:`apply_plan` kein zweiter Lauf dieselben Datensätze anlegt.

    ``FOR NO KEY UPDATE`` (PostgreSQL) sperrt nur gegen weitere Importläufe und Änderungen am Mandanten, nicht
    gegen neue Datensätze, die auf den Mandanten verweisen. SQLite (Tests) sperrt ohnehin die ganze Datenbank.
    """
    SessionTenant.objects.select_for_update(no_key=True).filter(pk=tenant.pk).values_list("pk", flat=True).first()


def apply_plan(plan: ImportPlan) -> None:
    """Geplante Änderungen schreiben – nur ohne Fehler, ganz oder gar nicht."""
    if plan.errors:
        raise ValueError("Ein Import mit Fehlern wird nicht ausgeführt.")
    tenant = plan.tenant
    with transaction.atomic():
        # Nur eindeutige Namen: Mehrdeutige hat der Plan als Fehler gemeldet (KeyError statt beliebiger Zuordnung)
        terms, _ = _by_name(SessionLegislativeTerm.objects.filter(tenant=tenant))
        for key, item in plan.terms.items():
            if item.action != UNCHANGED:
                term = item.instance or SessionLegislativeTerm(tenant=tenant)
                _assign(term, item.values)
                term.save()
                terms[key] = term

        orgs, _ = _by_name(SessionOrganization.objects.filter(tenant=tenant))
        for key, item in plan.organizations.items():
            if item.action != UNCHANGED:
                org = item.instance or SessionOrganization(tenant=tenant)
                _assign(org, item.values)
                org.save()
                orgs[key] = org
        for key, item in plan.organizations.items():
            parent_key = item.refs["parent"]
            if parent_key and orgs[key].parent_id != orgs[parent_key].pk:
                orgs[key].parent = orgs[parent_key]
                orgs[key].save()

        persons: dict[str, SessionPerson] = {}
        for kennung, item in plan.persons.items():
            if item.action == UNCHANGED:
                persons[kennung] = item.instance
                continue
            person = item.instance or SessionPerson(tenant=tenant)
            _assign(person, item.values)
            for secret, value in item.secrets.items():
                getattr(person, f"set_{secret}_encrypted")(value)
            person.save()
            persons[kennung] = person

        for item in plan.memberships:
            if item.action == UNCHANGED:
                continue
            membership = item.instance or SessionOrganizationMembership(
                organization=orgs[item.refs["organization"]], person=persons[item.refs["person"]]
            )
            _assign(membership, item.values)
            if item.refs["term"]:
                membership.legislative_term = terms[item.refs["term"]]
            if item.refs["substitute"]:
                membership.substitute_for = persons[item.refs["substitute"]]
            membership.save()

    plan.applied = True
    for kind, tally in plan.tallies.items():
        tally.after = _stock(tenant, kind)


def _stock(tenant: SessionTenant, kind: str) -> int:
    """Bestand einer Objektart im Mandanten."""
    orgs = SessionOrganization.objects.filter(tenant=tenant)
    if kind == "wahlperioden":
        return SessionLegislativeTerm.objects.filter(tenant=tenant).count()
    if kind == "gremien":
        return orgs.exclude(organization_type__in=list(_ORG_TYPE_BY_KIND.values())).count()
    if kind in _ORG_TYPE_BY_KIND:
        return orgs.filter(organization_type=_ORG_TYPE_BY_KIND[kind]).count()
    if kind == "personen":
        return SessionPerson.objects.filter(tenant=tenant).count()
    return SessionOrganizationMembership.objects.filter(organization__tenant=tenant).count()


# ---------------------------------------------------------------------------
# Gegenprobe mit den öffentlichen Mitgliederlisten
# ---------------------------------------------------------------------------

_SALUTATIONS = {"frau": "Frau", "herr": "Herr"}
# Akademische Titel wie „Dr.“, „Prof.“, „Dipl.-Ing.“, „Dr.-Ing.“, „Dr. med.“; ohne Punkt nur „Dr“ und „Prof“,
# denn „Phil“, „Ing“ oder „Med“ ohne Punkt sind Namen. Jede Wiederholung beginnt mit „-“, damit das Muster
# eindeutig bleibt (kein exponentielles Backtracking)
_TITLE_RE = re.compile(
    r"^(?:(?:dr|prof)\.?|(?:dipl|ing|med|rer|nat|phil|jur|dent|vet|habil|h\.c)\.)(?:-[a-zäöüß]+\.?)*$",
    re.IGNORECASE,
)


def person_key(name: str) -> str:
    """Namensvergleich: ohne Anrede und akademische Titel, „Nachname, Vorname“ gedreht, Groß-/Kleinschreibung egal."""
    name = re.sub(r"\s+", " ", name).strip()
    if "," in name:
        family, _, given = name.partition(",")
        name = f"{given.strip()} {family.strip()}"
    tokens = [token for token in name.split(" ") if token and not _TITLE_RE.match(token)]
    if len(tokens) > 1 and tokens[0].casefold() in _SALUTATIONS:
        tokens = tokens[1:]
    return " ".join(tokens).casefold()


def cross_check(plan: ImportPlan, public: dict[str, list[str]], day: date) -> None:
    """
    Am Stichtag laufende Besetzungen der Datei je Gremium gegen die öffentlichen Mitgliederlisten.

    ``public``: Gremienname -> Namen laufender Mitglieder im RIS-Bestand
    (:func:`insight_core.services.public_members.current_members`). Abweichungen werden Hinweise.
    """
    public_by_key = {_name_key(name): (name, members) for name, members in public.items()}
    imported: dict[str, dict[str, str]] = {}
    labels: dict[str, str] = {}
    for item in plan.memberships:
        start = item.values["start_date"]
        end = item.values["end_date"] or (item.instance.end_date if item.instance is not None else None)
        if item.failed or (start is not None and start > day) or (end is not None and end < day):
            continue
        org_key = item.refs["organization"]
        labels[org_key] = item.refs["organization_label"]
        name = item.refs["person_label"]
        imported.setdefault(org_key, {})[person_key(name)] = name
    for org_key in sorted(imported, key=lambda key: labels[key].casefold()):
        mine = imported[org_key]
        if org_key not in public_by_key:
            plan.cross_check.append({"gremium": labels[org_key], "oeffentlich": None})
            continue
        public_name, members = public_by_key[org_key]
        theirs = {person_key(name): name for name in members}
        entry = {
            "gremium": labels[org_key],
            "oeffentlich": public_name,
            "uebereinstimmend": len(mine.keys() & theirs.keys()),
            "nur_oeffentlich": sorted(theirs[k] for k in theirs.keys() - mine.keys()),
            "nur_import": sorted(mine[k] for k in mine.keys() - theirs.keys()),
        }
        plan.cross_check.append(entry)
        if entry["nur_oeffentlich"] or entry["nur_import"]:
            plan.hint(
                plan.files.get("besetzungen", "besetzungen"),
                None,
                f"Gegenprobe „{labels[org_key]}“: Besetzung weicht von der öffentlichen Mitgliederliste ab.",
            )


# ---------------------------------------------------------------------------
# Vorlagen
# ---------------------------------------------------------------------------


class TemplateExistsError(Exception):
    """Im Zielverzeichnis liegen schon Importdateien; Vorlagen überschreiben sie nicht."""


_NAME_PARTICLES = {"von", "van", "de", "der", "den", "del", "zu", "zur", "vom", "ten", "ter", "op"}


def split_name(name: str) -> tuple[str, str, str, str]:
    """
    Anzeigename aus dem RIS (z. B. „Dr. Erika Musterfrau“, „Musterfrau, Erika“) -> (Anrede, Titel, Vorname,
    Nachname). Namenszusätze wie „von der“ gehören zum Nachnamen. Die Verwaltung prüft das Ergebnis.
    """
    text = re.sub(r"\s+", " ", name).strip()
    family_part = ""
    if "," in text:
        family_part, _, text = text.partition(",")
    tokens = [token for token in text.strip().split(" ") if token]
    salutation = ""
    if tokens and tokens[0].casefold() in _SALUTATIONS:
        salutation = _SALUTATIONS[tokens.pop(0).casefold()]
    family_tokens = [token for token in family_part.split(" ") if token]
    titles = [token for token in tokens + family_tokens if _TITLE_RE.match(token)]
    tokens = [token for token in tokens if not _TITLE_RE.match(token)]
    family_tokens = [token for token in family_tokens if not _TITLE_RE.match(token)]
    if not family_tokens and tokens:
        family_tokens = [tokens.pop()]
        while len(tokens) > 1 and tokens[-1].casefold() in _NAME_PARTICLES:
            family_tokens.insert(0, tokens.pop())
    return salutation, " ".join(titles), " ".join(tokens), " ".join(family_tokens)


_COMMITTEE_KIND_TEXT = {
    SessionOrganization.COMMITTEE_KIND_MAIN: "Hauptausschuss",
    SessionOrganization.COMMITTEE_KIND_FINANCE: "Finanzausschuss",
    SessionOrganization.COMMITTEE_KIND_AUDIT: "Rechnungsprüfungsausschuss",
}


def _guess_organization(name: str) -> tuple[str, str, str]:
    """Gremienname -> (Datei, Art, Ausschussart) für die Vorlage; Art leer, wenn nicht erkennbar."""
    tokens = _fold(name).split("_")
    if any(token.endswith("fraktion") or token == "gruppe" for token in tokens):
        return "fraktionen", "", ""
    for token in tokens:
        org_type = _ART.get(token)
        if token.endswith("beirat"):
            org_type = "advisory"
        elif org_type is None and token.endswith("ausschuss"):
            org_type = "committee"
        elif org_type is None and token.endswith("kommission"):
            org_type = "commission"
        if org_type in ("council", "committee", "advisory", "commission"):
            kind = _COMMITTEE_KIND.get(token, "") if org_type == "committee" else ""
            return "gremien", _ORG_TYPE_LABELS[org_type], _COMMITTEE_KIND_TEXT.get(kind, "")
    return "gremien", "", ""


def _day_text(day: date | None) -> str:
    return f"{day:%d.%m.%Y}" if day is not None else ""


def _prefill(roster: PublicRoster, day: date) -> dict[str, list[dict[str, str]]]:
    """Zeilen der Vorlagen aus dem öffentlichen Bestand (nur Namen, Funktionen und Zeiträume)."""
    rows: dict[str, list[dict[str, str]]] = {kind: [] for kind in KINDS}
    current_term = ""
    for term in roster.terms:
        term_name = (term.name or "").strip()
        if not term_name:
            continue
        rows["wahlperioden"].append(
            {"name": term_name, "beginn": _day_text(term.start_date), "ende": _day_text(term.end_date)}
        )
        if (term.start_date or term.end_date) and (term.start_date or day) <= day <= (term.end_date or day):
            current_term = term_name
    seen: set[str] = set()
    for org in roster.organizations:
        name = re.sub(r"\s+", " ", org.name or "").strip()
        if _name_key(name) in seen:
            continue
        seen.add(_name_key(name))
        kind, art, committee = _guess_organization(name)
        row = {
            "name": name,
            "kurzname": (org.short_name or "").strip(),
            "beginn": _day_text(org.start_date),
            "ende": _day_text(org.end_date),
        }
        if kind == "gremien":
            row |= {"art": art, "ausschussart": committee}
        rows[kind].append(row)
    for person in roster.persons:
        salutation, title, given, family = split_name(person.name)
        if person.given_name and person.family_name:
            given, family = person.given_name, person.family_name
        rows["personen"].append(
            {
                "kennung": person.key,
                "anrede": salutation,
                "titel": person.title or title,
                "vorname": given,
                "nachname": family,
            }
        )
    rows["personen"].sort(key=lambda row: (row["nachname"].casefold(), row["vorname"].casefold()))
    for membership in roster.memberships:
        vote = "" if membership.voting_right is None else ("ja" if membership.voting_right else "nein")
        role, deputy = resolve_role(membership.role) or ("", False)
        stated = "stimm" in _fold(membership.role)  # „mit Stimmrecht“, „stimmberechtigt“
        if deputy or (role == "expert_citizen" and membership.voting_right and not stated):
            # Bei Stellvertretungen und Hinzugewählten ist ein öffentliches „ja“ nur der Standard der Quelle
            # (SessionNet: ja, außer die Abschnittsüberschrift sagt „ohne Stimmrecht“); leer lässt die
            # Importregel greifen (default_vote; Stellvertretung ohne vertretene Person kein Stimmrecht)
            vote = ""
        rows["besetzungen"].append(
            {
                "person": membership.person_key,
                "gremium": membership.organization,
                "funktion": role_text(membership.role),
                "stimmrecht": vote,
                "beginn": _day_text(membership.start_date),
                "ende": _day_text(membership.end_date),
                # Ohne Beginn bestimmt der Import keine Wahlperiode; dann die am Stichtag laufende
                "wahlperiode": current_term if membership.start_date is None else "",
            }
        )
    return rows


def write_templates(directory: Path, roster: PublicRoster | None = None, day: date | None = None) -> dict[Path, int]:
    """
    Vorlagen je Objektart: Kopfzeile, Semikolon, UTF-8 mit BOM (öffnet in Excel direkt) -> Pfad: Zeilen.

    Mit ``roster`` (:func:`insight_core.services.public_members.public_roster`) vorbefüllt mit dem
    öffentlichen Bestand, z. B. aus dem SessionNet-Adapter; Kontakt- und Bankdaten bleiben leer. Vorhandene
    Importdateien im Verzeichnis werden nie überschrieben.
    """
    targets = [directory / f"{kind}{suffix}" for kind in KINDS for suffix in (".csv", ".xlsx")]
    existing = [path.name for path in targets if path.exists()]
    if existing:
        raise TemplateExistsError(", ".join(existing))
    directory.mkdir(parents=True, exist_ok=True)
    rows = _prefill(roster, day or date.today()) if roster is not None else {kind: [] for kind in KINDS}
    written: dict[Path, int] = {}
    for kind in KINDS:
        path = directory / f"{kind}.csv"
        keys = [column.key for column in COLUMNS[kind]]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            # Namen stammen aus fremden Quellen: Formel-Anfänge entschärft (beim Einlesen wieder entfernt)
            writer = csv_safety.writer(handle, delimiter=";")
            writer.writerow(keys)
            writer.writerows([row.get(key, "") for key in keys] for row in rows[kind])
        written[path] = len(rows[kind])
    return written
