# Generated manually

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0008_alter_organization_ai_api_key_encrypted_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="registration_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Nutzer können sich selbst für diese Organisation registrieren",
                verbose_name="Selbstregistrierung aktiviert",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="registration_email_domains",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Liste von Domains, z.B. ["volt-muenster.de"]. Leer = alle erlaubt.',
                verbose_name="Erlaubte E-Mail-Domains",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="registration_auto_approve",
            field=models.BooleanField(
                default=False,
                help_text="Neue Mitglieder werden sofort freigeschaltet (ohne Admin-Bestätigung)",
                verbose_name="Automatische Freischaltung",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="registration_default_role",
            field=models.ForeignKey(
                blank=True,
                help_text="Rolle, die Selbstregistrierten automatisch zugewiesen wird",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="default_for_registration",
                to="tenants.role",
                verbose_name="Standardrolle für Registrierungen",
            ),
        ),
    ]
