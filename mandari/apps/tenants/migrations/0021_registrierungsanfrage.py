# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Offene Selbstregistrierungen eindeutig kennzeichnen.

Bisher galt jede inaktive Mitgliedschaft ohne angenommene Einladung als offene Anfrage –
auch deaktivierte Mitglieder und Konten mit Löschwunsch. Für den Bestand werden nur
Mitgliedschaften übernommen, die seit dem Anlegen nie gespeichert wurden (keine Deaktivierung,
keine Bearbeitung), nicht eingeladen wurden und keine Gäste sind.
"""

from datetime import timedelta

from django.db import migrations, models

UNVERAENDERT_SEIT_ANLAGE = timedelta(seconds=60)


def offene_anfragen_markieren(apps, schema_editor):
    Membership = apps.get_model("tenants", "Membership")
    kandidaten = Membership.objects.filter(
        is_active=False,
        is_guest=False,
        invitation_accepted_at__isnull=True,
        invited_by__isnull=True,
    )
    for membership in kandidaten.iterator():
        if membership.updated_at - membership.joined_at <= UNVERAENDERT_SEIT_ANLAGE:
            Membership.objects.filter(pk=membership.pk).update(registration_requested_at=membership.joined_at)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0020_eigene_absender_mail"),
    ]

    operations = [
        migrations.AddField(
            model_name="membership",
            name="registration_requested_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Registrierung angefragt"),
        ),
        migrations.RunPython(offene_anfragen_markieren, migrations.RunPython.noop),
    ]
