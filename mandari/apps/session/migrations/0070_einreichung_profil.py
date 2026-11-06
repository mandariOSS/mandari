# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil Einreichung von kommParl (Issue #1228): Prüfsumme der Einreichung, einreichende Person und vorab
hochgeladene Anlagen.

Rein additiv: eine neue Tabelle, ein Textfeld an den Anträgen, leer auch in der Datenbank (``db_default``), und ein
Verweis auf das Konto, das eingereicht hat (darf leer sein). Ein Rückfall auf das vorige Image liest das nicht und
legt Anträge weiter an.
"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0069_antragsfassung_pdf"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionapplication",
            name="content_digest",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=100, verbose_name="Prüfsumme der Einreichung"
            ),
        ),
        migrations.AddField(
            model_name="sessionapplication",
            name="submitted_by_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Eingereicht von (Konto)",
            ),
        ),
        migrations.CreateModel(
            name="SessionSubmissionUpload",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "client_id",
                    models.CharField(blank=True, max_length=100, verbose_name="Anwendung (Client-ID)"),
                ),
                ("name", models.CharField(max_length=255, verbose_name="Dateiname")),
                (
                    "mime_type",
                    models.CharField(max_length=100, verbose_name="MIME-Typ"),
                ),
                ("size", models.PositiveBigIntegerField(verbose_name="Größe (Bytes)")),
                ("sha256", models.CharField(max_length=64, verbose_name="SHA-256")),
                (
                    "file",
                    models.FileField(
                        blank=True,
                        upload_to="session/files/einreichungen/%Y/%m/",
                        verbose_name="Datei",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Hochgeladen am"),
                ),
                ("expires_at", models.DateTimeField(verbose_name="Verwendbar bis")),
                (
                    "used_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Verwendet am"),
                ),
                (
                    "used_for_key",
                    models.CharField(blank=True, default="", max_length=64, verbose_name="Verwendet für (Schlüssel)"),
                ),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="session.sessionapplication",
                        verbose_name="Einreichung",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="submission_uploads",
                        to="session.sessiontenant",
                        verbose_name="Mandant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Hochgeladen von",
                    ),
                ),
            ],
            options={
                "verbose_name": "Hochgeladene Anlage (Einreichung)",
                "verbose_name_plural": "Hochgeladene Anlagen (Einreichung)",
                "db_table": "session_submission_uploads",
                "indexes": [
                    models.Index(fields=["tenant", "user"], name="session_sub_upload_owner_idx"),
                    models.Index(fields=["expires_at"], name="session_sub_upload_exp_idx"),
                ],
            },
        ),
    ]
