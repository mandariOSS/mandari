# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter (Issue #1206): eigene Modelle für die austauschbaren Modelle von django-oauth-toolkit.

Neue Tabellen, keine Änderung an bestehenden Daten; ältere Images lesen die Tabellen nicht und stören sich nicht
an ihnen. Im Sicherheitsprotokoll kommen nur Einträge der Auswahlliste dazu (``oidc_*``), die Spalte bleibt.

``run_before``: Die Migrationen der Bibliothek (``oauth2_provider``) verweisen über die Einstellungen
``OAUTH2_PROVIDER_*_MODEL`` auf diese Modelle und müssen deshalb danach laufen. Sie legen selbst keine Tabellen an,
weil alle sechs Modelle ersetzt sind.
"""

import django.db.models.deletion
import oauth2_provider.generators
import oauth2_provider.models
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_sicherheitsprotokoll_drosselung"),
    ]

    run_before = [
        ("oauth2_provider", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="securityauditlog",
            name="event",
            field=models.CharField(
                choices=[
                    ("login", "Anmeldung"),
                    ("logout", "Abmeldung"),
                    ("login_failed", "Anmeldung fehlgeschlagen"),
                    ("login_throttled", "Anmeldung durch Drosselung abgewiesen"),
                    ("betrieb", "Eingriff in den Betrieb"),
                    ("oidc_token", "Token für eine Anwendung ausgegeben"),
                    ("oidc_widerruf", "Zugang einer Anwendung widerrufen"),
                    ("oidc_anwendung", "Anwendung (OIDC-Client) geändert"),
                ],
                max_length=30,
                verbose_name="Ereignis",
            ),
        ),
        migrations.CreateModel(
            name="OAuthApplication",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "client_id",
                    models.CharField(
                        db_index=True,
                        default=oauth2_provider.generators.generate_client_id,
                        max_length=255,
                        unique=True,
                        verbose_name="client ID",
                    ),
                ),
                (
                    "redirect_uris",
                    models.TextField(
                        blank=True,
                        help_text="Allowed URIs list, space separated",
                        verbose_name="redirect URIs",
                    ),
                ),
                (
                    "post_logout_redirect_uris",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Allowed Post Logout URIs list, space separated",
                        verbose_name="post logout redirect URIs",
                    ),
                ),
                (
                    "client_type",
                    models.CharField(
                        choices=[
                            ("confidential", "Confidential"),
                            ("public", "Public"),
                        ],
                        max_length=32,
                        verbose_name="client type",
                    ),
                ),
                (
                    "authorization_grant_type",
                    models.CharField(
                        choices=[
                            ("authorization-code", "Authorization code"),
                            (
                                "urn:ietf:params:oauth:grant-type:device_code",
                                "Device Code",
                            ),
                            ("implicit", "Implicit"),
                            ("password", "Resource owner password-based"),
                            ("client-credentials", "Client credentials"),
                            ("openid-hybrid", "OpenID connect hybrid"),
                        ],
                        max_length=44,
                        verbose_name="authorization grant type",
                    ),
                ),
                (
                    "client_secret",
                    oauth2_provider.models.ClientSecretField(
                        blank=True,
                        db_index=True,
                        default=oauth2_provider.generators.generate_client_secret,
                        help_text="Client secret for authentication",
                        max_length=255,
                        verbose_name="client secret",
                    ),
                ),
                (
                    "hash_client_secret",
                    models.BooleanField(
                        default=True, verbose_name="hash client secret"
                    ),
                ),
                (
                    "name",
                    models.CharField(blank=True, max_length=255, verbose_name="name"),
                ),
                (
                    "skip_authorization",
                    models.BooleanField(
                        default=False, verbose_name="skip authorization"
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "updated",
                    models.DateTimeField(auto_now=True, verbose_name="updated"),
                ),
                (
                    "algorithm",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "No OIDC support"),
                            ("RS256", "RSA with SHA-2 256"),
                            ("HS256", "HMAC with SHA-2 256"),
                        ],
                        default="",
                        max_length=5,
                        verbose_name="algorithm",
                    ),
                ),
                (
                    "allowed_origins",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Allowed origins list to enable CORS, space separated",
                        verbose_name="allowed origins",
                    ),
                ),
                (
                    "registration_source",
                    models.CharField(
                        choices=[
                            ("manual", "Manual"),
                            ("dcr", "Dynamic Client Registration"),
                            ("cimd", "Client ID Metadata Document"),
                        ],
                        default="manual",
                        help_text="How this application was registered (manual, DCR per RFC 7591, or CIMD)",
                        max_length=32,
                        verbose_name="registration source",
                    ),
                ),
                (
                    "cimd_expires_at",
                    models.DateTimeField(
                        blank=True,
                        default=None,
                        help_text="When the cached Client ID Metadata Document should be re-fetched",
                        null=True,
                        verbose_name="CIMD expires at",
                    ),
                ),
                (
                    "allowed_scopes",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Mehr als diese Scopes kann die Anwendung nicht anfordern.",
                        verbose_name="Erlaubte Scopes",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Ausgeschaltet bekommt die Anwendung keine Tokens mehr, und ihre Tokens gelten nicht mehr.",
                        verbose_name="Aktiv",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Anwendung",
                "verbose_name_plural": "OIDC-Anwendungen",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OAuthGrant",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "code",
                    models.CharField(max_length=255, unique=True, verbose_name="code"),
                ),
                ("expires", models.DateTimeField(verbose_name="expires")),
                ("redirect_uri", models.TextField(verbose_name="redirect URI")),
                ("scope", models.TextField(blank=True, verbose_name="scope")),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "updated",
                    models.DateTimeField(auto_now=True, verbose_name="updated"),
                ),
                (
                    "code_challenge",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="code challenge",
                    ),
                ),
                (
                    "code_challenge_method",
                    models.CharField(
                        blank=True,
                        choices=[("plain", "plain"), ("S256", "S256")],
                        default="",
                        max_length=10,
                        verbose_name="code challenge method",
                    ),
                ),
                (
                    "nonce",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="nonce"
                    ),
                ),
                ("claims", models.TextField(blank=True, verbose_name="claims")),
                (
                    "resource",
                    oauth2_provider.models.ResourceJSONField(
                        blank=True, default=list, verbose_name="resource"
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
                        verbose_name="application",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Autorisierungscode",
                "verbose_name_plural": "OIDC-Autorisierungscodes",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OAuthIDToken",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "jti",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        unique=True,
                        verbose_name="JWT Token ID",
                    ),
                ),
                ("expires", models.DateTimeField(verbose_name="expires")),
                ("scope", models.TextField(blank=True, verbose_name="scope")),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "updated",
                    models.DateTimeField(auto_now=True, verbose_name="updated"),
                ),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
                        verbose_name="application",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-ID-Token",
                "verbose_name_plural": "OIDC-ID-Tokens",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OAuthAccessToken",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("token", models.TextField(verbose_name="token")),
                (
                    "token_checksum",
                    oauth2_provider.models.TokenChecksumField(
                        db_index=True,
                        max_length=64,
                        unique=True,
                        verbose_name="token checksum",
                    ),
                ),
                ("expires", models.DateTimeField(verbose_name="expires")),
                ("scope", models.TextField(blank=True, verbose_name="scope")),
                (
                    "resource",
                    oauth2_provider.models.ResourceJSONField(
                        blank=True, default=list, verbose_name="resource"
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "updated",
                    models.DateTimeField(auto_now=True, verbose_name="updated"),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
                        verbose_name="application",
                    ),
                ),
                (
                    "id_token",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_token",
                        to=settings.OAUTH2_PROVIDER_ID_TOKEN_MODEL,
                        verbose_name="ID token",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Access-Token",
                "verbose_name_plural": "OIDC-Access-Tokens",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OAuthRefreshToken",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("token", models.TextField(verbose_name="token")),
                (
                    "token_checksum",
                    oauth2_provider.models.TokenChecksumField(
                        max_length=64, verbose_name="token checksum"
                    ),
                ),
                (
                    "token_family",
                    models.UUIDField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name="token family",
                    ),
                ),
                (
                    "resource",
                    oauth2_provider.models.ResourceJSONField(
                        blank=True, default=list, verbose_name="resource"
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "updated",
                    models.DateTimeField(auto_now=True, verbose_name="updated"),
                ),
                ("revoked", models.DateTimeField(null=True, verbose_name="revoked")),
                (
                    "access_token",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="refresh_token",
                        to=settings.OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL,
                        verbose_name="access token",
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
                        verbose_name="application",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Refresh-Token",
                "verbose_name_plural": "OIDC-Refresh-Tokens",
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="oauthaccesstoken",
            name="source_refresh_token",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="refreshed_access_token",
                to=settings.OAUTH2_PROVIDER_REFRESH_TOKEN_MODEL,
                verbose_name="source refresh token",
            ),
        ),
        migrations.CreateModel(
            name="OAuthDeviceGrant",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "device_code",
                    models.CharField(max_length=100, verbose_name="device code"),
                ),
                (
                    "user_code",
                    models.CharField(max_length=100, verbose_name="user code"),
                ),
                ("scope", models.TextField(blank=True, verbose_name="scope")),
                ("interval", models.IntegerField(default=5, verbose_name="interval")),
                ("expires", models.DateTimeField(verbose_name="expires")),
                (
                    "status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("authorized", "Authorized"),
                            ("authorization-pending", "Authorization pending"),
                            ("expired", "Expired"),
                            ("denied", "Denied"),
                        ],
                        default="authorization-pending",
                        max_length=64,
                        verbose_name="status",
                    ),
                ),
                (
                    "client_id",
                    models.CharField(
                        db_index=True, max_length=100, verbose_name="client ID"
                    ),
                ),
                (
                    "last_checked",
                    models.DateTimeField(auto_now=True, verbose_name="last checked"),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "OIDC-Gerätefreigabe",
                "verbose_name_plural": "OIDC-Gerätefreigaben",
                "abstract": False,
                "constraints": [
                    models.UniqueConstraint(
                        fields=("device_code",),
                        name="accounts_oauthdevicegrant_unique_device_code",
                    )
                ],
            },
        ),
        migrations.AddIndex(
            model_name="oauthrefreshtoken",
            index=models.Index(
                fields=["token_family"], name="accounts_oa_token_f_300deb_idx"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="oauthrefreshtoken",
            unique_together={("token_checksum", "revoked")},
        ),
    ]
