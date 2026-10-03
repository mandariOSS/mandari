# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Landesrecht als Sitzungsrecht (Issue #757): Fassungen mit Stichtag und Ortsrecht der Körperschaft.

**Fassungen.** Ein Landesprofil (``SessionStateProfile``) beschreibt hybride und digitale Sitzungen. Was darüber
hinaus den Sitzungsdienst bestimmt – Öffentlichkeit, Einberufung, Beschlussfähigkeit, Abstimmungen und Wahlen,
Niederschrift, Ausschussbesetzung, Ämter –, steht in Fassungen (``SessionStateProfileVersion``) mit Stichtag.
Maßgeblich ist die Fassung zum Sitzungsdatum (:func:`effective`): Spätere Fassungen erben die Einträge früherer
und überschreiben nur, was sich ändert. In Niedersachsen gilt so für eine Sitzung am 31.10.2026 die Fassung ab
07.05.2026 und für eine am 01.11.2026 die Fassung ab 01.11.2026 (Sainte-Laguë/Schepers, Sitzungsleitung,
Öffentlichkeit per Video). Vor der ersten Fassung gilt das Landesprofil ohne Sitzungsrecht.

Jeder Eintrag nennt Norm, Quelle und Stand; „ungeklärt“ (``unclear``) heißt, die Quelle belegt nichts sicher.
Der Katalog der Einträge ist :data:`LAW_FIELDS`; Werte mit fester Bedeutung (z. B. ``tie_vote``) prüft der
Code, reine Erläuterungen erscheinen in der Rechtsübersicht.

**Ortsrecht.** Hauptsatzung und Geschäftsordnung gelten je Körperschaft (``SessionBody.local_rules``,
:class:`LocalRules`): Zuschaltung je Ladung, Zuschaltung nur in öffentlichen Sitzungen, Nachweise für Bild- und
Tonaufnahmen und die Öffentlichkeit per Video, Notlagenbeschluss mit Ablauf und die Parameter der
Geschäftsordnung (Fristen, Fristbeginn, Unterzeichnende, Abstimmungsarten, Öffentlichkeit der Ausschüsse).

Keine Rechtsberatung – die Einträge geben den recherchierten Stand wieder (docs/SESSION_SITZUNGSFORMAT_LANDESRECHT.md).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

# =============================================================================
# Katalog des Sitzungsrechts
# =============================================================================

KIND_TEXT = "text"
KIND_CHOICE = "choice"
KIND_INT = "int"
KIND_BOOL = "bool"
KIND_LIST = "list"
KIND_MAP = "map"

UNCLEAR = "unclear"


@dataclass(frozen=True)
class LawField:
    """Eintrag des Sitzungsrechts: Gruppe, Bezeichnung und Art des Werts."""

    key: str
    group: str
    label: str
    kind: str = KIND_TEXT
    choices: tuple[tuple[str, str], ...] = ()


GROUPS: tuple[tuple[str, str], ...] = (
    ("bodies", "Körperschaften und Gremien"),
    ("publicity", "Öffentlichkeit"),
    ("convocation", "Einberufung und Tagesordnung"),
    ("quorum", "Beschlussfähigkeit"),
    ("voting", "Abstimmungen und Wahlen"),
    ("bias", "Mitwirkungsverbot"),
    ("minutes", "Niederschrift"),
    ("rights", "Antragsrecht und Anfragen"),
    ("urgent", "Eilentscheidung und Einspruch"),
    ("term", "Wahlperiode"),
    ("committees", "Ausschussbesetzung"),
    ("offices", "Ämter und Funktionen"),
    ("remote", "Hybride und digitale Sitzungen"),
    ("media", "Aufnahmen und Übertragung"),
)

_LOCAL_BASIS_CHOICES = (
    ("local_basis", "zulässig, soweit die Hauptsatzung es bestimmt"),
    ("none", "nicht vorgesehen"),
    (UNCLEAR, "ungeklärt"),
)

LAW_FIELDS: dict[str, LawField] = {
    f.key: f
    for f in (
        LawField("body_types", "bodies", "Körperschaftstypen"),
        LawField("designations", "bodies", "Gesetzliche Gremienbezeichnungen", KIND_MAP),
        LawField("local_councils", "bodies", "Ortsräte und Stadtbezirksräte"),
        LawField("youth_body", "bodies", "Jugendbeteiligungsgremium"),
        LawField("publicity", "publicity", "Öffentlichkeit der Sitzungen"),
        LawField("non_public_committee_kinds", "publicity", "Stets nichtöffentliche Ausschüsse", KIND_LIST),
        LawField(
            "main_committee_chair",
            "publicity",
            "Vorsitz im Hauptausschuss",
            KIND_CHOICE,
            (
                ("hvb", "die Hauptverwaltungsbeamtin bzw. der Hauptverwaltungsbeamte"),
                ("elected", "gewählt"),
                (UNCLEAR, "ungeklärt"),
            ),
        ),
        LawField(
            "residents_questions",
            "publicity",
            "Einwohnerfragestunde",
            KIND_CHOICE,
            (("all", "Einwohnerinnen und Einwohner"), ("present_only", "nur anwesende Einwohnerinnen und Einwohner")),
        ),
        LawField("convocation", "convocation", "Einberufung"),
        LawField("first_meeting", "convocation", "Erste Sitzung der Wahlperiode"),
        LawField("agenda", "convocation", "Tagesordnung"),
        LawField("quorum", "quorum", "Beschlussfähigkeit"),
        LawField("majority", "voting", "Mehrheit"),
        LawField(
            "tie_vote",
            "voting",
            "Stimmengleichheit",
            KIND_CHOICE,
            (("rejected", "Antrag abgelehnt"), (UNCLEAR, "ungeklärt")),
        ),
        LawField(
            "abstentions",
            "voting",
            "Enthaltungen",
            KIND_CHOICE,
            (("not_counted", "zählen nicht mit"), ("counted", "zählen mit"), (UNCLEAR, "ungeklärt")),
        ),
        LawField("voting_methods", "voting", "Abstimmungsarten"),
        LawField("elections", "voting", "Wahlen"),
        LawField("bias", "bias", "Mitwirkungsverbot"),
        LawField("minutes", "minutes", "Niederschrift"),
        LawField("motions", "rights", "Antragsrecht, Anfragen und Akteneinsicht"),
        LawField("urgent_decisions", "urgent", "Eilentscheidung"),
        LawField("objection", "urgent", "Einspruch"),
        LawField("term_years", "term", "Dauer der Wahlperiode (Jahre)", KIND_INT),
        LawField("term", "term", "Beginn der Wahlperiode"),
        LawField(
            "seat_allocation",
            "committees",
            "Verteilung der Ausschusssitze",
            KIND_CHOICE,
            (
                ("dhondt", "Höchstzahlverfahren nach d’Hondt"),
                ("sainte_lague_schepers", "Divisorverfahren nach Sainte-Laguë/Schepers"),
                ("hare_niemeyer", "Quotenverfahren nach Hare/Niemeyer"),
                (UNCLEAR, "ungeklärt"),
            ),
        ),
        LawField("committee_seats", "committees", "Ausschussbesetzung"),
        LawField("chair_recall_bar", "committees", "Abberufener Ausschussvorsitz nicht erneut benennbar", KIND_BOOL),
        LawField("hvb", "offices", "Hauptverwaltungsbeamtin bzw. Hauptverwaltungsbeamter"),
        LawField("hvb_deputies_max", "offices", "Ehrenamtliche Stellvertretungen des HVB (höchstens)", KIND_INT),
        LawField("adult_offices", "offices", "Ämter erst ab 18 Jahren", KIND_LIST),
        LawField("remote_basis_hint", "remote", "Nachweis der Hauptsatzungsregel"),
        LawField("remote_per_invitation", "remote", "Zulassung der Zuschaltung je Ladung möglich", KIND_BOOL),
        LawField("remote_public_only", "remote", "Beschränkung auf öffentliche Sitzungen möglich", KIND_BOOL),
        LawField(
            "session_lead",
            "remote",
            "Im Sitzungsraum anwesend sein muss",
            KIND_CHOICE,
            (("chair", "der Vorsitz"), ("session_lead", "die Sitzungsleitung (auch eine leitende Stellvertretung)")),
        ),
        LawField("committee_deviation", "remote", "Örtliche Abweichung für Ausschüsse möglich", KIND_BOOL),
        LawField("remote_non_public_notice", "remote", "Hinweis an Zugeschaltete in nichtöffentlichen Sitzungen"),
        LawField(
            "disruption",
            "remote",
            "Störung der Übertragung",
            KIND_CHOICE,
            (("interrupt", "im Verantwortungsbereich der Kommune: Sitzung unterbrechen"), (UNCLEAR, "ungeklärt")),
        ),
        LawField("video_hearings", "remote", "Anhörungen per Video"),
        LawField("emergency_resolution", "remote", "Notlage: Beschluss der Vertretung mit Ablauf", KIND_BOOL),
        LawField("emergency_max_months", "remote", "Notlagenbeschluss gilt höchstens (Monate)", KIND_INT),
        LawField("recording", "media", "Bild- und Tonaufnahmen", KIND_CHOICE, _LOCAL_BASIS_CHOICES),
        LawField("recording_objection", "media", "Widerspruch je Abgeordnetem", KIND_BOOL),
        LawField("video_public", "media", "Öffentlichkeit per Video", KIND_CHOICE, _LOCAL_BASIS_CHOICES),
    )
}


class LawDataError(ValueError):
    """Profildatei mit ungültigem Sitzungsrecht; die Meldung nennt Land, Fassung und Eintrag."""


def _check_value(spec: LawField, value: Any, where: str) -> None:
    if spec.kind == KIND_TEXT:
        if value is not None:
            raise LawDataError(f"{where}: reiner Texteintrag ohne Wert erwartet")
        return
    if value is None:
        raise LawDataError(f"{where}: Wert fehlt")
    if spec.kind == KIND_CHOICE and value not in {key for key, _ in spec.choices}:
        raise LawDataError(f"{where}: unbekannter Wert {value!r}")
    if spec.kind == KIND_INT and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise LawDataError(f"{where}: ganze Zahl erwartet")
    if spec.kind == KIND_BOOL and not isinstance(value, bool):
        raise LawDataError(f"{where}: Wahrheitswert erwartet")
    if spec.kind == KIND_LIST and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
        raise LawDataError(f"{where}: Liste von Texten erwartet")
    if spec.kind == KIND_MAP and not isinstance(value, dict):
        raise LawDataError(f"{where}: Objekt erwartet")


def normalize_version(code: str, raw: dict[str, Any], *, profile_fields: set[str]) -> dict[str, Any]:
    """
    Fassung aus der Profildatei prüfen und in Feldwerte für ``SessionStateProfileVersion`` übersetzen.

    Je Eintrag: ``value`` (außer bei reinen Erläuterungen), ``text``, ``norm`` (Pflicht, außer „ungeklärt“),
    ``source`` (Index in die Quellen der Fassung, sonst die erste), ``as_of`` (sonst Stand der Fassung).
    """
    valid_from = date.fromisoformat(raw["valid_from"])
    as_of = date.fromisoformat(raw["as_of"])
    sources = list(raw.get("sources") or [])
    where_version = f"{code}, Fassung ab {valid_from:%d.%m.%Y}"
    if not sources:
        raise LawDataError(f"{where_version}: Quellen fehlen")
    overrides = dict(raw.get("overrides") or {})
    unknown = sorted(set(overrides) - profile_fields)
    if unknown:
        raise LawDataError(f"{where_version}: unbekannte Felder des Landesprofils {', '.join(unknown)}")
    law: dict[str, dict[str, Any]] = {}
    for key, entry in (raw.get("law") or {}).items():
        where = f"{where_version}, Eintrag {key}"
        spec = LAW_FIELDS.get(key)
        if spec is None:
            raise LawDataError(f"{where}: nicht im Katalog")
        if not isinstance(entry, dict):
            raise LawDataError(f"{where}: Objekt erwartet")
        unclear = bool(entry.get("unclear", False))
        _check_value(spec, entry.get("value"), where)
        norm = str(entry.get("norm") or "").strip()
        if not norm and not unclear:
            raise LawDataError(f"{where}: Norm fehlt (oder „unclear“ setzen)")
        source = entry.get("source", 0)
        if isinstance(source, int):
            if not 0 <= source < len(sources):
                raise LawDataError(f"{where}: Quelle {source} gibt es nicht")
            source = sources[source]
        if not isinstance(source, dict) or not source.get("url"):
            raise LawDataError(f"{where}: Quelle ohne Adresse")
        law[key] = {
            "value": entry.get("value"),
            "text": str(entry.get("text") or "").strip(),
            "norm": norm,
            "source": {"title": str(source.get("title") or ""), "url": str(source["url"])},
            "as_of": str(entry.get("as_of") or as_of.isoformat()),
            "unclear": unclear,
        }
    return {
        "valid_from": valid_from,
        "title": str(raw["title"]),
        "amendment": str(raw.get("amendment") or ""),
        "overrides": overrides,
        "law": law,
        "sources": sources,
        "as_of": as_of,
        "verification": str(raw.get("verification") or "teilweise"),
    }


def sync_versions(profile_model: Any, version_model: Any, rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """
    Fassungen aus den Profilzeilen anlegen, aktualisieren und entfernen, was die Datei nicht mehr kennt.

    Rückgabe: (angelegt, aktualisiert, entfernt). Fassungen sind Referenzdaten ohne Fremdschlüssel von
    Mandantendaten; eine entfernte Fassung ändert nur, welches Recht für künftige Prüfungen gilt.
    """
    profile_fields = {f.name for f in profile_model._meta.get_fields() if getattr(f, "concrete", False)}
    created = updated = removed = 0
    for row in rows:
        code = row["code"]
        wanted = [normalize_version(code, raw, profile_fields=profile_fields) for raw in row.get("versions") or []]
        days = [values["valid_from"] for values in wanted]
        if len(days) != len(set(days)):
            raise LawDataError(f"{code}: zwei Fassungen mit demselben Stichtag")
        for values in wanted:
            valid_from = values.pop("valid_from")
            _obj, was_created = version_model.objects.update_or_create(
                profile_id=code, valid_from=valid_from, defaults=values
            )
            created += int(was_created)
            updated += int(not was_created)
        stale = version_model.objects.filter(profile_id=code).exclude(valid_from__in=days)
        removed += stale.count()
        stale.delete()
    return created, updated, removed


# =============================================================================
# Fassung zum Stichtag
# =============================================================================


@dataclass(frozen=True)
class LawEntry:
    """Eintrag des Sitzungsrechts in der maßgeblichen Fassung."""

    spec: LawField
    value: Any
    text: str
    norm: str
    source_title: str
    source_url: str
    as_of: date | None
    unclear: bool
    #: Stichtag der Fassung, die den Eintrag gesetzt hat
    since: date

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def value_label(self) -> str:
        """Wert als Text: Auswahl als Bezeichnung, ja/nein, Listen mit Komma; leer bei reinen Erläuterungen."""
        if self.unclear and self.value in (None, UNCLEAR):
            return "ungeklärt"
        if self.spec.kind == KIND_CHOICE:
            return dict(self.spec.choices).get(self.value, str(self.value))
        if self.spec.kind == KIND_BOOL:
            return "ja" if self.value else "nein"
        if self.spec.kind == KIND_LIST:
            return ", ".join(self.value or []) or "–"
        if self.spec.kind == KIND_INT:
            return str(self.value)
        return ""


@dataclass
class EffectiveProfile:
    """Landesprofil mit der Fassung zu einem Stichtag (Felder samt Abweichungen, Sitzungsrecht)."""

    #: Kopie des Landesprofils mit den Abweichungen der Fassung – nie speichern
    profile: Any
    version: Any | None
    day: date
    entries: dict[str, LawEntry] = field(default_factory=dict)

    def value(self, key: str, default: Any = None) -> Any:
        entry = self.entries.get(key)
        if entry is None or (entry.unclear and entry.value in (None, UNCLEAR)):
            return default
        return entry.value

    def norm(self, key: str) -> str:
        entry = self.entries.get(key)
        return entry.norm if entry is not None else ""

    def text(self, key: str) -> str:
        entry = self.entries.get(key)
        return entry.text if entry is not None else ""

    @property
    def version_label(self) -> str:
        """„Fassung ab 01.11.2026“ – bzw. Hinweis, dass für den Tag keine Fassung erfasst ist."""
        if self.version is None:
            return "keine Fassung zum Sitzungsrecht erfasst"
        return f"Fassung ab {self.version.valid_from:%d.%m.%Y}"

    def groups(self) -> list[tuple[str, list[LawEntry]]]:
        """Einträge nach Gruppen in der Reihenfolge des Katalogs (Rechtsübersicht)."""
        result = []
        for group, label in GROUPS:
            items = [entry for key, entry in self.entries.items() if LAW_FIELDS[key].group == group]
            items.sort(key=lambda entry: list(LAW_FIELDS).index(entry.spec.key))
            if items:
                result.append((label, items))
        return result


def versions(profile: Any) -> list[Any]:
    """Fassungen eines Landesprofils nach Stichtag (je Profilobjekt einmal geladen)."""
    cached = getattr(profile, "_state_law_versions", None)
    if cached is None:
        cached = sorted(profile.versions.all(), key=lambda version: version.valid_from)
        profile._state_law_versions = cached
    return list(cached)


def _entry(key: str, raw: dict[str, Any], since: date) -> LawEntry | None:
    spec = LAW_FIELDS.get(key)
    if spec is None:  # Eintrag aus einer neueren Profildatei, den dieser Code nicht kennt
        return None
    source = raw.get("source") or {}
    try:
        as_of: date | None = date.fromisoformat(str(raw.get("as_of")))
    except ValueError:
        as_of = None
    return LawEntry(
        spec=spec,
        value=raw.get("value"),
        text=str(raw.get("text") or ""),
        norm=str(raw.get("norm") or ""),
        source_title=str(source.get("title") or ""),
        source_url=str(source.get("url") or ""),
        as_of=as_of,
        unclear=bool(raw.get("unclear")),
        since=since,
    )


def effective(profile: Any, day: date | None = None) -> EffectiveProfile:
    """Landesprofil zum Stichtag: Abweichungen und Sitzungsrecht aller Fassungen bis ``day`` (Standard heute)."""
    day = day or timezone.localdate()
    applicable = [version for version in versions(profile) if version.valid_from <= day]
    current = copy.copy(profile)
    entries: dict[str, LawEntry] = {}
    concrete = {f.name for f in profile._meta.get_fields() if getattr(f, "concrete", False)}
    for version in applicable:
        for name, value in (version.overrides or {}).items():
            if name in concrete and name != "code":
                setattr(current, name, value)
        for key, raw in (version.law or {}).items():
            entry = _entry(key, raw, version.valid_from)
            if entry is not None:
                entries[key] = entry
    return EffectiveProfile(profile=current, version=applicable[-1] if applicable else None, day=day, entries=entries)


def local_day(moment: Any) -> date | None:
    """Tag eines Zeitpunkts in Ortszeit; ``None`` ohne Zeitpunkt (Formular ohne gültigen Beginn)."""
    if moment is None or not hasattr(moment, "date"):
        return None
    return (timezone.localtime(moment) if timezone.is_aware(moment) else moment).date()


def meeting_day(meeting: Any) -> date:
    """Sitzungsdatum in Ortszeit (Stichtag der Fassung); ohne Beginn heute."""
    return local_day(getattr(meeting, "start", None)) or timezone.localdate()


def for_meeting(meeting: Any) -> EffectiveProfile | None:
    """Fassung des Landesprofils zum Sitzungsdatum; ``None`` ohne Landesprofil."""
    profile = meeting.tenant.state_profile
    if profile is None:
        return None
    cache = getattr(meeting, "_state_law_cache", None)
    day = meeting_day(meeting)
    if isinstance(cache, EffectiveProfile) and cache.day == day and cache.profile.code == profile.code:
        return cache
    result = effective(profile, day)
    meeting._state_law_cache = result
    return result


# =============================================================================
# Ortsrecht der Körperschaft (Hauptsatzung und Geschäftsordnung)
# =============================================================================

DAY_KIND_CHOICES = (("", "keine Angabe"), ("calendar", "Kalendertage"), ("working", "Arbeitstage"))
DEADLINE_START_CHOICES = (
    ("", "keine Angabe"),
    ("dispatch", "Absendung der Ladung"),
    ("provision", "Bereitstellung im Ratsinformationssystem"),
)
COMMITTEE_PUBLICITY_CHOICES = (("", "keine Angabe"), ("public", "öffentlich"), ("non_public", "nichtöffentlich"))
#: Ganze Tage bzw. Minuten in der Geschäftsordnung: 0 bis 120
MAX_DAYS = 120


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= MAX_DAYS else None


def _choice(value: Any, choices: tuple[tuple[str, str], ...]) -> str:
    return str(value) if value in {key for key, _ in choices} else ""


@dataclass
class LocalRules:
    """
    Ortsrecht einer Körperschaft (``SessionBody.local_rules``). Unbekannte oder ungültige Werte gelten als
    „nicht geregelt“, damit eine Datei aus einem anderen Stand nie zu einem Fehler beim Lesen führt.
    """

    # Hauptsatzung
    remote_per_invitation: bool = False
    remote_public_only: bool = False
    recording_date: date | None = None
    recording_reference: str = ""
    video_public_date: date | None = None
    video_public_reference: str = ""
    emergency_date: date | None = None
    emergency_until: date | None = None
    emergency_reference: str = ""
    # Geschäftsordnung
    rules_date: date | None = None
    rules_reference: str = ""
    invitation_days: int | None = None
    invitation_day_kind: str = ""
    deadline_start: str = ""
    urgent_days: int | None = None
    urgent_notice: str = ""
    motion_days: int | None = None
    question_days: int | None = None
    minutes_signers: str = ""
    voting_methods: str = ""
    committees_public: str = ""
    residents_questions_minutes: int | None = None

    DATE_FIELDS = ("recording_date", "video_public_date", "emergency_date", "emergency_until", "rules_date")
    INT_FIELDS = ("invitation_days", "urgent_days", "motion_days", "question_days", "residents_questions_minutes")
    BOOL_FIELDS = ("remote_per_invitation", "remote_public_only")
    TEXT_FIELDS = (
        "recording_reference",
        "video_public_reference",
        "emergency_reference",
        "rules_reference",
        "urgent_notice",
        "minutes_signers",
        "voting_methods",
    )

    @classmethod
    def of(cls, body: Any) -> LocalRules:
        """Ortsrecht einer Körperschaft (``None`` → nichts geregelt)."""
        return cls.from_json(getattr(body, "local_rules", None) if body is not None else None)

    @classmethod
    def from_json(cls, data: Any) -> LocalRules:
        data = data if isinstance(data, dict) else {}
        rules = cls()
        for name in cls.BOOL_FIELDS:
            setattr(rules, name, data.get(name) is True)
        for name in cls.DATE_FIELDS:
            setattr(rules, name, _date(data.get(name)))
        for name in cls.INT_FIELDS:
            setattr(rules, name, _int(data.get(name)))
        for name in cls.TEXT_FIELDS:
            setattr(rules, name, str(data.get(name) or "").strip()[:500])
        rules.invitation_day_kind = _choice(data.get("invitation_day_kind"), DAY_KIND_CHOICES)
        rules.deadline_start = _choice(data.get("deadline_start"), DEADLINE_START_CHOICES)
        rules.committees_public = _choice(data.get("committees_public"), COMMITTEE_PUBLICITY_CHOICES)
        return rules

    def to_json(self) -> dict[str, Any]:
        """Nur gesetzte Werte, Datumsangaben als ISO-Text."""
        data: dict[str, Any] = {}
        for name in (*self.BOOL_FIELDS, *self.DATE_FIELDS, *self.INT_FIELDS, *self.TEXT_FIELDS):
            value = getattr(self, name)
            if value in (None, "", False):
                continue
            data[name] = value.isoformat() if isinstance(value, date) else value
        for name in ("invitation_day_kind", "deadline_start", "committees_public"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        return data

    @staticmethod
    def _basis(day: date | None, reference: str) -> str:
        return f"Hauptsatzung vom {day:%d.%m.%Y}, {reference}" if day and reference else ""

    @property
    def recording_basis(self) -> str:
        """Nachweis der Hauptsatzungsregel zu Bild- und Tonaufnahmen als Zeile; leer ohne vollständigen Nachweis."""
        return self._basis(self.recording_date, self.recording_reference)

    @property
    def video_public_basis(self) -> str:
        """Nachweis der Hauptsatzungsregel zur Öffentlichkeit per Video; leer ohne vollständigen Nachweis."""
        return self._basis(self.video_public_date, self.video_public_reference)

    @property
    def rules_label(self) -> str:
        """„Geschäftsordnung vom 24.03.2022 (…)“; leer ohne Datum."""
        if self.rules_date is None:
            return ""
        suffix = f" ({self.rules_reference})" if self.rules_reference else ""
        return f"Geschäftsordnung vom {self.rules_date:%d.%m.%Y}{suffix}"


def body_of(meeting: Any) -> Any:
    """Körperschaft einer Sitzung: die des federführenden Gremiums, sonst die Standardkörperschaft."""
    organization = getattr(meeting, "organization", None)
    body = getattr(organization, "body", None) if organization is not None else None
    if body is not None:
        return body
    from apps.session.services import body_service

    return body_service.default_body(meeting.tenant)


def residents_questions_note(meeting: Any) -> str:
    """
    Hinweis zur Einwohnerfragestunde einer Sitzung (Issue #757, z. B. § 62 NKomVG): Zeitrahmen aus der
    Geschäftsordnung der Körperschaft und – nach der Fassung zum Sitzungsdatum – „nur Anwesende“ (Niedersachsen ab
    01.11.2026). Leer, wenn nichts davon geregelt ist. Für TOP-Liste und Ladung.
    """
    parts = []
    minutes = LocalRules.of(body_of(meeting)).residents_questions_minutes
    if minutes:
        parts.append(f"Höchstens {minutes} Minuten (Geschäftsordnung).")
    law = for_meeting(meeting)
    if law is not None and law.value("residents_questions") == "present_only":
        norm = law.norm("residents_questions")
        parts.append(f"Fragen nur von anwesenden Einwohnerinnen und Einwohnern{f' ({norm})' if norm else ''}.")
    return " ".join(parts)


@dataclass(frozen=True)
class Publicity:
    """Öffentlichkeit der Sitzungen eines Gremiums (Issue #757): Vorgabe, Sperre und Grund."""

    public: bool
    #: Landesrecht schreibt „nichtöffentlich“ vor; „öffentlich“ ist nicht wählbar
    locked: bool = False
    reason: str = ""


#: Gremientypen, die ohne Angabe öffentlich bzw. nichtöffentlich tagen
_PUBLIC_TYPES = frozenset({"council", "local_council", "youth_council", "advisory", "commission", "other"})
_NON_PUBLIC_TYPES = frozenset({"faction", "group", "department"})


#: Platzhalter: Fassung des Landesprofils selbst bestimmen
_FROM_TENANT: Any = object()


def publicity(organization: Any, day: date | None = None, *, law: Any = _FROM_TENANT, body: Any = None) -> Publicity:
    """
    Öffentlichkeit neuer Sitzungen eines Gremiums:

    1. Stets nichtöffentlich, wenn das Landesprofil die Ausschussart ausnimmt (z. B. Hauptausschuss, § 78 Abs. 2
       NKomVG) – gesperrt.
    2. Einstellung am Gremium (öffentlich bzw. nichtöffentlich).
    3. Nach Gremientyp: Vertretung, Ortsrat, Beiräte öffentlich; Fraktionen, Gruppen und Verwaltung nichtöffentlich;
       Ausschüsse nach der Geschäftsordnung der Körperschaft (Ortsrecht), ohne Angabe öffentlich.

    ``law`` (Fassung, ``None`` ohne Landesprofil) und ``body`` (Körperschaft des Gremiums) lassen sich für Listen
    einmal vorab bestimmen, statt je Gremium zu laden.
    """
    kind = organization.committee_kind or ""
    if organization.organization_type == "committee" and kind:
        if law is _FROM_TENANT:
            profile = organization.tenant.state_profile
            law = effective(profile, day) if profile is not None else None
        if law is not None and kind in (law.value("non_public_committee_kinds") or []):
            label = dict(organization.COMMITTEE_KIND_CHOICES).get(kind, kind)
            norm = law.norm("non_public_committee_kinds")
            return Publicity(False, True, f"Der {label} tagt stets nichtöffentlich{f' ({norm})' if norm else ''}.")
    if organization.publicity == organization.PUBLICITY_PUBLIC:
        return Publicity(True, reason="Einstellung am Gremium")
    if organization.publicity == organization.PUBLICITY_NON_PUBLIC:
        return Publicity(False, reason="Einstellung am Gremium")
    if organization.organization_type in _NON_PUBLIC_TYPES:
        return Publicity(False, reason="Gremientyp")
    if organization.organization_type == "committee":
        if body is None:
            from apps.session.services import body_service

            body = organization.body if organization.body_id else body_service.default_body(organization.tenant)
        if LocalRules.of(body).committees_public == "non_public":
            return Publicity(False, reason="Geschäftsordnung")
    return Publicity(True, reason="Gremientyp")


def main_committee_hint(organization: Any, memberships: Any) -> str:
    """
    Hinweis am Hauptausschuss (Issue #757): Führt nach dem Landesprofil die bzw. der HVB den Vorsitz (z. B. § 74
    NKomVG), aber keine laufende Besetzung hat diese Funktion, fehlt sie in Ladung und Anwesenheit.
    """
    if organization.committee_kind != "main" or organization.tenant.state_profile is None:
        return ""
    law = effective(organization.tenant.state_profile)
    if law.value("main_committee_chair") != "hvb" or any(m.role == "hvb" for m in memberships):
        return ""
    norm = law.norm("main_committee_chair")
    return (
        f"Den Vorsitz führt die Hauptverwaltungsbeamtin bzw. der Hauptverwaltungsbeamte{f' ({norm})' if norm else ''}; "
        "bitte mit der Funktion „Hauptverwaltungsbeamtin/-beamter (kraft Amtes)“ in die Besetzung aufnehmen. "
        "Die Sitzungen sind stets nichtöffentlich."
    )


def legal_designation(organization: Any) -> str:
    """
    Gesetzlicher Name eines Gremiums nach dem Körperschaftstyp (Issue #757, z. B. § 7 Abs. 2 NKomVG):
    Hauptausschuss → Verwaltungs-, Samtgemeinde-, Kreis- bzw. Regionsausschuss; Vertretung → Rat, Samtgemeinderat,
    Kreistag bzw. Regionsversammlung. Leer, wenn das Landesprofil keine Bezeichnung kennt.
    """
    tenant = organization.tenant
    if tenant.state_profile is None:
        return ""
    if organization.organization_type == "council":
        key = "council"
    elif organization.organization_type == "committee" and organization.committee_kind == "main":
        key = "main"
    else:
        return ""
    names = effective(tenant.state_profile).value("designations") or {}
    body = organization.body if organization.body_id else None
    body_type = (getattr(body, "body_type", "") or tenant.body_type or "") if body is not None else tenant.body_type
    return str((names.get(key) or {}).get(body_type or "", ""))


# =============================================================================
# Notlagenbeschluss (z. B. § 182 Abs. 1 Satz 2 NKomVG)
# =============================================================================

#: So viele Tage vor dem Ablauf warnt die Prüfung
EMERGENCY_WARN_DAYS = 14


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    for candidate in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    return day  # pragma: no cover - einer der Tage passt immer


def emergency_problems(law: EffectiveProfile, rules: LocalRules, day: date, body_name: str) -> list[str]:
    """
    Fehler, wenn das Landesrecht für die Notlage einen Beschluss mit Ablauf verlangt und dieser fehlt, den Tag
    nicht abdeckt oder länger gilt, als das Gesetz erlaubt.
    """
    if not law.value("emergency_resolution", False):
        return []
    norm = law.norm("emergency_resolution") or law.profile.norm_emergency or law.profile.law
    months = law.value("emergency_max_months")
    start, until = rules.emergency_date, rules.emergency_until
    if start is None or until is None:
        return [
            f"Sitzungen in einer Notlage setzen nach {norm} einen Beschluss der Vertretung mit Geltungsdauer voraus. "
            f"Bitte für „{body_name}“ im Ortsrecht Datum, Ablauf und Fundstelle des Beschlusses hinterlegen."
        ]
    problems = []
    if until < start:
        problems.append(f"Der Notlagenbeschluss für „{body_name}“ endet vor seinem Datum.")
    elif months and until > add_months(start, int(months)):
        problems.append(
            f"Ein Notlagenbeschluss gilt nach {norm} höchstens {months} Monate; der Beschluss für „{body_name}“ "
            f"vom {start:%d.%m.%Y} reicht bis {until:%d.%m.%Y}."
        )
    if not start <= day <= until:
        problems.append(
            f"Der Notlagenbeschluss für „{body_name}“ gilt vom {start:%d.%m.%Y} bis {until:%d.%m.%Y} und deckt "
            f"die Sitzung am {day:%d.%m.%Y} nicht ab."
        )
    return problems


def emergency_warning(rules: LocalRules, today: date | None = None) -> str:
    """Hinweis, wenn ein Notlagenbeschluss in den nächsten Tagen abläuft (Einstellungen, Sitzungen)."""
    today = today or timezone.localdate()
    until = rules.emergency_until
    if until is None or until < today or until - today > timedelta(days=EMERGENCY_WARN_DAYS):
        return ""
    return (
        f"Der Notlagenbeschluss läuft am {until:%d.%m.%Y} ab. Danach sind Sitzungen nach den Erleichterungen für "
        "Notlagen nur mit einem neuen Beschluss zulässig."
    )
