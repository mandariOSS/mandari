# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verdeckte Wahl des Mail-Backends in den Systemeinstellungen entfällt (Issue #501).

``SiteSettings.email_backend`` stand in keinem Formular, wirkte aber in ``get_email_config`` –
ein Schalter, der nur per Shell setzbar war. In Produktion ist er leer. Das Backend wählt jetzt
allein die Umgebung (``EMAIL_BACKEND``) bzw. SMTP, sobald ein Host eingetragen ist.

1. Die Spalte bekommt einen Standardwert in der Datenbank (``''``), damit neue Zeilen auch ohne
   das Feld im Modell gültig sind.
2. Das Feld verlässt nur den Modellstand; die Spalte bleibt, damit ein Rückfall auf das vorherige
   Image ohne Rückbau funktioniert. Sie entfällt im Folge-Release (Issue #481).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0006_systemeinstellungen_verschluesselt"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sitesettings",
            name="email_backend",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Leer lassen für Standardwert aus Umgebungsvariablen",
                max_length=200,
                verbose_name="E-Mail Backend",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="sitesettings", name="email_backend"),
            ],
            database_operations=[],
        ),
    ]
