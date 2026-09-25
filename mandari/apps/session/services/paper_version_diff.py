# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vergleich zweier Fassungen einer Vorlage (Issue #226).

- Texte wortgenau: zuerst absatzweise, innerhalb geänderter Absätze Wort für Wort
  (``difflib``). Jedes Textstück wird HTML-escaped, bevor es in ``<del>``/``<ins>`` steht –
  die Ausgabe enthält nie Markup aus dem Vorlagentext.
- Angaben (Art, Datum, Gremien …) als Gegenüberstellung alt/neu.
- Anlagen über Metadaten: hinzugefügt, entfernt, ersetzt (andere Prüfsumme), umbenannt,
  Ö/NÖ geändert. Zugeordnet wird über die Kennung der Anlage, ersatzweise über den Inhalt.
  Ein seitenweiser PDF-Vergleich ist nicht Teil dieses Vergleichs.

Sichtbarkeit: Der Aufrufer übergibt nur Einträge, die der Nutzer sehen darf – nicht sichtbare
Anlagen tauchen auf keiner Seite auf und werden auch nicht gezählt.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.utils.html import escape, format_html
from django.utils.safestring import SafeString, mark_safe

from apps.session.models import SessionPaper, SessionPaperVersion, SessionPaperVersionFile, human_size

_TOKEN_RE = re.compile(r"\s+|[^\s]+")
#: Oberhalb dieser Größe (Wörter alt × Wörter neu) wird ein Absatzblock als Ganzes ersetzt
_WORD_DIFF_LIMIT = 4_000_000

TEXT_FIELDS = (
    ("name", "Betreff"),
    ("main_text", "Sachverhalt"),
    ("resolution_text", "Beschlussvorschlag"),
    ("financial_impact_note", "Erläuterung der finanziellen Auswirkungen"),
)
DETAIL_LABELS = (
    ("main_organization", "Federführendes Gremium"),
    ("lead_department", "Federführendes Amt"),
    ("originator_organization", "Einreichendes Gremium"),
    ("originator_person", "Einreichende Person"),
)


# =============================================================================
# Wortgenauer Textvergleich
# =============================================================================


def _normalize(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _deleted(text: str) -> str:
    return str(format_html("<del>{}</del>", text)) if text else ""


def _inserted(text: str) -> str:
    return str(format_html("<ins>{}</ins>", text)) if text else ""


def _word_parts(old: str, new: str) -> list[str]:
    old_tokens = _TOKEN_RE.findall(old)
    new_tokens = _TOKEN_RE.findall(new)
    if len(old_tokens) * len(new_tokens) > _WORD_DIFF_LIMIT:
        return [_deleted(old), _inserted(new)]
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    # (gleich?, alt, neu) je Abschnitt
    chunks = [
        (tag == "equal", "".join(old_tokens[i1:i2]), "".join(new_tokens[j1:j2]))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    ]
    # Reiner Leerraum zwischen zwei Änderungen zählt zur Änderung – sonst zerfiele ein
    # umgeschriebener Satz in einzelne Wörter mit „gleichen“ Leerzeichen dazwischen
    merged: list[tuple[bool, str, str]] = []
    for index, (equal, before, after) in enumerate(chunks):
        between_changes = 0 < index < len(chunks) - 1 and not chunks[index - 1][0] and not chunks[index + 1][0]
        if equal and between_changes and not before.strip():
            equal = False
        if not equal and merged and not merged[-1][0]:
            merged[-1] = (False, merged[-1][1] + before, merged[-1][2] + after)
        else:
            merged.append((equal, before, after))
    parts: list[str] = []
    for equal, before, after in merged:
        if equal:
            parts.append(str(escape(before)))
        else:
            parts.append(_deleted(before))
            parts.append(_inserted(after))
    return parts


def word_diff(old: str | None, new: str | None) -> SafeString:
    """Wortgenauer Vergleich als HTML mit ``<del>``/``<ins>``; jedes Textstück ist escaped."""
    old_lines = _normalize(old).splitlines(keepends=True)
    new_lines = _normalize(new).splitlines(keepends=True)
    parts: list[str] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        before = "".join(old_lines[i1:i2])
        after = "".join(new_lines[j1:j2])
        if tag == "equal":
            parts.append(str(escape(before)))
        elif tag == "replace":
            parts.extend(_word_parts(before, after))
        else:
            parts.append(_deleted(before))
            parts.append(_inserted(after))
    # Alle Teile sind oben escaped bzw. über format_html entstanden
    return mark_safe("".join(parts))


# =============================================================================
# Vergleich zweier Fassungen
# =============================================================================


@dataclass
class TextComparison:
    label: str
    html: SafeString
    changed: bool
    empty: bool


@dataclass
class FieldChange:
    label: str
    old: str
    new: str


@dataclass
class FileComparison:
    """Eine Anlage im Vergleich; ``changes`` nennt, was sich geändert hat (leer = unverändert)."""

    old: SessionPaperVersionFile | None
    new: SessionPaperVersionFile | None
    changes: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.old is None:
            return "added"
        if self.new is None:
            return "removed"
        return "changed" if self.changes else "unchanged"

    @property
    def name(self) -> str:
        entry = self.new or self.old
        return entry.name if entry is not None else ""


@dataclass
class Comparison:
    older: SessionPaperVersion
    newer: SessionPaperVersion
    texts: list[TextComparison]
    fields: list[FieldChange]
    files: list[FileComparison]

    @property
    def has_changes(self) -> bool:
        return (
            any(text.changed for text in self.texts)
            or bool(self.fields)
            or any(item.kind != "unchanged" for item in self.files)
        )


def _yes_no(value: Any) -> str:
    if value is None:
        return "nicht erfasst"
    return "Ja" if value else "Nein"


def _date(value: Any) -> str:
    return value.strftime("%d.%m.%Y") if value else "–"


def _detail(version: SessionPaperVersion, key: str) -> str:
    value = (version.details or {}).get(key)
    return str(value.get("name", "")) if isinstance(value, dict) else "–"


def _field_changes(older: SessionPaperVersion, newer: SessionPaperVersion) -> list[FieldChange]:
    types = dict(SessionPaper._meta.get_field("paper_type").choices or [])
    statuses = dict(SessionPaper._meta.get_field("status").choices or [])
    pairs = [
        ("Vorlagennummer", older.reference or "–", newer.reference or "–"),
        (
            "Vorlagenart",
            str(types.get(older.paper_type, older.paper_type)),
            str(types.get(newer.paper_type, newer.paper_type)),
        ),
        ("Status", str(statuses.get(older.status, older.status)), str(statuses.get(newer.status, newer.status))),
        ("Öffentlich", _yes_no(older.is_public), _yes_no(newer.is_public)),
        ("Datum", _date(older.date), _date(newer.date)),
        ("Frist", _date(older.deadline), _date(newer.deadline)),
        ("Finanzielle Auswirkungen", _yes_no(older.has_financial_impact), _yes_no(newer.has_financial_impact)),
        *[(label, _detail(older, key), _detail(newer, key)) for key, label in DETAIL_LABELS],
    ]
    return [FieldChange(label, old, new) for label, old, new in pairs if old != new]


def _file_changes(old: SessionPaperVersionFile, new: SessionPaperVersionFile) -> list[str]:
    changes = []
    if old.sha256 and new.sha256:
        replaced = old.sha256 != new.sha256
    else:  # Inhalt beim Sichern nicht lesbar: ersatzweise über die Fassungsnummer der Anlage
        replaced = old.file_version_number != new.file_version_number
    if replaced:
        changes.append(
            f"ersetzt ({human_size(old.size)} → {human_size(new.size)}, "
            f"Prüfsumme {old.sha256[:12] or '–'} → {new.sha256[:12] or '–'})"
        )
    if old.name != new.name:
        changes.append(f"umbenannt (vorher „{old.name}“)")
    if old.is_public != new.is_public:
        changes.append("jetzt öffentlich" if new.is_public else "jetzt nichtöffentlich")
    return changes


def compare_files(
    old_entries: Iterable[SessionPaperVersionFile], new_entries: Iterable[SessionPaperVersionFile]
) -> list[FileComparison]:
    """Anlagen zuordnen – über die Kennung der Anlage, übrig gebliebene über gleichen Inhalt."""
    old_list = list(old_entries)
    new_list = list(new_entries)
    old_by_id = {entry.attachment_id: entry for entry in old_list}
    matched_old: set[Any] = set()
    result: list[FileComparison] = []
    unmatched_new: list[SessionPaperVersionFile] = []
    for entry in new_list:
        partner = old_by_id.get(entry.attachment_id)
        if partner is None:
            unmatched_new.append(entry)
            continue
        matched_old.add(partner.pk)
        result.append(FileComparison(old=partner, new=entry, changes=_file_changes(partner, entry)))
    leftover_old = [entry for entry in old_list if entry.pk not in matched_old]
    for entry in unmatched_new:
        partner = next((o for o in leftover_old if o.sha256 and o.sha256 == entry.sha256), None)
        if partner is None:
            result.append(FileComparison(old=None, new=entry, changes=["hinzugefügt"]))
            continue
        leftover_old.remove(partner)
        result.append(FileComparison(old=partner, new=entry, changes=_file_changes(partner, entry)))
    result.extend(FileComparison(old=entry, new=None, changes=["entfernt"]) for entry in leftover_old)
    order = {"removed": 0, "added": 1, "changed": 2, "unchanged": 3}
    result.sort(key=lambda item: (order[item.kind], item.name.lower()))
    return result


def compare(
    older: SessionPaperVersion,
    newer: SessionPaperVersion,
    *,
    older_entries: Iterable[SessionPaperVersionFile],
    newer_entries: Iterable[SessionPaperVersionFile],
) -> Comparison:
    """Zwei Fassungen vergleichen; die Einträge sind bereits auf das Sichtbare gefiltert."""
    texts = []
    for name, label in TEXT_FIELDS:
        before = _normalize(getattr(older, name))
        after = _normalize(getattr(newer, name))
        texts.append(
            TextComparison(
                label=label,
                html=word_diff(before, after),
                changed=before != after,
                empty=not before and not after,
            )
        )
    return Comparison(
        older=older,
        newer=newer,
        texts=texts,
        fields=_field_changes(older, newer),
        files=compare_files(older_entries, newer_entries),
    )
