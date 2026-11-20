# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geteilte Wahlperioden (Issue #1453).

Mehrere Kommunen einer Quelle nennen dieselbe Wahlperiode unter derselben Kennung. Eine Wahlperiode ist im Bestand eine
Zeile (``external_id`` eindeutig) und gehört der Kommune, die sie zuerst gespeichert hat; die übrigen hatten keine. Die
Zuordnungstabelle ``oparl_legislative_terms_shared_bodies`` verknüpft sie mit den weiteren Kommunen. Nur eine neue,
leere Tabelle: Bestandsdaten bleiben unberührt, der Rückweg ist das Entfernen der Tabelle.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0074_datei_tagesordnungspunkt"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparllegislativeterm",
            name="shared_bodies",
            field=models.ManyToManyField(
                blank=True,
                db_table="oparl_legislative_terms_shared_bodies",
                related_name="shared_legislative_terms",
                to="insight_core.oparlbody",
            ),
        ),
    ]
