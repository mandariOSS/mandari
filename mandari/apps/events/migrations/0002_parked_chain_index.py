# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Index für die Kette geparkter Ereignisse je Objekt (Issue #504).

Die Zustellung sucht zu einem Objekt das nächste geparkte Ereignis nach Folgenummer
(``apps.events.dispatch``). Der neue Index ``(subscription, aggregate_id, event_seq)`` liefert es
direkt und deckt auch alle Abfragen des bisherigen Index ``(subscription, aggregate_id)`` ab, der
deshalb entfällt. Erst anlegen, dann entfernen: Es gibt immer einen passenden Index.

Nur Indizes; kein Code liest ihre Namen. Ein Rückfall auf ein älteres Image braucht keinen
Rückbau.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="parkedevent",
            index=models.Index(
                fields=["subscription", "aggregate_id", "event_seq"],
                name="events_parked_chain",
            ),
        ),
        migrations.RemoveIndex(
            model_name="parkedevent",
            name="events_parked_aggregate",
        ),
    ]
