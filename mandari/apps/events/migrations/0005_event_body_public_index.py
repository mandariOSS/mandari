# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Index für öffentliche Ereignisse je Kommune (Issue #562).

Der Änderungsfeed einer Kommune liest ``seq > cursor`` unter ihren öffentlichen Ereignissen. Ohne
diesen Index liefe die Abfrage über die Ereignisse aller Kommunen, bis genug eigene gefunden sind. Der
Index umfasst nur nummerierte Ereignisse der Sichtbarkeit ``oeffentlich`` und bleibt damit klein.

Nur ein Index; kein Code liest seinen Namen. Ein Rückfall auf ein älteres Image braucht keinen
Rückbau.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0004_idempotenz"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="event",
            index=models.Index(
                condition=models.Q(("seq__isnull", False), ("visibility", "oeffentlich")),
                fields=["body_id", "seq"],
                name="events_event_body_public",
            ),
        ),
    ]
