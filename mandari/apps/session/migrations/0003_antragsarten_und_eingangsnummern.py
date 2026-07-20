# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Issue #24: Antragsarten-Mismatch beheben + Eingangsnummern absichern.

1. Datenmigration: Über die alte API eingereichte ungültige Antragsarten
   (``proposal``, ``urgent_motion``) werden auf gültige Model-Choices gemappt;
   alle sonstigen unbekannten Werte werden zu ``other``.
2. Doppelte Eingangsnummern (Race der alten Vergabe) werden dedupliziert,
   danach sichert ein UniqueConstraint (tenant, reference) die Vergabe ab.
"""

from django.db import migrations, models


def _fix_application_types(apps, schema_editor):
    SessionApplication = apps.get_model("session", "SessionApplication")

    valid_types = {"motion", "inquiry", "resolution", "urgent", "amendment", "other"}
    aliases = {"proposal": "motion", "urgent_motion": "urgent"}

    for app_obj in SessionApplication.objects.exclude(application_type__in=valid_types):
        app_obj.application_type = aliases.get(app_obj.application_type, "other")
        app_obj.save(update_fields=["application_type"])


def _dedupe_references(apps, schema_editor):
    """Doppelte Eingangsnummern je Tenant neu vergeben (älteste behält die Nummer)."""
    SessionApplication = apps.get_model("session", "SessionApplication")

    seen: dict[tuple, object] = {}
    duplicates = []
    qs = SessionApplication.objects.exclude(reference="").order_by("submitted_at", "id")
    for app_obj in qs:
        key = (app_obj.tenant_id, app_obj.reference)
        if key in seen:
            duplicates.append(app_obj)
        else:
            seen[key] = app_obj.id

    for app_obj in duplicates:
        # Nächste freie Nummer im Jahres-Präfix der bestehenden Referenz suchen
        prefix = app_obj.reference.rsplit("/", 1)[0] + "/"
        max_num = 0
        refs = SessionApplication.objects.filter(
            tenant_id=app_obj.tenant_id,
            reference__startswith=prefix,
        ).values_list("reference", flat=True)
        for ref in refs:
            try:
                max_num = max(max_num, int(ref.rsplit("/", 1)[-1]))
            except (TypeError, ValueError):
                continue
        app_obj.reference = f"{prefix}{max_num + 1:04d}"
        app_obj.save(update_fields=["reference"])


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0002_sessionperson_verschluesselung"),
    ]

    operations = [
        migrations.RunPython(_fix_application_types, migrations.RunPython.noop),
        migrations.RunPython(_dedupe_references, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="sessionapplication",
            constraint=models.UniqueConstraint(
                condition=models.Q(("reference", ""), _negated=True),
                fields=("tenant", "reference"),
                name="uniq_session_application_reference",
            ),
        ),
    ]
