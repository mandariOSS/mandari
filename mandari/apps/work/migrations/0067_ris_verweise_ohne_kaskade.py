# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise aus Work auf den RIS-Bestand nie mit ``CASCADE`` (Issue #524, ADR ``docs/adr/20260929-fremdschluessel-ris-bestand.md``).

``PROTECT`` für Inhalte, die an genau einem RIS-Objekt hängen, und für Konfiguration; ``SET_NULL`` für den
optionalen Sitzungsbezug eines Redebeitrags. ``on_delete`` wirkt nur in Django: Am Schema ändert sich nichts
(``sqlmigrate`` ohne Anweisung), ein Rückfall per Image braucht keinen Rückbau.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0050_loeschgrund"),
        ("work", "0066_einreichung_per_email"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agendaitemnote",
            name="agenda_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_notes",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
        migrations.AlterField(
            model_name="agendaitemposition",
            name="agenda_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_positions",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
        migrations.AlterField(
            model_name="agendaprivatenote",
            name="agenda_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_private_notes",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
        migrations.AlterField(
            model_name="agendaspeechnote",
            name="agenda_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_speech_notes",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
        migrations.AlterField(
            model_name="agendaspeechnote",
            name="meeting",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="work_speech_notes",
                to="insight_core.oparlmeeting",
                verbose_name="Sitzung",
            ),
        ),
        migrations.AlterField(
            model_name="agendasupplementarydocument",
            name="agenda_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_supplementary_documents",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
        migrations.AlterField(
            model_name="factionsuspensionrule",
            name="ris_organization",
            field=models.ForeignKey(
                help_text="Nach einer Sitzung dieses Gremiums entfällt die nächste Fraktionssitzung",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="faction_suspension_rules",
                to="insight_core.oparlorganization",
                verbose_name="RIS-Gremium",
            ),
        ),
        migrations.AlterField(
            model_name="fileannotation",
            name="oparl_file",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_annotations",
                to="insight_core.oparlfile",
                verbose_name="OParl-Datei",
            ),
        ),
        migrations.AlterField(
            model_name="meetingpreparation",
            name="meeting",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_preparations",
                to="insight_core.oparlmeeting",
                verbose_name="Sitzung",
            ),
        ),
        migrations.AlterField(
            model_name="motionshare",
            name="body",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="shared_motions",
                to="insight_core.oparlbody",
                verbose_name="OParl Body",
            ),
        ),
        migrations.AlterField(
            model_name="papercomment",
            name="paper",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_comments",
                to="insight_core.oparlpaper",
                verbose_name="Vorgang",
            ),
        ),
    ]
