# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schatten-Quelle des RIS-Projektors für Session-Mandanten (Issue #536).

Je Objekt eines Session-Mandanten die Zeile, die der Projektor in den RIS-Bestand schriebe: die fachlichen Spalten
(``hub.ris.uebernahme``) und die Bezüge auf andere Zeilen über deren Adresse. Die Tabelle steht bewusst **neben** dem
RIS-Bestand (``oparl_*``): Bürgerportal, Suche, Sitemaps, OParl-Schnittstelle, Änderungsfeed, Snapshot, Work,
Ingestor und die Aufträge des Bestands (Texterkennung, Zusammenfassung, Abo-Treffer) lesen sie nicht. Lesen dürfen
sie nur der Projektor und der Vergleich (``hub.projections``); ein Test hält das fest.

Gelöschte bzw. nicht mehr öffentliche Objekte bleiben als Zeile mit Grund stehen, ohne Spalten und Bezüge.
"""

from __future__ import annotations

from django.db import models


class RisSchatten(models.Model):
    """Ein Objekt der Schatten-Quelle (Zeile, die der Projektor in den RIS-Bestand schriebe)."""

    id = models.BigAutoField(primary_key=True)
    mandant = models.UUIDField("Session-Mandant")
    typ = models.CharField("Objekttyp", max_length=20, help_text="Segment der OParl-Adresse, z. B. meeting")
    bestand_id = models.UUIDField("Kennung im RIS-Bestand", help_text="kanonische Kennung, wie im Journal")
    external_id = models.TextField("Adresse", help_text="OParl-Adresse, unter der der Bestand das Objekt führt")
    spalten = models.JSONField("Spalten", default=dict, help_text="fachliche Spalten des Bestands")
    verweise = models.JSONField("Bezüge", default=dict, help_text="Adressen der Kommune, Sitzung, Vorlage …")
    deleted = models.BooleanField("gelöscht bzw. zurückgenommen", default=False)
    deletion_reason = models.CharField("Grund", max_length=20, blank=True, null=True)
    oparl_modified = models.DateTimeField("geändert laut Quelle", blank=True, null=True)
    seq = models.BigIntegerField("Folgenummer", blank=True, null=True, help_text="zuletzt verarbeitetes Ereignis")
    updated_at = models.DateTimeField("aktualisiert am", auto_now=True)

    class Meta:
        db_table = "hub_ris_schatten"
        verbose_name = "Objekt der Schatten-Quelle"
        verbose_name_plural = "Schatten-Quelle des RIS-Projektors"
        constraints = [models.UniqueConstraint(fields=["mandant", "bestand_id"], name="hub_ris_schatten_kennung")]
        indexes = [models.Index(fields=["mandant", "typ"], name="hub_ris_schatten_typ")]

    def __str__(self) -> str:
        return f"{self.typ} {self.bestand_id}"
