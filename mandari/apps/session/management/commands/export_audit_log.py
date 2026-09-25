# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokoll eines Mandanten exportieren (Issue #221) – für Zeiträume, die über die Obergrenze der
Oberfläche (AUDIT_EXPORT_MAX_ROWS) hinausgehen, etwa für eine Prüfinstanz.

    python manage.py export_audit_log --tenant stadt-musterstadt --from 2026-01-01 --to 2026-06-30 \\
        --format zip --output /pfad/zum/verzeichnis

Gibt Dateiname, Anzahl und SHA-256-Prüfsumme aus. Der Export ist selbst ein Protokolleintrag
(Aktion „Protokoll exportiert“, mit Prüfsumme).
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.session import audit
from apps.session.models import SessionTenant
from apps.session.services import audit_log_service


def _date(raw: str | None, name: str) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise CommandError(f"{name}: Datum im Format JJJJ-MM-TT angeben.") from None


class Command(BaseCommand):
    help = "Protokoll eines Session-Mandanten als CSV, JSON oder ZIP mit SHA-256-Prüfsumme exportieren."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", required=True, help="Slug des Mandanten")
        parser.add_argument("--from", dest="date_from", help="Beginn (JJJJ-MM-TT, einschließlich)")
        parser.add_argument("--to", dest="date_to", help="Ende (JJJJ-MM-TT, einschließlich)")
        parser.add_argument("--format", choices=audit_log_service.EXPORT_FORMATS, default="zip")
        parser.add_argument("--output", required=True, help="Zieldatei oder vorhandenes Verzeichnis")

    def handle(self, *args: Any, **options: Any) -> None:
        tenant = SessionTenant.objects.filter(slug=options["tenant"]).first()
        if tenant is None:
            raise CommandError(f"Mandant '{options['tenant']}' nicht gefunden.")
        date_from = _date(options.get("date_from"), "--from")
        date_to = _date(options.get("date_to"), "--to")
        export = audit_log_service.build_export(
            tenant, fmt=options["format"], date_from=date_from, date_to=date_to, requested_by="Kommandozeile"
        )
        target = Path(options["output"])
        if target.is_dir():
            target = target / export.name
        export.file.seek(0)
        with target.open("wb") as handle:
            shutil.copyfileobj(export.file, handle)
        audit.log_event(
            "audit_export",
            tenant,
            tenant=tenant,
            changes={
                "format": options["format"],
                "von": date_from.isoformat() if date_from else None,
                "bis": date_to.isoformat() if date_to else None,
                "anzahl": export.count,
                "datei": export.name,
                "sha256": export.sha256,
                "weg": "Kommandozeile",
            },
        )
        self.stdout.write(f"{target}: {export.count} Einträge, SHA-256 {export.sha256}")
