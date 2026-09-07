# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Personenfotos aus den RIS lokal zwischenspeichern.

Cronjob (wöchentlich reicht):
    python manage.py fetch_person_photos
Einzelne Kommune, alles neu laden:
    python manage.py fetch_person_photos --body muenster --force
"""

from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Lädt Personenfotos aus den Ratsinformationssystemen und speichert sie lokal"

    def add_arguments(self, parser):
        parser.add_argument("--body", help="Kommune (Slug, Name-Teil oder UUID); Standard: alle")
        parser.add_argument("--limit", type=int, default=500, help="Max. Personen je Kommune und Lauf")
        parser.add_argument("--force", action="store_true", help="Auch bereits geprüfte Personen neu laden")
        parser.add_argument("--max-age-days", type=int, default=30, help="Erneut prüfen, wenn älter als N Tage")
        parser.add_argument("--sleep", type=float, default=0.1, help="Pause zwischen Abrufen (Sekunden)")

    def handle(self, *args, **options):
        from insight_core.models import OParlBody
        from insight_core.services.person_photos import apply_photo_presets, fetch_photos_for_body

        bodies = OParlBody.objects.filter(deleted=False).select_related("source")
        if options["body"]:
            key = options["body"]
            body_filter = Q(slug=key) | Q(name__icontains=key)
            if len(key) >= 32:  # UUID
                body_filter |= Q(id=key)
            bodies = bodies.filter(body_filter)

        total = 0
        for body in bodies:
            if apply_photo_presets(body):
                self.stdout.write(f"  Foto-Konfiguration für „{body.name}“ aus Preset gesetzt")
            if not (body.person_photo_url_template and body.person_photo_id_pattern):
                self.stdout.write(f"- {body.name}: keine Foto-Konfiguration, übersprungen")
                continue
            results = fetch_photos_for_body(
                body,
                limit=options["limit"],
                force=options["force"],
                max_age_days=options["max_age_days"],
                sleep=options["sleep"],
            )
            summary = ", ".join(f"{k}={v}" for k, v in sorted(results.items())) or "nichts zu tun"
            self.stdout.write(f"- {body.name}: {summary}")
            total += results.get("ok", 0)

        self.stdout.write(self.style.SUCCESS(f"Fertig: {total} Foto(s) gespeichert"))
