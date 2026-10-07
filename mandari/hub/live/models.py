# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Übertragungen von Gremiensitzungen (Issue #915).

- ``BroadcastSource``: Übertragungsquelle je Gremium (Anbieter, Kennung, Einblendungsprofil, Takt).
- ``Broadcast``: Übertragung einer Sitzung mit Zustand, Entprellung und aktuellem Stand.
- ``BroadcastSection``: Abschnitt je Tagesordnungspunkt (Beginn, Ende, Zuordnung mit gelesener Nummer,
  Titelähnlichkeit und Sicherheit, ``hub.live.zuordnung.top_zuordnen``).
- ``BroadcastSpeech``: Wortmeldung (Person laut Einblendung, Zeitpunkt des Beginns). Bewusst ohne Ende und ohne
  Dauer: Redezeiten werden nicht erfasst.
- ``BroadcastLog``: Protokoll der Abfragen, Lesungen, Zustandswechsel und Fehler für die Auswertung, 90 Tage.

Keine Tabelle enthält Bild- oder Tondaten. Verweise auf den RIS-Bestand sind ``PROTECT`` bzw. ``SET_NULL``
(``docs/adr/20260929-fremdschluessel-ris-bestand.md``).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models

from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlOrganization, OParlPerson

from .profil import pruefe_profil


class BroadcastSource(models.Model):
    """Übertragungsquelle eines Gremiums: wo und wie die Kommune seine Sitzungen überträgt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    body = models.ForeignKey(OParlBody, on_delete=models.PROTECT, related_name="+", verbose_name="Kommune")
    organization = models.ForeignKey(
        OParlOrganization, on_delete=models.PROTECT, related_name="+", verbose_name="Gremium"
    )
    provider = models.CharField("Anbieter", max_length=32, help_text="Code des Adapters, z. B. 3q oder hls")
    identifier = models.CharField(
        "Kennung beim Anbieter", max_length=1000, help_text="z. B. Embed-ID des Players oder Adresse der HLS-Playlist"
    )
    page_url = models.URLField("Seite der Übertragung", max_length=1000, blank=True, default="")
    overlay_profile = models.JSONField(
        "Einblendungsprofil", default=dict, blank=True, help_text="Lage der Felder, Balken, Funktionsbezeichnungen"
    )
    active = models.BooleanField("aktiv", default=False)
    interval_seconds = models.PositiveSmallIntegerField("Takt der Einzelbilder (Sekunden)", default=10)
    created_at = models.DateTimeField("angelegt am", auto_now_add=True)
    updated_at = models.DateTimeField("geändert am", auto_now=True)

    class Meta:
        db_table = "hub_live_quelle"
        verbose_name = "Übertragungsquelle"
        verbose_name_plural = "Übertragungsquellen"
        constraints = [models.UniqueConstraint(fields=["organization"], name="hub_live_quelle_gremium")]

    def __str__(self) -> str:
        return f"{self.provider}: {self.organization_id}"

    def clean(self) -> None:
        from .anbieter import anbieter

        fehler: dict[str, Any] = {}
        try:
            adapter = anbieter(self.provider)
        except KeyError:
            fehler["provider"] = "Unbekannter Anbieter"
        else:
            if not adapter.kennung_gueltig(self.identifier):
                fehler["identifier"] = "Kennung passt nicht zum Anbieter"
        probleme = pruefe_profil(self.overlay_profile)
        if probleme:
            fehler["overlay_profile"] = probleme
        if not 5 <= self.interval_seconds <= 60:
            fehler["interval_seconds"] = "5 bis 60 Sekunden"
        if self.organization_id and self.body_id:
            gremium_body = OParlOrganization.objects.filter(pk=self.organization_id).values_list("body_id", flat=True)
            if gremium_body.first() != self.body_id:
                fehler["organization"] = "Das Gremium gehört nicht zur Kommune"
        if fehler:
            raise ValidationError(fehler)


class BroadcastStatus(models.TextChoices):
    GEPLANT = "geplant", "geplant"
    LIVE = "live", "läuft"
    BEENDET = "beendet", "beendet"
    NICHT_UEBERTRAGEN = "nicht_uebertragen", "nicht übertragen"


class Broadcast(models.Model):
    """Übertragung einer Sitzung."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        BroadcastSource, on_delete=models.PROTECT, related_name="broadcasts", verbose_name="Quelle"
    )
    meeting = models.ForeignKey(OParlMeeting, on_delete=models.PROTECT, related_name="+", verbose_name="Sitzung")
    status = models.CharField(
        "Status", max_length=20, choices=BroadcastStatus.choices, default=BroadcastStatus.GEPLANT, db_index=True
    )
    started_at = models.DateTimeField("begonnen am", null=True, blank=True)
    ended_at = models.DateTimeField("beendet am", null=True, blank=True)
    offline_since = models.DateTimeField(
        "ohne Übertragung seit", null=True, blank=True, help_text="Aussetzer während der Übertragung"
    )
    last_checked_at = models.DateTimeField("letzte Abfrage", null=True, blank=True)
    last_status = models.JSONField("letzter Status", default=dict, blank=True, help_text="ohne Bild-Adressen")
    stream = models.JSONField(
        "Stream", default=dict, blank=True, help_text="aufgelöste Kennung, HLS- und Player-Adresse"
    )
    debounce = models.JSONField("Entprellung", default=dict, blank=True, help_text="Kandidaten für TOP und Person")
    current_section = models.ForeignKey(
        "BroadcastSection", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="TOP jetzt"
    )
    current_speech = models.ForeignKey(
        "BroadcastSpeech", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="am Wort"
    )
    frames_read = models.PositiveIntegerField("gelesene Bilder", default=0)
    frames_without_overlay = models.PositiveIntegerField("Bilder ohne Einblendung", default=0)
    created_at = models.DateTimeField("angelegt am", auto_now_add=True)
    updated_at = models.DateTimeField("geändert am", auto_now=True)

    class Meta:
        db_table = "hub_live_uebertragung"
        verbose_name = "Übertragung"
        verbose_name_plural = "Übertragungen"
        constraints = [models.UniqueConstraint(fields=["source", "meeting"], name="hub_live_uebertragung_sitzung")]

    def __str__(self) -> str:
        return f"Übertragung {self.meeting_id} ({self.status})"


class SectionOrigin(models.TextChoices):
    EINBLENDUNG = "einblendung", "Einblendung"
    HAND = "hand", "von Hand"


class SectionConfidence(models.TextChoices):
    """Wie sicher ein Abschnitt seinem Tagesordnungspunkt zugeordnet ist (``hub.live.zuordnung``)."""

    NUMMER_UND_TITEL = "nummer_titel", "Nummer und Titel passen"
    TITEL = "titel", "nur der Titel passt"
    NUMMER = "nummer", "nur die Nummer (niedrig)"
    KEINE = "keine", "kein Tagesordnungspunkt"


class BroadcastSection(models.Model):
    """Abschnitt einer Übertragung je Tagesordnungspunkt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    broadcast = models.ForeignKey(Broadcast, on_delete=models.CASCADE, related_name="sections")
    agenda_item = models.ForeignKey(
        OParlAgendaItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Tagesordnungspunkt",
    )
    number = models.CharField(
        "TOP-Nummer", max_length=20, help_text="des zugeordneten Tagesordnungspunkts, sonst die gelesene"
    )
    # db_default: Ein älteres Image legt Abschnitte ohne diese Felder an (Rückfall ohne Rückbau)
    number_read = models.CharField(
        "gelesene Nummer", max_length=20, blank=True, default="", db_default="", help_text="leer = vor der Titelprüfung"
    )
    title_read = models.CharField("gelesener Titel", max_length=500, blank=True, default="")
    title_similarity = models.FloatField("Ähnlichkeit zum Titel im RIS", null=True, blank=True)
    confidence = models.CharField(
        "Sicherheit der Zuordnung",
        max_length=20,
        choices=SectionConfidence.choices,
        blank=True,
        default="",
        db_default="",
        help_text="leer = vor der Titelprüfung",
    )
    origin = models.CharField("Quelle", max_length=20, choices=SectionOrigin.choices, default=SectionOrigin.EINBLENDUNG)
    started_at = models.DateTimeField("begonnen am")
    ended_at = models.DateTimeField("beendet am", null=True, blank=True)

    class Meta:
        db_table = "hub_live_abschnitt"
        verbose_name = "TOP-Abschnitt"
        verbose_name_plural = "TOP-Abschnitte"
        ordering = ["started_at"]

    def __str__(self) -> str:
        return f"TOP {self.number}"


class SpeechAssignment(models.TextChoices):
    EINDEUTIG = "eindeutig", "eindeutig"
    UNSICHER = "unsicher", "unsicher"
    KEINE = "keine", "keine"


class BroadcastSpeech(models.Model):
    """Wortmeldung laut Einblendung: wer ab wann am Wort war (ohne Ende, ohne Dauer)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    broadcast = models.ForeignKey(Broadcast, on_delete=models.CASCADE, related_name="speeches")
    section = models.ForeignKey(
        BroadcastSection, on_delete=models.SET_NULL, null=True, blank=True, related_name="speeches"
    )
    person = models.ForeignKey(
        OParlPerson, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Person"
    )
    name_read = models.CharField("gelesener Name", max_length=200)
    faction_read = models.CharField("gelesene Fraktion", max_length=200, blank=True, default="")
    function_read = models.CharField("gelesene Funktion", max_length=200, blank=True, default="")
    started_at = models.DateTimeField("begonnen am")
    assignment = models.CharField(
        "Zuordnung", max_length=20, choices=SpeechAssignment.choices, default=SpeechAssignment.KEINE
    )
    readings = models.PositiveIntegerField("gleiche Lesungen", default=1)

    class Meta:
        db_table = "hub_live_wortmeldung"
        verbose_name = "Wortmeldung"
        verbose_name_plural = "Wortmeldungen"
        ordering = ["started_at"]

    def __str__(self) -> str:
        return self.name_read


class LogKind(models.TextChoices):
    ABFRAGE = "abfrage", "Statusabfrage"
    LESUNG = "lesung", "Lesung"
    ZUSTAND = "zustand", "Zustandswechsel"
    FEHLER = "fehler", "Fehler"


class BroadcastLog(models.Model):
    """Protokolleintrag für die Auswertung (ohne Bilddaten, Aufbewahrung ``LIVE_PROTOKOLL_TAGE``)."""

    id = models.BigAutoField(primary_key=True)
    source = models.ForeignKey(BroadcastSource, on_delete=models.CASCADE, related_name="+")
    broadcast = models.ForeignKey(Broadcast, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    at = models.DateTimeField("Zeit", db_index=True)
    kind = models.CharField("Art", max_length=20, choices=LogKind.choices)
    data = models.JSONField("Daten", default=dict, blank=True)

    class Meta:
        db_table = "hub_live_protokoll"
        verbose_name = "Protokolleintrag"
        verbose_name_plural = "Protokoll"
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["broadcast", "at"], name="hub_live_protokoll_uebertr")]

    def __str__(self) -> str:
        return f"{self.kind} {self.at:%Y-%m-%d %H:%M:%S}"
