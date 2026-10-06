# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter je Organisation für das neue Erscheinungsbild von Work (#852).

Übergang: Den Schalter selbst legt die Arbeit am neuen Rahmen (#852) an. Bis dahin liest diese
Hilfsfunktion das gleichnamige Feld der Organisation, sobald es existiert, und ist sonst aus. Seiten
im neuen Design (zuerst die Sitzungsvorbereitung, #856) fragen nur diese Funktion; beim Zusammenführen
mit #852 wird sie auf dessen Schalter umgestellt, die Aufrufer bleiben unverändert.
"""

from __future__ import annotations

from typing import Any

#: Feldname des Schalters an ``tenants.Organization`` (Standard aus, Demo an)
SCHALTER_FELD = "work_neues_design"


def neues_design_aktiv(organization: Any) -> bool:
    """Zeigt Work für diese Organisation im neuen Design? Ohne Organisation oder Feld: nein."""
    if organization is None:
        return False
    return bool(getattr(organization, SCHALTER_FELD, False))
