# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Basisadresse der Kennungen einmal je Installation festschreiben (Issue #733).

1. Neue Tabelle mit genau einer Zeile (``IdentifierBase``).
2. Die Zeile übernimmt den heutigen Wert von ``SITE_URL``. Aus dieser Adresse sind alle bisherigen
   Kennungen der Session-Objekte gebildet; sie bleiben deshalb unverändert. Ist schon eine Basis
   festgelegt, bleibt sie (wiederholbar).

Abwärtskompatibel: Ein älteres Image kennt die Tabelle nicht und bildet die Kennungen wie bisher aus
``SITE_URL`` – solange sich ``SITE_URL`` nicht ändert, sind beide gleich.
"""

from django.conf import settings
from django.db import migrations, models


def basis_festlegen(apps, schema_editor):
    IdentifierBase = apps.get_model("common", "IdentifierBase")
    zeilen = IdentifierBase.objects.using(schema_editor.connection.alias)
    if not zeilen.filter(pk=1).exists():
        basis = str(getattr(settings, "SITE_URL", "") or "http://localhost:8000").rstrip("/")
        zeilen.create(pk=1, url=basis)


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0008_verdeckte_mail_backend_wahl_nur_aus_dem_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="IdentifierBase",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "url",
                    models.CharField(
                        help_text="Ohne abschließenden Schrägstrich, z. B. https://mandari.de",
                        max_length=500,
                        verbose_name="Basisadresse",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Festgelegt am")),
            ],
            options={
                "verbose_name": "Basisadresse der Kennungen",
                "verbose_name_plural": "Basisadresse der Kennungen",
            },
        ),
        migrations.RunPython(basis_festlegen, migrations.RunPython.noop),
    ]
