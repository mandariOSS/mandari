# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verständliche Suchtreffer (Konzept Insight-Suche, P0.6/P0.7): Kontextzeile, Stand-Satz, saubere Ausschnitte.

Der Suchdienst liefert Gruppen (``ElasticsearchService.search_grouped``): einen Vorgang mit seinen Dokumenten,
eine Sitzung mit ihren Unterlagen oder einen einzelnen Treffer. Hier wird daraus, was die Trefferliste zeigt:

* **Kontextzeile** – Art (normalisiert: „Vorlagen“ → „Vorlage“, „Antrag an die BV …“ → „Antrag“),
  Aktenzeichen, Gremium, Datum der letzten Aktivität.
* **Stand-Satz** aus dem Beratungsverlauf (``paper_status``), für alle Vorgänge einer Seite in **einer**
  Datenbankabfrage.
* **Ausschnitt** ohne Steuer- und Symbolschriftzeichen und ohne offene Silbentrennung; gesäubert wird der
  Rohtext **vor** ``_safe_highlight``, denn das nutzt ``\\x00`` als Platzhalter für die Markierung.
* **Dokumentname** statt technischem Dateinamen („V-0169-2016-Anlage-2-Begruendung“ → „Anlage 2 – Begründung“).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Final

from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import SafeString

#: Symbolschrift-Zeichen im Privatbereich von Unicode, wie pypdf sie aus Symbol/Wingdings übernimmt
_PRIVATE_USE_MAP: Final = {
    "\uf0b7": "•",
    "\uf0a7": "•",
    "\uf0a0": "•",
    "\uf09f": "•",
    "\uf06c": "•",
    "\uf02d": "–",
    "\uf0be": "–",
    "\uf0e8": "→",
    "\uf0d8": "→",
}
_PRIVATE_USE: Final = re.compile("[\ue000-\uf8ff]")
#: C0-Steuerzeichen (ohne Tab/Zeilenumbruch, die zu Leerraum werden), DEL und das Ersatzzeichen U+FFFD
_CONTROL: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
#: Offene Silbentrennung „fol- gender“; nicht vor Bindewörtern („Geh- und Radweg“, „Ein- oder Ausfahrt“)
_HYPHENATION: Final = re.compile(r"(?<=[a-zäöüß])-\s+(?!(?:und|oder|bzw|sowie|bis|als|noch)\b)(?=[a-zäöüß])")
_SPACE: Final = re.compile(r"\s+")


def clean_snippet(text: str | None) -> str:
    """Ausschnitt aus PDF-Text für die Anzeige säubern (vor der Hervorhebung, auf dem Rohtext).

    ``\\x00`` fällt zuerst weg: ``_safe_highlight`` nutzt es als Platzhalter für ``<mark>``, Rohtext darf
    keinen vortäuschen.
    """
    if not text:
        return ""
    value = str(text).replace("\x00", "")
    value = "".join(_PRIVATE_USE_MAP.get(char, char) for char in value)
    value = _PRIVATE_USE.sub("", value)
    value = _CONTROL.sub(" ", value)
    value = _HYPHENATION.sub("", value)
    return _SPACE.sub(" ", value).strip()


# --- Art des Vorgangs ---------------------------------------------------------------------------------

#: Regeln in dieser Reihenfolge (Wortanfang, klein): erste passende gewinnt
_PAPER_TYPE_RULES: Final = (
    ("einwohnerfrage", "Einwohnerfrage"),
    ("einwohneranfrage", "Einwohnerfrage"),
    ("bürgerantrag", "Bürgerantrag"),
    ("anregung", "Anregung"),
    ("anfrage", "Anfrage"),
    ("dringlichkeitsantrag", "Antrag"),
    ("änderungsantrag", "Antrag"),
    ("antrag", "Antrag"),
    ("mitteilung", "Mitteilung"),
    ("informationsvorlage", "Mitteilung"),
    ("bericht", "Bericht"),
    ("stellungnahme", "Stellungnahme"),
    ("beschlussvorlage", "Vorlage"),
    ("vorlage", "Vorlage"),
    ("drucksache", "Vorlage"),
    ("niederschrift", "Niederschrift"),
)
#: Abweichungen je Kommune (Kurzname → Originalwert → Art); leer heißt: allgemeine Regeln
PAPER_TYPE_OVERRIDES: Final[dict[str, dict[str, str]]] = {}


def normalize_paper_type(raw: str | None, body_slug: str = "") -> str:
    """Art eines Vorgangs als kurzer, einheitlicher Begriff; Rückfall auf den Originalwert."""
    value = " ".join(str(raw or "").split())
    if not value:
        return ""
    override = PAPER_TYPE_OVERRIDES.get(body_slug, {}).get(value)
    if override:
        return override
    lowered = value.lower()
    for prefix, label in _PAPER_TYPE_RULES:
        if lowered.startswith(prefix):
            return label
    return value


# --- Dokumentname -------------------------------------------------------------------------------------

_EXTENSION: Final = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|odt|rtf|txt|zip)$", re.IGNORECASE)
#: Aktenzeichen am Anfang eines technischen Dateinamens („V-0169-2016-“, „A-R_0055_2026_“)
_REFERENCE_PREFIX: Final = re.compile(r"^[A-Za-z]{1,6}(?:[-_][A-Za-z]{1,3})?[-_/]\d{2,5}[-_/]\d{2,4}[-_ ]*")
_ATTACHMENT: Final = re.compile(r"^(Anlage|Anhang)\s*(\d+[a-z]?)\s*(.*)$", re.IGNORECASE)
#: Häufige Wörter in ASCII-Dateinamen; nur ganze Wörter, damit „Feuerwehr“ nicht zu „Feürwehr“ wird
_ASCII_WORDS: Final = {
    "Begruendung": "Begründung",
    "Uebersicht": "Übersicht",
    "Erlaeuterung": "Erläuterung",
    "Erlaeuterungen": "Erläuterungen",
    "Massnahmen": "Maßnahmen",
    "Massnahme": "Maßnahme",
    "Strasse": "Straße",
    "Aenderung": "Änderung",
    "Ergaenzung": "Ergänzung",
    "Stellungnahmen": "Stellungnahmen",
    "Gutachten": "Gutachten",
    "Plaene": "Pläne",
    "Plaen": "Plän",
    "oeffentlich": "öffentlich",
    "nichtoeffentlich": "nichtöffentlich",
}


def document_label(name: str | None, file_name: str | None = None) -> str:
    """Lesbarer Name eines Dokuments: OParl-Name, sonst bereinigter Dateiname."""
    label = " ".join(str(name or "").split()) or " ".join(str(file_name or "").split())
    if not label:
        return "Dokument"
    label = _EXTENSION.sub("", label)
    technical = " " not in label and ("-" in label or "_" in label)
    if technical:
        label = _REFERENCE_PREFIX.sub("", label)
        label = " ".join(_ASCII_WORDS.get(word, word) for word in re.split(r"[-_]+", label) if word)
    match = _ATTACHMENT.match(label)
    if match and match.group(3).strip(" -–:"):
        label = f"{match.group(1).capitalize()} {match.group(2)} – {match.group(3).strip(' -–:')}"
    return label or "Dokument"


# --- Datum, Gremium, Stand ----------------------------------------------------------------------------


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def german_date(value: Any) -> str:
    """„28.09.2016“ (Ortszeit) oder leer."""
    parsed = _date(value)
    if parsed is None:
        return ""
    return timezone.localtime(parsed).strftime("%d.%m.%Y")


def statuses_for_papers(paper_ids: Iterable[str]) -> dict[str, Any]:
    """Stand je Vorgang (``PaperStatus``) aus dem Beratungsverlauf, für alle Vorgänge in einer Abfrage.

    Wie die Vorgangsseite (``PaperDetailView``), aber ohne Zeitstrahl: Sitzung (Beginn, abgesagt,
    Gremium) und Tagesordnungspunkt (Ergebnis, öffentlich) kommen als Unterabfragen derselben Abfrage.
    """
    from django.db.models import OuterRef, Subquery

    from ..models import OParlAgendaItem, OParlConsultation, OParlMeeting, withdrawn_q
    from .paper_status import paper_status

    ids = [uuid.UUID(str(pk)) for pk in paper_ids if _is_uuid(pk)]
    if not ids:
        return {}
    meetings = OParlMeeting.objects.filter(external_id=OuterRef("meeting_external_id")).exclude(withdrawn_q())
    items = (
        OParlAgendaItem.objects.filter(external_id=OuterRef("agenda_item_external_id"))
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("meeting"))
    )
    rows = (
        OParlConsultation.objects.filter(paper_id__in=ids)
        .exclude(withdrawn_q())
        .annotate(
            sitzung_beginn=Subquery(meetings.values("start")[:1]),
            sitzung_abgesagt=Subquery(meetings.values("cancelled")[:1]),
            sitzung_name=Subquery(meetings.values("name")[:1]),
            gremium=Subquery(meetings.filter(organizations__name__gt="").values("organizations__name")[:1]),
            ergebnis=Subquery(items.values("result")[:1]),
            oeffentlich=Subquery(items.values("public")[:1]),
        )
        .values("paper_id", "sitzung_beginn", "sitzung_abgesagt", "sitzung_name", "gremium", "ergebnis", "oeffentlich")
    )
    entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        name = row["gremium"] or (row["sitzung_name"] if (row["sitzung_name"] or "").lower() != "sitzung" else None)
        entries[str(row["paper_id"])].append(
            {
                "meeting": SimpleNamespace(cancelled=bool(row["sitzung_abgesagt"])),
                "date": row["sitzung_beginn"],
                "organization_name": name,
                "result": row["ergebnis"],
                "public": row["oeffentlich"] is not False,
            }
        )
    now = timezone.now()
    return {paper_id: paper_status(paper_entries, now) for paper_id, paper_entries in entries.items()}


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


# --- Treffer der Liste --------------------------------------------------------------------------------


def _first(values: Any) -> str:
    if isinstance(values, list):
        return next((str(v) for v in values if v), "")
    return str(values or "")


def _snippet(doc: Mapping[str, Any] | None, field: str) -> SafeString | None:
    """Hervorgehobener Ausschnitt eines Felds, gesäubert und maskiert (nur ``<mark>`` bleibt HTML)."""
    from .search_service import _safe_highlight

    if not doc:
        return None
    fragment = (doc.get("_formatted") or {}).get(field)
    if not fragment:
        return None
    if fragment == doc.get(field):  # keine Hervorhebung, nur der Rohwert
        return None
    cleaned = clean_snippet(fragment)
    return _safe_highlight(cleaned) if cleaned else None


def _title(doc: Mapping[str, Any], fallback: str) -> SafeString:
    from .search_service import _safe_highlight

    formatted = (doc.get("_formatted") or {}).get("name")
    if formatted:
        return _safe_highlight(clean_snippet(formatted))
    return escape(clean_snippet(str(doc.get("name") or "")) or fallback)


def _document(doc: Mapping[str, Any]) -> dict[str, Any]:
    from .search_service import _preview_url

    return {"label": document_label(doc.get("name"), doc.get("file_name")), "url": _preview_url(doc.get("id"))}


def present_groups(groups: list[dict[str, Any]], body_slug: str = "") -> list[dict[str, Any]]:
    """Gruppen des Suchdienstes als Treffer der Liste; ein Datenbankzugriff für alle Stand-Sätze."""
    paper_ids = [str(g["paper"]["id"]) for g in groups if g.get("paper")]
    statuses = statuses_for_papers(paper_ids)
    return [_present(group, statuses, body_slug) for group in groups]


def _present(group: dict[str, Any], statuses: Mapping[str, Any], body_slug: str) -> dict[str, Any]:
    paper, meeting, main_file = group.get("paper"), group.get("meeting"), group.get("file")
    others = [_document(doc) for doc in group.get("others", [])]
    fundstelle = _document(main_file) if main_file else None
    snippet = _snippet(main_file, "text_content")

    if paper:
        status = statuses.get(str(paper.get("id")))
        last = getattr(status, "last", None)
        datum = german_date(last["date"] if last else paper.get("date"))
        if not datum and main_file:
            datum = german_date(main_file.get("meeting_date"))
        context = [
            normalize_paper_type(paper.get("paper_type"), body_slug),
            str(paper.get("reference") or ""),
            _first(paper.get("organization_names"))
            or (_first(main_file.get("organization_names")) if main_file else ""),
            datum,
        ]
        return {
            "kind": "vorgang",
            "url": f"/insight/vorgaenge/{paper.get('id')}/",
            "title": _title(paper, str(paper.get("reference") or "Vorgang")),
            "context": [part for part in context if part],
            "status": getattr(status, "text", "") or "Noch keine Beratung bekannt.",
            "status_kind": getattr(status, "kind", ""),
            "snippet": snippet or _snippet(paper, "file_contents_preview"),
            "fundstelle": fundstelle,
            "others": others,
        }

    if meeting or group.get("meeting_id"):
        meeting = meeting or {}
        gremium = _first(meeting.get("organization_names")) or (
            _first(main_file.get("organization_names")) if main_file else ""
        )
        datum = german_date(meeting.get("start") or (main_file or {}).get("meeting_date"))
        name = str(meeting.get("name") or (main_file or {}).get("meeting_name") or "Sitzung")
        title = _title(meeting, name) if meeting.get("_formatted") else escape(f"{name} am {datum}" if datum else name)
        return {
            "kind": "sitzung",
            "url": f"/insight/termine/{meeting.get('id') or group.get('meeting_id')}/",
            "title": title,
            "context": [
                part for part in ("Sitzung" if not main_file else "Sitzungsunterlagen", gremium, datum) if part
            ],
            "status": "",
            "status_kind": "",
            "snippet": snippet,
            "fundstelle": fundstelle,
            "others": others,
        }

    if main_file:
        label = document_label(main_file.get("name"), main_file.get("file_name"))
        return {
            "kind": "dokument",
            "url": fundstelle["url"] if fundstelle else "#",
            "title": escape(label),
            "context": [p for p in ("Dokument", german_date(main_file.get("meeting_date"))) if p],
            "status": "",
            "status_kind": "",
            "snippet": snippet,
            "fundstelle": None,
            "others": others,
        }

    doc = group.get("person") or group.get("organization") or {}
    if group.get("person"):
        return {
            "kind": "person",
            "url": f"/insight/personen/{doc.get('id')}/",
            "title": _title(doc, "Person"),
            "context": ["Person"],
            "status": "",
            "status_kind": "",
            "snippet": None,
            "fundstelle": None,
            "others": [],
        }
    return {
        "kind": "gremium",
        "url": f"/insight/gremien/{doc.get('id')}/",
        "title": _title(doc, "Gremium"),
        "context": [p for p in ("Gremium", str(doc.get("organization_type") or "")) if p],
        "status": "",
        "status_kind": "",
        "snippet": None,
        "fundstelle": None,
        "others": [],
    }


def count_sentence(counts: Mapping[str, Any]) -> str:
    """Ehrliche Zahl: „79 Vorgänge und 15 Sitzungsunterlagen“ statt „206 Ergebnisse“."""
    teile: list[str] = []
    rund = "rund " if counts.get("approx") else ""
    for key, one, many in (
        ("vorgaenge", "Vorgang", "Vorgänge"),
        ("unterlagen", "Sitzungsunterlage", "Sitzungsunterlagen"),
        ("meetings", "Sitzung", "Sitzungen"),
    ):
        n = int(counts.get(key) or 0)
        if n:
            teile.append(f"{rund if key != 'meetings' else ''}{n:,} {one if n == 1 else many}".replace(",", "."))
    if not teile:
        return ""
    if len(teile) == 1:
        return teile[0]
    return ", ".join(teile[:-1]) + " und " + teile[-1]
