# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter „neues Design“ in Work je Organisation setzen (Ausrollen #884, Schalter aus #852).

Der neue Rahmen von Work steht hinter dem Feld ``Organization.work_new_design``: für neu angelegte Organisationen aus,
die Demo an; in Produktion ist es seit dem 06.10.2026 für alle Organisationen an (Soll-Stand und Ablauf:
docs/WORK_NEUES_DESIGN.md). Der bisherige Rahmen bleibt verfügbar, und „aus“ ist der Rückweg.

Gelesen wird der Schalter überall mit :func:`apps.work.rahmen.neues_design`. Gesetzt wird er im Betrieb nur hier
(Verwaltungsbefehl ``work_neues_design``, Prüfskript): :func:`setzen` ändert genau diese eine Spalte – keine
Einstellungen, keine weiteren Felder der Organisation (auch nicht ``updated_at``) und keine Inhalte.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from apps.tenants.models import Organization

logger = logging.getLogger(__name__)

#: Feld des Schalters an der Organisation (Issue #852)
FELD = "work_new_design"


def setzen(organization: Organization, an: bool) -> bool:
    """
    Schalter setzen; liefert, ob sich etwas geändert hat.

    Liest den Stand unter Sperre neu und schreibt nur die eine Spalte (ohne ``save()``: keine Signale, kein neues
    ``updated_at``, keine veralteten Werte anderer Felder aus der übergebenen Instanz).
    """
    from apps.tenants.models import Organization

    with transaction.atomic():
        bisher = Organization.objects.select_for_update().filter(pk=organization.pk).values_list(FELD, flat=True).get()
        organization.work_new_design = an
        if bool(bisher) == an:
            return False
        Organization.objects.filter(pk=organization.pk).update(work_new_design=an)
    logger.info("Work-Design umgeschaltet (organisation=%s, neues_design=%s)", organization.pk, an)
    return True
