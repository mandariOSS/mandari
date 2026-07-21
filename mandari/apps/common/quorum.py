# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsamer Quorum-Baustein (Issue #69).

Generalisiert aus der Session-RIS-Beschlussfähigkeit (Issue #30,
apps/session/services/attendance_service.py) — Session-RIS und
Fraktionssitzungen nutzen dieselbe Berechnung.

Standardregel "majority": beschlussfähig, wenn MEHR als die Hälfte der
stimmberechtigten Mitglieder anwesend ist (genau 50 % reicht nicht).

Andere Regeln sind bewusst noch nicht implementiert — die Einstellung
``quorum_rule`` existiert nur als Datenfeld/Erweiterungspunkt.
"""

# Implementierte Quorum-Regeln (Erweiterungspunkt für spätere Regeln)
QUORUM_RULES = ("majority",)
DEFAULT_QUORUM_RULE = "majority"


def quorum_status(
    *, voting_total: int, voting_present: int, has_list: bool = True, rule: str = DEFAULT_QUORUM_RULE
) -> dict:
    """
    Beschlussfähigkeit (Quorum) berechnen.

    Args:
        voting_total: Anzahl stimmberechtigter Mitglieder in der Liste
        voting_present: davon anwesend
        has_list: gibt es überhaupt eine Anwesenheitsliste?
        rule: Quorum-Regel (aktuell nur "majority" implementiert;
              unbekannte Werte fallen auf die Mehrheitsregel zurück)

    Returns:
        dict: has_list, voting_total, voting_present, required, met, rule
    """
    if rule not in QUORUM_RULES:
        rule = DEFAULT_QUORUM_RULE

    # Mehrheitsregel: mehr als 50 % der Stimmberechtigten
    required = (voting_total // 2) + 1 if voting_total else 0

    return {
        "has_list": has_list,
        "voting_total": voting_total,
        "voting_present": voting_present,
        "required": required,
        "met": voting_total > 0 and voting_present >= required,
        "rule": rule,
    }
