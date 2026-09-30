# Landesprofile für alle 16 Länder übernehmen (Issue #138).
#
# Quelle ist apps/session/presets/landesprofile.json. Die Übernahme ist idempotent (update_or_create je
# Länderkürzel) und übergeht Schlüssel, die das historische Modell nicht kennt. Spätere Änderungen der
# Rechtslage kommen über dieselbe Datei und `manage.py session_state_profiles --sync` bzw. eine weitere
# Datenmigration mit demselben Aufruf. Rücknahme: nichts zu tun (die Tabelle entfällt mit 0044).

from django.db import migrations


def load_profiles(apps, schema_editor):
    from apps.session.services.meeting_format_service import sync_profiles

    sync_profiles(apps.get_model("session", "SessionStateProfile"))


class Migration(migrations.Migration):

    dependencies = [
        ("session", "0044_sitzungsformat_landesprofil"),
    ]

    operations = [
        migrations.RunPython(load_profiles, migrations.RunPython.noop),
    ]
