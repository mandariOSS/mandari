# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumenteigenes Briefpapier für treu übernommene PDFs (Issue #1600).

Zwei neue Spalten am Briefkopf: ``dokument`` (zu welchem Dokument das Briefpapier gehört; leer bei Briefköpfen der
Organisation) und ``satz`` (genaue Maße des übernommenen Satzes). Rein additiv und beide Spalten dürfen leer sein:
Das Image davor kennt sie nicht, legt Briefköpfe ohne sie an und läuft unverändert weiter.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0094_motioncomment_ki_erzeugt"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationletterhead",
            name="dokument",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="eigene_briefpapiere",
                to="work.motion",
                verbose_name="Dokument",
            ),
        ),
        migrations.AddField(
            model_name="organizationletterhead",
            name="satz",
            field=models.JSONField(blank=True, default=dict, null=True, verbose_name="Satz"),
        ),
    ]
