# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tabellen der früheren Protokollierung (``apps.minutes``) löschen, nach dem Deploy.

Das Modul hatte nur Datenmodell, Orchestrator der Rechenknoten und Verwaltung im Admin, aber nie eine Oberfläche.
Die Protokollierung ist jetzt ein eigener Dienst, den Session über eine Schnittstelle anbindet
(``docs/PROTOKOLLDIENST.md``); ``apps.minutes`` ist aus dem Code entfernt.

Die Migration liegt in ``common``, weil das Modul nicht mehr installiert ist und seine eigenen Migrationen nicht
mehr laufen. Sie ist ``Safe.after_deploy()``: ``safemigrate`` lässt sie vor dem Umschalten aus, denn das laufende
Image (vor dem Rückbau) liest die Tabellen noch (Metriken, Service-Level-Prüfung, Orchestrator). ``update.sh`` spielt
sie mit den übrigen Migrationen nach dem Umschalten ein, ``deploy.sh`` braucht dafür einmal ``manage.py migrate``.

Sie löscht nur, was es gibt (``IF EXISTS``): Frische Installationen haben die Tabellen nie bekommen. Danach streicht
sie die Einträge des Moduls aus ``django_migrations``: Ein Image vor dem Rückbau, auf das jemand zurückfällt, legt die
Tabellen mit ``migrate`` dann wieder leer an, statt sie für vorhanden zu halten. Rückwärts tut sie nichts; Daten gab es
ohne Oberfläche keine, die Tabellen kommen nur mit einem alten Image zurück.
"""

from typing import Any

from django.db import migrations
from django_safemigrate import Safe

#: Tabellen von ``apps.minutes`` (Migrationen minutes/0001 und 0002), abhängige vor den Tabellen, auf die sie zeigen
TABELLEN = (
    "minutes_protocol_drafts",
    "minutes_transcript_segments",
    "minutes_voice_profiles",
    "minutes_speaker_consents",
    "minutes_transcription_jobs",
    "minutes_recording_segments",
    "minutes_recordings",
    "minutes_protocol_styles",
    "minutes_gpu_nodes",
    "minutes_compute_settings",
)
APP_LABEL = "minutes"


def tabellen_loeschen(apps: Any, schema_editor: Any) -> None:
    verbindung = schema_editor.connection
    # Nur PostgreSQL kennt CASCADE; die Fremdschlüssel zeigen ohnehin nur aus diesen Tabellen hinaus
    zusatz = " CASCADE" if verbindung.vendor == "postgresql" else ""
    for tabelle in TABELLEN:
        schema_editor.execute(f"DROP TABLE IF EXISTS {schema_editor.quote_name(tabelle)}{zusatz}")
    with verbindung.cursor() as cursor:
        cursor.execute("DELETE FROM django_migrations WHERE app = %s", [APP_LABEL])


class Migration(migrations.Migration):
    safe = Safe.after_deploy()

    dependencies = [
        ("common", "0015_systemeinstellungen_spalten_entfernen"),
    ]

    operations = [
        migrations.RunPython(tabellen_loeschen, migrations.RunPython.noop, elidable=True),
    ]
