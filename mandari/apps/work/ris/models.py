# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fachliche Anker der Work-Verknüpfungen mit dem RIS-Bestand (Issue #547).

Notizen, Positionen, Redebeiträge, Dokumente und Aufgaben hängen an einem Tagesordnungspunkt, Kommentare und
Dokumente an einer Vorlage. Veröffentlicht ein RIS einen Stand neu, kann derselbe fachliche Punkt eine andere Zeile
im Bestand werden. Der Anker hält fest, welcher Punkt bzw. welche Vorlage gemeint war (``kennung``, Beschreibung
aus ``hub.ris.neuveroeffentlichung``); der Abgleich (``apps.work.ris.verknuepfungen``) findet sie nach einer
Neuveröffentlichung wieder und hängt die Daten um.

Anker und Protokoll gehören je einer Organisation: Jede hält fest, was sie beim Verknüpfen meinte, und wird für
sich umgehängt und angezeigt – keine Organisation erfährt, ob eine andere am selben Punkt arbeitet. Sie enthalten
nur Angaben aus dem RIS-Bestand und Kennungen, keine Inhalte. Die Kennung des RIS-Objekts ist bewusst kein
Fremdschlüssel: Der Anker soll das harte Aufräumen des Bestands nicht blockieren; ohne Verknüpfung räumt der
Abgleich ihn selbst weg.
"""

from __future__ import annotations

import uuid

from django.db import models


class RisAnker(models.Model):
    """Was eine Verknüpfung aus Work fachlich meinte: ein Tagesordnungspunkt oder eine Vorlage."""

    ART_TOP = "top"
    ART_VORLAGE = "vorlage"
    ART_CHOICES = [(ART_TOP, "Tagesordnungspunkt"), (ART_VORLAGE, "Vorlage")]

    AKTUELL = "aktuell"
    NICHT_ZUGEORDNET = "nicht_zugeordnet"
    ENTFALLEN = "entfallen"
    MEHRDEUTIG = "mehrdeutig"
    STATUS_CHOICES = [
        (AKTUELL, "Aktuell"),
        (NICHT_ZUGEORDNET, "Nicht zugeordnet (anderer Punkt unter derselben Kennung)"),
        (ENTFALLEN, "Entfallen (gelöscht oder nicht mehr auf der Tagesordnung)"),
        (MEHRDEUTIG, "Mehrdeutig (mehrere mögliche Nachfolger)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization", on_delete=models.CASCADE, related_name="ris_anker", verbose_name="Organisation"
    )
    art = models.CharField("Art", max_length=10, choices=ART_CHOICES)
    objekt = models.UUIDField("RIS-Objekt", help_text="Kennung im RIS-Bestand, an der die Work-Daten heute hängen")
    kennung = models.JSONField(
        "Fachliche Kennung", default=dict, blank=True, help_text="Leer, bis der Abgleich sie erstmals erfasst"
    )
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default=AKTUELL)
    geprueft_am = models.DateTimeField("Zuletzt geprüft", blank=True, null=True)
    status_seit = models.DateTimeField("Status seit", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "RIS-Anker"
        verbose_name_plural = "RIS-Anker"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "art", "objekt"], name="work_risanker_org_art_objekt_eindeutig"
            )
        ]
        indexes = [models.Index(fields=["art", "objekt"], name="work_risanker_objekt_idx")]

    def __str__(self) -> str:
        return f"{self.get_art_display()} {self.objekt} ({self.status})"


class RisNeuzuordnung(models.Model):
    """Protokoll: Work-Daten wurden nach einer Neuveröffentlichung umgehängt oder sind nicht zuzuordnen."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization", on_delete=models.CASCADE, related_name="ris_neuzuordnungen", verbose_name="Organisation"
    )
    art = models.CharField("Art", max_length=10, choices=RisAnker.ART_CHOICES)
    von = models.UUIDField("Bisheriges RIS-Objekt")
    nach = models.UUIDField("Neues RIS-Objekt", blank=True, null=True)
    ergebnis = models.CharField("Ergebnis", max_length=20)
    #: je Modell die Kennungen der umgehängten Datensätze (für Nachvollziehbarkeit und Rückweg von Hand)
    verschoben = models.JSONField("Umgehängt", default=dict, blank=True)
    #: je Modell die Kennungen der Datensätze, die wegen eines Gegenstücks am Ziel bleiben mussten
    konflikte = models.JSONField("Nicht umgehängt", default=dict, blank=True)
    erfolgt_am = models.DateTimeField("Erfolgt am", auto_now_add=True)

    class Meta:
        verbose_name = "RIS-Neuzuordnung"
        verbose_name_plural = "RIS-Neuzuordnungen"
        ordering = ["-erfolgt_am"]
        indexes = [models.Index(fields=["von"], name="work_risneuzuord_von_idx")]

    def __str__(self) -> str:
        return f"{self.art} {self.von} → {self.nach or '–'} ({self.ergebnis})"
