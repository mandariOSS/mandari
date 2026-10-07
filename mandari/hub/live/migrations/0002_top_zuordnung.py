# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP-Zuordnung mit Titelprüfung (Teil von Issue #47).

1. ``BroadcastSection`` bekommt die gelesene Nummer (``number_read``) und die Sicherheit der Zuordnung
   (``confidence``). Beide mit ``db_default=""``: Ein älteres Image legt Abschnitte ohne diese Felder an
   (Rückfall ohne Rückbau). Bestehende Abschnitte bleiben unverändert (leer = vor der Titelprüfung).
2. Gespeicherte Profile aus der Vorlage „balken_unten_dreizeilig“ bekommen deren neue Ausschnitte: Titel ab 0.545
   statt 0.555 (der erste Buchstabe fehlte), TOP-Feld bis 0.545, damit es nicht in den Titel ragt. Geändert wird
   nur, wenn **beide** Ausschnitte noch genau den alten Werten der Vorlage entsprechen; angepasste Profile bleiben,
   wie sie sind. Sonst ändert sich nichts am Profil (ein älteres Image liest es weiter). Rückweg: dieselbe Prüfung
   mit vertauschten Werten. Idempotent.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations, models

ALT_TOP = (0.36, 0.87, 0.555, 0.94)
NEU_TOP = (0.36, 0.87, 0.545, 0.94)
ALT_TITEL = (0.555, 0.82, 1.0, 0.955)
NEU_TITEL = (0.545, 0.82, 1.0, 0.955)


def _gleich(box: object, werte: tuple[float, ...]) -> bool:
    if not isinstance(box, list | tuple) or len(box) != len(werte):
        return False
    try:
        return all(abs(float(a) - b) < 1e-9 for a, b in zip(box, werte, strict=True))
    except (TypeError, ValueError):
        return False


def _ausschnitte(apps: Any, von: tuple[tuple[float, ...], ...], nach: tuple[tuple[float, ...], ...]) -> int:
    """Setzt ``felder.top.box``/``felder.titel.box`` von ``von`` auf ``nach``, wenn beide passen; Rückgabe: Anzahl."""
    quelle_modell = apps.get_model("hub_live", "BroadcastSource")
    geaendert = 0
    for quelle in quelle_modell.objects.order_by("pk"):
        profil = quelle.overlay_profile
        felder = profil.get("felder") if isinstance(profil, dict) else None
        if not isinstance(felder, dict):
            continue
        top, titel = felder.get("top"), felder.get("titel")
        if not (isinstance(top, dict) and isinstance(titel, dict)):
            continue
        if not (_gleich(top.get("box"), von[0]) and _gleich(titel.get("box"), von[1])):
            continue
        top["box"], titel["box"] = list(nach[0]), list(nach[1])
        quelle.overlay_profile = profil
        quelle.save(update_fields=["overlay_profile"])
        geaendert += 1
    return geaendert


def vorwaerts(apps: Any, schema_editor: Any) -> None:
    _ausschnitte(apps, (ALT_TOP, ALT_TITEL), (NEU_TOP, NEU_TITEL))


def rueckwaerts(apps: Any, schema_editor: Any) -> None:
    _ausschnitte(apps, (NEU_TOP, NEU_TITEL), (ALT_TOP, ALT_TITEL))


class Migration(migrations.Migration):
    dependencies = [
        ("hub_live", "0001_live_uebertragung"),
    ]

    operations = [
        migrations.AddField(
            model_name="broadcastsection",
            name="confidence",
            field=models.CharField(
                blank=True,
                choices=[
                    ("nummer_titel", "Nummer und Titel passen"),
                    ("titel", "nur der Titel passt"),
                    ("nummer", "nur die Nummer (niedrig)"),
                    ("keine", "kein Tagesordnungspunkt"),
                ],
                db_default="",
                default="",
                help_text="leer = vor der Titelprüfung",
                max_length=20,
                verbose_name="Sicherheit der Zuordnung",
            ),
        ),
        migrations.AddField(
            model_name="broadcastsection",
            name="number_read",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="leer = vor der Titelprüfung",
                max_length=20,
                verbose_name="gelesene Nummer",
            ),
        ),
        migrations.AlterField(
            model_name="broadcastsection",
            name="number",
            field=models.CharField(
                help_text="des zugeordneten Tagesordnungspunkts, sonst die gelesene",
                max_length=20,
                verbose_name="TOP-Nummer",
            ),
        ),
        migrations.RunPython(vorwaerts, rueckwaerts),
    ]
