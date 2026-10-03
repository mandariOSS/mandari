# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsformat und Landesprofil (Issue #138).

Eine Sitzung ist präsent, hybrid (einzelne Mitglieder per Bild-Ton-Übertragung zugeschaltet) oder
digital (alle zugeschaltet). Ob das zulässig ist, entscheidet das Kommunalverfassungsrecht des Landes;
es liegt als Landesprofil (``SessionStateProfile``) vor und wird je Mandant gewählt.

Prüfregeln (``check``):

- Präsenzsitzungen sind immer zulässig.
- Hybride oder digitale Sitzungen brauchen ein Landesprofil des Mandanten.
- Maßgeblich ist die Regel des Landesprofils für den Gremientyp (Rat bzw. Ausschüsse); gesetzlich
  ausgenommene Ausschussarten (z. B. NRW Hauptausschuss) fallen auf die Regel für ausgenommene
  Ausschüsse zurück. Bei gemeinsamen Sitzungen gilt die strengste Regel der beteiligten Gremien.
- Kennt das Landesprofil ausgenommene Ausschussarten, müssen Gremien ohne gesetzliche Ausschussart,
  deren Name auf eine dieser Arten hindeutet (``suspected_committee_kinds``), erst eingeordnet werden
  (Fehler); nicht eingeordnete Ausschüsse ohne solchen Namen erzeugen eine Warnung. Bis zur
  Einordnung gilt bei verdächtigem Namen vorsorglich die strengere Regel.
- „nicht vorgesehen“ verhindert das Format mit Begründung.
- „nur in Notlagen“ und jede digitale Sitzung verlangen eine Begründung (Notlage, Beschluss); verlangt
  das Land auch für die Notlage eine örtliche Regelung (``emergency_needs_local_basis``), zusätzlich
  deren Nachweis.
- „zulässig (Regelbetrieb)“ verlangt den Nachweis der örtlichen Rechtsgrundlage (Hauptsatzung bzw.
  Geschäftsordnung mit Datum und Fundstelle), „ungeklärt“ ebenso.

Fraktionen und Verwaltungseinheiten unterliegen nicht den Sitzungsregeln der Kommunalverfassung.

Die Profile stehen in ``apps/session/presets/landesprofile.json`` (Quellen und Stand je Land) und
werden per Datenmigration bzw. ``manage.py session_state_profiles --sync`` übernommen.
Keine Rechtsberatung – die Prüfung setzt die recherchierte Rechtslage um, ersetzt aber keine
Prüfung vor Ort (docs/SESSION_SITZUNGSFORMAT_LANDESRECHT.md).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

PROFILE_FILE = Path(__file__).resolve().parent.parent / "presets" / "landesprofile.json"

FORMAT_PRESENCE = "presence"
FORMAT_HYBRID = "hybrid"
FORMAT_DIGITAL = "digital"

RULE_REGULAR = "regular"
RULE_EMERGENCY = "emergency"
RULE_NONE = "none"
RULE_UNCLEAR = "unclear"

#: Strenge der Regeln – bei gemeinsamen Sitzungen und ausgenommenen Ausschüssen gilt die strengere
_STRICTNESS = {RULE_REGULAR: 0, RULE_UNCLEAR: 1, RULE_EMERGENCY: 2, RULE_NONE: 3}

#: Gremientypen ohne Sitzungsregeln der Kommunalverfassung (Fraktionen, Verwaltung)
UNREGULATED_TYPES = frozenset({"faction", "group", "department"})

_FORMAT_PLURAL = {FORMAT_HYBRID: "Hybride Sitzungen", FORMAT_DIGITAL: "Digitale Sitzungen"}

#: Gremientypen, die auch ohne verdächtigen Namen eingeordnet sein sollten (sonst Warnung)
_COMMITTEE_TYPES = frozenset({"committee"})

#: Namensmuster gesetzlich besonders geregelter Ausschüsse – nur ein Hinweis, keine Einordnung.
#: Treffer z. B. „Haupt- und Finanzausschuss“, „Ausschuss für Finanzen und Beteiligungen“,
#: „Rechnungsprüfungsausschuss“; kein Treffer „Hauptsatzungskommission“.
_KIND_NAME_PATTERNS = {
    "main": re.compile(r"\bhaupt(?:ausschuss|\s*-|\s*,|\s+und\b)", re.IGNORECASE),
    "finance": re.compile(r"finanz|haushalt", re.IGNORECASE),
    "audit": re.compile(r"rechnungspr(?:ü|ue|u)f", re.IGNORECASE),
}
#: Übliche Kurznamen (ganzer Kurzname, ohne Groß-/Kleinschreibung)
_KIND_SHORT_NAMES = {
    "ha": ("main",),
    "hfa": ("main", "finance"),
    "hufa": ("main", "finance"),
    "rpa": ("audit",),
    "rpra": ("audit",),
}


# =============================================================================
# Profile laden
# =============================================================================


def load_profile_data(path: Path | None = None) -> dict[str, Any]:
    """Profildatei lesen (Stand, Hinweis, Liste der Länder)."""
    with (path or PROFILE_FILE).open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


def profile_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Profile als Feldwerte; ``as_of`` fällt auf den Stand der Datei zurück."""
    data = load_profile_data(path)
    stand = date.fromisoformat(data["stand"])
    rows = []
    for entry in data["profile"]:
        row = dict(entry)
        row["as_of"] = date.fromisoformat(row["as_of"]) if row.get("as_of") else stand
        rows.append(row)
    return rows


def sync_profiles(model: Any = None, path: Path | None = None) -> tuple[int, int]:
    """
    Landesprofile aus der Datei anlegen bzw. aktualisieren (idempotent).

    ``model`` erlaubt den Aufruf aus Datenmigrationen mit dem historischen Modell; Schlüssel, die das
    Modell (noch) nicht kennt, werden übergangen. Ohne ``model`` übernimmt der Lauf auch die Fassungen mit
    Sitzungsrecht (Issue #757, ``state_law_service.sync_versions``). Rückgabe: (angelegt, aktualisiert)
    der Landesprofile.
    """
    with_versions = model is None
    if model is None:
        from apps.session.models import SessionStateProfile

        model = SessionStateProfile
    known = {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}
    created = updated = 0
    rows = profile_rows(path)
    for row in rows:
        values = {key: value for key, value in row.items() if key in known and key != "code"}
        _obj, was_created = model.objects.update_or_create(code=row["code"], defaults=values)
        if was_created:
            created += 1
        else:
            updated += 1
    if with_versions:
        from apps.session.models import SessionStateProfileVersion
        from apps.session.services import state_law_service

        state_law_service.sync_versions(model, SessionStateProfileVersion, rows)
    return created, updated


def profile_differences(path: Path | None = None) -> list[str]:
    """Abweichungen zwischen Datenbank und Profildatei (für ``session_state_profiles --check``)."""
    from apps.session.models import SessionStateProfile, SessionStateProfileVersion
    from apps.session.services import state_law_service

    known = {f.name for f in SessionStateProfile._meta.get_fields() if getattr(f, "concrete", False)}
    stored = {p.code: p for p in SessionStateProfile.objects.all()}
    stored_versions = {(v.profile_id, v.valid_from): v for v in SessionStateProfileVersion.objects.all()}
    differences = []
    wanted_versions = set()
    for row in profile_rows(path):
        profile = stored.get(row["code"])
        if profile is None:
            differences.append(f"{row['code']}: fehlt in der Datenbank")
            continue
        changed = sorted(key for key, value in row.items() if key in known and getattr(profile, key) != value)
        if changed:
            differences.append(f"{row['code']}: abweichend ({', '.join(changed)})")
        for raw in row.get("versions") or []:
            values = state_law_service.normalize_version(row["code"], raw, profile_fields=known)
            key = (row["code"], values.pop("valid_from"))
            wanted_versions.add(key)
            version = stored_versions.get(key)
            label = f"{key[0]}, Fassung ab {key[1]:%d.%m.%Y}"
            if version is None:
                differences.append(f"{label}: fehlt in der Datenbank")
                continue
            changed = sorted(name for name, value in values.items() if getattr(version, name) != value)
            if changed:
                differences.append(f"{label}: abweichend ({', '.join(changed)})")
    for code, valid_from in sorted(set(stored_versions) - wanted_versions):
        differences.append(f"{code}, Fassung ab {valid_from:%d.%m.%Y}: nicht mehr in der Profildatei")
    return differences


# =============================================================================
# Regeln
# =============================================================================


@dataclass(frozen=True)
class FormatRule:
    """Maßgebliche Regel für ein Gremium und ein Format."""

    organization: Any
    rule: str
    norm: str
    excluded_kind: bool = False
    unregulated: bool = False
    #: Keine gesetzliche Ausschussart gesetzt, obwohl das Landesprofil Ausschussarten ausnimmt
    unclassified: bool = False
    #: Ausgenommene Ausschussarten, auf die der Name eines nicht eingeordneten Gremiums hindeutet
    suspected_kinds: tuple[str, ...] = ()
    #: Örtliche Regel am Gremium: laut Hauptsatzung keine Zuschaltung (Issue #757)
    locally_excluded: bool = False


def _stricter(first: str, second: str) -> str:
    return first if _STRICTNESS.get(first, 3) >= _STRICTNESS.get(second, 3) else second


def suspected_committee_kinds(organization: Any, kinds: Iterable[str] | None = None) -> list[str]:
    """
    Gesetzliche Ausschussarten, auf die Name oder Kurzname eines Gremiums hindeuten (nur Hinweis).

    ``kinds`` begrenzt auf die Arten, die ein Landesprofil ausnimmt. Ein „Haupt- und Finanzausschuss“
    deutet auf Haupt- und Finanzausschuss zugleich hin.
    """
    short_name = str(getattr(organization, "short_name", "") or "").strip()
    text = f"{organization.name or ''} {short_name}"
    found = [kind for kind, pattern in _KIND_NAME_PATTERNS.items() if pattern.search(text)]
    for kind in _KIND_SHORT_NAMES.get(short_name.lower(), ()):
        if kind not in found:
            found.append(kind)
    if kinds is not None:
        allowed = set(kinds)
        found = [kind for kind in found if kind in allowed]
    return found


def committee_kind_labels(kinds: Iterable[str]) -> list[str]:
    """Ausschussarten als Bezeichnungen."""
    from apps.session.models import SessionOrganization

    labels = dict(SessionOrganization.COMMITTEE_KIND_CHOICES)
    return [str(labels.get(kind, kind)) for kind in kinds]


def join_labels(labels: list[str], word: str = "und") -> str:
    """„A, B und C“ bzw. „A oder B“."""
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} {word} {labels[-1]}"


def rule_for(profile: Any, organization: Any, meeting_format: str) -> FormatRule:
    """Regel des Landesprofils für ein Gremium (Rat bzw. Ausschuss, ausgenommene Ausschussart)."""
    if organization.organization_type in UNREGULATED_TYPES:
        return FormatRule(organization, RULE_REGULAR, "", unregulated=True)
    council = organization.organization_type == "council"
    if meeting_format == FORMAT_DIGITAL:
        rule = profile.digital_council if council else profile.digital_committees
    else:
        rule = profile.hybrid_council if council else profile.hybrid_committees
    kind = getattr(organization, "committee_kind", "") or ""
    excluded_kinds = list(profile.excluded_committee_kinds or [])
    excluded = not council and bool(kind) and kind in excluded_kinds
    suspected: tuple[str, ...] = ()
    unclassified = False
    if not council and not kind and excluded_kinds:
        suspected = tuple(suspected_committee_kinds(organization, excluded_kinds))
        unclassified = bool(suspected) or organization.organization_type in _COMMITTEE_TYPES
    if excluded or suspected:
        # Bis zur Einordnung gilt bei verdächtigem Namen vorsorglich die strengere Regel
        rule = _stricter(rule, profile.excluded_committee_rule)
    # Örtliche Abweichung (Issue #757, z. B. § 64 Abs. 8 NKomVG): Die Hauptsatzung schließt die Zuschaltung aus.
    # Sie betrifft nur die hybride Sitzung (§ 64 Abs. 3 bis 7); Videositzungen in einer Notlage (§ 182) beruhen auf
    # einer eigenen Grundlage und folgen weiter der Regel des Landesprofils.
    locally_excluded = meeting_format == FORMAT_HYBRID and getattr(organization, "remote_local_rule", "") == "excluded"
    if locally_excluded:
        rule = RULE_NONE
    norm = {RULE_REGULAR: profile.norm_regular, RULE_EMERGENCY: profile.norm_emergency}.get(rule, "")
    return FormatRule(
        organization,
        rule,
        norm,
        excluded_kind=excluded,
        unclassified=unclassified,
        suspected_kinds=suspected,
        locally_excluded=locally_excluded,
    )


@dataclass
class FormatCheck:
    """Ergebnis der Prüfung: Fehler verhindern das Speichern, ``rules`` erklären die Rechtslage."""

    errors: list[str] = field(default_factory=list)
    #: Hinweise, die das Speichern nicht verhindern (z. B. nicht eingeordnete Ausschüsse)
    warnings: list[str] = field(default_factory=list)
    rules: list[FormatRule] = field(default_factory=list)
    needs_reason: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def _add(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def check(
    tenant: Any,
    organizations: Iterable[Any],
    meeting_format: str,
    reason: str = "",
    *,
    day: date | None = None,
    is_public: bool | None = None,
) -> FormatCheck:
    """
    Sitzungsformat für die beteiligten Gremien gegen das Landesprofil des Mandanten prüfen.

    ``day`` wählt die Fassung des Landesprofils (Issue #757; Standard heute) und prüft einen Notlagenbeschluss
    gegen das Sitzungsdatum; ``is_public`` prüft die örtliche Beschränkung der Zuschaltung auf öffentliche
    Sitzungen. Das Ortsrecht kommt von der Körperschaft des ersten (federführenden) Gremiums.
    """
    from apps.session.services import state_law_service

    result = FormatCheck()
    if meeting_format == FORMAT_PRESENCE:
        return result
    organizations = list(organizations)
    label = _FORMAT_PLURAL.get(meeting_format, "Hybride oder digitale Sitzungen")
    if tenant.state_profile is None:
        result._add(
            f"{label} setzen ein Landesprofil voraus. Bitte in den Einstellungen unter „Sitzungsformate“ "
            "das Land wählen und die örtliche Rechtsgrundlage nachweisen."
        )
        return result
    law = state_law_service.effective(tenant.state_profile, day)
    profile = law.profile
    body = _body_of_organizations(tenant, organizations)
    local = state_law_service.LocalRules.of(body)

    result.rules = [rule_for(profile, org, meeting_format) for org in organizations]
    regulated = [r for r in result.rules if not r.unregulated]
    result.needs_reason = meeting_format == FORMAT_DIGITAL and bool(regulated)
    documented = tenant.hybrid_basis_documented
    basis_label = profile.get_legal_basis_display()
    reason_explained = False
    excluded_labels = join_labels(committee_kind_labels(profile.excluded_committee_kinds or []))
    excluded_rule = profile.get_excluded_committee_rule_display()
    if profile.excluded_committee_rule == RULE_EMERGENCY and profile.norm_emergency:
        excluded_rule = f"{excluded_rule} ({profile.norm_emergency})"

    for item in regulated:
        org_name = item.organization.name
        kind_label = item.organization.get_committee_kind_display() if item.excluded_kind else ""
        if item.suspected_kinds:
            # Ohne Einordnung lassen sich Zulässigkeit und Rechtsgrundlage nicht sicher bestimmen
            suspected = join_labels(committee_kind_labels(item.suspected_kinds), "oder")
            result._add(
                f"„{org_name}“ ist keiner gesetzlichen Ausschussart zugeordnet; der Name deutet auf "
                f"{suspected} hin. Für {excluded_labels} gilt im Landesprofil {profile.name} bei "
                f"{label.lower()}: {excluded_rule}. Bitte beim Gremium die gesetzliche Ausschussart "
                "festlegen („Anderer Ausschuss“, wenn keine besondere Art zutrifft)."
            )
            continue
        if item.unclassified:
            result._warn(
                f"„{org_name}“ ist keiner gesetzlichen Ausschussart zugeordnet. Für {excluded_labels} gilt "
                f"im Landesprofil {profile.name} bei {label.lower()}: {excluded_rule}. Bitte beim Gremium "
                "festlegen, ob eine dieser Arten zutrifft."
            )
        if item.rule == RULE_NONE:
            if item.locally_excluded:
                result._add(
                    f"{label} sind für „{org_name}“ nach der Hauptsatzung ausgeschlossen (örtliche Regel beim "
                    "Gremium unter „Zuschaltung“)."
                )
            elif item.excluded_kind:
                result._add(
                    f"{label} sind für „{org_name}“ ({kind_label}) nach dem Landesprofil {profile.name} "
                    f"ausgeschlossen ({profile.norm_regular or profile.law})."
                )
            else:
                result._add(f"{label} sind für „{org_name}“ nach {profile.law} nicht vorgesehen.")
        elif item.rule == RULE_EMERGENCY:
            result.needs_reason = True
            for problem in state_law_service.emergency_problems(law, local, law.day, body.name):
                result._add(problem)
            if profile.emergency_needs_local_basis and not documented:
                result._add(
                    f"{label} in einer Notlage setzen nach {profile.norm_emergency or profile.law} eine "
                    f"Regelung in der {basis_label} voraus. Bitte den Nachweis (Datum und Fundstelle) in den "
                    "Einstellungen unter „Sitzungsformate“ hinterlegen."
                )
            if item.excluded_kind and not reason.strip():
                reason_explained = True
                result._add(
                    f"Für „{org_name}“ ({kind_label}) sind {label.lower()} im Regelbetrieb ausgeschlossen "
                    f"({profile.norm_regular}); zulässig nur in einer Notlage nach {profile.norm_emergency} "
                    "mit Begründung."
                )
        elif item.rule == RULE_UNCLEAR and not documented:
            result._add(
                f"Die Rechtslage für {label.lower()} von „{org_name}“ ist im Landesprofil {profile.name} "
                "ungeklärt. Bitte die örtliche Rechtsgrundlage mit Datum und Fundstelle in den Einstellungen "
                "nachweisen."
            )
        elif item.rule == RULE_REGULAR:
            if profile.legal_basis == profile.BASIS_BESCHLUSS:
                result.needs_reason = True
            elif not documented:
                result._add(
                    f"{label} setzen nach {item.norm or profile.law} eine Regelung in der {basis_label} voraus. "
                    "Bitte den Nachweis (Datum und Fundstelle) in den Einstellungen unter „Sitzungsformate“ "
                    "hinterlegen."
                )

    if result.needs_reason and not reason.strip() and not reason_explained:
        requirements = profile.emergency_requirements.strip()
        hint = f" Voraussetzungen: {requirements}" if requirements else ""
        result._add(f"Bitte das Sitzungsformat begründen (Notlage bzw. Beschluss des Gremiums).{hint}")
    # Hauptsatzung beschränkt die Zuschaltung auf öffentliche Sitzungen (Issue #757, § 64 Abs. 3 Satz 3 NKomVG)
    if regulated and is_public is False and local.remote_public_only and law.value("remote_public_only", False):
        norm = law.norm("remote_public_only")
        result._add(
            "Nach der Hauptsatzung ist die Zuschaltung nur in öffentlichen Sitzungen zulässig"
            f"{f' ({norm})' if norm else ''}. Bitte die Sitzung als Präsenzsitzung führen oder öffentlich laden."
        )
    return result


def _body_of_organizations(tenant: Any, organizations: list[Any]) -> Any:
    """Körperschaft des federführenden Gremiums (Ortsrecht); ohne Gremium die Standardkörperschaft."""
    from apps.session.services import body_service

    lead = organizations[0] if organizations else None
    body = getattr(lead, "body", None) if lead is not None else None
    return body if body is not None else body_service.default_body(tenant)


def strictest_rule(rules: Iterable[FormatRule]) -> FormatRule | None:
    """Strengste Regel der beteiligten Gremien (für Rechtsgrundlage in Ladung und Anzeige)."""
    regulated = [r for r in rules if not r.unregulated]
    if not regulated:
        return None
    return max(regulated, key=lambda r: _STRICTNESS.get(r.rule, 3))


def legal_basis_text(tenant: Any, rules: Iterable[FormatRule]) -> str:
    """
    Rechtsgrundlage als Zeile, z. B. „§ 58a GO NRW i. V. m. Hauptsatzung vom 12.03.2024, § 7“.

    Notlage: Norm der Notlage; ungeklärte Rechtslage: nur der örtliche Nachweis.
    """
    rule = strictest_rule(rules)
    if rule is None or tenant.state_profile is None:
        return ""
    local: str = tenant.hybrid_basis_label
    if rule.rule == RULE_EMERGENCY:
        emergency = f"{rule.norm} (Notlage)" if rule.norm else "Notlage"
        if tenant.state_profile.emergency_needs_local_basis and local:
            return f"{emergency} i. V. m. {local}"
        return emergency
    if rule.rule == RULE_REGULAR:
        if rule.norm and local:
            return f"{rule.norm} i. V. m. {local}"
        return rule.norm or local
    return local


# =============================================================================
# Einordnung der Ausschüsse (Einstellungen „Sitzungsformate“)
# =============================================================================


@dataclass
class CommitteeKindOverview:
    """Stand der Einordnung, wenn das Landesprofil Ausschussarten von hybriden Sitzungen ausnimmt."""

    profile: Any
    kind_labels: list[str]
    rule_label: str
    #: Gremien mit einer ausgenommenen Ausschussart
    classified: list[Any] = field(default_factory=list)
    #: (Gremium, Bezeichnungen der Arten, auf die der Name hindeutet) – nicht eingeordnet
    unclassified: list[tuple[Any, list[str]]] = field(default_factory=list)

    @property
    def missing(self) -> bool:
        """Keinem aktiven Gremium ist eine der ausgenommenen Arten zugeordnet."""
        return not self.classified


def committee_kind_overview(tenant: Any) -> CommitteeKindOverview | None:
    """
    Aktive Gremien des Mandanten nach Einordnung; ``None``, wenn das Landesprofil keine Ausschussarten
    ausnimmt. Grundlage der Warnung in den Einstellungen: Ohne Einordnung behandelt die Prüfung einen
    Hauptausschuss mit unauffälligem Namen wie einen gewöhnlichen Fachausschuss.
    """
    from apps.session.models import SessionOrganization

    profile = tenant.state_profile
    kinds = list(getattr(profile, "excluded_committee_kinds", None) or [])
    if profile is None or not kinds:
        return None
    overview = CommitteeKindOverview(
        profile=profile,
        kind_labels=committee_kind_labels(kinds),
        rule_label=profile.get_excluded_committee_rule_display(),
    )
    organizations = (
        SessionOrganization.objects.filter(tenant=tenant, is_active=True)
        .exclude(organization_type__in=[*UNREGULATED_TYPES, "council"])
        .order_by("name")
    )
    for organization in organizations:
        if organization.committee_kind in kinds:
            overview.classified.append(organization)
        elif not organization.committee_kind:
            suspected = suspected_committee_kinds(organization, kinds)
            if suspected or organization.organization_type in _COMMITTEE_TYPES:
                overview.unclassified.append((organization, committee_kind_labels(suspected)))
    return overview


# =============================================================================
# Sitzung beschreiben (Ladung, Tagesordnung, Detailseite, Öffentlichkeit)
# =============================================================================

_DESCRIPTIONS = {
    FORMAT_HYBRID: "Hybride Sitzung – Teilnahme im Sitzungsraum oder per Bild-Ton-Übertragung",
    FORMAT_DIGITAL: "Digitale Sitzung – alle Mitglieder nehmen per Bild-Ton-Übertragung teil",
}


def check_meeting(meeting: Any) -> FormatCheck:
    """Gespeichertes Format einer Sitzung gegen das Landesprofil in der Fassung zum Sitzungsdatum prüfen."""
    from apps.session.services import state_law_service

    return check(
        meeting.tenant,
        meeting.participating_organizations,
        meeting.format or FORMAT_PRESENCE,
        meeting.format_reason or "",
        day=state_law_service.meeting_day(meeting),
        is_public=bool(meeting.is_public),
    )


@dataclass(frozen=True)
class MeetingFormatInfo:
    """Angaben zum Sitzungsformat für Ladung, Tagesordnung und Anzeige."""

    format: str
    label: str
    description: str
    legal_basis: str
    reason: str
    remote_access: str
    public_url: str
    public_note: str
    public_registration_required: bool
    public_registration_days: int | None
    #: Fehler der Prüfung – das gespeicherte Format ist nach dem Landesprofil nicht (mehr) zulässig
    warnings: tuple[str, ...]
    #: Hinweise der Prüfung, die die Ladung nicht sperren (z. B. nicht eingeordneter Ausschuss)
    hints: tuple[str, ...] = ()
    #: Vermerke für die Ladung (Issue #757): Zulassung der Zuschaltung, Pflichthinweis an Zugeschaltete
    notices: tuple[str, ...] = ()

    @property
    def is_remote(self) -> bool:
        return self.format != FORMAT_PRESENCE

    @property
    def public_hint(self) -> str:
        """Hinweis für die Öffentlichkeit (Übertragung, Anmeldung); leer, wenn es nichts zu sagen gibt."""
        parts = []
        if self.public_registration_required and self.format == FORMAT_DIGITAL:
            frist = (
                f" bis {self.public_registration_days} Tag(e) vor der Sitzung"
                if self.public_registration_days is not None
                else ""
            )
            parts.append(f"Zuhören über einen geschützten Zugang nach vorheriger Anmeldung{frist}.")
        elif self.public_url and self.format == FORMAT_DIGITAL:
            parts.append("Die Sitzung wird für die Öffentlichkeit übertragen.")
        elif self.public_url:
            parts.append("Die öffentliche Sitzung wird übertragen.")
        if self.public_note:
            parts.append(self.public_note)
        return " ".join(parts)


def describe(meeting: Any, *, for_members: bool = False, checks: bool = True) -> MeetingFormatInfo:
    """
    Sitzungsformat einer Sitzung beschreiben.

    ``for_members`` entschlüsselt den Zugangsweg für Zugeschaltete – nur für Ladung und Mappe an
    Gremienmitglieder, nie für öffentliche Dokumente oder Schnittstellen. ``checks=False`` verzichtet
    auf Prüfung und Rechtsgrundlage (öffentliche Schnittstelle: keine Abfragen je Gremium).
    """
    meeting_format = meeting.format or FORMAT_PRESENCE
    remote = meeting_format != FORMAT_PRESENCE
    tenant = meeting.tenant
    profile = tenant.state_profile if remote else None
    result = check_meeting(meeting) if remote and checks else FormatCheck()
    remote_access = ""
    if for_members and remote:
        remote_access = str(meeting.get_remote_access_decrypted() or "")
    hints = list(result.warnings)
    if checks:
        hints.extend(media_hints(meeting))
    return MeetingFormatInfo(
        format=meeting_format,
        label=str(meeting.get_format_display()),
        description=_DESCRIPTIONS.get(meeting_format, ""),
        legal_basis=legal_basis_text(tenant, result.rules) if remote and checks else "",
        reason=(meeting.format_reason or "").strip() if remote else "",
        remote_access=remote_access,
        public_url=meeting.public_access_url or "",
        public_note=(meeting.public_access_note or "").strip(),
        public_registration_required=bool(profile and profile.public_registration_required),
        public_registration_days=tenant.digital_public_registration_days,
        warnings=tuple(result.errors),
        hints=tuple(hints),
        notices=tuple(invitation_notices(meeting, for_members=for_members)) if remote and checks else (),
    )


# =============================================================================
# Ortsrecht in Ladung und Anzeige (Issue #757)
# =============================================================================


def invitation_notices(meeting: Any, *, for_members: bool = False) -> list[str]:
    """
    Vermerke zum Sitzungsformat für die Ladung einer hybriden oder digitalen Sitzung:

    - Zulassung der Zuschaltung mit dieser Ladung, wenn die Hauptsatzung sie je Ladung vorsieht (§ 64 Abs. 3
      Satz 2 NKomVG)
    - Pflichthinweis an Zugeschaltete, dass niemand den nichtöffentlichen Teil mitverfolgen darf (§ 64 Abs. 6
      NKomVG) – in der Fassung für Mitglieder bzw. bei einer nichtöffentlichen Sitzung
    """
    from apps.session.services import state_law_service

    law = state_law_service.for_meeting(meeting)
    if law is None or (meeting.format or FORMAT_PRESENCE) == FORMAT_PRESENCE:
        return []
    notices = []
    local = state_law_service.LocalRules.of(state_law_service.body_of(meeting))
    if local.remote_per_invitation and law.value("remote_per_invitation", False):
        norm = law.norm("remote_per_invitation")
        basis = f" ({norm} i. V. m. der Hauptsatzung)" if norm else " (Hauptsatzung)"
        notices.append(f"Die Teilnahme per Bild-Ton-Übertragung ist mit dieser Ladung zugelassen{basis}.")
    text = law.text("remote_non_public_notice")
    if text and (for_members or not meeting.is_public):
        notices.append(text)
    return notices


def media_hints(meeting: Any) -> list[str]:
    """
    Hinweise zur Übertragung einer Sitzung (``public_access_url``) nach Landesrecht und Hauptsatzung:
    fehlender Nachweis für Bild- und Tonaufnahmen bzw. die Öffentlichkeit per Video, Widersprüche von
    Mitgliedern (§ 64 Abs. 2 und 9 NKomVG). Nur intern (Detailseite) – nie in öffentlichen Dokumenten.
    """
    from apps.session.services import state_law_service

    if not (meeting.public_access_url or "").strip():
        return []
    law = state_law_service.for_meeting(meeting)
    if law is None:
        return []
    local = state_law_service.LocalRules.of(state_law_service.body_of(meeting))
    hints = []
    video = law.value("video_public")
    recording = law.value("recording")
    if video == "local_basis" and not local.video_public_basis:
        hints.append(
            "Die Öffentlichkeit darf die Sitzung per Video nur verfolgen, soweit die Hauptsatzung es zulässt "
            f"({law.norm('video_public')}). Bitte den Nachweis im Ortsrecht der Körperschaft hinterlegen."
        )
    elif video != "local_basis" and recording == "local_basis" and not local.recording_basis:
        hints.append(
            "Bild- und Tonaufnahmen von Mitgliedern sind nur zulässig, soweit die Hauptsatzung es bestimmt "
            f"({law.norm('recording')}). Bitte den Nachweis im Ortsrecht der Körperschaft hinterlegen."
        )
    if law.value("recording_objection", False):
        names = recording_objections(meeting)
        if names:
            hints.append(
                f"Widerspruch gegen Bild- und Tonaufnahmen ({law.norm('recording_objection')}): "
                f"{join_labels(names)} – nicht aufnehmen bzw. übertragen."
            )
    return hints


def recording_objections(meeting: Any) -> list[str]:
    """Mitglieder der beteiligten Gremien mit Widerspruch gegen Bild- und Tonaufnahmen (Namen, sortiert)."""
    from apps.session.models import SessionOrganizationMembership
    from apps.session.services import membership_service, state_law_service

    day = state_law_service.meeting_day(meeting)
    memberships = SessionOrganizationMembership.objects.filter(
        membership_service.active_q(day),
        organization_id__in=meeting.participating_organization_ids,
        person__recording_objection=True,
    ).select_related("person")
    return sorted({membership.person.display_name for membership in memberships})
