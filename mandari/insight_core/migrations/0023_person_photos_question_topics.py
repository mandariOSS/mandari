# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Personenfotos lokal cachen + Ratsfragen-Ausbau (Themenbereich, Veröffentlichungsdatum).

Data-Migration: Für bekannte RIS (Stadt Münster, SessionNet) wird die
Foto-Konfiguration gesetzt, falls sie fehlt — sie war durch frühere
Ingestor-Re-Syncs (vor dem ENRICHMENT_FIELDS-Schutz) verloren gegangen.
"""

from django.db import migrations, models

PHOTO_PRESETS = [
    {
        "match": "oparl.stadt-muenster.de",
        "template": "https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg",
        "pattern": r"/people/(\d+)$",
    },
]


def apply_photo_presets(apps, schema_editor):
    OParlBody = apps.get_model("insight_core", "OParlBody")
    for body in OParlBody.objects.select_related("source").all():
        if body.person_photo_url_template and body.person_photo_id_pattern:
            continue
        source_url = (getattr(body.source, "url", "") or "") if body.source_id else ""
        haystack = f"{source_url} {body.external_id or ''}"
        for preset in PHOTO_PRESETS:
            if preset["match"] in haystack:
                body.person_photo_url_template = preset["template"]
                body.person_photo_id_pattern = preset["pattern"]
                body.save(update_fields=["person_photo_url_template", "person_photo_id_pattern"])
                break


def backfill_published_at(apps, schema_editor):
    PublicQuestion = apps.get_model("insight_core", "PublicQuestion")
    for question in PublicQuestion.objects.filter(status="published", published_at__isnull=True):
        question.published_at = question.moderated_at or question.created_at
        question.save(update_fields=["published_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0022_oparl_tombstones"),
    ]

    operations = [
        # --- Personenfotos -------------------------------------------------
        migrations.AddField(
            model_name="oparlperson",
            name="photo",
            field=models.FileField(blank=True, null=True, upload_to="persons/photos/", verbose_name="Foto"),
        ),
        migrations.AddField(
            model_name="oparlperson",
            name="photo_status",
            field=models.CharField(
                choices=[
                    ("unknown", "Noch nicht geprüft"),
                    ("ok", "Foto vorhanden"),
                    ("missing", "Kein Foto im RIS"),
                    ("error", "Fehler beim Abruf"),
                    ("manual", "Manuell hochgeladen"),
                ],
                default="unknown",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="oparlperson",
            name="photo_fetched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="oparlperson",
            name="photo_error",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="oparlbody",
            name="person_photo_url_template",
            field=models.CharField(
                blank=True,
                help_text=(
                    "URL-Template für Personenfotos. Verwende {id} als Platzhalter für die Person-ID. "
                    "Beispiel SessionNet: https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg"
                ),
                max_length=500,
                null=True,
            ),
        ),
        # --- Ratsfragen ----------------------------------------------------
        migrations.AddField(
            model_name="publicquestion",
            name="topic",
            field=models.CharField(
                choices=[
                    ("verkehr", "Verkehr & Mobilität"),
                    ("bauen", "Bauen, Wohnen & Stadtentwicklung"),
                    ("umwelt", "Umwelt & Klima"),
                    ("bildung", "Bildung, Kinder & Jugend"),
                    ("soziales", "Soziales & Gesundheit"),
                    ("kultur", "Kultur, Sport & Freizeit"),
                    ("finanzen", "Finanzen & Haushalt"),
                    ("wirtschaft", "Wirtschaft & Arbeit"),
                    ("sicherheit", "Sicherheit & Ordnung"),
                    ("digitales", "Digitalisierung & Verwaltung"),
                    ("sonstiges", "Sonstiges"),
                ],
                default="sonstiges",
                max_length=30,
                verbose_name="Themenbereich",
            ),
        ),
        migrations.AddField(
            model_name="publicquestion",
            name="published_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Veröffentlicht am"),
        ),
        migrations.AddIndex(
            model_name="publicquestion",
            index=models.Index(fields=["body", "topic"], name="insight_pub_body_id_8213a5_idx"),
        ),
        migrations.RunPython(apply_photo_presets, migrations.RunPython.noop),
        migrations.RunPython(backfill_published_at, migrations.RunPython.noop),
    ]
