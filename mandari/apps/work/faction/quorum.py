# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfähigkeit (Quorum) für Fraktionssitzungen (Issue #69).

Standardregel: beschlussfähig, wenn MEHR als 50 % der stimmberechtigten
(Rats-)Mitglieder anwesend sind. Stimmberechtigt sind aktive Mitglieder
mit der Berechtigung ``voting.participate`` aus einer Rolle, die sie
enthält, oder als Einzelrecht; die Administrator-Rolle allein gibt kein
Stimmrecht (Issue #1166).

Berechnung über den gemeinsamen Baustein :mod:`apps.common.quorum`
(generalisiert aus dem Session-RIS). Andere Regeln gibt es noch nicht —
die Organisations-Einstellung ``quorum_rule`` ist nur ein
Datenfeld/Erweiterungspunkt (Default "majority").
"""

from apps.common.quorum import DEFAULT_QUORUM_RULE, quorum_status


def get_quorum_rule(ziel) -> str:
    """
    Quorum-Regel einer Sitzung oder Sitzungsart, je Art übersteuerbar (Issue #1240); mit einer Organisation die der
    Organisation selbst (Erweiterungspunkt, Default Mehrheitsregel).
    """
    from .regeln import einstellungen

    return einstellungen(ziel).get("quorum_rule", DEFAULT_QUORUM_RULE)


def stimmberechtigt(membership) -> bool:
    """
    Zählt die Person als stimmberechtigt? Aktives Mitglied mit Stimmrecht (``PermissionChecker``).

    Dieselbe Regel für Beschlussfähigkeit und die Gruppe „Stimmberechtigt“ der Anwesenheitsliste.
    """
    from apps.common.permissions import STIMMRECHT, PermissionChecker

    return bool(
        membership is not None and membership.is_active and PermissionChecker(membership).has_permission(STIMMRECHT)
    )


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
    # Rechte vorgeladen: die Prüfung „stimmberechtigt“ fragt nicht je Teilnahme nach (Issue #874)
    attendances = list(
        meeting.attendances.filter(is_guest=False, membership__isnull=False)
        .select_related("membership")
        .prefetch_related(
            "membership__roles__permissions", "membership__individual_permissions", "membership__denied_permissions"
        )
    )
    voting = [a for a in attendances if stimmberechtigt(a.membership)]
    present = [a for a in voting if a.status == "present"]

    return quorum_status(
        voting_total=len(voting),
        voting_present=len(present),
        has_list=bool(attendances),
        rule=get_quorum_rule(meeting),
    )


def quorum_der_sitzung(meeting) -> dict | None:
    """
    Beschlussfähigkeit zur Anzeige: ``None`` in einer Sitzungsart ohne Abstimmungen und Beschlussfähigkeit
    (Issue #1240), sonst :func:`faction_quorum_status`.
    """
    from .regeln import abstimmungen_erlaubt

    if not abstimmungen_erlaubt(meeting):
        return None
    return faction_quorum_status(meeting)
