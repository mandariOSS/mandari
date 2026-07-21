# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zentrale Sichtbarkeitsfunktionen für den nicht-öffentlichen Teil von
Fraktionssitzungen (Issues #64, #66).

Nicht-Vereidigte sehen von NÖ-TOPs NICHTS außer "Gesperrte Information" —
kein Titel, keine Inhalte, keine Anhänge. Alle Ausgabewege (Listen,
Detailansicht, Panel, Protokoll, PDFs, E-Mails, Änderungshistorie)
nutzen diese Funktionen.
"""

# Einziger Text, den Nicht-Vereidigte von einem NÖ-TOP zu sehen bekommen
LOCKED_PLACEHOLDER = "Gesperrte Information"


def can_view_internal(membership) -> bool:
    """
    Darf dieses Mitglied den nicht-öffentlichen Teil sehen?

    Erfordert BEIDES (PermissionChecker.can_access_non_public):
    - Berechtigung faction.view_non_public
    - is_sworn_in-Flag auf der Membership (Vereidigung)
    """
    if membership is None:
        return False
    from apps.common.permissions import PermissionChecker

    return PermissionChecker(membership).can_access_non_public()


def is_item_internal(item) -> bool:
    """Gehört der TOP zum nicht-öffentlichen Teil?"""
    return getattr(item, "visibility", None) == "internal"


def can_view_item(item, membership) -> bool:
    """Darf das Mitglied Inhalte dieses TOPs sehen?"""
    return not is_item_internal(item) or can_view_internal(membership)
