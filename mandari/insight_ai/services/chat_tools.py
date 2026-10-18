# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Werkzeuge des KI-Assistenten (Issue #899): strukturierte Ratsdaten statt Volltext.

Das Modell ruft die Werkzeuge per Function Calling auf (OpenAI-kompatibel, ``TOOLS``). Jedes Werkzeug liest über
die Lese-Fassade ``hub.ris.selectors`` und nur öffentliche Einträge der gewählten Kommune (``ToolContext``). Die
Ergebnisse sind kompakt – Titel, Datum, Gremium und der Link auf die Insight-Seite – und in der Länge begrenzt.
Volltext gibt es nur abschnittsweise (``dokument_abschnitt``), sonst kurze Ausschnitte aus der Suche.

Kennungen nehmen die Werkzeuge als UUID oder als Insight-Link entgegen; die Ergebnisse nennen deshalb meist nur
den Link. Fehler kommen als ``{"fehler": "…"}`` mit festen Texten zurück, nie mit Ausnahmetexten.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypedDict

from django.urls import reverse
from django.utils import timezone

from hub.ris import selectors as ris

if TYPE_CHECKING:
    from insight_core.models import OParlMeeting, OParlOrganization, OParlPaper

logger = logging.getLogger(__name__)

#: Höchstlänge eines Werkzeugergebnisses (Zeichen, etwa 2.000 Token)
MAX_RESULT_CHARS = 6000
#: Längster Zeitraum je Abfrage von Sitzungen (Tage)
MAX_RANGE_DAYS = 92
MAX_MEETINGS = 30
MAX_AGENDA_ITEMS = 60
#: Höchstens so viele Tagesordnungen liefert ``sitzungen_im_zeitraum`` gleich mit
MAX_AGENDAS = 3
MAX_PAPERS = 8
MAX_ORGANIZATIONS = 60
MAX_PERSONS = 8
MAX_MEMBERSHIPS = 8
MAX_DOCUMENTS = 6
MAX_PAPER_FILES = 12
#: Ausschnitte der Dokumentsuche (Zeichen)
SNIPPET_CHARS = 300
#: ``dokument_abschnitt``: Länge eines Abschnitts, Zahl der Abschnitte und Obergrenze insgesamt (Zeichen)
PASSAGE_CHARS = 1000
MAX_PASSAGES = 3
MAX_PASSAGE_TOTAL = 3000

WOCHENTAGE_KURZ = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MARK_RE = re.compile(r"</?mark[^>]*>")
#: Kurze und häufige Wörter zählen bei der Auswahl von Abschnitten nicht
_STOPWORD_TEXT = (
    "aber alle auch auf aus bei bis das dass dem den der des die dies diese dieser durch ein eine einem einen einer "
    "für hat haben ist mit nach noch nicht oder sich sind über und vom von vor was welche welcher wie wird wurde "
    "wurden zum zur zu"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


class Source(TypedDict):
    """Quelle für die Kacheln unter der Antwort."""

    title: str
    url: str
    type: str


class ToolError(Exception):
    """Fehler mit festem Text für das Modell (keine Ausnahmetexte nach außen)."""


@dataclass
class ToolContext:
    """Kommune und Zeitpunkt einer Anfrage; sammelt die Einträge, die die Werkzeuge geliefert haben."""

    body_id: uuid.UUID
    body_name: str
    now: datetime
    #: Vor die Links gesetzt: leer im Chat (relative Links), Adresse der Instanz für fremde Werkzeuge (MCP)
    link_base: str = ""
    sources: dict[str, Source] = field(default_factory=dict)
    #: Links aus Detailwerkzeugen (Sitzung, Vorgang, Dokumentabschnitt) in der Reihenfolge der Aufrufe
    detail_urls: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    @property
    def bodies(self) -> list[uuid.UUID]:
        return [self.body_id]

    def link(self, name: str, pk: object) -> str:
        return f"{self.link_base}{reverse(f'insight_core:insight:{name}', args=[pk])}"

    def note(self, url: str, title: str, kind: str, *, detail: bool = False) -> None:
        """Eintrag als mögliche Quelle merken."""
        if url not in self.sources:
            self.sources[url] = Source(title=(title or "Eintrag")[:100], url=url, type=kind)
        if detail and url not in self.detail_urls:
            self.detail_urls.append(url)


def context_for(body_id: object, *, now: datetime, link_base: str = "") -> ToolContext | None:
    """
    Kontext für eine Kommune; ``None``, wenn es sie nicht gibt oder ihre Veröffentlichung abgeschaltet bzw.
    zurückgenommen ist (``insight_core.publication``). Ohne Kontext gibt es keine Werkzeuge.
    """
    body = ris.public_body(body_id)
    if body is None:
        return None
    from insight_core.publication import body_state

    state = body_state(body.pk)
    if state is not None and (state.paused or state.withdrawn):
        return None
    return ToolContext(body_id=body.pk, body_name=str(body.get_display_name()), now=now, link_base=link_base)


# =============================================================================
# Beschreibung der Werkzeuge (OpenAI-kompatibles Function Calling)
# =============================================================================

_ID = {"type": "string", "description": "UUID oder Link aus einem Ergebnis"}
_VON = {"type": "string", "description": "JJJJ-MM-TT"}
_BIS = {"type": "string", "description": "JJJJ-MM-TT"}
_GREMIUM = {"type": "string", "description": "Gremienname, z. B. Rat"}


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


#: Die Beschreibung geht mit jeder Runde an das Modell; sie bleibt deshalb knapp.
TOOLS: list[dict[str, Any]] = [
    _function(
        "sitzungen_im_zeitraum",
        f"Sitzungen im Zeitraum (höchstens {MAX_RANGE_DAYS} Tage), optional eines Gremiums; "
        f"tagesordnung=true liefert bei bis zu {MAX_AGENDAS} Sitzungen die Tagesordnungen mit.",
        {"von": _VON, "bis": _BIS, "gremium": _GREMIUM, "tagesordnung": {"type": "boolean"}},
        ["von", "bis"],
    ),
    _function("sitzung", "Sitzung mit Tagesordnung, Ergebnissen und Vorlagen.", {"id": _ID}, ["id"]),
    _function(
        "vorgaenge_suchen",
        "Vorgänge (Vorlagen, Anträge) nach Stichworten oder Drucksachennummer, mit Stand.",
        {"text": {"type": "string"}, "von": _VON, "bis": _BIS, "gremium": _GREMIUM},
        ["text"],
    ),
    _function("vorgang", "Vorgang mit Stand, Beratungsfolge, Beschluss und Dokumenten.", {"id": _ID}, ["id"]),
    _function("gremien", "Bestehende Gremien, optional mit Teil des Namens.", {"suchtext": {"type": "string"}}, []),
    _function(
        "personen", "Personen nach Namen mit laufenden Mitgliedschaften.", {"name": {"type": "string"}}, ["name"]
    ),
    _function(
        "dokumente_suchen", "Volltextsuche in Dokumenten; kurze Ausschnitte.", {"text": {"type": "string"}}, ["text"]
    ),
    _function(
        "dokument_abschnitt",
        "Zur Frage passende Abschnitte eines Dokuments, wenn der Ausschnitt nicht reicht.",
        {"id": _ID, "frage": {"type": "string"}},
        ["id", "frage"],
    ),
]

TOOL_NAMES = frozenset(tool["function"]["name"] for tool in TOOLS)


# =============================================================================
# Hilfen
# =============================================================================


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Leere Angaben weglassen (spart Token)."""
    return {
        key: value
        for key, value in values.items()
        if not (value is None or value is False or (isinstance(value, str | list | dict) and not value))
    }


def _short(text: str | None, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _line(*parts: str | None) -> str:
    """Eintrag einer Liste als eine Zeile „Teil | Teil | …“ (knapper als ein JSON-Objekt je Eintrag)."""
    return " | ".join(part for part in parts if part)


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    lokal = timezone.localtime(value) if timezone.is_aware(value) else value
    zeit = lokal.strftime("%H:%M")
    tag = f"{WOCHENTAGE_KURZ[lokal.weekday()]} {lokal.strftime('%d.%m.%Y')}"
    return tag if zeit == "00:00" else f"{tag} {zeit}"


def _day(value: date | None) -> str | None:
    return value.strftime("%d.%m.%Y") if value else None


def _text_arg(args: Mapping[str, Any], name: str, *, required: bool = False, limit: int = 200) -> str:
    value = args.get(name)
    text = " ".join(str(value).split())[:limit] if value is not None else ""
    if required and not text:
        raise ToolError(f"Angabe „{name}“ fehlt.")
    return text


def _date_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> date | None:
    value = _text_arg(args, name, required=required, limit=20)
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ToolError(f"Datum „{name}“ ist ungültig (JJJJ-MM-TT erwartet).")


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _id_arg(args: Mapping[str, Any]) -> str:
    """Kennung aus ``id``: eine UUID oder ein Link, der eine enthält."""
    match = UUID_RE.search(_text_arg(args, "id", required=True, limit=300))
    if not match:
        raise ToolError("Kennung ist ungültig (UUID oder Link aus einem Werkzeugergebnis erwartet).")
    return match.group(0)


def _organizations(ctx: ToolContext, name: str) -> list[OParlOrganization] | None:
    """Gremien zum Namen aus der Frage; ``None`` ohne Angabe. Findet sich keins, endet das Werkzeug mit Hinweis."""
    if not name:
        return None
    found = ris.public_organizations_named(ctx.bodies, name)
    if not found:
        raise ToolError("Kein Gremium mit diesem Namen gefunden; das Werkzeug gremien listet alle.")
    return found


def _search(ctx: ToolContext, index: str, text: str, size: int, **filters: Any) -> list[dict[str, Any]] | None:
    """Treffer der Volltextsuche (Elasticsearch) in der Kommune; ``None``, wenn die Suche nicht antwortet."""
    try:
        from insight_core.services.search_service import get_search_service

        result = get_search_service().search_all(
            query=text, body_id=str(ctx.body_id), page=1, page_size=size, index_names=[index], **filters
        )
    except Exception:
        logger.warning("KI-Assistent: Volltextsuche im Index %s nicht verfügbar", index, exc_info=True)
        return None
    hits = result.get("results") if isinstance(result, dict) else None
    return [hit for hit in hits if isinstance(hit, dict)] if isinstance(hits, list) else []


def _meeting_title(meeting: Any) -> str:
    return str(meeting.get_display_name() or meeting.name or "Sitzung")


def _paper_entry(ctx: ToolContext, paper: OParlPaper, stand: str | None = None) -> dict[str, Any]:
    url = ctx.link("paper_detail", paper.pk)
    ctx.note(url, paper.name or paper.reference or "Vorgang", "paper")
    return _compact(
        {
            "titel": _short(paper.name, 200),
            "az": paper.reference,
            "art": paper.paper_type,
            "datum": _day(paper.date),
            "stand": stand,
            "link": url,
        }
    )


def _status_entries(steps: Iterable[ris.ConsultationStep]) -> list[dict[str, Any]]:
    """Beratungsschritte im Format von ``paper_status`` (Ergebnis nichtöffentlicher Punkte bleibt leer)."""
    return [
        {
            "meeting": SimpleNamespace(cancelled=step.cancelled),
            "date": step.date,
            "organization_name": step.organization_name,
            "agenda_number": step.agenda_number,
            "result": step.result,
            "public": step.public,
            "role": step.role,
            "authoritative": step.authoritative,
        }
        for step in steps
    ]


def _status_texts(ctx: ToolContext, paper_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    from insight_core.services.paper_status import paper_status

    history = ris.public_consultation_history(paper_ids)
    return {pk: paper_status(_status_entries(steps), ctx.now).text for pk, steps in history.items()}


# =============================================================================
# Werkzeuge
# =============================================================================


def sitzungen_im_zeitraum(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    von = _date_arg(args, "von", required=True)
    bis = _date_arg(args, "bis", required=True)
    if von is None or bis is None:
        raise ToolError("Zeitraum fehlt.")
    if bis < von:
        von, bis = bis, von
    hinweis = None
    if (bis - von).days > MAX_RANGE_DAYS:
        bis = von + timedelta(days=MAX_RANGE_DAYS)
        hinweis = f"Zeitraum auf {MAX_RANGE_DAYS} Tage gekürzt."
    gremien = _organizations(ctx, _text_arg(args, "gremium"))
    mit_tagesordnung = args.get("tagesordnung") in (True, "true", "ja", 1)
    start = timezone.make_aware(datetime.combine(von, time.min))
    ende = timezone.make_aware(datetime.combine(bis, time.max))
    found = ris.public_meetings_between(ctx.bodies, start, ende, organizations=gremien)
    sitzungen = list(found[: MAX_MEETINGS + 1])
    tagesordnungen = mit_tagesordnung and 0 < len(sitzungen) <= MAX_AGENDAS
    eintraege = []
    for meeting in sitzungen[:MAX_MEETINGS]:
        url = ctx.link("meeting_detail", meeting.pk)
        titel = _meeting_title(meeting)
        ctx.note(url, f"{titel} ({_when(meeting.start)})", "meeting")
        eintraege.append(
            _line(
                _when(meeting.start),
                _short(titel, 160),
                _short(meeting.location_name, 120) or "-",
                url,
                "abgesagt" if meeting.cancelled else None,
            )
        )
    return _compact(
        {
            "kommune": ctx.body_name,
            "zeitraum": f"{_day(von)}–{_day(bis)}",
            "gremium": [g.get_display_name() for g in gremien] if gremien else None,
            "anzahl": len(eintraege),
            "weitere": "ja" if len(sitzungen) > MAX_MEETINGS else None,
            "hinweis": hinweis,
            "spalten": "Beginn | Gremium | Ort | Link | abgesagt",
            "sitzungen": eintraege,
            "tagesordnungen": [_sitzung_details(ctx, m) for m in sitzungen[:MAX_AGENDAS]] if tagesordnungen else None,
            "hinweis_tagesordnung": (
                f"Tagesordnungen nur bei höchstens {MAX_AGENDAS} Sitzungen; sonst einzeln mit sitzung."
                if mit_tagesordnung and len(sitzungen) > MAX_AGENDAS
                else None
            ),
        }
    )


def sitzung(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    meeting = ris.public_meeting(ctx.bodies, _id_arg(args))
    if meeting is None:
        raise ToolError("Sitzung nicht gefunden.")
    return _sitzung_details(ctx, meeting)


def _sitzung_details(ctx: ToolContext, meeting: OParlMeeting) -> dict[str, Any]:
    """Sitzung mit Tagesordnung; nichtöffentliche Punkte nur mit Nummer."""
    url = ctx.link("meeting_detail", meeting.pk)
    titel = _meeting_title(meeting)
    ctx.note(url, f"{titel} ({_when(meeting.start) or 'ohne Termin'})", "meeting", detail=True)
    agenda = ris.public_agenda(meeting)
    punkte = []
    for entry in agenda[:MAX_AGENDA_ITEMS]:
        item = entry.item
        if not item.public:
            punkte.append(f"TOP {item.number or '?'}: nichtöffentlich")
            continue
        vorlagen = []
        for paper in entry.papers[:3]:
            paper_url = ctx.link("paper_detail", paper.pk)
            ctx.note(paper_url, paper.name or paper.reference or "Vorgang", "paper")
            # Der Titel der Vorlage steht nur dabei, wenn er nicht schon der des Punkts ist
            name = _short(paper.name, 120) if paper.name != item.name else None
            vorlagen.append(f"Vorlage {' '.join(filter(None, (paper.reference, name))) or '-'} {paper_url}")
        ergebnis = _short(item.result, 160)
        punkte.append(
            _line(
                f"TOP {item.number or '?'}: {_short(item.name, 160) or '-'}",
                f"Ergebnis: {ergebnis}" if ergebnis else None,
                *vorlagen,
            )
        )
    return _compact(
        {
            "titel": _short(titel, 160),
            "name": _short(meeting.name, 160) if meeting.name and meeting.name != titel else None,
            "beginn": _when(meeting.start),
            "ende": _when(meeting.end),
            "ort": _short(meeting.location_name, 120),
            "abgesagt": meeting.cancelled,
            "link": url,
            "tagesordnung": punkte,
            "weitere_punkte": len(agenda) - MAX_AGENDA_ITEMS if len(agenda) > MAX_AGENDA_ITEMS else None,
            "hinweis": None if agenda else "Keine Tagesordnung veröffentlicht.",
        }
    )


def vorgaenge_suchen(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    text = _text_arg(args, "text", required=True)
    von = _date_arg(args, "von")
    bis = _date_arg(args, "bis")
    gremien = _organizations(ctx, _text_arg(args, "gremium"))
    gefunden: dict[uuid.UUID, OParlPaper] = {}
    # Drucksachennummer genau getroffen: bei einem Treffer gleich die Einzelheiten (spart eine Runde)
    exakt = list(ris.public_papers_by_reference(ctx.bodies, text)[:MAX_PAPERS])
    if len(exakt) == 1 and not (von or bis or gremien):
        return {"suche": text, "treffer": "Drucksachennummer", "vorgang": _vorgang_details(ctx, exakt[0])}
    for paper in exakt:
        gefunden.setdefault(paper.pk, paper)
    hits = None
    # Die Volltextsuche filtert nur nach einem Gremium; passen mehrere zum Namen, sucht die Datenbank
    if not (gremien and len(gremien) > 1):
        hits = _search(
            ctx,
            "papers",
            text,
            MAX_PAPERS,
            date_from=von.isoformat() if von else None,
            date_to=bis.isoformat() if bis else None,
            organization_name=gremien[0].name if gremien else None,
        )
    if hits:
        papers = ris.public_papers_by_ids(ctx.bodies, [hit.get("id") for hit in hits])
        for hit in hits:
            pk = _as_uuid(hit.get("id"))
            if pk is not None and pk in papers:
                gefunden.setdefault(pk, papers[pk])
    else:
        # Datenbank, wenn die Volltextsuche nicht antwortet, nichts findet oder nicht filtern kann
        found = ris.search_public_papers(ctx.bodies, text, date_from=von, date_to=bis, organizations=gremien)
        for paper in found[:MAX_PAPERS]:
            gefunden.setdefault(paper.pk, paper)
    auswahl = list(gefunden.values())[:MAX_PAPERS]
    staende = _status_texts(ctx, [paper.pk for paper in auswahl])
    return _compact(
        {
            "suche": text,
            "anzahl": len(auswahl),
            "vorgaenge": [_paper_entry(ctx, paper, staende.get(paper.pk)) for paper in auswahl],
            "hinweis": None if auswahl else "Keine passenden Vorgänge gefunden.",
        }
    )


def vorgang(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    paper = ris.public_paper(ctx.bodies, _id_arg(args))
    if paper is None:
        raise ToolError("Vorgang nicht gefunden.")
    return _vorgang_details(ctx, paper)


def _vorgang_details(ctx: ToolContext, paper: OParlPaper) -> dict[str, Any]:
    """Vorgang mit Stand, Beratungsfolge (nur öffentliche Ergebnisse), Beschlusstext und Dokumentliste."""
    from insight_core.services.paper_status import paper_status

    url = ctx.link("paper_detail", paper.pk)
    ctx.note(url, paper.name or paper.reference or "Vorgang", "paper", detail=True)
    steps = ris.public_consultation_history([paper.pk]).get(paper.pk, [])
    folge = []
    for step in steps:
        rolle = _short(step.role, 60) if step.role and not step.role.startswith("http") else None
        ergebnis = _short(step.result, 200)
        folge.append(
            _line(
                _when(step.date) or "ohne Termin",
                step.organization_name or "-",
                f"TOP {step.agenda_number}" if step.agenda_number else None,
                rolle,
                "entscheidend" if step.authoritative else None,
                "abgesagt" if step.cancelled else None,
                "nichtöffentlich" if not step.public else None,
                f"Ergebnis: {ergebnis}" if ergebnis else None,
                ctx.link("meeting_detail", step.meeting_id) if step.meeting_id else None,
            )
        )
    entscheidend = [step for step in steps if step.resolution_text and step.authoritative]
    mit_beschluss = entscheidend or [step for step in steps if step.resolution_text]
    beschluss = _short(mit_beschluss[-1].resolution_text, 800) if mit_beschluss else None
    dokumente = []
    for datei in ris.public_files_of_paper(paper)[:MAX_PAPER_FILES]:
        dokumente.append({"id": str(datei.pk), "name": _short(datei.name or datei.file_name or "Dokument", 120)})
    return _compact(
        {
            "titel": _short(paper.name, 300),
            "az": paper.reference,
            "art": paper.paper_type,
            "datum": _day(paper.date),
            "link": url,
            "stand": paper_status(_status_entries(steps), ctx.now).text,
            "beratungsfolge": folge[-12:],
            "beschluss": beschluss,
            "zusammenfassung": _short(paper.summary, 600),
            "dokumente": dokumente,
        }
    )


def gremien(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    suchtext = _text_arg(args, "suchtext", limit=100)
    stichtag = timezone.localtime(ctx.now).date()
    found = list(ris.public_organizations(ctx.bodies, suchtext, on=stichtag)[: MAX_ORGANIZATIONS + 1])
    eintraege = []
    for organization in found[:MAX_ORGANIZATIONS]:
        url = ctx.link("organization_detail", organization.pk)
        name = organization.get_display_name()
        ctx.note(url, name, "organization")
        art = _short(organization.classification or organization.organization_type, 60)
        # Die Art nur, wenn der Name sie nicht schon nennt („Rat (Rat)“)
        zusatz = f" ({art})" if art and art.lower() not in name.lower() else ""
        eintraege.append(_line(f"{_short(name, 160)}{zusatz}", url))
    return _compact(
        {
            "kommune": ctx.body_name,
            "anzahl": len(eintraege),
            "weitere": "ja" if len(found) > MAX_ORGANIZATIONS else None,
            "gremien": eintraege,
            "hinweis": None if eintraege else "Keine Gremien gefunden.",
        }
    )


def personen(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    name = _text_arg(args, "name", required=True, limit=100)
    if len(name) < 2:
        raise ToolError("Bitte mindestens zwei Zeichen des Namens angeben.")
    heute = timezone.localtime(ctx.now).date()
    eintraege = []
    for person in ris.search_public_persons(ctx.bodies, name)[:MAX_PERSONS]:
        url = ctx.link("person_detail", person.pk)
        anzeige = str(person.display_name)
        ctx.note(url, anzeige, "person")
        mitgliedschaften = []
        for membership in ris.public_current_memberships(person, on=heute)[:MAX_MEMBERSHIPS]:
            org_url = ctx.link("organization_detail", membership.organization_id)
            gremium = _short(membership.organization.get_display_name(), 120)
            rolle = _short(membership.role, 60)
            mitgliedschaften.append(_line(f"{gremium} ({rolle})" if rolle else gremium, org_url))
        eintraege.append(_compact({"name": _short(anzeige, 120), "link": url, "mitgliedschaften": mitgliedschaften}))
    return _compact(
        {
            "anzahl": len(eintraege),
            "personen": eintraege,
            "hinweis": None if eintraege else "Keine Person mit diesem Namen gefunden.",
        }
    )


def _snippet(hit: Mapping[str, Any]) -> str:
    from insight_core.services.search_presentation import clean_snippet

    formatted = hit.get("_formatted")
    text = formatted.get("text_content") if isinstance(formatted, dict) else None
    if not text:
        text = hit.get("text_preview") or ""
    return _short(clean_snippet(MARK_RE.sub("", str(text))), SNIPPET_CHARS)


def dokumente_suchen(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    text = _text_arg(args, "text", required=True)
    hits = _search(ctx, "files", text, MAX_DOCUMENTS)
    if hits is None:
        raise ToolError("Die Dokumentsuche ist gerade nicht verfügbar; versuchen Sie vorgaenge_suchen.")
    dateien = ris.public_files_by_ids(ctx.bodies, [hit.get("id") for hit in hits])
    eintraege = []
    for hit in hits:
        pk = _as_uuid(hit.get("id"))
        datei = dateien.get(pk) if pk is not None else None
        if datei is None:
            continue
        paper = datei.paper
        if paper is not None:
            url = ctx.link("paper_detail", paper.pk)
            ctx.note(url, paper.name or paper.reference or "Vorgang", "paper")
        elif datei.meeting_id:
            url = ctx.link("meeting_detail", datei.meeting_id)
            ctx.note(url, datei.name or "Sitzung", "meeting")
        else:
            url = None
        eintraege.append(
            _compact(
                {
                    "id": str(datei.pk),
                    "name": _short(datei.name or datei.file_name, 120),
                    "vorgang": _short(paper.name, 120) if paper is not None else None,
                    "az": paper.reference if paper is not None else None,
                    "link": url,
                    "ausschnitt": _snippet(hit),
                }
            )
        )
    return _compact(
        {
            "suche": text,
            "anzahl": len(eintraege),
            "dokumente": eintraege,
            "hinweis": None if eintraege else "Keine passenden Dokumente gefunden.",
        }
    )


def _terms(frage: str) -> list[str]:
    """Suchwörter einer Frage (klein, ohne Füllwörter), auf sechs Zeichen gekürzt (einfache Wortstämme)."""
    words = re.findall(r"[a-zäöüß0-9]{3,}", frage.lower())
    return list(dict.fromkeys(word[:6] for word in words if word not in STOPWORDS))


def _chunks(text: str) -> list[tuple[int, str]]:
    """Text in Abschnitte von etwa ``PASSAGE_CHARS`` Zeichen, getrennt an Absätzen oder Satzenden."""
    chunks: list[tuple[int, str]] = []
    position = 0
    length = len(text)
    while position < length:
        end = min(position + PASSAGE_CHARS, length)
        if end < length:
            window = text[position:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > PASSAGE_CHARS // 2:
                end = position + cut + 1
        chunks.append((position, text[position:end]))
        position = end
    return chunks


def best_passages(text: str, frage: str) -> list[tuple[int, str]]:
    """
    Die zur Frage passenden Abschnitte eines Texts in Dokumentreihenfolge: die mit den meisten Treffern der
    Suchwörter, höchstens ``MAX_PASSAGES`` und ``MAX_PASSAGE_TOTAL`` Zeichen; ohne Treffer der Anfang.
    """
    chunks = _chunks(text)
    terms = _terms(frage)
    scored = []
    for index, (position, chunk) in enumerate(chunks):
        lower = chunk.lower()
        score = sum(lower.count(term) for term in terms)
        scored.append((score, -index, position, chunk))
    scored.sort(reverse=True)
    picked = [entry for entry in scored if entry[0] > 0][:MAX_PASSAGES] or [
        (0, -index, position, chunk) for index, (position, chunk) in enumerate(chunks[:2])
    ]
    result: list[tuple[int, str]] = []
    total = 0
    for _score, _index, position, chunk in sorted(picked, key=lambda entry: entry[2]):
        if total + len(chunk) > MAX_PASSAGE_TOTAL and result:
            break
        result.append((position, chunk))
        total += len(chunk)
    return result


def dokument_abschnitt(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    frage = _text_arg(args, "frage", required=True, limit=300)
    datei = ris.public_file_with_text(ctx.bodies, _id_arg(args))
    if datei is None:
        raise ToolError("Dokument nicht gefunden.")
    paper = datei.paper
    url = None
    if paper is not None:
        url = ctx.link("paper_detail", paper.pk)
        ctx.note(url, paper.name or paper.reference or "Vorgang", "paper", detail=True)
    elif datei.meeting_id:
        url = ctx.link("meeting_detail", datei.meeting_id)
        ctx.note(url, datei.name or "Sitzung", "meeting", detail=True)
    text = datei.text_content or ""
    kopf = {
        "name": _short(datei.name or datei.file_name, 120),
        "vorgang": _short(paper.name, 120) if paper else None,
        "link": url,
    }
    if not text.strip():
        return _compact({**kopf, "hinweis": "Für dieses Dokument liegt kein erkannter Text vor."})
    abschnitte = [
        {"ab_zeichen": position, "text": " ".join(chunk.split())} for position, chunk in best_passages(text, frage)
    ]
    return _compact({**kopf, "laenge_zeichen": len(text), "abschnitte": abschnitte})


#: Längere Ergebnisse für Tagesordnungen und Dokumentabschnitte (Zeichen)
RESULT_LIMITS = {"sitzung": 10_000, "sitzungen_im_zeitraum": 12_000, "dokument_abschnitt": MAX_PASSAGE_TOTAL + 1000}

HANDLERS: dict[str, Callable[[ToolContext, Mapping[str, Any]], dict[str, Any]]] = {
    "sitzungen_im_zeitraum": sitzungen_im_zeitraum,
    "sitzung": sitzung,
    "vorgaenge_suchen": vorgaenge_suchen,
    "vorgang": vorgang,
    "gremien": gremien,
    "personen": personen,
    "dokumente_suchen": dokumente_suchen,
    "dokument_abschnitt": dokument_abschnitt,
}


def _json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


def dump(result: Mapping[str, Any], limit: int = MAX_RESULT_CHARS) -> str:
    """
    Ergebnis als knappes JSON. Ist es länger als ``limit``, fallen hinten Einträge der längsten Liste weg
    (``weggelassen`` nennt, wie viele); das Ergebnis bleibt gültiges JSON.
    """
    text = _json(result)
    if len(text) <= limit:
        return text
    trimmed = dict(result)
    dropped: dict[str, int] = {}
    while len(text) > limit:
        lists = [(len(value), key) for key, value in trimmed.items() if isinstance(value, list) and len(value) > 1]
        if not lists:
            return _json({key: value for key, value in trimmed.items() if not isinstance(value, list | dict)})
        _length, key = max(lists)
        trimmed[key] = trimmed[key][: max(1, len(trimmed[key]) * 4 // 5)]
        dropped[key] = len(result[key]) - len(trimmed[key])
        trimmed["weggelassen"] = dict(dropped)
        text = _json(trimmed)
    return text


def run_tool(name: str, arguments: str | Mapping[str, Any] | None, ctx: ToolContext) -> str:
    """Ein Werkzeug ausführen; Ergebnis als JSON-Text für das Modell, Fehler mit festem Text."""
    ctx.calls.append(name)
    handler = HANDLERS.get(name)
    if handler is None:
        return dump({"fehler": "Unbekanntes Werkzeug."})
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except ValueError:
            return dump({"fehler": "Argumente sind kein gültiges JSON."})
    else:
        parsed = dict(arguments or {})
    if not isinstance(parsed, dict):
        return dump({"fehler": "Argumente müssen ein JSON-Objekt sein."})
    try:
        return dump(handler(ctx, parsed), RESULT_LIMITS.get(name, MAX_RESULT_CHARS))
    except ToolError as e:
        return dump({"fehler": str(e)})
    except Exception:
        logger.exception("KI-Assistent: Werkzeug %s gescheitert", name)
        return dump({"fehler": "Das Werkzeug ist gerade nicht verfügbar."})


#: Markdown-Links auf Seiten des Bürgerportals oder (Fragen an die Ratsdaten in Work, Issue #853) auf Work-Seiten
LINK_RE = re.compile(r"\]\(\s*(?:https?://[^/\s)]+)?(/(?:insight|work)/[^)\s]+)\s*\)")


def select_sources(ctx: ToolContext, answer: str, *, limit: int = 8) -> list[Source]:
    """
    Quellen-Kacheln: die Einträge aus den Werkzeugergebnissen, die die Antwort verlinkt (in ihrer Reihenfolge);
    verlinkt sie keinen, die Einträge der Detailwerkzeuge.
    """
    known = {url.removeprefix(ctx.link_base): source for url, source in ctx.sources.items()}
    chosen: list[Source] = []
    for path in LINK_RE.findall(answer or ""):
        source = known.get(path)
        if source is not None and source not in chosen:
            chosen.append(source)
    if not chosen:
        chosen = [ctx.sources[url] for url in ctx.detail_urls if url in ctx.sources]
    return chosen[:limit]
