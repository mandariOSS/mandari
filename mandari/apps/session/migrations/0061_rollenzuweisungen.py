# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechte mit Geltungsbereich, Teil 1 (Issue #772): Modell ``SessionRoleAssignment`` und Schalter je Mandant.

Abwärtskompatibel: neue Tabelle, neuer Schalter mit Datenbank-Standardwert (aus). Ein älteres Image kennt die
Tabelle nicht und läuft unverändert weiter; damit es Konten, Rollen und Mandanten weiter löschen kann, setzt 0062
die Fremdschlüssel in PostgreSQL auf ``ON DELETE CASCADE`` bzw. ``SET NULL``. Die Spiegelzuweisungen des Bestands
legt ebenfalls 0062 an.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0057_koerperschaften_zuordnen"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessiontenant",
            name="scoped_permissions_enabled",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Befristete Rollenzuweisungen und Zuweisungen für Körperschaften, Gremien oder Ämter wirken lassen",
                verbose_name="Rechte mit Geltungsbereich",
            ),
        ),
        migrations.CreateModel(
            name="SessionRoleAssignment",
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
                    "scope_type",
                    models.CharField(
                        choices=[
                            ("mandant", "Mandant"),
                            ("koerperschaft", "Körperschaft"),
                            ("gremium", "Gremium"),
                            ("amt", "Amt"),
                        ],
                        default="mandant",
                        max_length=20,
                        verbose_name="Geltungsbereich",
                    ),
                ),
                (
                    "scope_id",
                    models.UUIDField(
                        blank=True,
                        help_text="Körperschaft, Gremium oder Amt; leer für den ganzen Mandanten",
                        null=True,
                        verbose_name="Kennung des Geltungsbereichs",
                    ),
                ),
                (
                    "valid_from",
                    models.DateField(blank=True, null=True, verbose_name="Gültig ab"),
                ),
                (
                    "valid_until",
                    models.DateField(blank=True, null=True, verbose_name="Gültig bis einschließlich"),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("manuell", "Manuell"),
                            ("mandat", "Mandat"),
                            ("stelle", "Stelle"),
                            ("vertretung", "Vertretung"),
                            ("migration", "Migration"),
                        ],
                        default="manuell",
                        max_length=20,
                        verbose_name="Quelle",
                    ),
                ),
                (
                    "note",
                    models.CharField(blank=True, max_length=500, verbose_name="Vermerk"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Angelegt am"),
                ),
                (
                    "revoked_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Aufgehoben am"),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="session.sessionuser",
                        verbose_name="Angelegt von",
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="session.sessionuser",
                        verbose_name="Aufgehoben von",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assignments",
                        to="session.sessionrole",
                        verbose_name="Rolle",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_assignments",
                        to="session.sessiontenant",
                        verbose_name="Mandant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_assignments",
                        to="session.sessionuser",
                        verbose_name="Konto",
                    ),
                ),
            ],
            options={
                "verbose_name": "Rollenzuweisung",
                "verbose_name_plural": "Rollenzuweisungen",
                "db_table": "session_role_assignments",
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "user", "revoked_at"],
                        name="session_rz_konto_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("revoked_at__isnull", True),
                            ("scope_type", "mandant"),
                            ("valid_from__isnull", True),
                            ("valid_until__isnull", True),
                        ),
                        fields=("user", "role"),
                        name="session_rz_spiegel_eindeutig",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("valid_from__isnull", True),
                            ("valid_until__isnull", True),
                            ("valid_until__gte", models.F("valid_from")),
                            _connector="OR",
                        ),
                        name="session_rz_zeitraum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("scope_id__isnull", True), ("scope_type", "mandant")),
                            models.Q(
                                models.Q(("scope_type", "mandant"), _negated=True),
                                ("scope_id__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="session_rz_bereich",
                    ),
                ],
            },
        ),
    ]
