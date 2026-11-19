# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigener Termin der Organisation (Issue #1489, Schritt 1).

Sitzungen der Gremien kommen aus dem Ratsinformationssystem, Fraktionssitzungen haben Tagesordnung und Protokoll.
Alles andere, was eine Fraktion im Kalender braucht (Ortstermin, Pressegespräch, Klausur, Sprechstunde), ist ein
Termin: Titel, Zeit, Ort, Beschreibung, ein Link zur Videokonferenz, wer ihn sieht und wer eingeladen ist. Eingeladene
Mitglieder sagen zu, ab oder „vielleicht“ (``TerminTeilnahme``). Ein Termin kann auf eine Fraktionssitzung verweisen,
etwa das Vorgespräch zur Sitzung. Tagesordnung und Protokoll bleiben Sache der Fraktionssitzung, die Abstimmung eines
Termins ist die Terminumfrage.

Die Beschreibung liegt wie die übrigen Inhalte von Work mit dem Schlüssel der Organisation verschlüsselt; Titel, Zeit
und Ort stehen wie bei Fraktionssitzungen und Aufgaben im Klartext (sie erscheinen im Kalender).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class Termin(EncryptionMixin, models.Model):
    """Termin im Kalender der Organisation."""

    ALLE = "organization"
    ART = "meeting_type"
    PRIVAT = "private"
    SICHTBARKEIT = [
        (ALLE, "Alle Mitglieder"),
        (ART, "Teilnehmende einer Sitzungsart"),
        (PRIVAT, "Nur ich und die Eingeladenen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization", on_delete=models.CASCADE, related_name="termine", verbose_name="Organisation"
    )
    title = models.CharField(max_length=200, verbose_name="Titel")
    start = models.DateTimeField(verbose_name="Beginn")
    end = models.DateTimeField(blank=True, null=True, verbose_name="Ende")
    all_day = models.BooleanField(default=False, verbose_name="Ganztägig")
    location = models.CharField(max_length=500, blank=True, verbose_name="Ort")
    description_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Beschreibung")
    video_url = models.URLField(max_length=500, blank=True, verbose_name="Link zur Videokonferenz")
    # Bezug zu einer Fraktionssitzung (etwa das Vorgespräch); der Termin bleibt, wenn die Sitzung gelöscht wird
    faction_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="termine",
        verbose_name="Fraktionssitzung",
    )

    visibility = models.CharField(max_length=20, choices=SICHTBARKEIT, default=ALLE, verbose_name="Sichtbarkeit")
    # Gruppe für die Sichtbarkeit „Teilnehmende einer Sitzungsart“ (etwa ein Arbeitskreis). Fällt die Art weg, sieht
    # den Termin nur noch, wer ihn angelegt hat oder alle Termine verwalten darf (apps.work.termine.termine).
    meeting_type = models.ForeignKey(
        "work.FactionMeetingType",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="termine",
        verbose_name="Sitzungsart",
    )

    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="angelegte_termine",
        verbose_name="Angelegt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Termin"
        verbose_name_plural = "Termine"
        ordering = ["start"]
        indexes = [models.Index(fields=["organization", "start"], name="work_termin_org_start")]

    def __str__(self) -> str:
        return self.title

    def get_encryption_organization(self) -> Any:
        return self.organization


class TerminTeilnahme(models.Model):
    """Einladung eines Mitglieds zu einem Termin mit seiner Antwort."""

    OFFEN = "offen"
    ZUGESAGT = "zugesagt"
    VIELLEICHT = "vielleicht"
    ABGESAGT = "abgesagt"
    STATUS = [
        (OFFEN, "Noch keine Antwort"),
        (ZUGESAGT, "Zugesagt"),
        (VIELLEICHT, "Vielleicht"),
        (ABGESAGT, "Abgesagt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    termin = models.ForeignKey(Termin, on_delete=models.CASCADE, related_name="teilnahmen", verbose_name="Termin")
    membership = models.ForeignKey(
        "tenants.Membership", on_delete=models.CASCADE, related_name="termin_teilnahmen", verbose_name="Mitglied"
    )
    status = models.CharField(max_length=12, choices=STATUS, default=OFFEN, verbose_name="Antwort")
    geantwortet_am = models.DateTimeField(blank=True, null=True, verbose_name="Geantwortet am")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Einladung zu einem Termin"
        verbose_name_plural = "Einladungen zu Terminen"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["termin", "membership"], name="work_terminteilnahme_einmal"),
        ]

    def __str__(self) -> str:
        return f"{self.termin}: {self.get_status_display()}"
