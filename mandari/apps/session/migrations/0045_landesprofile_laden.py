# Landesprofile für alle 16 Länder übernehmen (Issue #138).
#
# Quelle ist apps/session/presets/landesprofile.json. Die Übernahme ist idempotent (update_or_create je
# Länderkürzel) und übergeht Schlüssel, die das historische Modell nicht kennt. Spätere Änderungen der
# Rechtslage kommen über dieselbe Datei und `manage.py session_state_profiles --sync` bzw. eine weitere
# Datenmigration. Rücknahme: nichts zu tun (die Tabelle entfällt mit 0044).
#
# Die Ladelogik ist hier bewusst eingefroren (nicht meeting_format_service.sync_profiles), damit spätere
# Änderungen am Service diese Migration auf einer frischen Datenbank nicht verändern. Die Datei selbst
# muss dafür lesbar bleiben: Schlüssel "stand" und "profile", je Eintrag "code" und optional "as_of";
# neue Schlüssel sind unschädlich. Der Migrationstest in test_sitzungsformat.py prüft das.

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
        ("session", "0044_sitzungsformat_landesprofil"),
    ]

    operations = [
        migrations.RunPython(load_profiles, migrations.RunPython.noop),
    ]
