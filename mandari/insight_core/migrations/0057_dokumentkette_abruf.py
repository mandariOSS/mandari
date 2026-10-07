# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentkette (Issue #919, docs/adr/20261007-dokumentkette.md): Zustände des Abrufs und Herkunft des Texts.

Spalten (alle Spalten der Welle in einer Migration):

- ``fetch_attempts`` (Standard 0 in der Datenbank), ``fetch_next_at`` (nullbar) und ``fetch_error`` (Standard
  leer): Fehlschläge des Abrufs in Folge, frühester nächster Versuch und Fehlercode (``hub/ris/abruf.py``).
- ``text_source_sha256`` und ``text_extraction_version`` (nullbar): aus welchem Inhalt und mit welcher Version
  der Bibliothek ``mandari_dokumente`` der Text stammt (Auftrag ``file.extract_text``).
- ``local_status`` bekommt die Zustände ``fetching``, ``retry`` und ``refused`` (nur Auswahlliste, keine Änderung
  in der Datenbank); ``evicted`` aus ``0056_dokumentcache_grenze`` (Obergrenze des Dokument-Caches, #961) bleibt.

Nummer 0057: hängt an ``0056_dokumentcache_grenze`` (#971, geht zuerst nach ``dev``).

Abwärtskompatibel: Ingestor-INSERTs und ein älteres Image kennen die Spalten nicht und schreiben sie nie; in
PostgreSQL ohne Umschreiben der Tabelle (konstante Standardwerte).

Datenmigration: Abrufe, die die Quelle verweigert (robots.txt, HTML-Seite statt der Datei), standen bisher als
``error`` da und werden ``refused`` mit Fehlercode ``robots`` bzw. ``html``. ``local_error`` bleibt unverändert,
es geht nichts verloren. Rückweg (auch ``manage.py dokumentkette zuruecksetzen`` für einen Rückfall per Image
ohne Rückbau der Migration): ``refused`` wird wieder ``error`` mit unverändertem Fehlertext, ``retry`` und
``fetching`` werden ``none``; ``evicted`` bleibt (sonst lüde ein älteres Image verdrängte Dokumente nach).
Idempotent; betroffen sind nur Zustandsspalten.
"""

from django.db import migrations, models

#: Anfang des Fehlertexts einer Sperre durch die robots.txt (``mandari_oparl.robots.SKIP_ERROR_PREFIX``)
ROBOTS_PREFIX = "robots.txt"
#: Fehlertext, wenn die Quelle eine HTML-Seite statt der Datei liefert (bisher in ``file_cache.fetch_and_cache``)
HTML_TEXT = "Quelle liefert eine HTML-Seite statt der Datei"


def verweigert_markieren(apps, schema_editor):
    datei = apps.get_model("insight_core", "OParlFile")
    datei.objects.filter(local_status="error", local_error__startswith=ROBOTS_PREFIX).update(
        local_status="refused", fetch_error="robots"
    )
    datei.objects.filter(local_status="error", local_error=HTML_TEXT).update(local_status="refused", fetch_error="html")


def zuruecksetzen(apps, schema_editor):
    datei = apps.get_model("insight_core", "OParlFile")
    datei.objects.filter(local_status="refused").update(local_status="error")
    datei.objects.filter(local_status__in=["retry", "fetching"]).update(local_status="none")


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0056_dokumentcache_grenze"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlfile",
            name="fetch_attempts",
            field=models.IntegerField(
                db_default=0,
                default=0,
                help_text="Bei Erfolg wieder 0",
                verbose_name="Fehlgeschlagene Abrufe in Folge",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="fetch_error",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=40,
                verbose_name="Fehlercode des Abrufs",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="fetch_next_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Wiederholung des Abrufs; während eines Abrufs das Ende der Beanspruchung",
                null=True,
                verbose_name="Nächster Abruf frühestens",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_version",
            field=models.CharField(
                blank=True,
                max_length=40,
                null=True,
                verbose_name="Version der Texterkennung",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_source_sha256",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                verbose_name="Text erkannt aus Inhalt (SHA-256)",
            ),
        ),
        migrations.AlterField(
            model_name="oparlfile",
            name="local_status",
            field=models.CharField(
                choices=[
                    ("none", "Nicht zwischengespeichert"),
                    ("fetching", "Wird abgerufen"),
                    ("ok", "Lokal vorhanden"),
                    ("retry", "Abruf wird wiederholt"),
                    ("missing", "Quelle liefert 404/410"),
                    ("refused", "Quelle verweigert den Abruf"),
                    ("error", "Fehler beim Abruf"),
                    ("too_large", "Zu groß für den Cache"),
                    ("evicted", "Verdrängt (bei Bedarf neu abrufbar)"),
                ],
                db_default="none",
                db_index=True,
                default="none",
                max_length=20,
                verbose_name="Lokale Kopie",
            ),
        ),
        migrations.RunPython(verweigert_markieren, zuruecksetzen, elidable=False),
    ]
