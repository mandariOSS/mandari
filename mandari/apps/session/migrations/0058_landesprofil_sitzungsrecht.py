# Landesprofil als Sitzungsrecht (Issue #757): Fassungen mit Stichtag, Ortsrecht je Körperschaft und
# Korrekturen am Hybridteil.
#
# Schema: neue Tabelle der Fassungen (Referenzdaten) und neue Spalten mit Standardwert auch in der Datenbank –
# Ortsrecht der Körperschaft (nullbar), örtliche Regel zur Zuschaltung am Gremium, Widerspruch gegen Bild- und
# Tonaufnahmen an der Person, Verantwortungsbereich einer Störung. Ein Rückfall per Image ohne Migrationsrückbau
# legt weiter Körperschaften, Gremien, Personen und Störungen an; älterer Code liest die neuen Spalten nicht.
#
# Daten: Landesprofile und Fassungen aus apps/session/presets/landesprofile.json neu übernehmen (Niedersachsen:
# Fassungen ab 07.05.2026 und 01.11.2026; digitale Sitzungen nach § 182 NKomVG „nur in Notlagen“ statt
# „ungeklärt“). Idempotent wie `manage.py session_state_profiles --sync`; die Ladelogik ist hier eingefroren.
# Rücknahme: nichts zu tun (Referenzdaten; ein älteres Image kennt die Fassungen nicht).

import json
from datetime import date
from pathlib import Path

import django.db.models.deletion
from django.db import migrations, models

PROFILE_FILE = Path(__file__).resolve().parent.parent / "presets" / "landesprofile.json"


def load_profiles_and_versions(apps, schema_editor):
    profile_model = apps.get_model("session", "SessionStateProfile")
    version_model = apps.get_model("session", "SessionStateProfileVersion")
    known = {f.name for f in profile_model._meta.get_fields() if getattr(f, "concrete", False)}
    data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    stand = date.fromisoformat(data["stand"])
    for entry in data["profile"]:
        values = {key: value for key, value in entry.items() if key in known and key != "code"}
        values["as_of"] = date.fromisoformat(entry["as_of"]) if entry.get("as_of") else stand
        profile_model.objects.update_or_create(code=entry["code"], defaults=values)
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
        ("session", "0057_koerperschaften_zuordnen"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionattendancedisruption",
            name="responsibility",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Nicht festgestellt"),
                    ("municipality", "Im Verantwortungsbereich der Kommune"),
                    ("other", "Außerhalb des Verantwortungsbereichs der Kommune"),
                ],
                db_default="",
                default="",
                max_length=20,
                verbose_name="Verantwortungsbereich",
            ),
        ),
        migrations.AddField(
            model_name="sessionbody",
            name="local_rules",
            field=models.JSONField(
                blank=True,
                default=dict,
                null=True,
                verbose_name="Ortsrecht (Hauptsatzung und Geschäftsordnung)",
            ),
        ),
        migrations.AddField(
            model_name="sessionorganization",
            name="remote_local_rule",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Wie im Landesprofil"),
                    ("excluded", "Laut Hauptsatzung keine Zuschaltung"),
                ],
                db_default="",
                default="",
                help_text="Abweichung der Hauptsatzung für dieses Gremium, z. B. keine hybriden Ausschusssitzungen",
                max_length=20,
                verbose_name="Zuschaltung (örtliche Regel)",
            ),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="recording_objection",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Die Person widerspricht Aufnahmen und Übertragungen ihrer Person in Sitzungen",
                verbose_name="Widerspruch gegen Bild- und Tonaufnahmen",
            ),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="recording_objection_date",
            field=models.DateField(
                blank=True, null=True, verbose_name="Widerspruch vom"
            ),
        ),
        migrations.CreateModel(
            name="SessionStateProfileVersion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("valid_from", models.DateField(verbose_name="Gültig ab")),
                ("title", models.CharField(max_length=255, verbose_name="Bezeichnung")),
                (
                    "amendment",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Änderungsgesetz mit Fundstelle",
                        max_length=255,
                        verbose_name="Änderung",
                    ),
                ),
                (
                    "overrides",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        verbose_name="Abweichende Felder des Landesprofils ab dem Stichtag",
                    ),
                ),
                (
                    "law",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="Sitzungsrecht"
                    ),
                ),
                (
                    "sources",
                    models.JSONField(blank=True, default=list, verbose_name="Quellen"),
                ),
                ("as_of", models.DateField(verbose_name="Stand der Recherche")),
                (
                    "verification",
                    models.CharField(
                        choices=[
                            ("wortlaut", "Gesetzeswortlaut eingesehen"),
                            (
                                "teilweise",
                                "teilweise Wortlaut, teilweise Sekundärquellen",
                            ),
                            ("sekundaer", "nur Sekundärquellen"),
                        ],
                        max_length=20,
                        verbose_name="Prüftiefe",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="session.sessionstateprofile",
                        verbose_name="Landesprofil",
                    ),
                ),
            ],
            options={
                "verbose_name": "Fassung eines Landesprofils",
                "verbose_name_plural": "Fassungen der Landesprofile",
                "db_table": "session_state_profile_versions",
                "ordering": ["profile", "valid_from"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("profile", "valid_from"),
                        name="uniq_state_profile_version",
                    )
                ],
            },
        ),
        migrations.RunPython(load_profiles_and_versions, migrations.RunPython.noop),
    ]
