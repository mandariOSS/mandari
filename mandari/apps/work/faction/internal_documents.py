# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Unterlagen für Fraktionssitzungen (Issue #873).

Ablauf:

1. Der Vorsitz lädt eine PDF-Unterlage hoch (etwa die Tagesordnung des nichtöffentlichen Teils einer
   Ratssitzung). Work erkennt den Text im eigenen Betrieb und legt die Datei im Dokumentenspeicher im Ordner
   „Nichtöffentliche Vorgänge“ ab (``apps.work.motions.non_public``).
2. Aus dem Text schlägt Work Tagesordnungspunkte mit Nummer und Titel vor (``parse_agenda_items``).
3. Der Vorsitz wählt aus, korrigiert und bestätigt. Erst dann entstehen nichtöffentliche TOPs, verknüpft
   mit der Unterlage.

Berechtigt sind vereidigte Mitglieder, die Tagesordnungen genehmigen (``agenda.approve``: Vorsitz,
stellvertretender Vorsitz) oder Fraktionssitzungen verwalten (``faction.manage``: u. a. Geschäftsführung).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

from .visibility import is_sworn_member

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem, FactionMeeting

#: Sitzungen, deren Tagesordnung sich noch ändern lässt (wie ``can_edit`` der Sitzungsansicht)
EDITABLE_MEETING_STATUSES = ("draft", "planned", "invited", "ongoing")

#: Höchstzahl vorgeschlagener TOPs je Unterlage und Länge der Felder
MAX_PROPOSALS = 60
MAX_TITLE = 500
MAX_SOURCE_NUMBER = 20

#: Feste Meldungen
NOT_ALLOWED = "Nichtöffentliche Unterlagen lesen nur vereidigte Mitglieder ein, die die Tagesordnung genehmigen."
NOT_EDITABLE = "Die Tagesordnung dieser Sitzung lässt sich nicht mehr ändern."
NOTHING_SELECTED = "Bitte mindestens einen Tagesordnungspunkt auswählen."

_MONTHS = "januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember"

#: TOP-Zeile: optional „TOP“/„Punkt“, optional Kennung des Teils (Ö, N, NÖ), Nummer (1, 2.1, 12a), Titel.
#: Der Titel beginnt mit einem Buchstaben oder Anführungszeichen – so fallen Datumsangaben, Uhrzeiten,
#: Postleitzahlen und Drucksachennummern heraus.
_ITEM_RE = re.compile(
    r"^(?:(?:TOP|Top|TO-Punkt|Tagesordnungspunkt|Punkt|Pkt\.?)\s*)?"
    r"(?P<part>N[ÖO]|NÖ|N|Ö|O)?\s?[-.]?\s?"
    r"(?P<num>\d{1,3}(?:\.\d{1,3}){0,3}[a-z]?)"
    r"(?:\.|\)|:)?"
    r"\s+[-–:]?\s*"
    r"(?P<title>[A-Za-zÄÖÜäöüß„\"'»(].*)$"
)
_MONTH_RE = re.compile(rf"^(?:{_MONTHS})\b", re.IGNORECASE)
#: Abschnittsüberschriften („Nichtöffentlicher Teil“, „B. Nicht öffentliche Sitzung“, „Öffentlicher Teil“)
_NON_PUBLIC_RE = re.compile(r"nicht\s*[-‐–]?\s*(?:ö|oe|o)ffentlich", re.IGNORECASE)
_PUBLIC_RE = re.compile(r"(?:^|[\s.:])(?:ö|oe|o)ffentliche[rn]?\s+(?:Teil|Sitzung|Tagesordnung)", re.IGNORECASE)
_SECTION_MAX_LENGTH = 80
#: Fortsetzungszeilen eines Titels: kleingeschrieben oder nach Trennstrich, Komma bzw. Bindewort
_CONTINUES_RE = re.compile(r"(?:[-,/]|\b(?:und|oder|sowie|für|der|des|zur|zum|im|in|an|auf|von|mit))$", re.IGNORECASE)


@dataclass(frozen=True)
class AgendaProposal:
    """Vorgeschlagener TOP: Nummer in der Unterlage (z. B. „N 3.1“) und Titel."""

    source_number: str
    title: str


def can_import(membership: Membership | None, meeting: FactionMeeting) -> bool:
    """Darf die Person nichtöffentliche Unterlagen für diese Sitzung einlesen und TOPs übernehmen?"""
    if membership is None or meeting.organization_id != membership.organization_id:
        return False
    if not is_sworn_member(membership):
        return False
    return membership.has_permission("agenda.approve") or membership.has_permission("faction.manage")


def is_editable(meeting: FactionMeeting) -> bool:
    return meeting.status in EDITABLE_MEETING_STATUSES


# =============================================================================
# TOPs aus dem Text erkennen
# =============================================================================


def _is_section(line: str, pattern: re.Pattern[str]) -> bool:
    return len(line) <= _SECTION_MAX_LENGTH and bool(pattern.search(line)) and not _ITEM_RE.match(line)


def _section_mode(line: str) -> str | None:
    """``"internal"`` bzw. ``"public"`` für Abschnittsüberschriften, sonst ``None``."""
    if _is_section(line, _NON_PUBLIC_RE):
        return "internal"
    if _is_section(line, _PUBLIC_RE):
        return "public"
    return None


def _clean(text: str) -> str:
    return " ".join(text.split()).strip(" .:-–")


def parse_agenda_items(lines: list[str]) -> list[AgendaProposal]:
    """
    Tagesordnungspunkte (Nummer und Titel) aus den Textzeilen einer Unterlage.

    Überschriften wie „Nichtöffentlicher Teil“ und „Öffentlicher Teil“ teilen die Unterlage in Abschnitte. Gibt es
    Punkte im nichtöffentlichen Abschnitt, zählen nur diese. Sonst zählen Punkte mit Kennung N/NÖ, falls es welche
    gibt, und andernfalls alle erkannten Punkte. Mehrzeilige Titel werden zusammengesetzt, solange die Folgezeile
    erkennbar weiterläuft.
    """
    found: list[list[str]] = []  # [Abschnitt, Kennung, Nummer, Titel]
    current: list[str] | None = None
    mode = ""
    for raw in lines:
        line = " ".join((raw or "").split())
        if not line:
            current = None
            continue
        section = _section_mode(line)
        if section is not None:
            mode = section
            current = None
            continue
        match = _ITEM_RE.match(line)
        if match and not _MONTH_RE.match(match.group("title")):
            part = (match.group("part") or "").upper().replace("NO", "NÖ")
            current = [mode, part, match.group("num"), match.group("title")]
            found.append(current)
            continue
        if current is None or len(current[3]) >= MAX_TITLE:
            continue
        title = current[3]
        if title.endswith("-") and line[:1].islower():
            current[3] = f"{title[:-1]}{line}"
        elif line[:1].islower() or _CONTINUES_RE.search(title):
            current[3] = f"{title} {line}"
        else:
            current = None

    internal = [entry for entry in found if entry[0] == "internal" and entry[1] not in ("Ö", "O")]
    if internal:
        found = internal
    elif any(entry[1] in ("N", "NÖ") for entry in found):
        found = [entry for entry in found if entry[1] in ("N", "NÖ")]
    elif any(entry[0] == "public" for entry in found):
        # Nur ein öffentlicher Abschnitt erkannt: dessen Punkte sind keine nichtöffentlichen TOPs
        found = [entry for entry in found if entry[0] != "public"]

    proposals: list[AgendaProposal] = []
    seen: set[tuple[str, str]] = set()
    for _mode, part, num, raw_title in found:
        title = _clean(raw_title)[:MAX_TITLE]
        number = f"{part} {num}".strip()[:MAX_SOURCE_NUMBER]
        if not title or (number, title) in seen:
            continue
        seen.add((number, title))
        proposals.append(AgendaProposal(source_number=number, title=title))
        if len(proposals) >= MAX_PROPOSALS:
            break
    return proposals


# =============================================================================
# Einlesen und Übernehmen
# =============================================================================


@dataclass
class ImportResult:
    """Ergebnis des Einlesens: abgelegtes Dokument und Vorschläge (oder eine feste Fehlermeldung)."""

    motion: Any = None
    proposals: list[AgendaProposal] | None = None
    notice: str = ""
    error: str = ""


def import_document(meeting: FactionMeeting, membership: Membership, uploaded_file: Any) -> ImportResult:
    """
    PDF-Unterlage einlesen: Text erkennen, Datei in „Nichtöffentliche Vorgänge“ ablegen, TOPs vorschlagen.

    Die Rechte prüft der Aufrufer (``can_import``); die Datei ist mit ``validate_upload`` geprüft.
    """
    from apps.work.motions import non_public
    from apps.work.motions.import_service import title_from_filename

    data = uploaded_file.read()
    uploaded_file.seek(0)
    extracted = non_public.extract(data, uploaded_file.name or "")
    if extracted is None:
        return ImportResult(error=non_public.UNREADABLE)

    title = f"Nichtöffentliche Unterlage: {title_from_filename(uploaded_file.name or '', ('.pdf',))}"
    motion = non_public.store(
        organization=meeting.organization,
        author=membership,
        uploaded_file=uploaded_file,
        file_size=len(data),
        title=title,
        extracted=extracted,
    )
    return ImportResult(motion=motion, proposals=parse_agenda_items(extracted.lines), notice=extracted.notice)


def proposals_for(motion: Any) -> tuple[list[AgendaProposal], str]:
    """Vorschläge einer bereits abgelegten Unterlage erneut aus ihrem PDF-Anhang erkennen (Rückkehr zur Auswahl)."""
    from apps.work.motions import non_public

    document = motion.documents.filter(mime_type="application/pdf").order_by("uploaded_at").first()
    if document is None:
        return [], ""
    try:
        with document.file.open("rb") as handle:
            data = handle.read()
    except (FileNotFoundError, OSError, ValueError):
        return [], ""
    extracted = non_public.extract(data, document.filename)
    if extracted is None:
        return [], ""
    return parse_agenda_items(extracted.lines), extracted.notice


def selected_proposals(*, numbers: list[str], titles: list[str], chosen: list[str]) -> list[AgendaProposal]:
    """Ausgewählte Zeilen des Bestätigungsformulars (Index in ``chosen``), ohne leere Titel, höchstens ``MAX_PROPOSALS``."""
    wanted = {value.strip() for value in chosen}
    selected: list[AgendaProposal] = []
    for index, title in enumerate(titles[:MAX_PROPOSALS]):
        clean_title = " ".join((title or "").split())[:MAX_TITLE]
        if str(index) not in wanted or not clean_title:
            continue
        number = " ".join((numbers[index] if index < len(numbers) else "").split())[:MAX_SOURCE_NUMBER]
        selected.append(AgendaProposal(source_number=number, title=clean_title))
    return selected


def confirm_items(
    meeting: FactionMeeting,
    membership: Membership,
    motion: Any,
    selected: list[AgendaProposal],
) -> list[FactionAgendaItem]:
    """
    Bestätigte Vorschläge als nichtöffentliche TOPs anlegen, fortlaufend nach den vorhandenen NÖ-TOPs
    nummeriert und mit der Unterlage verknüpft. Die Nummer der Unterlage steht in der Beschreibung.
    """
    from .models import FactionAgendaItem

    created: list[FactionAgendaItem] = []
    with transaction.atomic():
        existing = meeting.agenda_items.filter(visibility="internal", parent__isnull=True).exclude(
            is_approval_item=True
        )
        next_number = existing.count() + 1
        order = meeting.agenda_items.count() + 1
        for proposal in selected:
            item = FactionAgendaItem(
                meeting=meeting,
                title=proposal.title[:MAX_TITLE],
                number=f"NÖ {next_number}",
                visibility="internal",
                order=order,
            )
            if proposal.source_number:
                cast(Any, item).set_description_encrypted(
                    f"Nr. {proposal.source_number} der nichtöffentlichen Unterlage"
                )
            item.save()
            item.related_motions.add(motion)
            created.append(item)
            next_number += 1
            order += 1
    return created
