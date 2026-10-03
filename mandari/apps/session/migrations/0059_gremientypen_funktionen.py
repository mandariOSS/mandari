# Gremientypen, Funktionen und Vorlagen nach Landesrecht (Issue #757).
#
# Neue Auswahlwerte (Ortsrat bzw. Stadtbezirksrat, Gruppe, Jugendbeteiligungsgremium; Ausschuss nach besonderen
# Rechtsvorschriften; Funktionen HVB, ehrenamtliche Stellvertretung, Grundmandat, hinzugewählt, Ortsvorsteher,
# Gemeindedirektor) ändern nur die Auswahl, nicht die Spalten. Neue Spalten haben einen Standard auch in der
# Datenbank bzw. sind nullbar: Art des TOP und des Standard-TOP, Grund des Endes einer Besetzung (Abberufung),
# „volljährig ab“ an der Person. Ein Rückfall per Image ohne Migrationsrückbau legt weiter an; älterer Code zeigt
# die neuen Werte als Rohwert.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("session", "0058_landesprofil_sitzungsrecht"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionagendaitem",
            name="kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Tagesordnungspunkt"),
                    ("residents_questions", "Einwohnerfragestunde"),
                ],
                db_default="",
                default="",
                max_length=30,
                verbose_name="Art des Tagesordnungspunkts",
            ),
        ),
        migrations.AddField(
            model_name="sessionorganizationmembership",
            name="end_reason",
            field=models.CharField(
                blank=True,
                choices=[("", "Kein besonderer Grund"), ("recalled", "Abberufen")],
                db_default="",
                default="",
                max_length=20,
                verbose_name="Grund des Endes",
            ),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="adult_from",
            field=models.DateField(
                blank=True,
                help_text="Nur bei Minderjährigen: Tag des 18. Geburtstags (Ämter, die das Landesrecht erst ab 18 zulässt)",
                null=True,
                verbose_name="Volljährig ab",
            ),
        ),
        migrations.AddField(
            model_name="sessionstandardagendaitem",
            name="kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Tagesordnungspunkt"),
                    ("residents_questions", "Einwohnerfragestunde"),
                ],
                db_default="",
                default="",
                max_length=30,
                verbose_name="Art des Tagesordnungspunkts",
            ),
        ),
        migrations.AlterField(
            model_name="sessionorganization",
            name="committee_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("main", "Hauptausschuss"),
                    ("finance", "Finanzausschuss"),
                    ("audit", "Rechnungsprüfungsausschuss"),
                    ("special", "Ausschuss nach besonderen Rechtsvorschriften"),
                    ("ordinary", "Anderer Ausschuss (keine besondere Art)"),
                ],
                db_default="",
                default="",
                help_text="Für Sitzungsformate: Haupt-, Finanz- und Rechnungsprüfungsausschuss haben im Kommunalrecht teils besondere Regeln",
                max_length=20,
                verbose_name="Gesetzliche Ausschussart",
            ),
        ),
        migrations.AlterField(
            model_name="sessionorganization",
            name="organization_type",
            field=models.CharField(
                choices=[
                    ("committee", "Ausschuss"),
                    ("council", "Rat"),
                    ("local_council", "Ortsrat bzw. Stadtbezirksrat"),
                    ("faction", "Fraktion"),
                    ("group", "Gruppe"),
                    ("advisory", "Beirat"),
                    ("youth_council", "Jugendbeteiligungsgremium"),
                    ("commission", "Kommission"),
                    ("department", "Amt/Fachbereich"),
                    ("other", "Sonstiges"),
                ],
                default="committee",
                max_length=100,
                verbose_name="Typ",
            ),
        ),
        migrations.AlterField(
            model_name="sessionorganizationmembership",
            name="role",
            field=models.CharField(
                choices=[
                    ("member", "Mitglied"),
                    ("chair", "Vorsitzende/r"),
                    ("deputy_chair", "Stellv. Vorsitzende/r"),
                    ("expert_citizen", "Sachkundige/r Bürger/in"),
                    ("advisor", "Beratendes Mitglied"),
                    ("hvb", "Hauptverwaltungsbeamtin/-beamter (kraft Amtes)"),
                    ("hvb_deputy", "Ehrenamtliche Stellvertretung des HVB"),
                    ("basic_mandate", "Grundmandat (beratend)"),
                    ("co_opted", "Hinzugewählt (ohne Stimmrecht)"),
                    ("local_mayor", "Ortsvorsteher/in"),
                    ("municipal_director", "Gemeindedirektor/in"),
                    ("guest", "Gast"),
                ],
                default="member",
                max_length=100,
                verbose_name="Funktion",
            ),
        ),
    ]
