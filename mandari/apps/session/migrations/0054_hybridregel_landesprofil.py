# Hybridregel im Landesprofil (Issue #754): Regelart „in der Sitzung unzulässig“, Umfang der Regel für Wahlen,
# Regel für geheimhaltungspflichtige Angelegenheiten, Norm für den Hinweis und das TOP-Merkmal
# „geheimhaltungspflichtig“. Nur neue Spalten mit Standardwert (auch in der Datenbank) und geänderte Auswahlen:
# Ein Rückfall per Image ohne Migrationsrückbau legt weiter Profile und TOPs an; älterer Code liest den Wert
# „meeting“ wie „ungeklärt“ (nur Hinweis). Die Werte der Länder übernimmt 0055.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("session", "0053_dcat_kennung"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionagendaitem",
            name="requires_secrecy",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Geheimhaltung gesetzlich vorgeschrieben oder behördlich angeordnet (z. B. § 6 Abs. 3 Satz 1 NKomVG); nicht dasselbe wie nichtöffentlich",
                verbose_name="Geheimhaltungspflichtig",
            ),
        ),
        migrations.AddField(
            model_name="sessionstateprofile",
            name="remote_elections_scope",
            field=models.CharField(
                choices=[("all", "alle Wahlen"), ("secret_only", "nur geheime Wahlen")],
                db_default="all",
                default="all",
                help_text="z. B. Niedersachsen: nur geheime Wahlen (§ 67 Satz 2 NKomVG); offene Wahlen bleiben möglich",
                max_length=20,
                verbose_name="Regel für Wahlen gilt für",
            ),
        ),
        migrations.AddField(
            model_name="sessionstateprofile",
            name="remote_secrecy_matters",
            field=models.CharField(
                choices=[
                    ("allowed", "zulässig"),
                    ("excluded", "für Zugeschaltete ausgeschlossen"),
                    (
                        "meeting",
                        "in der Sitzung unzulässig, sobald jemand zugeschaltet ist",
                    ),
                    ("conditional", "nur unter Bedingungen"),
                    ("unclear", "ungeklärt"),
                ],
                db_default="unclear",
                default="unclear",
                help_text="Beratung von Angelegenheiten, deren Geheimhaltung gesetzlich vorgeschrieben oder behördlich angeordnet ist (z. B. § 6 Abs. 3 Satz 1 NKomVG)",
                max_length=20,
                verbose_name="Geheimhaltungspflichtige Angelegenheiten mit Zugeschalteten",
            ),
        ),
        migrations.AddField(
            model_name="sessionstateprofile",
            name="remote_vote_norm",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Erscheint im Hinweis, wenn eine Abstimmung gesperrt ist",
                max_length=255,
                verbose_name="Norm zu Wahlen und geheimen Abstimmungen",
            ),
        ),
        migrations.AlterField(
            model_name="sessionstateprofile",
            name="remote_elections",
            field=models.CharField(
                choices=[
                    ("allowed", "zulässig"),
                    ("excluded", "für Zugeschaltete ausgeschlossen"),
                    (
                        "meeting",
                        "in der Sitzung unzulässig, sobald jemand zugeschaltet ist",
                    ),
                    ("conditional", "nur unter Bedingungen"),
                    ("unclear", "ungeklärt"),
                ],
                max_length=20,
                verbose_name="Wahlen für Zugeschaltete",
            ),
        ),
        migrations.AlterField(
            model_name="sessionstateprofile",
            name="remote_secret_votes",
            field=models.CharField(
                choices=[
                    ("allowed", "zulässig"),
                    ("excluded", "für Zugeschaltete ausgeschlossen"),
                    (
                        "meeting",
                        "in der Sitzung unzulässig, sobald jemand zugeschaltet ist",
                    ),
                    ("conditional", "nur unter Bedingungen"),
                    ("unclear", "ungeklärt"),
                ],
                max_length=20,
                verbose_name="Geheime Abstimmungen für Zugeschaltete",
            ),
        ),
    ]
