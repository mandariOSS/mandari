# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter (Issue #1215): Anmeldezeitpunkt der Sitzung an Code und Refresh-Token (``auth_time``).

Zwei neue, leere Spalten (``NULL`` erlaubt), keine Änderung an bestehenden Daten; ältere Images lesen sie nicht.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_oidc_anbieter"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthgrant",
            name="auth_time",
            field=models.DateTimeField(
                blank=True,
                help_text="Anmeldung der Person in der Sitzung, in der der Code entstand (auth_time im ID-Token).",
                null=True,
                verbose_name="Anmeldezeitpunkt",
            ),
        ),
        migrations.AddField(
            model_name="oauthrefreshtoken",
            name="auth_time",
            field=models.DateTimeField(
                blank=True,
                help_text="Anmeldung der Person in der Sitzung, aus der die Kette stammt (auth_time im ID-Token).",
                null=True,
                verbose_name="Anmeldezeitpunkt",
            ),
        ),
    ]
