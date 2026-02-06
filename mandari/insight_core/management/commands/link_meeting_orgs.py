"""Management command to link meetings with their organizations based on raw_json data."""

from django.core.management.base import BaseCommand

from insight_core.models import OParlMeeting, OParlOrganization


class Command(BaseCommand):
    help = "Link meetings with organizations based on raw_json organization references"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Don't actually make changes, just show what would be done",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Build a lookup of organization external_id -> OParlOrganization
        org_lookup = {}
        for org in OParlOrganization.objects.all():
            org_lookup[org.external_id] = org

        self.stdout.write(f"Found {len(org_lookup)} organizations")

        meetings_updated = 0
        links_created = 0

        # Process all meetings
        meetings = OParlMeeting.objects.all()
        total = meetings.count()

        self.stdout.write(f"Processing {total} meetings...")

        for i, meeting in enumerate(meetings.iterator()):
            if i % 100 == 0:
                self.stdout.write(f"  Progress: {i}/{total}")

            # Get organization references from raw_json
            raw = meeting.raw_json or {}
            org_refs = raw.get("organization", [])

            if not org_refs:
                continue

            # Ensure it's a list
            if isinstance(org_refs, str):
                org_refs = [org_refs]

            # Find matching organizations
            orgs_to_link = []
            for org_ref in org_refs:
                if org_ref in org_lookup:
                    orgs_to_link.append(org_lookup[org_ref])

            if orgs_to_link:
                if not dry_run:
                    # Clear existing and add new
                    meeting.organizations.set(orgs_to_link)

                meetings_updated += 1
                links_created += len(orgs_to_link)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: Would update {meetings_updated} meetings with {links_created} organization links"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Updated {meetings_updated} meetings with {links_created} organization links")
            )
