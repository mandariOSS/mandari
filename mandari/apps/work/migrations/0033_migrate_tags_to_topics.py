# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration: bestehende Motion.tags (JSON-Strings) je Organisation in
tenants.Topic überführen und als M2M zuordnen.

Das JSON-Feld `tags` bleibt erhalten (deprecated, ungenutzt).
Farben werden rotierend aus einer kleinen Palette vergeben.
"""

from django.db import migrations

COLOR_PALETTE = ["blue", "green", "amber", "purple", "teal", "pink", "orange", "indigo"]


def migrate_tags_to_topics(apps, schema_editor):
    Motion = apps.get_model("work", "Motion")
    Topic = apps.get_model("tenants", "Topic")

    # Bestehende Topics je Org als Cache (Name -> Topic)
    topic_cache = {}
    org_counters = {}

    motions = Motion.objects.exclude(tags=[]).exclude(tags__isnull=True).only("id", "tags", "organization_id")
    for motion in motions.iterator():
        if not isinstance(motion.tags, list):
            continue
        for raw_tag in motion.tags:
            name = str(raw_tag).strip()[:100]
            if not name:
                continue
            key = (motion.organization_id, name.lower())
            topic = topic_cache.get(key)
            if topic is None:
                topic = Topic.objects.filter(organization_id=motion.organization_id, name__iexact=name).first()
                if topic is None:
                    counter = org_counters.get(motion.organization_id)
                    if counter is None:
                        counter = Topic.objects.filter(organization_id=motion.organization_id).count()
                    color = COLOR_PALETTE[counter % len(COLOR_PALETTE)]
                    topic = Topic.objects.create(
                        organization_id=motion.organization_id,
                        name=name,
                        color=color,
                        sort_order=counter,
                    )
                    org_counters[motion.organization_id] = counter + 1
                topic_cache[key] = topic
            motion.topics.add(topic)


def reverse_noop(apps, schema_editor):
    """Rückwärts: Topics behalten, nichts löschen."""


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0032_antragsdatenbank_tracking"),
        ("tenants", "0015_antragsdatenbank_tracking"),
    ]

    operations = [
        migrations.RunPython(migrate_tags_to_topics, reverse_noop),
    ]
