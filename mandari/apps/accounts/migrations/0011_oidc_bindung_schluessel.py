# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter, Profil Nichtöffentliches (Issue #565): Bindung einer Anwendung an genau einen Bereich,
öffentliche Schlüssel für private_key_jwt und die Sperrliste angenommener Client-Assertions (jti).

Abwärtsverträglich: Die Bindung hat einen Standardwert in der Datenbank (leer), die Schlüssel dürfen fehlen, und die
neue Tabelle hat keinen Fremdschlüssel. Ältere Images legen weiter Anwendungen an und löschen sie.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_oidc_anmeldezeit"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthapplication",
            name="binding",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Nur für Dienste: genau ein Bereich, an den die Anwendung gebunden ist, etwa ein Session-Mandant („session-mandant:<Kennung>“). Leer: keine Bindung.",
                max_length=128,
                verbose_name="Bindung",
            ),
        ),
        migrations.AddField(
            model_name="oauthapplication",
            name="jwks",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text="Für die Anmeldung mit private_key_jwt (RFC 7523): öffentliche Schlüssel der Anwendung als JWK Set, nur RS256 (RSA ab 2048 Bit) und ES256 (P-256). Dann gilt kein Client-Secret.",
                null=True,
                verbose_name="Öffentliche Schlüssel (JWKS)",
            ),
        ),
        migrations.CreateModel(
            name="OAuthClientAssertion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "client_id",
                    models.CharField(max_length=100, verbose_name="Client-ID"),
                ),
                ("jti", models.CharField(max_length=255, verbose_name="Kennung (jti)")),
                ("expires", models.DateTimeField(verbose_name="Gesperrt bis")),
                (
                    "created",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Angenommen am"
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Client-Assertion",
                "verbose_name_plural": "OIDC-Client-Assertions",
                "indexes": [
                    models.Index(fields=["expires"], name="oidc_assertion_ablauf")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("client_id", "jti"), name="oidc_assertion_jti_einmal"
                    )
                ],
            },
        ),
    ]
