# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Common models for the Mandari platform.

Includes global site settings that can be configured via Admin.
"""

import logging
from typing import Any

from django.core.cache import cache
from django.db import models

from .ki_anbieter import ANBIETER_AUSWAHL, ANBIETER_VORLAGEN

logger = logging.getLogger(__name__)


def _encrypt_platform_secret(value: str) -> bytes | None:
    """Plattformweites Geheimnis mit dem Hauptschlüssel verschlüsseln (AES-256-GCM); leer ergibt ``None``."""
    from apps.common.encryption import encrypt_key

    return encrypt_key(value.encode("utf-8")) if value else None


def _decrypt_platform_secret(value: bytes | memoryview | None, label: str) -> str:
    """Gegenstück zu ``_encrypt_platform_secret``. Nicht lesbar: leer, mit Protokolleintrag ohne Wert."""
    from apps.common.encryption import decrypt_key

    if not value:
        return ""
    try:
        return decrypt_key(bytes(value)).decode("utf-8")
    except Exception:
        logger.error("%s der Systemeinstellungen ist nicht lesbar (Hauptschlüssel prüfen)", label)
        return ""


class SiteSettings(models.Model):
    """
    Singleton model for global site settings.

    Accessible via Admin, with fallback to environment variables.
    Use SiteSettings.get_settings() to retrieve the instance.

    Das SMTP-Passwort liegt mit dem ENCRYPTION_MASTER_KEY verschlüsselt in der Datenbank
    (AES-256-GCM, wie ``AISettings``). Lesen und Schreiben nur über
    ``get_email_host_password()``/``set_email_host_password()``.

    Die Spalten des früheren Nebius-Schlüssels sind seit Issue #950 ungenutzt: Die Migration
    common/0011 leert sie, gelesen und geschrieben werden sie nicht mehr. Sie bleiben nur, damit
    ein älteres Image nach einem Rückfall weiterläuft, und entfallen mit einer Folgeversion.
    KI-Zugänge stehen ausschließlich in den KI-Einstellungen (``AISettings``).

    Die ``*_legacy``-Felder sind die früheren Klartextspalten. Die Migration common/0006
    verschlüsselt ihren Inhalt und leert sie; neu beschrieben werden sie nur noch von einer
    älteren Version nach einem Rückfall. Ein Wert darin ist deshalb stets der jüngere und
    gewinnt; das nächste Speichern im Admin-Formular verschlüsselt ihn. Die Spalten entfallen
    mit einer Folgeversion.
    """

    CACHE_KEY = "site_settings"
    CACHE_TIMEOUT = 300  # 5 minutes

    # ==========================================================================
    # Email / SMTP Settings
    # ==========================================================================
    email_host = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="SMTP Host",
        help_text="z.B. smtp.gmail.com oder mail.example.de",
    )
    email_port = models.PositiveIntegerField(
        default=587, verbose_name="SMTP Port", help_text="Standardport: 587 (TLS) oder 465 (SSL)"
    )
    email_host_user = models.CharField(max_length=255, blank=True, verbose_name="SMTP Benutzername")
    email_host_password_encrypted = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="SMTP Passwort (verschlüsselt)",
        help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
    )
    email_host_password_legacy = models.CharField(
        max_length=255,
        blank=True,
        editable=False,
        db_column="email_host_password",
        verbose_name="SMTP Passwort (Altbestand)",
        help_text="Frühere Klartextspalte, wird nicht mehr beschrieben.",
    )
    email_use_tls = models.BooleanField(default=True, verbose_name="TLS verwenden", help_text="STARTTLS (Port 587)")
    email_use_ssl = models.BooleanField(
        default=False, verbose_name="SSL verwenden", help_text="Implizites SSL (Port 465)"
    )
    email_timeout = models.PositiveIntegerField(default=30, verbose_name="Timeout (Sekunden)")
    default_from_email = models.EmailField(
        blank=True, verbose_name="Standard-Absender", help_text="z.B. noreply@mandari.de"
    )
    default_from_name = models.CharField(
        max_length=255,
        blank=True,
        default="Mandari",
        verbose_name="Absender-Name",
        help_text="z.B. 'Mandari System'",
    )

    # ==========================================================================
    # Früherer KI-Schlüssel: ungenutzt seit Issue #950, von Migration common/0011 geleert
    # ==========================================================================
    nebius_api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Nebius API Key (verschlüsselt)",
        help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
    )
    nebius_api_key_legacy = models.CharField(
        max_length=255,
        blank=True,
        editable=False,
        db_column="nebius_api_key",
        verbose_name="Nebius API Key (Altbestand)",
        help_text="Frühere Klartextspalte, wird nicht mehr beschrieben.",
    )

    # ==========================================================================
    # Wartungsmodus (wirkt über apps.common.maintenance.MaintenanceModeMiddleware)
    # ==========================================================================
    # Die früheren Felder „Seitenname“ und „Seitenbeschreibung“ wirkten nirgends und sind
    # entfallen (Migration common/0007). Ihre Spalten bleiben mit Datenbank-Standardwert
    # stehen, damit eine ältere Version nach einem Rückfall weiterläuft.
    maintenance_mode = models.BooleanField(
        default=False, verbose_name="Wartungsmodus", help_text="Website für Besucher sperren"
    )
    maintenance_message = models.TextField(
        blank=True,
        default="Die Website wird gerade gewartet. Bitte versuchen Sie es später erneut.",
        verbose_name="Wartungsnachricht",
    )

    # ==========================================================================
    # Meta
    # ==========================================================================
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Systemeinstellungen"
        verbose_name_plural = "Systemeinstellungen"

    def __str__(self):
        return "Systemeinstellungen"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Ensure only one instance exists (Singleton pattern)
        self.pk = 1
        super().save(*args, **kwargs)
        # Clear cache on save
        cache.delete(self.CACHE_KEY)

    def delete(self, *args, **kwargs):
        # Prevent deletion
        pass

    @classmethod
    def get_settings(cls) -> "SiteSettings":
        """
        Get the site settings instance (cached).

        Returns the singleton instance, creating it if necessary.
        """
        settings = cache.get(cls.CACHE_KEY)
        if settings is None:
            settings, _ = cls.objects.get_or_create(pk=1)
            cache.set(cls.CACHE_KEY, settings, cls.CACHE_TIMEOUT)
        return settings

    # -- Geheimnisse (verschlüsselt mit dem Hauptschlüssel) ----------------------------------

    def set_email_host_password(self, value: str) -> None:
        """SMTP-Passwort verschlüsselt ablegen; leer löscht es. Erst ``save()`` speichert."""
        self.email_host_password_encrypted = _encrypt_platform_secret(value)
        self.email_host_password_legacy = ""

    def get_email_host_password(self) -> str:
        return self.email_host_password_legacy or _decrypt_platform_secret(
            self.email_host_password_encrypted, "SMTP-Passwort"
        )

    @property
    def has_email_host_password(self) -> bool:
        return bool(self.email_host_password_legacy or self.email_host_password_encrypted)

    @classmethod
    def get_email_config(cls) -> dict:
        """
        SMTP-Zugang und Absender der Plattform als Wörterbuch (``EMAIL_*``-Schlüssel).

        Dieselbe Auflösung wie im Mail-Dienst (``apps.common.mail.config.platform_config``): Ist hier ein
        SMTP-Server eingetragen, gelten diese Werte, sonst die aus der Umgebung.
        """
        from apps.common.mail.config import platform_config

        config = platform_config()
        return {
            "EMAIL_BACKEND": config.backend,
            "EMAIL_HOST": config.host,
            "EMAIL_PORT": config.port,
            "EMAIL_HOST_USER": config.username,
            "EMAIL_HOST_PASSWORD": config.password,
            "EMAIL_USE_TLS": config.use_tls,
            "EMAIL_USE_SSL": config.use_ssl,
            "EMAIL_TIMEOUT": config.timeout,
            "DEFAULT_FROM_EMAIL": config.from_address,
        }


class AISettings(models.Model):
    """
    Eine Konfiguration für alle KI-Aufrufe der Plattform (Issue #950).

    Gilt für Schreibhilfe und Co-Editor in Work (Organisationen mit eigenem Schlüssel überschreiben sie,
    ``tenants.Organization``) und für Zusammenfassung, Bürger-Chat und KI-Verortung im Bürgerportal.
    Aufgelöst wird nur in ``apps.common.ki_anbieter``: Wirksam ist eine Adresse nur, wenn ihr Host in der
    Positivliste ``KI_ERLAUBTE_HOSTS`` steht. Ohne Anbieter (Standard) bleibt die KI aus.

    Der Schlüssel liegt mit dem ENCRYPTION_MASTER_KEY verschlüsselt (AES-256-GCM), Zugriff nur über
    ``set_api_key()``/``get_api_key()``. Die neuen Felder haben Datenbank-Standardwerte, damit ein älteres
    Image nach einem Rückfall weiterläuft.
    """

    CACHE_KEY = "ai_settings"
    CACHE_TIMEOUT = 300  # 5 minutes

    #: Nicht eingerichtet: Die KI bleibt aus
    PROVIDER_NONE = ""
    PROVIDER_CHOICES = [(PROVIDER_NONE, "Nicht eingerichtet (KI aus)"), *ANBIETER_AUSWAHL]

    enabled = models.BooleanField(
        default=True,
        verbose_name="KI in Work aktiviert",
        help_text="Schreibhilfe und Co-Editor in Work. Aus: Die KI in Work ist aus, auch mit Schlüssel.",
    )
    provider = models.CharField(
        max_length=20,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_NONE,
        blank=True,
        verbose_name="KI-Anbieter",
        help_text=(
            "Vorlage eines OpenAI-kompatiblen Anbieters oder eigener Endpunkt. Wirksam nur, wenn der Host in "
            "KI_ERLAUBTE_HOSTS steht."
        ),
    )
    base_url = models.URLField(
        blank=True,
        verbose_name="API Base URL",
        help_text=(
            "Leer: Basis-URL der Vorlage. Beim eigenen Endpunkt Pflicht, etwa https://…/v1. Nur https, nur "
            "Hosts aus KI_ERLAUBTE_HOSTS."
        ),
    )
    anzeigename = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_default="",
        verbose_name="Anzeigename",
        help_text="Name des Anbieters in Hinweisen und in der Einwilligung. Leer: Name der Vorlage.",
    )
    verarbeitungsort = models.CharField(
        max_length=200,
        blank=True,
        default="",
        db_default="",
        verbose_name="Verarbeitungsort",
        help_text="Etwa: Rechenzentren in Deutschland (EU). Leer: Angabe der Vorlage.",
    )
    model_name = models.CharField(
        max_length=100,
        default="openai/gpt-oss-120b",
        verbose_name="Modell",
        help_text="Modellname beim gewählten Anbieter, für Work und als Standard für das Bürgerportal.",
    )
    fallback_model = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_default="",
        verbose_name="Ausweichmodell",
        help_text=(
            "Optional. Wird im Bürgerportal einmal versucht, wenn das Modell nicht antwortet (HTTP 404, 408, "
            "429, 5xx oder Zeitüberschreitung); gleicher Endpunkt, gleicher Schlüssel."
        ),
    )
    api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="API Key (verschlüsselt)",
        help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
    )
    insight_enabled = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="KI im Bürgerportal aktiviert",
        help_text="Zusammenfassungen, KI-Assistent und KI-Verortung im Bürgerportal.",
    )
    insight_model = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_default="",
        verbose_name="Modell im Bürgerportal",
        help_text="Leer: dasselbe Modell wie in Work.",
    )
    max_output_tokens = models.PositiveIntegerField(
        default=2000,
        verbose_name="Max. Output-Tokens (Work)",
        help_text="Obergrenze für die Antwortlänge je KI-Aufruf in Work.",
    )
    insight_max_output_tokens = models.PositiveIntegerField(
        default=16000,
        db_default=16000,
        verbose_name="Max. Output-Tokens (Bürgerportal)",
        help_text="Obergrenze für die Antwortlänge je KI-Aufruf im Bürgerportal (Zusammenfassungen, Chat).",
    )
    default_org_monthly_token_limit = models.PositiveIntegerField(
        default=3000000,
        verbose_name="Standard-Monatslimit pro Organisation (Tokens)",
        help_text=(
            "Gilt für Organisationen ohne eigenes Monatslimit. Pro Organisation überschreibbar über "
            "Organization → 'Token-Limit pro Monat' (leer = dieser Standard, 0 = KI deaktiviert)."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KI-Einstellungen"
        verbose_name_plural = "KI-Einstellungen"

    def __str__(self) -> str:
        return "KI-Einstellungen"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Ensure only one instance exists (Singleton pattern)
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        # Prevent deletion
        return 0, {}

    @classmethod
    def get_settings(cls) -> "AISettings":
        """Get the AI settings instance (cached singleton)."""
        instance = cache.get(cls.CACHE_KEY)
        if instance is None:
            instance, _ = cls.objects.get_or_create(pk=1)
            cache.set(cls.CACHE_KEY, instance, cls.CACHE_TIMEOUT)
        return instance

    def set_api_key(self, api_key: str) -> None:
        """Encrypt and store the global AI API key (master key, AES-256-GCM)."""
        from apps.common.encryption import encrypt_key

        if not api_key:
            self.api_key_encrypted = None
            return
        self.api_key_encrypted = encrypt_key(api_key.encode("utf-8"))

    def get_api_key(self) -> str:
        """Decrypt and return the global AI API key."""
        from apps.common.encryption import decrypt_key

        if not self.api_key_encrypted:
            return ""
        try:
            return decrypt_key(bytes(self.api_key_encrypted)).decode("utf-8")
        except Exception:
            return ""

    def get_effective_base_url(self) -> str:
        """Eingetragene Basis-URL, sonst die der Vorlage (ungeprüft; geprüft wird in ``ki_anbieter``)."""
        if self.base_url:
            return self.base_url
        vorlage = ANBIETER_VORLAGEN.get(self.provider or "")
        return vorlage.basis_url if vorlage else ""


class ProblemReport(models.Model):
    """
    Fehlermeldung aus dem „Problem melden"-Formular (Fehlerseiten).

    Wird als Ticket im Admin-Dashboard bearbeitet; bei hinterlegter
    E-Mail-Adresse (oder angemeldetem Konto) erhält die meldende Person
    eine Rückmeldung, sobald der Status auf „gelöst" gesetzt wird.
    """

    STATUS_CHOICES = [
        ("open", "Offen"),
        ("in_progress", "In Bearbeitung"),
        ("resolved", "Gelöst"),
        ("closed", "Geschlossen"),
    ]

    id = models.UUIDField(primary_key=True, default=None, editable=False)
    reference = models.CharField(max_length=20, unique=True, verbose_name="Ticket-Nr.")
    error_id = models.CharField(max_length=64, blank=True, verbose_name="Fehler-ID")
    url = models.URLField(max_length=1000, blank=True, verbose_name="Betroffene Seite")
    message = models.TextField(verbose_name="Beschreibung")
    browser_info = models.TextField(blank=True, verbose_name="Browser-Informationen")

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="problem_reports",
        verbose_name="Konto",
    )
    email = models.EmailField(blank=True, verbose_name="E-Mail für Rückmeldung")
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open", verbose_name="Status")
    admin_note = models.TextField(
        blank=True,
        verbose_name="Rückmeldung an die meldende Person",
        help_text="Wird beim Lösen des Tickets per E-Mail mitgeschickt",
    )
    resolved_at = models.DateTimeField(blank=True, null=True, verbose_name="Gelöst am")
    notified_at = models.DateTimeField(blank=True, null=True, verbose_name="Rückmeldung versandt am")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Eingegangen am")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fehlermeldung"
        verbose_name_plural = "Fehlermeldungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference}: {self.message[:60]}"

    def save(self, *args, **kwargs):
        import uuid as _uuid

        if self.id is None:
            self.id = _uuid.uuid4()
        if not self.reference:
            from django.utils import timezone as _tz

            # Kurze, gut kommunizierbare Ticket-Nummer
            self.reference = f"PM-{_tz.localdate().year}-{_uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def reporter_email(self) -> str:
        if self.email:
            return self.email
        if self.user and self.user.email:
            return self.user.email
        return ""


class AuditChainHead(models.Model):
    """
    Kettenkopf einer Protokoll-Hash-Kette (Issue #221).

    Je Protokollbereich – ein Session-Mandant, eine Fraktion oder das plattformweite
    Sicherheitsprotokoll – gibt es genau eine Zeile. Wer einen neuen Protokolleintrag schreibt,
    sperrt diese Zeile (``SELECT … FOR UPDATE``); so können parallele Einträge die Kette nicht
    verzweigen. Nach einer fristgerechten Löschung hält der Anker fest, ab welchem Eintrag die
    Prüfung beginnt, damit die Kette trotz gelöschter Anfangsstücke prüfbar bleibt.

    Bewusst ohne Fremdschlüssel auf Mandant oder Organisation: Der Bereich ist ein Textschlüssel
    (``session:<uuid>``, ``faction:<uuid>``, ``security``), die Zeile verschwindet beim Löschen des
    Mandanten über dessen ``post_delete``-Signal.
    """

    scope = models.CharField(max_length=80, primary_key=True, verbose_name="Bereich")
    last_seq = models.BigIntegerField(default=0, verbose_name="Letzte laufende Nummer")
    last_hash = models.CharField(max_length=64, blank=True, verbose_name="Hash des letzten Eintrags")
    initialized = models.BooleanField(
        default=False,
        verbose_name="Verkettung aktiv",
        help_text="Aus, solange Altbestand noch nicht verkettet ist (audit_chain_backfill)",
    )
    started_at = models.DateTimeField(blank=True, null=True, verbose_name="Verkettet seit")
    anchor_seq = models.BigIntegerField(default=0, verbose_name="Anker: letzte gelöschte Nummer")
    anchor_hash = models.CharField(
        max_length=64, blank=True, verbose_name="Anker: Hash des letzten gelöschten Eintrags"
    )
    anchor_archive = models.CharField(max_length=255, blank=True, verbose_name="Anker: Archivpaket")
    anchor_at = models.DateTimeField(blank=True, null=True, verbose_name="Anker gesetzt am")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Protokoll-Kettenkopf"
        verbose_name_plural = "Protokoll-Kettenköpfe"

    def __str__(self) -> str:
        return f"{self.scope} (Nr. {self.last_seq})"


class IdentifierBase(models.Model):
    """
    Basisadresse der dauerhaften Kennungen dieser Installation (Issue #733).

    Aus ihr bilden sich die kanonischen Kennungen eigener Objekte: Ein Objekt eines Session-Mandanten
    trägt ``uuid5`` über seine OParl-URL auf dieser Basis (``shared/mandari_oparl/ids.py``, ADR
    ``docs/adr/20260929-kanonisches-modell.md``). Die Basis wird einmal festgelegt – mit der Migration
    common/0009 aus dem damaligen ``SITE_URL``, auf einer neuen Installation beim ersten Bedarf – und
    ändert sich danach nicht mehr, auch nicht mit ``SITE_URL``. Ausgegebene Adressen folgen weiter
    ``SITE_URL``; ein Domainwechsel ändert so keine Kennung.

    Genau eine Zeile (pk=1), bewusst ohne Verwaltungsoberfläche. Lesen über
    ``apps.common.identifiers.identifier_base``.
    """

    url = models.CharField(
        max_length=500,
        verbose_name="Basisadresse",
        help_text="Ohne abschließenden Schrägstrich, z. B. https://mandari.de",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Festgelegt am")

    class Meta:
        verbose_name = "Basisadresse der Kennungen"
        verbose_name_plural = "Basisadresse der Kennungen"

    def __str__(self) -> str:
        return self.url


class MailOutbox(models.Model):
    """
    Postausgang des Mail-Dienstes (Issue #528): eine Mail, bis der Auftrag sie versendet hat.

    Der Auftrag ``apps.common.mail.outbox.deliver_mail`` (Warteschlange ``mail``) bekommt nur die
    Kennung dieser Zeile; Empfänger, Inhalt und Anhänge stehen verschlüsselt darin, mit dem
    Mandantenschlüssel der Organisation (``payload_encrypted``) oder, ohne Organisation, mit dem
    Hauptschlüssel (``payload_platform_encrypted``). Nach dem Versand wird der Inhalt gelöscht; die Zeile
    bleibt ohne Inhalt als Nachweis (Art, Weg, Zeitpunkte) und verfällt mit ihrer Frist
    (``apps.common.mail.outbox.purge``). Der Idempotenzschlüssel verhindert, dass dieselbe Mail
    (etwa je Ereignis und Empfänger) zweimal in den Postausgang kommt.
    """

    class Status(models.TextChoices):
        WARTEND = "wartend", "Wartet auf Versand"
        VERSENDET = "versendet", "Versendet"
        FEHLGESCHLAGEN = "fehlgeschlagen", "Endgültig fehlgeschlagen"

    id = models.UUIDField(primary_key=True, editable=False)
    kind = models.CharField(max_length=64, verbose_name="Mailart")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WARTEND, verbose_name="Status")
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Organisation",
        help_text="Versandweg und Mandantenschlüssel; leer bei Mails der Plattform",
    )
    via_organization = models.BooleanField(default=True, verbose_name="Über den Weg der Organisation")
    payload_encrypted = models.BinaryField(
        blank=True, null=True, editable=False, verbose_name="Inhalt (Mandantenschlüssel)"
    )
    payload_platform_encrypted = models.BinaryField(
        blank=True, null=True, editable=False, verbose_name="Inhalt (Hauptschlüssel)"
    )
    idempotency_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True, verbose_name="Idempotenzschlüssel"
    )
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="Versuche")
    task_id = models.CharField(max_length=64, blank=True, default="", verbose_name="Versandauftrag")
    route = models.CharField(max_length=20, blank=True, verbose_name="Genutzter Weg")
    error_code = models.CharField(max_length=200, blank=True, verbose_name="Fehlerklasse")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Angelegt am")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Beendet am")

    class Meta:
        verbose_name = "Mail im Postausgang"
        verbose_name_plural = "Postausgang"
        db_table = "common_mail_outbox"
        indexes = [models.Index(fields=["status", "finished_at"], name="common_mail_status_idx")]

    def __str__(self) -> str:
        return f"{self.kind} ({self.get_status_display()})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        import uuid as _uuid

        if self.id is None:
            self.id = _uuid.uuid4()
        super().save(*args, **kwargs)

    def get_encryption_organization(self) -> Any:
        return self.organization
