# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hochgeladene Anlagen des Profils Einreichung aufräumen (Issue #1228), stündlich als Zeitplan im Worker
(``apps/common/schedules.py``).

Löscht in allen Mandanten Dateien, die bis zu ihrem Ablauf nicht eingereicht wurden, und nach
``UPLOAD_RETENTION`` die Angaben verwendeter Dateien (``apps.session.api.einreichung.purge_uploads``). Ohne
diesen Lauf blieben Dateien liegen, solange bei einem Mandanten niemand mehr hochlädt.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    sperre = "einreichung_aufraeumen"
    sperre_ttl = 1800
    help = "Abgelaufene Anlagen des Profils Einreichung in allen Mandanten löschen (Issue #1228)."

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.session.api.einreichung import purge_uploads

        self.stdout.write(f"Anlagen des Profils Einreichung gelöscht: {purge_uploads()}")
