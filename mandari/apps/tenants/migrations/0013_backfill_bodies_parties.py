# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Data migration: Bestehende FK-Verknüpfungen in die neuen M2M-Felder spiegeln.

- Organization.body (primäre Kommune) -> Organization.bodies
- Organization.party_group (primäre Parteigruppe) -> Organization.parties

Die FKs bleiben als "primäre" Verknüpfung erhalten; die M2M-Felder sind ab
jetzt die maßgebliche Quelle für Multi-Kommune-/Multi-Partei-Zugehörigkeit
(Helper Organization.get_all_bodies()/get_all_parties() vereinigen beide).
"""

from django.db import migrations


def backfill_forward(apps, schema_editor):
    Organization = apps.get_model("tenants", "Organization")

    for org in Organization.objects.all().iterator():
        if org.body_id:
            org.bodies.add(org.body_id)
        if org.party_group_id:
            org.parties.add(org.party_group_id)


def backfill_reverse(apps, schema_editor):
    """Rückwärts: M2M-Einträge entfernen, die dem FK entsprechen (Rest bleibt)."""
    Organization = apps.get_model("tenants", "Organization")

    for org in Organization.objects.all().iterator():
        if org.body_id:
            org.bodies.remove(org.body_id)
        if org.party_group_id:
            org.parties.remove(org.party_group_id)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0012_organization_bodies_parties"),
    ]

    operations = [
        migrations.RunPython(backfill_forward, backfill_reverse),
    ]
