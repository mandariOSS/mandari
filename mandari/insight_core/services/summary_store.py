# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gespeicherte KI-Zusammenfassungen der Vorgänge (``OParlPaper.summary``) beim Eigentümer des RIS-Bestands.

Erzeugt werden Zusammenfassungen auf Abruf (``insight_ai.services.summarizer``); verworfen werden sie, wenn sich
ihre Grundlage ändert: bei Rücknahme einer Anlage (``SourceDeletionModel._forget_summaries``), im Löschabgleich
(``file_reconcile``) und nach neuem Text einer Anlage (Abonnement ``insight.zusammenfassung``, Issue #919). Das
Abonnement liegt bei den KI-Funktionen und schreibt über diese Funktion, nicht selbst in den Bestand
(``scripts/check_ris_access_ratchet.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from ..models import OParlFile, OParlPaper


def discard_for_files(file_ids: Iterable[uuid.UUID], *, dry_run: bool = False) -> int:
    """
    Verwirft die Zusammenfassungen der Vorgänge dieser Dateien; Rückgabe: ihre Zahl (mit ``dry_run`` nur gezählt).

    Dateien ohne Vorgang (Sitzungsdokumente), unbekannte Kennungen und Vorgänge ohne Zusammenfassung zählen nicht.
    Ein ``UPDATE`` je Aufruf; idempotent.
    """
    kennungen = list(file_ids)
    if not kennungen:
        return 0
    vorgaenge = OParlFile.objects.filter(pk__in=kennungen, paper_id__isnull=False).values("paper_id")
    betroffen = OParlPaper.objects.filter(pk__in=vorgaenge, summary__isnull=False)
    return betroffen.count() if dry_run else betroffen.update(summary=None)
