# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO-Anonymisierungs-/Löschlauf für das Session RIS (Issue #43).

Wendet die je Mandant konfigurierten Aufbewahrungsfristen an
(Einstellungen -> Datenschutz):

- Kontakt-/Bankdaten ausgeschiedener Mandatsträger anonymisieren
  (der Name bleibt für historische Beschlüsse erhalten)
- Nicht-öffentliche Inhalte (NÖ-Protokollteil, interne Notizen) leeren
- Audit-Log-Einträge nach Fristablauf löschen

Jeder Lauf wird nachweisbar im Audit-Log dokumentiert.

Aufrufe:
    python manage.py session_privacy_purge                 # alle Mandanten
    python manage.py session_privacy_purge --tenant stadt  # ein Mandant
    python manage.py session_privacy_purge --dry-run       # nur zählen
"""

from django.core.management.base import BaseCommand, CommandError

from apps.session.models import SessionTenant
from apps.session.services import privacy_service


class Command(BaseCommand):
    help = "DSGVO-Löschlauf: Fristen anwenden, ausgeschiedene Mandatsträger anonymisieren (auditiert)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Slug eines einzelnen Mandanten (Default: alle aktiven)")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur zählen, nichts verändern",
        )

    def handle(self, *args, **options):
        if options.get("tenant"):
            tenants = SessionTenant.objects.filter(slug=options["tenant"])
            if not tenants.exists():
                raise CommandError(f"Mandant '{options['tenant']}' nicht gefunden.")
        else:
            tenants = SessionTenant.objects.filter(is_active=True)

        dry_run = options.get("dry_run", False)
        for tenant in tenants:
            stats = privacy_service.run_privacy_purge(tenant, dry_run=dry_run)
            prefix = "[DRY-RUN] " if dry_run else ""
            self.stdout.write(
                f"{prefix}{tenant.name}: "
                f"{stats['persons_anonymized']} Person(en) anonymisiert, "
                f"{stats['np_meetings_cleared']} Sitzung(en) NÖ-Inhalte geleert, "
                f"{stats['audit_deleted']} Audit-Eintrag/-Einträge gelöscht"
                + (f" — übersprungen: {', '.join(stats['skipped'])}" if stats["skipped"] else "")
            )
