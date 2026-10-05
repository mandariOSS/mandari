# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stand eines Vorgangs in einem Satz und der Beratungsverlauf als Zeitstrahl (Vorgangsdetail im Bürgerportal).

Wer über eine Suchmaschine auf einen Vorgang kommt, will zuerst wissen, wo die Sache steht. Der Satz fasst
die letzte Beratung zusammen: „Am 06.12.2011 in der Bezirksvertretung Mitte zur Kenntnis
genommen.“ Grundlage ist der Beratungsverlauf, wie ihn ``PaperDetailView`` aufbereitet (je Beratung ein
Eintrag mit ``date``, ``meeting``, ``organization_name``, ``agenda_number``, ``result``, ``public``,
``role`` und ``authoritative``, optional ``organization_count``).

Fälle:

- **beraten mit Ergebnis**: Datum, Gremium und Ergebnis der letzten Beratung.
- **vertagt** (auch zurückgestellt, abgesetzt, verschoben): dazu der nächste Termin oder der Hinweis,
  dass keiner bekannt ist.
- **ohne Ergebnis**: beraten, aber das Ratsinformationssystem nennt kein Ergebnis.
- **noch nicht beraten**: erste Beratung angesetzt, oder es ist gar keine Beratung bekannt.

Abgesagte Sitzungen zählen nicht als Beratung. Liegt nach der letzten Beratung eine weitere an
(etwa erst der Ausschuss, dann der Rat), nennt der Satz sie zusätzlich.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from django.utils import timezone

#: Art des Stands (für Tests und Darstellung)
DECIDED: Final = "decided"
DEFERRED: Final = "deferred"
NO_RESULT: Final = "no_result"
SCHEDULED: Final = "scheduled"
NONE: Final = "none"

#: Zustände der Einträge im Zeitstrahl
PAST: Final = "past"
LATEST: Final = "latest"
UPCOMING: Final = "upcoming"
CANCELLED: Final = "cancelled"
UNDATED: Final = "undated"

#: Ergebnisse, nach denen die Sache erneut beraten werden muss (Wortfolgen, klein geschrieben)
_DEFERRED_PHRASES: Final = (("vertagt",), ("zurückgestellt",), ("abgesetzt",), ("verschoben",), ("nicht", "behandelt"))

#: Trenner zwischen Teilen eines Ergebnisses („1. Lesung – vertagt“, „vertagt, neuer Termin offen“)
_RESULT_PARTS: Final = re.compile(r"\s[-–]\s|[:;,()]")

#: Längstes Ergebnis im Stand-Satz (Zeichen); der Zeitstrahl zeigt es ungekürzt
_RESULT_MAX_CHARS: Final = 120

#: So lange nach der Sitzung heißt ein fehlendes Ergebnis „noch nicht veröffentlicht“
_RESULT_PENDING_DAYS: Final = 60

#: Wörter, mit denen ein Ergebnis als Partizip-Satz beginnen darf („einstimmig beschlossen“)
_LEADING_WORDS: Final = frozenset(
    {
        "abgeändert",
        "als",
        "an",
        "auf",
        "bei",
        "durch",
        "einstimmig",
        "einvernehmlich",
        "geändert",
        "in",
        "mehrheitlich",
        "mit",
        "nicht",
        "ohne",
        "so",
        "ungeändert",
        "von",
        "wie",
        "zur",
    }
)

#: Ergebnisse aus einem Wort, die großgeschrieben im RIS stehen, aber Partizipien sind
_PARTICIPLES: Final = frozenset(
    {
        "abgelehnt",
        "abgesetzt",
        "angenommen",
        "beraten",
        "beschlossen",
        "bestätigt",
        "empfohlen",
        "erledigt",
        "genehmigt",
        "gewählt",
        "verschoben",
        "vertagt",
        "verwiesen",
        "zugestimmt",
        "zurückgestellt",
        "zurückgezogen",
        "überwiesen",
    }
)

#: Gremien, die mit „im“ stehen (Wortende des Kopfworts), und solche mit „in der“
_MASCULINE_OR_NEUTER: Final = ("rat", "ausschuss", "tag", "vorstand", "kreis", "um", "ment", "senat", "konvent")
_FEMININE: Final = ("ung", "ion", "schaft", "runde", "gruppe", "kammer", "konferenz")
_ARTICLES: Final = frozenset({"der", "die", "das", "den", "dem"})
_JOINERS: Final = frozenset({"und", "sowie", "&"})


@dataclass(frozen=True)
class PaperStatus:
    """Stand eines Vorgangs: ein Satz, dazu die zugrunde liegenden Einträge des Beratungsverlaufs."""

    kind: str
    text: str
    last: Mapping[str, Any] | None = None
    upcoming: Mapping[str, Any] | None = None


def _local_date(value: datetime) -> str:
    return timezone.localtime(value).strftime("%d.%m.%Y") if timezone.is_aware(value) else value.strftime("%d.%m.%Y")


def _is_cancelled(entry: Mapping[str, Any]) -> bool:
    meeting = entry.get("meeting")
    return bool(getattr(meeting, "cancelled", False))


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split()).rstrip(".").strip()


def _head_noun(name: str) -> str | None:
    """Kopfwort eines Gremiennamens: „Haupt- und Finanzausschuss“ → „Finanzausschuss“."""
    for token in name.split():
        word = token.strip(",;:()")
        if not word or word.endswith("-") or word.lower() in _JOINERS:
            continue
        return word
    return None


def in_committee(name: str | None, count: int | None = None) -> str:
    """Ortsangabe für ein Gremium: „im Rat der Stadt“, „in der Bezirksvertretung Mitte“.

    Unbekanntes Geschlecht, Abkürzungen und mehrere Gremien stehen neutral: „im Gremium „BV Süd““.
    ``count`` ist die Anzahl der Gremien hinter dem Namen (mehrere stehen mit Komma verbunden). Ist sie
    nicht bekannt, gilt ein Name mit Komma als mehrere Gremien; bei genau einem Gremium wird auch ein Name
    mit Komma gebeugt („im Ausschuss für Planung, Bau und Umwelt“).
    Ohne verwertbaren Namen (nur „Sitzung“) gibt es keine Ortsangabe.
    """
    name = " ".join(str(name or "").split())
    if not name or name.lower() == "sitzung":
        return ""
    several = count > 1 if count else "," in name
    head = None if several else _head_noun(name)
    if head and head.lower() not in _ARTICLES and head[:1].isupper():
        lowered = head.lower()
        if lowered.endswith(_FEMININE):
            return f"in der {name}"
        if lowered.endswith(_MASCULINE_OR_NEUTER):
            return f"im {name}"
    return f"im Gremium „{name}“"


def result_as_participle(result: str | None) -> str | None:
    """Ergebnis als Satzende („zur Kenntnis genommen“) oder ``None``, wenn es so nicht passt.

    Partizipien und kurze Wendungen wie „einstimmig beschlossen“ schließen den Satz, Substantive
    („Kenntnisnahme“) und längere Texte stehen als „Ergebnis: …“.
    """
    text = _clean(result)
    words = text.split()
    if not words or len(words) > 6:
        return None
    first, last = words[0], words[-1]
    single = len(words) == 1
    if single:
        if not (first[:1].islower() or first.lower() in _PARTICIPLES):
            return None
    elif not (last[:1].islower() and (first[:1].islower() or first.lower() in _LEADING_WORDS)):
        return None
    if not last.lower().endswith(("t", "en")):
        return None
    return first[:1].lower() + text[1:]


def _is_deferred(result: str | None) -> bool:
    """Vertagt, wenn ein Teil des Ergebnisses mit der Wendung beginnt oder als kurzer Partizip-Satz auf ihr endet.

    „Vertagt in die nächste Sitzung“, „einstimmig vertagt“ und „1. Lesung – vertagt“ zählen, ein
    Beschlussinhalt wie „Maßnahme auf 2027 verschoben“ nicht.
    """
    for part in _RESULT_PARTS.split(_clean(result)):
        words = tuple(word for word in (token.strip(".!?\"'„“‚‘").lower() for token in part.split()) if word)
        for phrase in _DEFERRED_PHRASES:
            if words[: len(phrase)] == phrase:
                return True
            if words[-len(phrase) :] == phrase and result_as_participle(part) is not None:
                return True
    return False


#: Ergebnisse ohne Entscheidung in der Sache (Wortanfänge, klein geschrieben): Kenntnisnahme, Antwort, Rücknahme
_KEINE_ENTSCHEIDUNG: Final = ("kenntnis", "beantwortet", "zurückgezogen", "erledigt durch")


def ist_beschluss(result: str | None) -> bool:
    """
    Ist das Ergebnis eine Entscheidung in der Sache (Liste „Zuletzt beschlossen“, Issue #841)?

    Nein bei leeren, vertagten oder abgesetzten Ergebnissen und bei „zur Kenntnis genommen“, „Kenntnisnahme“,
    „beantwortet“ und „zurückgezogen“.
    """
    text = _clean(result).lower()
    if not text or _is_deferred(result):
        return False
    return not any(wort in text for wort in _KEINE_ENTSCHEIDUNG)


def _shorten(text: str, limit: int = _RESULT_MAX_CHARS) -> str:
    """Auf höchstens ``limit`` Zeichen an einer Wortgrenze kürzen, mit „…“ am Ende."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-–") + "…"


def _next_sentence(upcoming: Mapping[str, Any] | None, *, first: bool = False) -> str:
    if upcoming is None:
        return ""
    where = in_committee(upcoming.get("organization_name"), upcoming.get("organization_count"))
    label = "Erste Beratung" if first else "Nächste Beratung"
    return f"{label} am {_local_date(upcoming['date'])}{' ' + where if where else ''}."


def _dated(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [entry for entry in entries if entry.get("date") and not _is_cancelled(entry)]


def paper_status(entries: Sequence[Mapping[str, Any]], now: datetime | None = None) -> PaperStatus:
    """Stand des Vorgangs aus dem Beratungsverlauf (Einträge in beliebiger Reihenfolge)."""
    now = now or timezone.now()
    dated = _dated(entries)
    past = [entry for entry in dated if entry["date"] <= now]
    future = [entry for entry in dated if entry["date"] > now]
    upcoming = min(future, key=lambda entry: entry["date"]) if future else None

    if not past:
        if upcoming is not None:
            return PaperStatus(SCHEDULED, f"Noch nicht beraten. {_next_sentence(upcoming, first=True)}", None, upcoming)
        if entries:
            return PaperStatus(NONE, "Noch keine Beratung mit Termin bekannt.")
        return PaperStatus(NONE, "Noch keine Beratung bekannt.")

    last = max(past, key=lambda entry: entry["date"])
    when = f"Am {_local_date(last['date'])}"
    where = in_committee(last.get("organization_name"), last.get("organization_count"))
    prefix = f"{when} {where}" if where else when
    result = _clean(last.get("result"))
    public = last.get("public", True) is not False
    follow = _next_sentence(upcoming)

    if not result:
        if not public:
            text = f"{prefix} nichtöffentlich beraten."
        elif now - last["date"] <= timedelta(days=_RESULT_PENDING_DAYS):
            text = f"{prefix} beraten. Das Ergebnis ist noch nicht veröffentlicht."
        else:
            text = f"{prefix} beraten. Ein Ergebnis ist nicht angegeben."
        return PaperStatus(NO_RESULT, _join(text, follow), last, upcoming)

    participle = result_as_participle(result)
    beraten = "nichtöffentlich beraten" if not public else "beraten"
    if participle:
        text = f"{prefix} {participle}."
    else:
        # Lange Beschlusstexte gekürzt; ganz stehen sie im Zeitstrahl
        shown = _shorten(result)
        text = f"{prefix} {beraten}. Ergebnis: {shown}{'' if shown.endswith('…') else '.'}"
    if _is_deferred(result):
        return PaperStatus(DEFERRED, _join(text, follow or "Ein neuer Termin ist nicht bekannt."), last, upcoming)
    return PaperStatus(DECIDED, _join(text, follow), last, upcoming)


def _join(*sentences: str) -> str:
    return " ".join(sentence for sentence in sentences if sentence)


def _detail(entry: Mapping[str, Any]) -> str:
    """Zweite Zeile im Zeitstrahl: „TOP 5.1, Entscheidung, nichtöffentlich“."""
    parts: list[str] = []
    if entry.get("agenda_number"):
        parts.append(f"TOP {entry['agenda_number']}")
    role = _clean(entry.get("role"))
    if role and not re.match(r"^https?://", role):
        parts.append(role)
    elif entry.get("authoritative"):
        parts.append("Entscheidung")
    if entry.get("public", True) is False:
        parts.append("nichtöffentlich")
    return ", ".join(parts)


def timeline(
    entries: Sequence[Mapping[str, Any]], status: PaperStatus, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Einträge des Beratungsverlaufs mit Zustand (``state``) und Detailzeile (``detail``) für den Zeitstrahl.

    Reihenfolge wie übergeben (chronologisch); „zuletzt“ trägt nur die Beratung, auf der der Stand beruht,
    und nur, wenn es mehr als einen Eintrag gibt.
    """
    now = now or timezone.now()
    result: list[dict[str, Any]] = []
    for entry in entries:
        if _is_cancelled(entry):
            state = CANCELLED
        elif not entry.get("date"):
            state = UNDATED
        elif entry is status.last and len(entries) > 1:
            state = LATEST
        elif entry["date"] > now:
            state = UPCOMING
        else:
            state = PAST
        result.append({**entry, "state": state, "detail": _detail(entry)})
    return result
