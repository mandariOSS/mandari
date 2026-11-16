# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen im Bestand mit geprüften Angaben (Issue #1441).

Der Ingestor prüft Name, Absage und Ende einer Sitzung seit diesem Stand beim Schreiben (``mandari_oparl.qualitaet``):
Eine Absage, die die Quelle nur in den Namen schreibt, setzt ``cancelled`` und fällt aus dem Namen; ein Ende, das nicht
nach dem Beginn liegt, gilt als nicht angegeben. Der inkrementelle Abgleich fasst unveränderte Sitzungen aber nicht an.
Ohne diese Korrektur blieben sie bis zum nächsten Vollabgleich in Kalender, Suche und KI-Assistent falsch: abgesagte
Sitzungen als stattfindend, Sitzungen mit einem Ende vor ihrem Beginn.

- Dieselben Regeln wie Ingestor und Ausgabe, mit dem Namen der Quelle aus ``raw_json`` (ohne Rohdaten der gespeicherte).
- Eigene Migration ohne umschließende Transaktion, in Stapeln, ohne serverseitigen Cursor (PgBouncer im
  Transaktionsmodus). ``updated_at`` bleibt, wie es ist.
- Idempotent und ``elidable``: Auf einer neuen Installation gibt es nichts zu korrigieren.
- Rückwärts nichts: Die Angaben der Quelle stehen unverändert in ``raw_json``.
"""

from django.db import migrations
from django.db.models import F

BATCH = 500
#: Vorauswahl in der Datenbank; ob es wirklich ein Absagevermerk ist, entscheidet ``sitzungsangaben``
ABSAGE = r"abgesagt|entf(ä|ae)llt|f(ä|ae)llt\s+aus"


def pruefen(apps, schema_editor):
    from mandari_oparl.qualitaet import sitzungsangaben

    meeting = apps.get_model("insight_core", "OParlMeeting")

    # 1. Ende nicht nach dem Beginn: gilt als nicht angegeben
    ids = list(
        meeting.objects.filter(start__isnull=False, end__isnull=False, end__lte=F("start")).values_list("pk", flat=True)
    )
    for start in range(0, len(ids), BATCH):
        meeting.objects.filter(pk__in=ids[start : start + BATCH]).update(end=None)

    # 2. Absage nur im Namen: abgesagt, Name ohne Vermerk
    ids = list(meeting.objects.filter(name__iregex=ABSAGE).values_list("pk", flat=True))
    for start in range(0, len(ids), BATCH):
        geaendert = []
        sitzungen = meeting.objects.filter(pk__in=ids[start : start + BATCH]).only(
            "pk", "name", "cancelled", "meeting_state", "raw_json"
        )
        for sitzung in sitzungen:
            roh = sitzung.raw_json if isinstance(sitzung.raw_json, dict) else {}
            quellname = roh.get("name")
            angaben = sitzungsangaben(
                {
                    "name": quellname if isinstance(quellname, str) and quellname else sitzung.name,
                    "cancelled": roh.get("cancelled"),
                    "meetingState": sitzung.meeting_state,
                }
            )
            if angaben.vermerk is None:
                continue
            sitzung.name = angaben.name
            sitzung.cancelled = True
            geaendert.append(sitzung)
        meeting.objects.bulk_update(geaendert, ["name", "cancelled"])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("insight_core", "0071_zusammenfassung_prompt_fassung"),
    ]

    operations = [
        migrations.RunPython(pruefen, migrations.RunPython.noop, elidable=True),
    ]
