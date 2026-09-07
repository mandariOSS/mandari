# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Betriebsmonitor — Quellen prüfen und Alarme verschicken.

Cronjob (stündlich):
    python manage.py check_source_health
Nur Bericht ausgeben:
    python manage.py check_source_health --report
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Bewertet OParl-Quellen und Systemdienste; verschickt Alarme und Entwarnungen per E-Mail"

    def add_arguments(self, parser):
        parser.add_argument("--report", action="store_true", help="Statusbericht ausgeben, keine Alarme senden")
        parser.add_argument("--dry-run", action="store_true", help="Alarme nur anzeigen, nicht senden")

    def handle(self, *args, **options):
        from insight_core.services.source_health import collect_health, send_health_alerts

        health = collect_health()
        self.stdout.write(f"Gesamtstatus: {health['overall_meta']['label']}")
        for check in health["system"]:
            self.stdout.write(f"  [{check['label']:8}] {check['name']}: {check['detail']}")
        for item in health["sources"]["items"]:
            source = item["source"]
            reasons = "; ".join(item["reasons"]) or "-"
            self.stdout.write(f"  [{item['label']:8}] {source.name} — {reasons}")
        for action in health["actions"]:
            self.stdout.write(f"  Handlungsbedarf: {action['count']} × {action['label']}")

        if options["report"]:
            return

        result = send_health_alerts(dry_run=options["dry_run"])
        verb = "fällig" if options["dry_run"] else "gesendet"
        self.stdout.write(
            self.style.SUCCESS(
                f"Alarme {verb}: {len(result['alerts'])}, Entwarnungen {verb}: {len(result['recoveries'])}"
                + (", Daemon-Alarm" if result["daemon_alert"] else "")
            )
        )
