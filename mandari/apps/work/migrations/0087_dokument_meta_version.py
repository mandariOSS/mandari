# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reihenfolge der Speicher-POSTs für Titel, Dokumenttyp und Briefpapier (Issue #1199): neuer Zähler ``meta_version`` am
Dokument, Standardwert 0 auch in der Datenbank: Ein Image ohne das Feld (laufender Wechsel, Rückfall) legt weiter
Dokumente an. Nur hinzugefügt, ohne Daten zu ändern; rückwärts fällt er wieder weg (dann lehnt der Server überholte
Requests nicht mehr ab).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0086_tagesordnung_ohne_aktualisierung"),
    ]

    operations = [
        migrations.AddField(
            model_name="motion",
            name="meta_version",
            field=models.PositiveIntegerField(
                db_default=0, default=0, editable=False, verbose_name="Stand der Kopfdaten"
            ),
        ),
    ]
