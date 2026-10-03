# Öffentlichkeit der Gremien (Issue #757): Vorgabe je Gremium, Termine nichtöffentlicher Sitzungen veröffentlichen.
#
# Schema: neue Spalten mit Standard auch in der Datenbank (Rückfall per Image ohne Migrationsrückbau legt weiter
# Gremien und Sitzungen an; ein älteres Image veröffentlicht keine Termine, weil es die Spalte nicht liest).
#
# Daten: Fassungen der Landesprofile erneut aus apps/session/presets/landesprofile.json übernehmen (neuer Eintrag
# „Vorsitz im Hauptausschuss“ für Niedersachsen) – idempotent, Ladelogik wie in 0058 eingefroren.

import json
from datetime import date
from pathlib import Path

from django.db import migrations, models

PROFILE_FILE = Path(__file__).resolve().parent.parent / "presets" / "landesprofile.json"


def reload_versions(apps, schema_editor):
    profile_model = apps.get_model("session", "SessionStateProfile")
    version_model = apps.get_model("session", "SessionStateProfileVersion")
    known = {f.name for f in profile_model._meta.get_fields() if getattr(f, "concrete", False)}
    data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    present = set(profile_model.objects.values_list("code", flat=True))
    for entry in data["profile"]:
        if entry["code"] not in present:
            continue
        days = []
        for raw in entry.get("versions") or []:
            valid_from = date.fromisoformat(raw["valid_from"])
            as_of = date.fromisoformat(raw["as_of"])
            sources = list(raw.get("sources") or [])
            law = {}
            for key, item in (raw.get("law") or {}).items():
                source = item.get("source", 0)
                if isinstance(source, int):
                    source = sources[source]
                law[key] = {
                    "value": item.get("value"),
                    "text": str(item.get("text") or "").strip(),
                    "norm": str(item.get("norm") or "").strip(),
                    "source": {"title": str(source.get("title") or ""), "url": str(source["url"])},
                    "as_of": str(item.get("as_of") or as_of.isoformat()),
                    "unclear": bool(item.get("unclear", False)),
                }
            version_model.objects.update_or_create(
                profile_id=entry["code"],
                valid_from=valid_from,
                defaults={
                    "title": str(raw["title"]),
                    "amendment": str(raw.get("amendment") or ""),
                    "overrides": {k: v for k, v in (raw.get("overrides") or {}).items() if k in known},
                    "law": law,
                    "sources": sources,
                    "as_of": as_of,
                    "verification": str(raw.get("verification") or "teilweise"),
                },
            )
            days.append(valid_from)
        version_model.objects.filter(profile_id=entry["code"]).exclude(valid_from__in=days).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("session", "0064_gremientypen_funktionen"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionmeeting",
            name="date_public",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Nur bei nichtöffentlichen Sitzungen: Datum und Gremium öffentlich, ohne Inhalte",
                verbose_name="Termin veröffentlichen",
            ),
        ),
        migrations.AddField(
            model_name="sessionorganization",
            name="publicity",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Nach Landesrecht und Geschäftsordnung"),
                    ("public", "Öffentlich"),
                    ("non_public", "Nichtöffentlich"),
                ],
                db_default="",
                default="",
                help_text="Vorgabe für neue Sitzungen",
                max_length=20,
                verbose_name="Öffentlichkeit der Sitzungen",
            ),
        ),
        migrations.AddField(
            model_name="sessionorganization",
            name="publish_dates",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Datum, Uhrzeit und Gremium erscheinen in der OParl-Schnittstelle und im Bürgerportal, ohne Tagesordnung, Ort und Unterlagen",
                verbose_name="Termine nichtöffentlicher Sitzungen veröffentlichen",
            ),
        ),
        migrations.RunPython(reload_versions, migrations.RunPython.noop),
    ]
