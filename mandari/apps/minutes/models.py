# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmodell für Aufzeichnung, Transkription und Protokollentwurf.

Die App wird von beiden Portalen genutzt: `session` für amtliche Sitzungen
(Niederschrift nach Gemeindeordnung) und `work` für Fraktionssitzungen. Eine
Aufzeichnung hängt deshalb an genau einer der beiden Sitzungsarten.

Alle Arbeitsmaterialien — Audio-Referenzen, Transkripte, Entwürfe — sind
Zwischenergebnisse mit begrenzter Lebensdauer. Sie werden mit der Genehmigung
der Niederschrift gelöscht; `retention_until` ist die Obergrenze, falls es nie
zu einer Genehmigung kommt.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin

from .attribution import AttributionSource
from .confidentiality import Confidentiality


class RecordingStatus(models.TextChoices):
    """Lebenszyklus einer Aufzeichnung."""

    PENDING = "pending", "Vorbereitet"
    RECORDING = "recording", "Läuft"
    PAUSED = "paused", "Pausiert"
    FINISHED = "finished", "Beendet"
    PROCESSING = "processing", "In Verarbeitung"
    READY = "ready", "Entwurf liegt vor"
    PURGED = "purged", "Gelöscht"
    FAILED = "failed", "Fehlgeschlagen"


class JobStage(models.TextChoices):
    """Verarbeitungsschritt eines Auftrags."""

    TRANSCRIBE = "transcribe", "Transkription"
    DIARIZE = "diarize", "Sprechertrennung"
    DRAFT = "draft", "Protokollentwurf"


class JobStatus(models.TextChoices):
    """Zustand eines Verarbeitungsauftrags."""

    QUEUED = "queued", "Wartet"
    RUNNING = "running", "Läuft"
    SUCCEEDED = "succeeded", "Erfolgreich"
    FAILED = "failed", "Fehlgeschlagen"
    SKIPPED = "skipped", "Übersprungen"


class Recording(EncryptionMixin, models.Model):
    """Aufzeichnung einer Sitzung."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session_meeting = models.ForeignKey(
        "session.SessionMeeting",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="recordings",
        verbose_name="Sitzung (Session)",
    )
    faction_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="recordings",
        verbose_name="Fraktionssitzung",
    )

    status = models.CharField(
        max_length=20,
        choices=RecordingStatus.choices,
        default=RecordingStatus.PENDING,
        verbose_name="Status",
    )

    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Begonnen am")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="Beendet am")

    # Art. 13 DSGVO: Hinweis an die Anwesenden ist Voraussetzung für den Start.
    notice_given_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Hinweis auf Aufzeichnung erteilt am",
    )

    # Obergrenze auch ohne Genehmigung der Niederschrift.
    retention_until = models.DateTimeField(verbose_name="Spätestens löschen am")
    purged_at = models.DateTimeField(null=True, blank=True, verbose_name="Gelöscht am")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "minutes_recordings"
        verbose_name = "Aufzeichnung"
        verbose_name_plural = "Aufzeichnungen"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(session_meeting__isnull=False, faction_meeting__isnull=True)
                    | models.Q(session_meeting__isnull=True, faction_meeting__isnull=False)
                ),
                name="minutes_recording_exactly_one_meeting",
            ),
        ]

    def __str__(self) -> str:
        return f"Aufzeichnung {self.id}"

    def get_encryption_organization(self) -> object:
        """Mandant für die Feldverschlüsselung (Session-Mandant oder Organisation)."""
        if self.session_meeting is not None:
            return self.session_meeting.tenant
        if self.faction_meeting is not None:
            return self.faction_meeting.organization
        raise ValueError("Aufzeichnung ohne zugeordnete Sitzung")


class RecordingSegment(models.Model):
    """
    Ein Abschnitt der Aufzeichnung, in der Regel ein Tagesordnungspunkt.

    Die Aufteilung nach TOP ist der Kern des Verfahrens: Sie liefert die
    Zuordnung zum Tagesordnungspunkt ohne KI, hält die Kontextfenster für das
    Sprachmodell klein und macht die Pausenfunktion für nichtöffentliche
    Abschnitte strukturell wirksam.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        Recording,
        on_delete=models.CASCADE,
        related_name="segments",
        verbose_name="Aufzeichnung",
    )

    session_agenda_item = models.ForeignKey(
        "session.SessionAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recording_segments",
        verbose_name="TOP (Session)",
    )
    faction_agenda_item = models.ForeignKey(
        "work.FactionAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recording_segments",
        verbose_name="TOP (Fraktion)",
    )

    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    confidentiality = models.CharField(
        max_length=20,
        choices=Confidentiality.choices,
        default=Confidentiality.PUBLIC,
        verbose_name="Vertraulichkeit",
    )

    started_at = models.DateTimeField(verbose_name="Beginn")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="Ende")

    # Objektschlüssel im verschlüsselten Audiospeicher, nie der Inhalt selbst.
    audio_key = models.CharField(max_length=500, blank=True, verbose_name="Audio-Objektschlüssel")
    duration_seconds = models.PositiveIntegerField(default=0, verbose_name="Dauer (s)")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "minutes_recording_segments"
        verbose_name = "Aufzeichnungsabschnitt"
        verbose_name_plural = "Aufzeichnungsabschnitte"
        ordering = ["recording", "order"]
        indexes = [models.Index(fields=["recording", "order"])]

    def __str__(self) -> str:
        return f"Abschnitt {self.order} ({self.get_confidentiality_display()})"


class TranscriptionJob(models.Model):
    """Ein Verarbeitungsauftrag an den GPU-Worker."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(
        RecordingSegment,
        on_delete=models.CASCADE,
        related_name="jobs",
        verbose_name="Abschnitt",
    )

    stage = models.CharField(max_length=20, choices=JobStage.choices, verbose_name="Schritt")
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.QUEUED,
        verbose_name="Status",
    )
    node = models.ForeignKey(
        "minutes.GpuNode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
        verbose_name="Knoten",
    )

    # Nachvollziehbarkeit: welches Modell hat welches Ergebnis erzeugt.
    engine = models.CharField(max_length=100, blank=True, verbose_name="Verfahren")
    model_name = models.CharField(max_length=200, blank=True, verbose_name="Modell")
    prompt_version = models.CharField(max_length=50, blank=True, verbose_name="Prompt-Version")

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="Versuche")
    error = models.TextField(blank=True, verbose_name="Fehler")

    class Meta:
        db_table = "minutes_transcription_jobs"
        verbose_name = "Verarbeitungsauftrag"
        verbose_name_plural = "Verarbeitungsaufträge"
        ordering = ["queued_at"]
        indexes = [models.Index(fields=["status", "stage"])]

    def __str__(self) -> str:
        return f"{self.get_stage_display()} — {self.get_status_display()}"


class TranscriptSegment(EncryptionMixin, models.Model):
    """
    Ein Sprechabschnitt mit Text und Sprecherzuordnung.

    Der Text ist durchgehend verschlüsselt: Bis zur Genehmigung der
    Niederschrift ist er Arbeitsmaterial, danach wird er gelöscht.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(
        RecordingSegment,
        on_delete=models.CASCADE,
        related_name="transcript",
        verbose_name="Abschnitt",
    )

    start_ms = models.PositiveIntegerField(verbose_name="Beginn (ms)")
    end_ms = models.PositiveIntegerField(verbose_name="Ende (ms)")
    text_encrypted = EncryptedTextField(verbose_name="Text")

    # Cluster-Label der Sprechertrennung, ohne Personenbezug.
    speaker_label = models.CharField(max_length=50, blank=True, verbose_name="Sprecher-Label")

    session_person = models.ForeignKey(
        "session.SessionPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transcript_segments",
        verbose_name="Person (Session)",
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transcript_segments",
        verbose_name="Mitglied (Work)",
    )

    attribution_source = models.CharField(
        max_length=20,
        choices=AttributionSource.choices,
        blank=True,
        verbose_name="Herkunft der Zuordnung",
    )
    attribution_confidence = models.FloatField(default=0.0, verbose_name="Konfidenz")
    needs_confirmation = models.BooleanField(default=True, verbose_name="Bestätigung nötig")
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name="Bestätigt am")

    class Meta:
        db_table = "minutes_transcript_segments"
        verbose_name = "Transkriptabschnitt"
        verbose_name_plural = "Transkriptabschnitte"
        ordering = ["segment", "start_ms"]
        indexes = [models.Index(fields=["segment", "start_ms"])]

    def __str__(self) -> str:
        return f"{self.start_ms}–{self.end_ms} ms"

    def get_encryption_organization(self) -> object:
        return self.segment.recording.get_encryption_organization()


class SpeakerConsent(models.Model):
    """
    Einwilligung in die Speicherung eines Stimmprofils (Art. 9 Abs. 2 lit. a DSGVO).

    Ohne gültige, nicht widerrufene Einwilligung darf für die Person kein
    Stimmprofil existieren und kein Profilabgleich stattfinden. Der Widerruf
    ist jederzeit möglich und löscht das Profil.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session_person = models.ForeignKey(
        "session.SessionPerson",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="speaker_consents",
        verbose_name="Person (Session)",
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="speaker_consents",
        verbose_name="Mitglied (Work)",
    )

    granted_at = models.DateTimeField(verbose_name="Erteilt am")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="Widerrufen am")
    # Fassung des Einwilligungstextes, damit später belegbar ist, worin
    # eingewilligt wurde.
    consent_text_version = models.CharField(max_length=50, verbose_name="Fassung des Einwilligungstextes")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "minutes_speaker_consents"
        verbose_name = "Einwilligung Stimmprofil"
        verbose_name_plural = "Einwilligungen Stimmprofil"
        ordering = ["-granted_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(session_person__isnull=False, membership__isnull=True)
                    | models.Q(session_person__isnull=True, membership__isnull=False)
                ),
                name="minutes_consent_exactly_one_subject",
            ),
        ]

    def __str__(self) -> str:
        return f"Einwilligung {self.id}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class VoiceProfile(models.Model):
    """
    Stimmprofil einer Person, ausschließlich zur Sprecherzuordnung.

    Das Embedding ist verschlüsselt abgelegt und an eine aktive Einwilligung
    gebunden. Es entsteht aus Abschnitten, die zuvor über Mikrofonkanal oder
    Rednerliste sicher zugeordnet wurden — ein gesonderter Aufnahmetermin ist
    nicht nötig.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consent = models.OneToOneField(
        SpeakerConsent,
        on_delete=models.CASCADE,
        related_name="voice_profile",
        verbose_name="Einwilligung",
    )

    embedding_encrypted = models.BinaryField(verbose_name="Stimmprofil (verschlüsselt)")
    model_name = models.CharField(max_length=200, verbose_name="Modell")
    sample_count = models.PositiveIntegerField(default=0, verbose_name="Anzahl Stimmproben")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "minutes_voice_profiles"
        verbose_name = "Stimmprofil"
        verbose_name_plural = "Stimmprofile"

    def __str__(self) -> str:
        return f"Stimmprofil {self.id}"


class ProtocolStyle(models.Model):
    """
    Die gewünschte Form der Niederschrift eines Mandanten.

    Statt Prompts je Kunde zu pflegen, lernt die Form aus genehmigten
    Protokollen: `example_ids` verweist auf frühere Niederschriften, die als
    Beispiele in den Prompt gegeben werden. Damit trifft der Entwurf die
    Hausform, ohne dass jemand Prompt-Text schreiben muss.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session_tenant = models.ForeignKey(
        "session.SessionTenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="protocol_styles",
        verbose_name="Mandant (Session)",
    )
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="protocol_styles",
        verbose_name="Organisation (Work)",
    )

    name = models.CharField(max_length=200, verbose_name="Bezeichnung")
    protocol_type = models.CharField(
        max_length=30,
        choices=[
            ("resolution", "Beschlussprotokoll"),
            ("result", "Ergebnisprotokoll"),
            ("summary", "Verlaufsprotokoll (zusammengefasst)"),
            ("verbatim", "Wortprotokoll"),
        ],
        default="result",
        verbose_name="Protokollart",
    )
    style_notes = models.TextField(blank=True, verbose_name="Hinweise zur Form")
    example_protocol_ids = models.JSONField(default=list, verbose_name="Beispielprotokolle")
    is_default = models.BooleanField(default=False, verbose_name="Voreinstellung")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "minutes_protocol_styles"
        verbose_name = "Protokollform"
        verbose_name_plural = "Protokollformen"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ProtocolDraft(EncryptionMixin, models.Model):
    """
    KI-Entwurf für einen Abschnitt der Niederschrift.

    Der Entwurf ist ausdrücklich Vorschlag, nie Ergebnis: Er wird erst durch
    Übernahme der Protokollführung Teil des Protokolls. Bis dahin bleibt er
    hier und wird mit der Aufzeichnung gelöscht.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.OneToOneField(
        RecordingSegment,
        on_delete=models.CASCADE,
        related_name="draft",
        verbose_name="Abschnitt",
    )
    style = models.ForeignKey(
        ProtocolStyle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drafts",
        verbose_name="Protokollform",
    )

    text_encrypted = EncryptedTextField(verbose_name="Entwurfstext")
    model_name = models.CharField(max_length=200, blank=True, verbose_name="Modell")
    prompt_version = models.CharField(max_length=50, blank=True, verbose_name="Prompt-Version")
    processing_target = models.CharField(max_length=20, blank=True, verbose_name="Verarbeitungsziel")

    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="Übernommen am")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "minutes_protocol_drafts"
        verbose_name = "Protokollentwurf"
        verbose_name_plural = "Protokollentwürfe"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Entwurf {self.id}"

    def get_encryption_organization(self) -> object:
        return self.segment.recording.get_encryption_organization()


# Zentrale Rechenknoten-Konfiguration und GPU-Knoten liegen in einem eigenen
# Modul; erst dieser Import registriert die Modelle bei Django.
from .models_compute import ComputeSettings, GpuNode  # noqa: E402, F401
