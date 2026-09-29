# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Gerät merken“ (``TrustedDevice``) und ``PasswordResetToken`` verlassen den Code (Issue #501).

Beide Modelle wurden nie beschrieben: „Gerät merken“ war nicht umgesetzt, das Zurücksetzen des
Passworts nutzt die zustandslosen Tokens von Django. In Produktion sind beide Tabellen leer.

Nur der Modellstand wird entfernt, die Tabellen bleiben stehen: Ein Rückfall auf das vorherige
Image (das die Modelle noch kennt) funktioniert so ohne Rückbau der Migration. Die Tabellen
entfallen im Folge-Release (Issue #481). Weil der neue Code keine Zeilen anlegt, bleiben die
Tabellen leer und ihre Fremdschlüssel auf ``accounts_user`` behindern das Löschen von Konten nicht.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_zugangstoken_als_hash"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="TrustedDevice"),
                migrations.DeleteModel(name="PasswordResetToken"),
            ],
            database_operations=[],
        ),
    ]
