# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Systemeinstellungen: Altspalten ``site_name`` und ``site_description`` löschen (Issue #604).

Die Felder haben mit common/0007 nur den Modellstand verlassen, die Spalten blieben für den Rückfall. Wie in #481
vereinbart, kommen die Felder hier nur im Modellstand zurück und werden dann mit ``RemoveField`` entfernt. Der
Modellstand ist danach derselbe wie vor dieser Migration: Ein Image ohne sie läuft weiter, sofern es common/0007
enthält. Ein älteres Image liest die Spalten noch (``DEPLOYMENT.md``, „Rollback“). ``email_backend`` bleibt (#481).

Ob die Spalten da sind, hängt von der Datenbank ab. In PostgreSQL stehen sie, auch auf einer frischen Installation:
common/0001 legt sie an. In SQLite fehlen sie seit common/0008, denn dort baut ``AlterField`` die Tabelle aus dem
Modellstand neu auf, und der kennt sie nicht mehr. Ein einfaches ``RemoveField`` scheitert an einer fehlenden Spalte,
deshalb prüft ``RemoveFieldFallsVorhanden`` vorher.
"""

from django.db import migrations, models


def _spalte_vorhanden(schema_editor, modell, feldname: str) -> bool:
    verbindung = schema_editor.connection
    with verbindung.cursor() as cursor:
        spalten = verbindung.introspection.get_table_description(cursor, modell._meta.db_table)
    return any(spalte.name == modell._meta.get_field(feldname).column for spalte in spalten)


class RemoveFieldFallsVorhanden(migrations.RemoveField):
    """``RemoveField``, das eine fehlende Spalte übergeht."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        modell = from_state.apps.get_model(app_label, self.model_name)
        if _spalte_vorhanden(schema_editor, modell, self.name):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Nicht auf SQLite: ``add_field`` baute die Tabelle aus dem Modellstand neu auf und verlöre ``email_backend``.
        # Die Spalten fehlten dort schon vor dieser Migration.
        if schema_editor.connection.vendor == "sqlite":
            return
        modell = to_state.apps.get_model(app_label, self.model_name)
        if not _spalte_vorhanden(schema_editor, modell, self.name):
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0013_ki_anbieter_auswahl_als_aufruf"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="sitesettings",
                    name="site_name",
                    field=models.CharField(
                        db_default="Mandari", default="Mandari", max_length=100, verbose_name="Seitenname"
                    ),
                ),
                migrations.AddField(
                    model_name="sitesettings",
                    name="site_description",
                    field=models.TextField(
                        blank=True,
                        db_default="Kommunalpolitische Transparenz",
                        default="Kommunalpolitische Transparenz",
                        verbose_name="Seitenbeschreibung",
                    ),
                ),
            ],
            database_operations=[],
        ),
        RemoveFieldFallsVorhanden(model_name="sitesettings", name="site_name"),
        RemoveFieldFallsVorhanden(model_name="sitesettings", name="site_description"),
    ]
