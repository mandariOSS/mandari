# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Data-Migration: Partei-Pflichtfeld (party_group) aus Bestandsdaten setzen.

Jede Organisation gehört genau EINER Partei an. Für Organisationen ohne
party_group wird — falls vorhanden — die erste verknüpfte Partei aus dem
M2M `parties` übernommen. Der body-Backfill ist bereits in Migration 0013
erfolgt. Idempotent: bereits gesetzte Werte werden nie überschrieben.
"""

from django.db import migrations


def backfill_party_group(apps, schema_editor):
    Organization = apps.get_model("tenants", "Organization")

    updated = 0
    for org in Organization.objects.filter(party_group__isnull=True).prefetch_related("parties"):
        first_party = org.parties.order_by("name").first()
        if first_party is not None:
            org.party_group = first_party
            org.save(update_fields=["party_group"])
            updated += 1

    if updated:
        print(f"\n[tenants.0018] party_group aus parties übernommen: {updated} Organisation(en)")


def noop(apps, schema_editor):
    """Rückwärts: nichts tun — gesetzte party_group-Werte bleiben erhalten."""


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0017_gastzugaenge_und_pflichtzuordnung"),
    ]

    operations = [
        migrations.RunPython(backfill_party_group, noop),
    ]
