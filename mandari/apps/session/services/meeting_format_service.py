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
- „nicht vorgesehen“ verhindert das Format mit Begründung.
- „nur in Notlagen“ und jede digitale Sitzung verlangen eine Begründung (Notlage, Beschluss).
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
UNREGULATED_TYPES = frozenset({"faction", "department"})

_FORMAT_PLURAL = {FORMAT_HYBRID: "Hybride Sitzungen", FORMAT_DIGITAL: "Digitale Sitzungen"}


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
    Modell (noch) nicht kennt, werden übergangen. Rückgabe: (angelegt, aktualisiert).
    """
    if model is None:
        from apps.session.models import SessionStateProfile

        model = SessionStateProfile
    known = {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}
    created = updated = 0
    for row in profile_rows(path):
        values = {key: value for key, value in row.items() if key in known and key != "code"}
        _obj, was_created = model.objects.update_or_create(code=row["code"], defaults=values)
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def profile_differences(path: Path | None = None) -> list[str]:
    """Abweichungen zwischen Datenbank und Profildatei (für ``session_state_profiles --check``)."""
    from apps.session.models import SessionStateProfile

    known = {f.name for f in SessionStateProfile._meta.get_fields() if getattr(f, "concrete", False)}
    stored = {p.code: p for p in SessionStateProfile.objects.all()}
    differences = []
    for row in profile_rows(path):
        profile = stored.get(row["code"])
        if profile is None:
            differences.append(f"{row['code']}: fehlt in der Datenbank")
            continue
        changed = sorted(key for key, value in row.items() if key in known and getattr(profile, key) != value)
        if changed:
            differences.append(f"{row['code']}: abweichend ({', '.join(changed)})")
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


def _stricter(first: str, second: str) -> str:
    return first if _STRICTNESS.get(first, 3) >= _STRICTNESS.get(second, 3) else second


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
    excluded = not council and bool(kind) and kind in (profile.excluded_committee_kinds or [])
    if excluded:
        rule = _stricter(rule, profile.excluded_committee_rule)
    norm = {RULE_REGULAR: profile.norm_regular, RULE_EMERGENCY: profile.norm_emergency}.get(rule, "")
    return FormatRule(organization, rule, norm, excluded_kind=excluded)


@dataclass
class FormatCheck:
    """Ergebnis der Prüfung: Fehler verhindern das Speichern, ``rules`` erklären die Rechtslage."""

    errors: list[str] = field(default_factory=list)
    rules: list[FormatRule] = field(default_factory=list)
    needs_reason: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def _add(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)


def check(tenant: Any, organizations: Iterable[Any], meeting_format: str, reason: str = "") -> FormatCheck:
    """Sitzungsformat für die beteiligten Gremien gegen das Landesprofil des Mandanten prüfen."""
    result = FormatCheck()
    if meeting_format == FORMAT_PRESENCE:
        return result
    label = _FORMAT_PLURAL.get(meeting_format, "Hybride oder digitale Sitzungen")
    profile = tenant.state_profile
    if profile is None:
        result._add(
            f"{label} setzen ein Landesprofil voraus. Bitte in den Einstellungen unter „Sitzungsformate“ "
            "das Land wählen und die örtliche Rechtsgrundlage nachweisen."
        )
        return result

    result.rules = [rule_for(profile, org, meeting_format) for org in organizations]
    regulated = [r for r in result.rules if not r.unregulated]
    result.needs_reason = meeting_format == FORMAT_DIGITAL and bool(regulated)
    documented = tenant.hybrid_basis_documented
    basis_label = profile.get_legal_basis_display()
    reason_explained = False

    for item in regulated:
        org_name = item.organization.name
        kind_label = item.organization.get_committee_kind_display() if item.excluded_kind else ""
        if item.rule == RULE_NONE:
            if item.excluded_kind:
                result._add(
                    f"{label} sind für „{org_name}“ ({kind_label}) nach dem Landesprofil {profile.name} "
                    f"ausgeschlossen ({profile.norm_regular or profile.law})."
                )
            else:
                result._add(f"{label} sind für „{org_name}“ nach {profile.law} nicht vorgesehen.")
        elif item.rule == RULE_EMERGENCY:
            result.needs_reason = True
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
    return result


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
        return f"{rule.norm} (Notlage)" if rule.norm else "Notlage"
    if rule.rule == RULE_REGULAR:
        if rule.norm and local:
            return f"{rule.norm} i. V. m. {local}"
        return rule.norm or local
    return local
