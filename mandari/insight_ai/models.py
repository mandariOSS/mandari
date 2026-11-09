# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassungen mit der Claude API: eingereichte Message Batches und Verbrauch (Grundlage des Monatsbudgets), dazu
das Protokoll verworfener Zusammenfassungen (mechanische Prüfung, Issue #1271).

Beides gehört den KI-Funktionen. Die Zusammenfassungen selbst stehen am Vorgang (``OParlPaper.summary``) und werden
nur über ``insight_core.services.summary_store`` geschrieben.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils import timezone


class ZusammenfassungsBatch(models.Model):
    """Ein Message Batch der Claude API mit Zusammenfassungen (``insight_ai.services.zusammenfassung_batch``)."""

    class Status(models.TextChoices):
        EINGEREICHT = "eingereicht", "Eingereicht"
        ABGEHOLT = "abgeholt", "Ergebnisse übernommen"
        VERLOREN = "verloren", "Ergebnisse nicht mehr abrufbar"

    batch_id = models.CharField("Batch-Kennung", max_length=100, unique=True)
    modell = models.CharField("Modell", max_length=100)
    status = models.CharField(
        "Status", max_length=20, choices=Status.choices, default=Status.EINGEREICHT, db_index=True
    )
    eingereicht_am = models.DateTimeField(
        "Eingereicht am", help_text="Beginn der Erstellung: Neuerer Text verwirft das Ergebnis eines Vorgangs."
    )
    abgeholt_am = models.DateTimeField("Übernommen am", null=True, blank=True)
    #: ``custom_id`` → ``{"vorgang": <uuid>, "dateien": [<uuid>, …], "stand": …, "fassung": <int>}``: verwendete
    #: Anlagen, Beratungsstand und Prompt-Fassung (Issue #1271) je Anfrage; ``"ersetzt"``: Stand der veralteten
    #: Zusammenfassung, die sie ersetzt (Issue #1257)
    anfragen = models.JSONField("Anfragen", default=dict, blank=True)
    anzahl = models.PositiveIntegerField("Anfragen", default=0)
    gespeichert = models.PositiveIntegerField("Gespeichert", default=0)
    verworfen = models.PositiveIntegerField(
        "Verworfen", default=0, help_text="Erstellt, aber nicht gespeichert (neuer Text, Rücknahme, schon vorhanden)."
    )
    fehlgeschlagen = models.PositiveIntegerField("Fehlgeschlagen", default=0)
    geschaetzt_usd = models.DecimalField(
        "Geschätzte Kosten (USD)",
        max_digits=12,
        decimal_places=6,
        default=Decimal(0),
        help_text="Obergrenze beim Einreichen; bis zur Übernahme im Monatsbudget reserviert.",
    )
    kosten_usd = models.DecimalField("Kosten (USD)", max_digits=12, decimal_places=6, default=Decimal(0))

    class Meta:
        verbose_name = "Zusammenfassungs-Batch (Claude)"
        verbose_name_plural = "Zusammenfassungs-Batches (Claude)"
        ordering = ("-eingereicht_am",)

    def __str__(self) -> str:
        return f"{self.batch_id} ({self.get_status_display()}, {self.anzahl} Anfragen)"


class KiVerbrauch(models.Model):
    """Verbrauch eines Aufrufs der Claude API für Zusammenfassungen (Einzelaufruf oder Ergebnis eines Batches)."""

    class Art(models.TextChoices):
        SOFORT = "sofort", "Einzelaufruf"
        BATCH = "batch", "Batch"

    zeitpunkt = models.DateTimeField("Zeitpunkt", default=timezone.now, db_index=True)
    art = models.CharField("Art", max_length=10, choices=Art.choices)
    modell = models.CharField("Modell", max_length=100)
    vorgang_id = models.UUIDField("Vorgang", null=True, blank=True)
    eingabe_tokens = models.PositiveBigIntegerField("Eingabe-Token", default=0)
    ausgabe_tokens = models.PositiveBigIntegerField("Ausgabe-Token", default=0)
    cache_lesen_tokens = models.PositiveBigIntegerField("Cache gelesen", default=0)
    cache_schreiben_tokens = models.PositiveBigIntegerField("Cache geschrieben", default=0)
    kosten_usd = models.DecimalField("Kosten (USD)", max_digits=12, decimal_places=6, default=Decimal(0))

    class Meta:
        verbose_name = "KI-Verbrauch (Claude)"
        verbose_name_plural = "KI-Verbrauch (Claude)"
        ordering = ("-zeitpunkt",)

    def __str__(self) -> str:
        return f"{self.get_art_display()} {self.modell}: {self.kosten_usd} USD"


class TagesbatchKommune(models.Model):
    """
    Kommune im täglichen Batch der Zusammenfassungen (Issue #1220): Vorgänge ab ``ab`` bekommen ihre Zusammenfassung
    automatisch, sobald der Text aller Anlagen erkannt ist; mit ``verortung_pruefen`` prüft Claude in derselben
    Anfrage die Verortung auf der Karte (Issue #1222). Ohne Eintrag reicht der Batch nichts ein.
    """

    # PROTECT: Verweise auf den RIS-Bestand nie mit CASCADE (hub/ris/tests/test_loeschsemantik.py)
    body = models.OneToOneField(
        "insight_core.OParlBody",
        on_delete=models.PROTECT,
        related_name="tagesbatch",
        verbose_name="Kommune",
    )
    ab = models.DateField("Vorgänge ab", help_text="Datum des Vorgangs; ältere bleiben beim Klick „Zusammenfassen“.")
    verortung_pruefen = models.BooleanField(
        "Verortung prüfen",
        default=True,
        help_text="Claude prüft in derselben Anfrage die gefundenen Orte und nennt fehlende (öffentliche Daten).",
    )
    angelegt_am = models.DateTimeField("Angelegt am", auto_now_add=True)

    class Meta:
        verbose_name = "Kommune im Tagesbatch"
        verbose_name_plural = "Kommunen im Tagesbatch"
        ordering = ("ab",)

    def __str__(self) -> str:
        return f"{self.body} ab {self.ab:%d.%m.%Y}"


class VerworfeneZusammenfassung(models.Model):
    """
    Von der mechanischen Prüfung verworfene KI-Zusammenfassung (Issue #1271, ``services.zusammenfassung_pruefung``).

    Sie erscheint nirgends. Der Eintrag zeigt, welche Regel wie oft greift (je Prompt-Fassung und Modell), und hält den
    Tagesbatch davon ab, denselben Vorgang mit derselben Prompt-Fassung erneut einzureichen. Der Text bleibt erhalten,
    außer er nennt eine Hausnummer. Verweise auf Vorgang und Kommune als Kennung, ohne Fremdschlüssel (wie
    ``KiVerbrauch``).
    """

    class Weg(models.TextChoices):
        KLICK = "klick", "Klick auf „Zusammenfassen“"
        BATCH = "batch", "Batch"

    class Teil(models.TextChoices):
        ZUSAMMENFASSUNG = "zusammenfassung", "Zusammenfassung"
        EINFACH = "einfache_sprache", "Einfache Sprache"

    zeitpunkt = models.DateTimeField("Zeitpunkt", default=timezone.now, db_index=True)
    vorgang_id = models.UUIDField("Vorgang", db_index=True)
    kommune_id = models.UUIDField("Kommune", null=True, blank=True)
    prompt_fassung = models.PositiveSmallIntegerField("Prompt-Fassung")
    weg = models.CharField("Weg", max_length=10, choices=Weg.choices)
    teil = models.CharField("Teil", max_length=20, choices=Teil.choices, default=Teil.ZUSAMMENFASSUNG)
    regel = models.CharField("Regel", max_length=20, db_index=True)
    grund = models.CharField("Grund", max_length=500)
    entwurf = models.TextField(
        "Verworfener Text", blank=True, help_text="Leer, wenn er eine Hausnummer nannte (Datenschutz)."
    )
    modell = models.CharField("Modell", max_length=100, blank=True)
    batch_id = models.CharField("Batch-Kennung", max_length=100, blank=True)

    class Meta:
        verbose_name = "Verworfene KI-Zusammenfassung"
        verbose_name_plural = "Verworfene KI-Zusammenfassungen"
        ordering = ("-zeitpunkt",)
        indexes = [models.Index(fields=["prompt_fassung", "teil", "vorgang_id"], name="insight_ai_verworfen_idx")]

    def __str__(self) -> str:
        return f"Vorgang {self.vorgang_id}: {self.regel} (Fassung {self.prompt_fassung})"
