# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Gerät merken“ (``TrustedDevice``) und ``PasswordResetToken`` verlassen den Code (Issue #501).

Beide Modelle wurden nie beschrieben: „Gerät merken“ war nicht umgesetzt, das Zurücksetzen des
Passworts nutzt die zustandslosen Tokens von Django. In Produktion sind beide Tabellen leer.

1. Der Fremdschlüssel der beiden Tabellen auf ``accounts_user`` entfällt in der Datenbank. Sonst
   verweisen Tabellen, die Django nicht mehr kennt, auf die Kontotabelle; PostgreSQL lehnt dann
   z. B. ``TRUNCATE accounts_user`` ab (Leeren der Testdatenbank), und Löschungen hingen an Zeilen,
   die kein Code mehr pflegt.
2. Nur der Modellstand wird entfernt, die Tabellen bleiben stehen: Ein Rückfall auf das vorherige
   Image (das die Modelle noch kennt) funktioniert ohne Rückbau der Migration; ein fehlender
   Fremdschlüssel stört es nicht.

Die Tabellen entfallen im Folge-Release (Issue #481).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_zugangstoken_als_hash"),
    ]

    operations = [
        migrations.AlterField(
            model_name="trusteddevice",
            name="user",
            field=models.ForeignKey(
                db_constraint=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="trusted_devices",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="passwordresettoken",
            name="user",
            field=models.ForeignKey(
                db_constraint=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="password_reset_tokens",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="TrustedDevice"),
                migrations.DeleteModel(name="PasswordResetToken"),
            ],
            database_operations=[],
        ),
    ]
