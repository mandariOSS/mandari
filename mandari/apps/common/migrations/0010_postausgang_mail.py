# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Postausgang des Mail-Dienstes (Issue #528): neue Tabelle ``common_mail_outbox``.

Rein additiv. Ein älteres Image kennt die Tabelle nicht und versendet wie bisher sofort. Was beim
Rückfall noch im Postausgang wartet, versendet es nicht: Sein Worker kennt den Auftrag
``apps.common.mail.outbox.deliver_mail`` nicht und wiederholt ihn bis „tot“. Deshalb vor einem Rückfall
den Postausgang leer laufen lassen; nach der Rückkehr zur neuen Version reiht
``manage.py postausgang --einreihen`` liegen gebliebene Zeilen neu ein.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0009_kennungsbasis"),
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailOutbox",
            fields=[
                (
                    "id",
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                ("kind", models.CharField(max_length=64, verbose_name="Mailart")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("wartend", "Wartet auf Versand"),
                            ("versendet", "Versendet"),
                            ("fehlgeschlagen", "Endgültig fehlgeschlagen"),
                        ],
                        default="wartend",
                        max_length=20,
                        verbose_name="Status",
                    ),
                ),
                (
                    "via_organization",
                    models.BooleanField(
                        default=True, verbose_name="Über den Weg der Organisation"
                    ),
                ),
                (
                    "payload_encrypted",
                    models.BinaryField(
                        blank=True,
                        null=True,
                        verbose_name="Inhalt (Mandantenschlüssel)",
                    ),
                ),
                (
                    "payload_platform_encrypted",
                    models.BinaryField(
                        blank=True, null=True, verbose_name="Inhalt (Hauptschlüssel)"
                    ),
                ),
                (
                    "idempotency_key",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        null=True,
                        unique=True,
                        verbose_name="Idempotenzschlüssel",
                    ),
                ),
                (
                    "attempts",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="Versuche"
                    ),
                ),
                (
                    "task_id",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="Versandauftrag",
                    ),
                ),
                (
                    "route",
                    models.CharField(
                        blank=True, max_length=20, verbose_name="Genutzter Weg"
                    ),
                ),
                (
                    "error_code",
                    models.CharField(
                        blank=True, max_length=200, verbose_name="Fehlerklasse"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Angelegt am"),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Beendet am"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        blank=True,
                        help_text="Versandweg und Mandantenschlüssel; leer bei Mails der Plattform",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mail im Postausgang",
                "verbose_name_plural": "Postausgang",
                "db_table": "common_mail_outbox",
                "indexes": [
                    models.Index(
                        fields=["status", "finished_at"], name="common_mail_status_idx"
                    )
                ],
            },
        ),
    ]
