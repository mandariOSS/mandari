# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollen, Rechte und Zugänge in der Fraktions-Änderungshistorie (Issue #366).

Rollen und Einzelrechte hängen über M2M-Beziehungen an Mitgliedschaft und Rolle; ``set()`` löst kein
``post_save`` aus, und die Modelle liegen in ``apps.tenants`` außerhalb der Signale der Fraktionssitzungen.
Die Dienste der Mitgliederverwaltung (:mod:`apps.work.organization.services`) schreiben deshalb selbst einen
Eintrag, wie ``apps.session.audit.log_role_assignment`` im Session RIS. Ohne Unterschied entsteht keiner.

Format wie die übrige Änderungshistorie: je Angabe ``{"alt": …, "neu": …}``. Rechte erscheinen als Kennungen
(``members.edit``), Rollen und Personen mit Namen; Passwörter, Tokens oder Inhalte nie. Handelnde Person,
Adresse und Browser ergänzt :func:`apps.work.faction.audit.log_event` aus dem Request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.work.faction import audit as faction_audit

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Role

#: Angabe für einen Zustand ohne Werte (keine Rolle, keine Rechte)
NONE = ""


def _text(values: Iterable[str]) -> str:
    return ", ".join(sorted(set(values)))


def _yes_no(value: bool) -> str:
    return "ja" if value else "nein"


def _diff(changes: dict[str, Any], label: str, old: str, new: str) -> None:
    if old != new:
        changes[label] = {"alt": old, "neu": new}


def _with_reason(changes: dict[str, Any], reason: str) -> dict[str, Any]:
    if reason:
        changes["Anlass"] = {"alt": NONE, "neu": reason}
    return changes


def member_repr(member: Membership) -> str:
    """Objekt-Beschreibung: Person der Mitgliedschaft (Name, ersatzweise E-Mail)."""
    user = member.user
    return f"Mitgliedschaft {user.get_full_name() or user.email}"


def _log_member(action: str, member: Membership, changes: dict[str, Any], actor: Membership | None) -> Any:
    return faction_audit.log_event(
        action,
        member,
        organization=member.organization,
        membership=actor,
        changes=changes,
        is_internal=False,
        object_repr=member_repr(member),
    )


def role_names(roles: Iterable[Role]) -> list[str]:
    return [role.name for role in roles]


def log_member_roles(
    member: Membership,
    old_roles: Iterable[str],
    new_roles: Iterable[str],
    *,
    actor: Membership | None = None,
    reason: str = "",
) -> Any:
    """Rollen einer Mitgliedschaft vorher und nachher (Namen); ohne Unterschied kein Eintrag."""
    changes: dict[str, Any] = {}
    _diff(changes, "Rollen", _text(old_roles), _text(new_roles))
    if not changes:
        return None
    return _log_member("update", member, _with_reason(changes, reason), actor)


def log_member_permissions(
    member: Membership,
    *,
    old_individual: Iterable[str],
    new_individual: Iterable[str],
    old_denied: Iterable[str],
    new_denied: Iterable[str],
    actor: Membership | None = None,
    reason: str = "",
) -> Any:
    """Zusätzliche und verweigerte Einzelrechte (Kennungen) vorher und nachher; ohne Unterschied kein Eintrag."""
    changes: dict[str, Any] = {}
    _diff(changes, "Zusätzliche Rechte", _text(old_individual), _text(new_individual))
    _diff(changes, "Verweigerte Rechte", _text(old_denied), _text(new_denied))
    if not changes:
        return None
    return _log_member("update", member, _with_reason(changes, reason), actor)


def log_member_access(
    member: Membership,
    old: str,
    new: str,
    *,
    action: str = "update",
    roles: Iterable[str] = (),
    actor: Membership | None = None,
    reason: str = "",
) -> Any:
    """
    Zugang einer Mitgliedschaft geändert, z. B. „aktiv“ → „deaktiviert“; beim Entfernen (``action="delete"``)
    stehen die bisherigen Rollen mit im Eintrag.
    """
    changes: dict[str, Any] = {}
    _diff(changes, "Zugang", old, new)
    if action == "delete":
        _diff(changes, "Rollen", _text(roles), NONE)
    if not changes:
        return None
    return _log_member(action, member, _with_reason(changes, reason), actor)


def log_sworn_in(member: Membership, old: bool, new: bool, *, actor: Membership | None = None) -> Any:
    """Vereidigung (schaltet nichtöffentliche Inhalte frei); ohne Unterschied kein Eintrag."""
    changes: dict[str, Any] = {}
    _diff(changes, "Vereidigt", _yes_no(old), _yes_no(new))
    if not changes:
        return None
    return _log_member("update", member, changes, actor)


@dataclass(frozen=True)
class RoleState:
    """Stand einer Rolle für die Änderungshistorie."""

    name: str
    is_admin: bool
    codes: frozenset[str]


def role_state(role: Role) -> RoleState:
    """Stand aus der Datenbank (Rechte als Kennungen)."""
    return RoleState(role.name, bool(role.is_admin), frozenset(role.permissions.values_list("codename", flat=True)))


def log_role(
    action: str,
    role: Role,
    before: RoleState | None,
    after: RoleState | None,
    *,
    actor: Membership | None = None,
    reason: str = "",
) -> Any:
    """
    Rolle angelegt (``before`` leer), geändert oder gelöscht (``after`` leer): Name, Administrator und Rechte.

    Beim Ändern stehen erteilte und entzogene Rechte getrennt im Eintrag statt der ganzen Liste.
    """
    changes: dict[str, Any] = {}
    if before is not None and after is not None:
        _diff(changes, "Name", before.name, after.name)
        _diff(changes, "Administrator", _yes_no(before.is_admin), _yes_no(after.is_admin))
        _diff(changes, "Rechte erteilt", NONE, _text(after.codes - before.codes))
        _diff(changes, "Rechte entzogen", _text(before.codes - after.codes), NONE)
    else:
        # Angelegt oder gelöscht: der ganze Stand auf einer Seite
        state = after or before or RoleState(role.name, bool(role.is_admin), frozenset())
        values = {"Name": state.name, "Administrator": _yes_no(state.is_admin), "Rechte": _text(state.codes)}
        for label, value in values.items():
            old, new = (NONE, value) if before is None else (value, NONE)
            _diff(changes, label, old, new)
    if not changes:
        return None
    name = (after or before or RoleState(role.name, False, frozenset())).name
    return faction_audit.log_event(
        action,
        role,
        organization=role.organization,
        membership=actor,
        changes=_with_reason(changes, reason),
        is_internal=False,
        object_repr=f"Rolle „{name}“",
    )
