# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nach PaperComment überführte TOP-Notizen entfernen (Issue #452).

Migration 0037 hat Notizen an TOPs mit Vorlage nach PaperComment kopiert (Inhalt als
Geheimtext 1:1, Autor, Sichtbarkeit, Beschluss-Markierung, Zeitstempel) und die Originale
markiert behalten. Löschte jemand den Kommentar, setzte ``SET_NULL`` die Markierung zurück
und das Original erschien wieder im Thread; der Inhalt lag außerdem dauerhaft doppelt vor.

- Das Feld wechselt auf Kaskade (reine Zustandsänderung, die Datenbank bleibt unverändert).
- Markierte Originale werden gelöscht: Ihr Inhalt steht vollständig im PaperComment, die
  Oberfläche blendet sie seit 0037 aus. Nicht umkehrbar; wiederholbar (danach gibt es keine
  markierten Notizen mehr), auf leeren Datenbanken ohne Wirkung.

Abwärtskompatibel: Das Feld bleibt bestehen, ein älteres Image filtert weiter danach.
"""

import django.db.models.deletion
from django.db import migrations, models


def markierte_originale_loeschen(apps, schema_editor):
    AgendaItemNote = apps.get_model("work", "AgendaItemNote")
    AgendaItemNote.objects.filter(migrated_to_paper_comment__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0059_mitglied_entfernen_inhalte_erhalten"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agendaitemnote",
            name="migrated_to_paper_comment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="migrated_from_notes",
                to="work.papercomment",
                verbose_name="Migriert nach Vorgang-Kommentar",
            ),
        ),
        migrations.RunPython(markierte_originale_loeschen, migrations.RunPython.noop, elidable=True),
    ]
