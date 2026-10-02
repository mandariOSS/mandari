# Landesprofile nach der Hybridregel neu übernehmen (Issue #754).
#
# Niedersachsen, Sachsen-Anhalt und Saarland sperren geheime Wahlen bzw. Abstimmungen für die ganze Sitzung,
# sobald jemand zugeschaltet teilnimmt; Brandenburg, Rheinland-Pfalz und Hessen sind am Wortlaut geprüft. Die
# Profile sind Referenzdaten für alle Mandanten (Fremdschlüssel am Mandanten); mit ihnen ändert sich die Regel
# für jeden Bestandsmandant.
#
# Quelle ist apps/session/presets/landesprofile.json. Idempotent (update_or_create je Länderkürzel, übergeht
# Schlüssel, die das historische Modell nicht kennt) – dasselbe wie `manage.py session_state_profiles --sync`.
# Rücknahme: nichts zu tun (ein Rückfall per Image liest die neuen Werte wie „ungeklärt“). Die Ladelogik ist wie in
# 0045 eingefroren, damit spätere Änderungen am Service diese Migration nicht verändern.

import json
from datetime import date
from pathlib import Path

from django.db import migrations

PROFILE_FILE = Path(__file__).resolve().parent.parent / "presets" / "landesprofile.json"


def load_profiles(apps, schema_editor):
    profile_model = apps.get_model("session", "SessionStateProfile")
    known = {f.name for f in profile_model._meta.get_fields() if getattr(f, "concrete", False)}
    data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    stand = date.fromisoformat(data["stand"])
    for entry in data["profile"]:
        values = {key: value for key, value in entry.items() if key in known and key != "code"}
        values["as_of"] = date.fromisoformat(entry["as_of"]) if entry.get("as_of") else stand
        profile_model.objects.update_or_create(code=entry["code"], defaults=values)


class Migration(migrations.Migration):

    dependencies = [
        ("session", "0054_hybridregel_landesprofil"),
    ]

    operations = [
        migrations.RunPython(load_profiles, migrations.RunPython.noop),
    ]
