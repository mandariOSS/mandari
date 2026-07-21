# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfähigkeit (Quorum) für Fraktionssitzungen (Issue #69).

Standardregel: beschlussfähig, wenn MEHR als 50 % der stimmberechtigten
(Rats-)Mitglieder anwesend sind. Stimmberechtigt sind aktive Mitglieder
mit der Berechtigung ``voting.participate``.

Berechnung über den gemeinsamen Baustein :mod:`apps.common.quorum`
(generalisiert aus dem Session-RIS). Andere Regeln gibt es noch nicht —
die Organisations-Einstellung ``quorum_rule`` ist nur ein
Datenfeld/Erweiterungspunkt (Default "majority").
"""

from apps.common.quorum import DEFAULT_QUORUM_RULE, quorum_status


def get_quorum_rule(organization) -> str:
    """Quorum-Regel der Organisation (Erweiterungspunkt, Default Mehrheitsregel)."""
    return ((organization.settings or {}).get("faction", {})).get("quorum_rule", DEFAULT_QUORUM_RULE)


def faction_quorum_status(meeting) -> dict:
    """
    Beschlussfähigkeit einer Fraktionssitzung live berechnen.

    Grundlage: Teilnahme-Zeilen der Sitzung (ohne Gäste). Stimmberechtigt
    zählt, wer als aktives Mitglied die Berechtigung voting.participate
    hat; anwesend zählt der Status "present" (Teilnahmeart vor Ort/online
    ist gleichwertig — digitale Teilnahme gilt als Vollteilnahme, #67).

    Returns:
        dict: has_list, voting_total, voting_present, required, met, rule
    """
    from apps.common.permissions import PermissionChecker

    attendances = list(
        meeting.attendances.filter(is_guest=False, membership__isnull=False).select_related("membership")
    )
    voting = [
        a
        for a in attendances
        if a.membership.is_active and PermissionChecker(a.membership).has_permission("voting.participate")
    ]
    present = [a for a in voting if a.status == "present"]

    return quorum_status(
        voting_total=len(voting),
        voting_present=len(present),
        has_list=bool(attendances),
        rule=get_quorum_rule(meeting.organization),
    )
