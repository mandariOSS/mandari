# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsvorschläge im Antragseditor (Teil von #856, Modus „Vorschlagen“).

Zwei neue, leere Spalten an ``MotionComment`` (rein additiv, beide ``NULL`` erlaubt, damit ein älteres Abbild
weiter Kommentare anlegen kann):

- ``vorschlag``: vorgeschlagener Ersatz für die markierte Stelle; ``NULL`` = gewöhnlicher Kommentar.
- ``vorschlag_angenommen``: Entscheidung über den Vorschlag (offen, angenommen, abgelehnt).

Bestand: Alle vorhandenen Kommentare bleiben gewöhnliche Kommentare (beide Spalten ``NULL``); kein Wert wird
geschrieben, geändert oder gelöscht. Rückweg: Spalten entfernen (nur Vorschläge verlören ihren Ersatztext, der
zusätzlich im Kommentartext steht).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0074_sitzungsreihe_automatik"),
    ]

    operations = [
        migrations.AddField(
            model_name="motioncomment",
            name="vorschlag",
            field=models.TextField(blank=True, default=None, null=True, verbose_name="Vorgeschlagener Text"),
        ),
        migrations.AddField(
            model_name="motioncomment",
            name="vorschlag_angenommen",
            field=models.BooleanField(blank=True, default=None, null=True, verbose_name="Vorschlag angenommen"),
        ),
    ]
