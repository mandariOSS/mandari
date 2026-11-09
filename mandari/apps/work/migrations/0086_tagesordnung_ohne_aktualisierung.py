# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungen der Tagesordnung nach dem Versand ohne Aktualisierung übernehmen (Issue #1285): neues, leeres Feld
``agenda_change_acknowledged_at`` an der Fraktionssitzung. Nur hinzugefügt, ohne Daten zu ändern; rückwärts fällt es
wieder weg (dann meldet die Infoleiste übernommene Änderungen wieder als „Tagesordnung geändert“).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0085_sitzungsarten_oeffentlich"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionmeeting",
            name="agenda_change_acknowledged_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Änderungen ohne Aktualisierung übernommen am"
            ),
        ),
    ]
