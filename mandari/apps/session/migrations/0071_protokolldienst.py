# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbindung an einen Protokolldienst (docs/PROTOKOLLDIENST.md): Einstellungen je Mandant, Postausgang der Meldungen aus
dem Cockpit, Vorschläge für die Niederschrift und der Zeitpunkt einer Sitzungsunterbrechung.

Rein additiv: drei neue Tabellen und eine Spalte, die leer sein darf. Ein Rückfall auf das vorige Image liest sie
nicht und legt Sitzungen weiter an.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models

import apps.common.encryption


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0070_einreichung_profil"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionmeeting",
            name="interrupted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Unterbrochen seit"),
        ),
        migrations.CreateModel(
            name="SessionProtokolldienst",
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
                    "enabled",
                    models.BooleanField(default=False, verbose_name="Eingeschaltet"),
                ),
                (
                    "base_url",
                    models.URLField(
                        blank=True,
                        max_length=300,
                        verbose_name="Adresse des Protokolldienstes",
                    ),
                ),
                (
                    "api_key_encrypted",
                    apps.common.encryption.EncryptedTextField(verbose_name="API-Schlüssel des Protokolldienstes"),
                ),
                (
                    "rueckgabe_token_hash",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        verbose_name="Rückgabeschlüssel",
                    ),
                ),
                (
                    "rueckgabe_token_prefix",
                    models.CharField(blank=True, editable=False, max_length=8, verbose_name="Anfang"),
                ),
                (
                    "rueckgabe_token_created_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Rückgabeschlüssel erzeugt"),
                ),
                (
                    "last_delivery_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Zuletzt zugestellt"),
                ),
                (
                    "last_error",
                    models.CharField(blank=True, max_length=40, verbose_name="Letzter Fehler"),
                ),
                (
                    "last_error_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Letzter Fehler am"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="protokolldienst",
                        to="session.sessiontenant",
                        verbose_name="Mandant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Protokolldienst",
                "verbose_name_plural": "Protokolldienste",
                "db_table": "session_protokolldienst",
            },
        ),
        migrations.CreateModel(
            name="SessionNiederschriftVorschlag",
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
                    "source_document",
                    models.UUIDField(verbose_name="Dokument beim Protokolldienst"),
                ),
                (
                    "revision",
                    models.PositiveIntegerField(default=1, verbose_name="Fassung"),
                ),
                (
                    "released_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="Beim Protokolldienst freigegeben",
                    ),
                ),
                (
                    "draft_text_encrypted",
                    apps.common.encryption.EncryptedTextField(verbose_name="Entwurf des Protokolltexts"),
                ),
                (
                    "resolution_text_encrypted",
                    apps.common.encryption.EncryptedTextField(verbose_name="Entwurf des Beschlusstexts"),
                ),
                (
                    "vote_result",
                    models.CharField(
                        blank=True,
                        max_length=50,
                        verbose_name="Vorgeschlagenes Ergebnis",
                    ),
                ),
                (
                    "votes_yes",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="Ja"),
                ),
                (
                    "votes_no",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="Nein"),
                ),
                (
                    "votes_abstain",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="Enthaltung"),
                ),
                (
                    "unanimous",
                    models.BooleanField(default=False, verbose_name="Einstimmig"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("offen", "Offen"),
                            ("uebernommen", "Übernommen"),
                            ("verworfen", "Verworfen"),
                            ("ersetzt", "Durch eine neuere Fassung ersetzt"),
                        ],
                        default="offen",
                        max_length=20,
                        verbose_name="Stand",
                    ),
                ),
                (
                    "received_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Eingegangen"),
                ),
                (
                    "decided_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Entschieden am"),
                ),
                (
                    "agenda_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="niederschrift_vorschlaege",
                        to="session.sessionagendaitem",
                        verbose_name="TOP",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="session.sessionuser",
                        verbose_name="Entschieden von",
                    ),
                ),
                (
                    "meeting",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="niederschrift_vorschlaege",
                        to="session.sessionmeeting",
                        verbose_name="Sitzung",
                    ),
                ),
            ],
            options={
                "verbose_name": "Vorschlag für die Niederschrift",
                "verbose_name_plural": "Vorschläge für die Niederschrift",
                "db_table": "session_niederschrift_vorschlaege",
                "ordering": [
                    "agenda_item__order",
                    "agenda_item__number",
                    "-received_at",
                ],
                "indexes": [models.Index(fields=["meeting", "status"], name="session_nds_vorschlag_idx")],
            },
        ),
        migrations.CreateModel(
            name="SessionProtokolldienstMeldung",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("agenda_item_changed", "TOP aufgerufen"),
                            ("pause_started", "Pause begonnen"),
                            ("pause_ended", "Pause beendet"),
                            ("vote_result", "Abstimmungsergebnis"),
                        ],
                        max_length=30,
                        verbose_name="Art",
                    ),
                ),
                ("occurred_at", models.DateTimeField(verbose_name="Zeitpunkt")),
                (
                    "payload",
                    models.JSONField(blank=True, default=dict, verbose_name="Nutzdaten"),
                ),
                (
                    "geschuetzt",
                    models.BooleanField(default=False, verbose_name="Geschützter Teil"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("wartend", "Wartend"),
                            ("zugestellt", "Zugestellt"),
                            ("fehlgeschlagen", "Fehlgeschlagen"),
                        ],
                        default="wartend",
                        max_length=20,
                        verbose_name="Stand",
                    ),
                ),
                (
                    "attempts",
                    models.PositiveSmallIntegerField(default=0, verbose_name="Versuche"),
                ),
                (
                    "error_code",
                    models.CharField(blank=True, max_length=40, verbose_name="Fehler"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "delivered_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Zugestellt am"),
                ),
                (
                    "agenda_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="session.sessionagendaitem",
                        verbose_name="TOP",
                    ),
                ),
                (
                    "meeting",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="protokolldienst_meldungen",
                        to="session.sessionmeeting",
                        verbose_name="Sitzung",
                    ),
                ),
            ],
            options={
                "verbose_name": "Meldung an den Protokolldienst",
                "verbose_name_plural": "Meldungen an den Protokolldienst",
                "db_table": "session_protokolldienst_meldungen",
                "ordering": ["occurred_at", "created_at"],
                "indexes": [
                    models.Index(
                        fields=["meeting", "status"],
                        name="session_pd_meldung_offen_idx",
                    )
                ],
            },
        ),
    ]
