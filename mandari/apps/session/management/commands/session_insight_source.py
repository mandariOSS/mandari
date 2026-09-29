# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session-Mandanten als OParl-Quelle für das Insight-Bürgerportal
registrieren bzw. deaktivieren (Issue #36).

Normalfall: Die Registrierung geschieht automatisch, sobald der Mandant
den Veröffentlichungs-Schalter aktiviert (Settings -> Bürgerportal).
Dieser Befehl ist das CLI-Gegenstück für Betrieb/Provisioning:

    # Quelle für einen Mandanten registrieren (setzt insight_publish)
    python manage.py session_insight_source --tenant musterstadt

    # Alle bereits veröffentlichten Mandanten (nach)registrieren
    python manage.py session_insight_source --all

    # Quelle deaktivieren (setzt insight_publish zurück; Bestand bleibt ohne Hinweis)
    python manage.py session_insight_source --tenant musterstadt --deactivate

    # Veröffentlichung beenden mit Auswahl, was mit dem Bestand geschieht (Issue #618)
    python manage.py session_insight_source --tenant musterstadt --deactivate --mode archived

    # Abweichende Basis-URL (z. B. lokale Instanz)
    python manage.py session_insight_source --tenant musterstadt --base-url http://localhost:8000
"""

from django.core.management.base import BaseCommand, CommandError

from apps.session.models import SessionTenant
from apps.session.services import insight_service


class Command(BaseCommand):
    help = "Registriert Session-Mandanten als OParl-Quelle für das Insight-Bürgerportal (Issue #36)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Slug des Session-Mandanten")
        parser.add_argument("--all", action="store_true", help="Alle Mandanten mit aktivem Veröffentlichungs-Schalter")
        parser.add_argument("--deactivate", action="store_true", help="Quelle deaktivieren statt registrieren")
        parser.add_argument(
            "--mode",
            choices=[key for key, _ in SessionTenant.PORTAL_END_CHOICES],
            help="Mit --deactivate: paused (vorübergehend), archived (Archiv) oder withdrawn (dauerhaft zurücknehmen)",
        )
        parser.add_argument("--base-url", help="Basis-URL der Instanz (Standard: SITE_URL)")

    def handle(self, *args, **options):
        base_url = options.get("base_url")

        if options["all"]:
            tenants = SessionTenant.objects.filter(is_active=True, insight_publish=True)
            if not tenants:
                self.stdout.write("Keine Mandanten mit aktiver Veröffentlichung gefunden.")
                return
            for tenant in tenants:
                source, created = insight_service.register_source(tenant, base_url)
                state = "registriert" if created else "aktualisiert"
                self.stdout.write(self.style.SUCCESS(f"{tenant.slug}: Quelle {state} -> {source.url}"))
            return

        slug = options.get("tenant")
        if not slug:
            raise CommandError("Bitte --tenant <slug> oder --all angeben.")
        tenant = SessionTenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"Session-Mandant '{slug}' nicht gefunden.")

        if options.get("mode") and not options["deactivate"]:
            raise CommandError("--mode gilt nur zusammen mit --deactivate.")
        if options["deactivate"] and options.get("mode"):
            from apps.session.services import portal_publication

            change = portal_publication.end_publication(tenant, options["mode"])
            label = portal_publication.state_label(tenant)
            wirkung = f" ({change.entries} Einträge zurückgenommen)" if change and change.entries else ""
            self.stdout.write(self.style.SUCCESS(f"{tenant.slug}: Veröffentlichung beendet – {label}{wirkung}."))
            return

        if options["deactivate"]:
            # Schalter zurücksetzen — das Signal deaktiviert die Quelle
            if tenant.insight_publish:
                tenant.insight_publish = False
                tenant.save(update_fields=["insight_publish", "updated_at"])
            else:
                insight_service.deactivate_source(tenant, base_url)
            self.stdout.write(self.style.SUCCESS(f"{tenant.slug}: Veröffentlichung beendet, Quelle deaktiviert."))
            return

        # Schalter setzen — das Signal registriert die Quelle (Standard-URL);
        # bei abweichender Basis-URL zusätzlich explizit registrieren.
        if not tenant.insight_publish:
            tenant.insight_publish = True
            tenant.insight_end_mode = ""
            tenant.save(update_fields=["insight_publish", "insight_end_mode", "updated_at"])
        source, created = insight_service.register_source(tenant, base_url)
        state = "registriert" if created else "bereits registriert (aktualisiert)"
        self.stdout.write(self.style.SUCCESS(f"{tenant.slug}: Quelle {state} -> {source.url}"))
