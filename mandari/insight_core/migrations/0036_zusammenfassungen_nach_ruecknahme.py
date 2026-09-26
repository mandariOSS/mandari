# KI-Zusammenfassungen verwerfen, die zurückgenommene Inhalte enthalten können.
#
# Bisher flossen alle Anlagen eines Vorgangs in die Zusammenfassung, auch solche, die mandari Session
# später aus der Öffentlichkeit genommen hat. Betroffen sind Zusammenfassungen zurückgenommener
# Vorgänge und von Vorgängen mit einer zurückgenommenen Anlage. Sie entstehen bei Bedarf neu – dann
# nur aus den aktuellen Anlagen. Nur Datenänderung, kein Schemawechsel.

from django.db import migrations
from django.db.models import Q

MARKERS = ("/session/", "/api/oparl/")


def _zurueckgenommen(prefix=""):
    feld = f"{prefix}__" if prefix else ""
    bedingung = Q(**{f"{feld}deleted": True})
    for marker in MARKERS:
        bedingung &= Q(**{f"{feld}external_id__contains": marker})
    return bedingung


def zusammenfassungen_verwerfen(apps, schema_editor):
    OParlPaper = apps.get_model("insight_core", "OParlPaper")
    OParlFile = apps.get_model("insight_core", "OParlFile")
    mit_zusammenfassung = OParlPaper.objects.filter(summary__isnull=False)
    betroffene_anlagen = OParlFile.objects.filter(_zurueckgenommen(), paper__isnull=False).values("paper_id")
    mit_zusammenfassung.filter(_zurueckgenommen() | Q(id__in=betroffene_anlagen)).update(summary=None)


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0035_oparlbody_accent_color"),
    ]

    operations = [
        migrations.RunPython(zusammenfassungen_verwerfen, migrations.RunPython.noop, elidable=True),
    ]
