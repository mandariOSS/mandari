# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigener Termin der Organisation (Issue #1489, Schritt 1).

Sitzungen der Gremien kommen aus dem Ratsinformationssystem, Fraktionssitzungen haben Tagesordnung und Protokoll.
Alles andere, was eine Fraktion im Kalender braucht (Ortstermin, Pressegespräch, Klausur, Sprechstunde), ist ein
Termin: Titel, Zeit, Ort, Beschreibung, ein Link zur Videokonferenz, wer ihn sieht und wer eingeladen ist. Eingeladene
Mitglieder sagen zu, ab oder „vielleicht“ (``TerminTeilnahme``). Ein Termin kann auf eine Fraktionssitzung verweisen,
etwa das Vorgespräch zur Sitzung. Tagesordnung und Protokoll bleiben Sache der Fraktionssitzung, die Abstimmung eines
Termins ist die Terminumfrage (``Terminumfrage`` mit Vorschlägen und Antworten, weiter unten).

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


class Terminumfrage(EncryptionMixin, models.Model):
    """
    Umfrage, an welchem der vorgeschlagenen Zeitpunkte ein Termin stattfindet (Issue #1489, Schritt 4).

    Die Gefragten (``UmfrageTeilnahme``) antworten je Vorschlag (``VorschlagAntwort``). Wird ein Vorschlag
    festgelegt, entsteht daraus ein ``Termin`` und die Umfrage ist abgeschlossen (``abgeschlossen_am``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization", on_delete=models.CASCADE, related_name="terminumfragen", verbose_name="Organisation"
    )
    title = models.CharField(max_length=200, verbose_name="Titel")
    location = models.CharField(max_length=500, blank=True, verbose_name="Ort")
    description_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Beschreibung")
    antwort_bis = models.DateField(blank=True, null=True, verbose_name="Antwort bis")
    abgeschlossen_am = models.DateTimeField(blank=True, null=True, verbose_name="Abgeschlossen am")
    # Der Termin, der aus der Umfrage entstanden ist; die Umfrage bleibt abgeschlossen, wenn er gelöscht wird
    termin = models.ForeignKey(
        Termin, on_delete=models.SET_NULL, blank=True, null=True, related_name="umfragen", verbose_name="Termin"
    )
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="angelegte_terminumfragen",
        verbose_name="Angelegt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Terminumfrage"
        verbose_name_plural = "Terminumfragen"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "abgeschlossen_am"], name="work_umfrage_org_offen")]

    def __str__(self) -> str:
        return self.title

    def get_encryption_organization(self) -> Any:
        return self.organization


class Terminvorschlag(models.Model):
    """Ein vorgeschlagener Zeitpunkt einer Terminumfrage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    umfrage = models.ForeignKey(
        Terminumfrage, on_delete=models.CASCADE, related_name="vorschlaege", verbose_name="Terminumfrage"
    )
    start = models.DateTimeField(verbose_name="Beginn")
    end = models.DateTimeField(blank=True, null=True, verbose_name="Ende")
    festgelegt = models.BooleanField(default=False, verbose_name="Festgelegt")

    class Meta:
        verbose_name = "Terminvorschlag"
        verbose_name_plural = "Terminvorschläge"
        ordering = ["start"]

    def __str__(self) -> str:
        return f"{self.umfrage}: {self.start:%d.%m.%Y %H:%M}"


class UmfrageTeilnahme(models.Model):
    """Ein Mitglied, das in einer Terminumfrage gefragt ist."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    umfrage = models.ForeignKey(
        Terminumfrage, on_delete=models.CASCADE, related_name="teilnahmen", verbose_name="Terminumfrage"
    )
    membership = models.ForeignKey(
        "tenants.Membership", on_delete=models.CASCADE, related_name="umfrage_teilnahmen", verbose_name="Mitglied"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Gefragtes Mitglied einer Terminumfrage"
        verbose_name_plural = "Gefragte Mitglieder von Terminumfragen"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["umfrage", "membership"], name="work_umfrageteilnahme_einmal"),
        ]

    def __str__(self) -> str:
        return f"{self.umfrage}: {self.membership_id}"


class VorschlagAntwort(models.Model):
    """Antwort eines Mitglieds auf einen Vorschlag."""

    JA = "ja"
    NOTFALLS = "notfalls"
    NEIN = "nein"
    WAHL = [(JA, "Ja"), (NOTFALLS, "Wenn nötig"), (NEIN, "Nein")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vorschlag = models.ForeignKey(
        Terminvorschlag, on_delete=models.CASCADE, related_name="antworten", verbose_name="Vorschlag"
    )
    membership = models.ForeignKey(
        "tenants.Membership", on_delete=models.CASCADE, related_name="vorschlag_antworten", verbose_name="Mitglied"
    )
    wahl = models.CharField(max_length=10, choices=WAHL, verbose_name="Antwort")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Antwort auf einen Terminvorschlag"
        verbose_name_plural = "Antworten auf Terminvorschläge"
        constraints = [
            models.UniqueConstraint(fields=["vorschlag", "membership"], name="work_vorschlagantwort_einmal"),
        ]

    def __str__(self) -> str:
        return f"{self.vorschlag}: {self.get_wahl_display()}"
