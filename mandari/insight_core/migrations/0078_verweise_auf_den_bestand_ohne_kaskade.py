# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise aus insight_core auf den RIS-Bestand ohne CASCADE (Issue #524).

Nach ADR ``docs/adr/20260929-fremdschluessel-ris-bestand.md``: Abos und Ratsfragen des Bürgerportals sowie die
abgeleiteten Geodaten (Straßen, Adressen, Orts-Zuordnungen, Verortungen, Umringe, Planbezüge) zeigen mit PROTECT auf
Kommune, Vorgang bzw. Person, Rückmeldungen zu Seiten mit SET_NULL (die Spalte ist schon nullbar). ``on_delete`` wirkt
nur in Django: Die Migration ändert kein Datenbankschema (``sqlmigrate`` zeigt für jede Operation „no-op“) und keine
Daten. Ein Rückfall per Image braucht keinen Rückbau.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0077_datenluecken_gemeldet"),
    ]

    operations = [
        migrations.AlterField(
            model_name="address",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="addresses",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="insightsubscriber",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="subscribers",
                to="insight_core.oparlbody",
            ),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="location_mappings",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="pagefeedback",
            name="body",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="page_feedback",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="paperlocation",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="paper_locations",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="paperlocation",
            name="paper",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="paper_locations",
                to="insight_core.oparlpaper",
                verbose_name="Vorgang",
            ),
        ),
        migrations.AlterField(
            model_name="paperplanreference",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plan_references",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="paperplanreference",
            name="paper",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plan_references",
                to="insight_core.oparlpaper",
                verbose_name="Vorgang",
            ),
        ),
        migrations.AlterField(
            model_name="planboundary",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plan_boundaries",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="planboundarysource",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plan_boundary_sources",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
        migrations.AlterField(
            model_name="publicquestion",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="public_questions",
                to="insight_core.oparlbody",
            ),
        ),
        migrations.AlterField(
            model_name="publicquestion",
            name="recipient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="public_questions",
                to="insight_core.oparlperson",
            ),
        ),
        migrations.AlterField(
            model_name="street",
            name="body",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="streets",
                to="insight_core.oparlbody",
                verbose_name="Kommune",
            ),
        ),
    ]
