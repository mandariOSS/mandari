# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Common models for the Mandari platform.

Includes global site settings that can be configured via Admin.
"""

from django.core.cache import cache
from django.db import models


class SiteSettings(models.Model):
    """
    Singleton model for global site settings.

    Accessible via Admin, with fallback to environment variables.
    Use SiteSettings.get_settings() to retrieve the instance.
    """

    CACHE_KEY = "site_settings"
    CACHE_TIMEOUT = 300  # 5 minutes

    # ==========================================================================
    # Email / SMTP Settings
    # ==========================================================================
    email_backend = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="E-Mail Backend",
        help_text="Leer lassen für Standardwert aus Umgebungsvariablen",
        default="",
    )
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
    email_host_password = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="SMTP Passwort",
        help_text="Wird verschlüsselt gespeichert",
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
    # AI API Settings
    # ==========================================================================
    nebius_api_key = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Nebius API Key",
        help_text="API Key für Nebius TokenFactory (KI-Zusammenfassungen)",
    )

    # ==========================================================================
    # General Settings
    # ==========================================================================
    site_name = models.CharField(max_length=100, default="Mandari", verbose_name="Seitenname")
    site_description = models.TextField(
        blank=True, default="Kommunalpolitische Transparenz", verbose_name="Seitenbeschreibung"
    )
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

    def save(self, *args, **kwargs):
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

    @classmethod
    def get_nebius_api_key(cls) -> str:
        """
        Get Nebius API key.

        Priority: Environment variable > SiteSettings
        """
        import os

        # Check environment variable first
        env_key = os.environ.get("NEBIUS_API_KEY", "").strip()
        if env_key:
            return env_key

        # Fallback to SiteSettings
        site_settings = cls.get_settings()
        return site_settings.nebius_api_key or ""

    @classmethod
    def get_email_config(cls) -> dict:
        """
        Get email configuration dict.

        Returns settings from database if set, otherwise from Django settings.
        If email_host is configured in SiteSettings, automatically uses SMTP backend.
        """
        from django.conf import settings as django_settings

        site_settings = cls.get_settings()

        # If email_host is set in SiteSettings, use SMTP backend automatically
        if site_settings.email_host:
            backend = site_settings.email_backend or "django.core.mail.backends.smtp.EmailBackend"
        else:
            backend = site_settings.email_backend or django_settings.EMAIL_BACKEND

        return {
            "EMAIL_BACKEND": backend,
            "EMAIL_HOST": site_settings.email_host or django_settings.EMAIL_HOST,
            "EMAIL_PORT": site_settings.email_port if site_settings.email_host else django_settings.EMAIL_PORT,
            "EMAIL_HOST_USER": site_settings.email_host_user or django_settings.EMAIL_HOST_USER,
            "EMAIL_HOST_PASSWORD": site_settings.email_host_password or django_settings.EMAIL_HOST_PASSWORD,
            "EMAIL_USE_TLS": site_settings.email_use_tls if site_settings.email_host else django_settings.EMAIL_USE_TLS,
            "EMAIL_USE_SSL": site_settings.email_use_ssl if site_settings.email_host else django_settings.EMAIL_USE_SSL,
            "EMAIL_TIMEOUT": site_settings.email_timeout if site_settings.email_host else django_settings.EMAIL_TIMEOUT,
            "DEFAULT_FROM_EMAIL": site_settings.default_from_email or django_settings.DEFAULT_FROM_EMAIL,
        }


class AISettings(models.Model):
    """
    Singleton model for global AI configuration (Work DMS editor).

    Configured via Admin (unfold). Serves as the platform-wide default for
    the document AI assistant; organizations can override provider/model/key
    via their own fields on ``tenants.Organization``.

    The API key is encrypted at rest with the ENCRYPTION_MASTER_KEY
    (AES-256-GCM) — use ``set_api_key()`` / ``get_api_key()``.
    """

    CACHE_KEY = "ai_settings"
    CACHE_TIMEOUT = 300  # 5 minutes

    PROVIDER_ANTHROPIC = "anthropic"
    PROVIDER_OPENAI = "openai"
    PROVIDER_MISTRAL = "mistral"
    PROVIDER_NEBIUS = "nebius"
    PROVIDER_OVH = "ovh"
    PROVIDER_IONOS = "ionos"
    PROVIDER_CHOICES = [
        (PROVIDER_ANTHROPIC, "Anthropic (Claude)"),
        (PROVIDER_OPENAI, "OpenAI"),
        (PROVIDER_MISTRAL, "Mistral"),
        (PROVIDER_NEBIUS, "Nebius TokenFactory"),
        (PROVIDER_OVH, "OVHcloud AI Endpoints"),
        (PROVIDER_IONOS, "IONOS AI Model Hub"),
    ]

    # Default base URLs per provider (OpenAI-kompatibel, außer Anthropic).
    PROVIDER_BASE_URLS = {
        PROVIDER_ANTHROPIC: "https://api.anthropic.com/v1/",
        PROVIDER_OPENAI: "https://api.openai.com/v1/",
        PROVIDER_MISTRAL: "https://api.mistral.ai/v1/",
        PROVIDER_NEBIUS: "https://api.tokenfactory.nebius.com/v1/",
        PROVIDER_OVH: "",
        PROVIDER_IONOS: "",
    }

    enabled = models.BooleanField(
        default=True,
        verbose_name="KI aktiviert",
        help_text="Globaler Schalter für den KI-Assistenten im Dokumenten-Editor.",
    )
    provider = models.CharField(
        max_length=20,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_NEBIUS,
        verbose_name="KI-Anbieter",
    )
    base_url = models.URLField(
        blank=True,
        verbose_name="API Base URL",
        help_text="Optional. Leer lassen für den Anbieter-Standard (OpenAI-kompatible Endpunkte bzw. Anthropic Messages API).",
    )
    model_name = models.CharField(
        max_length=100,
        default="openai/gpt-oss-120b",
        verbose_name="Modell",
        help_text="z.B. openai/gpt-oss-120b (Nebius), gpt-4o-mini (OpenAI), claude-sonnet-4-5 (Anthropic), mistral-small-latest (Mistral).",
    )
    api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="API Key (verschlüsselt)",
        help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
    )
    max_output_tokens = models.PositiveIntegerField(
        default=2000,
        verbose_name="Max. Output-Tokens",
        help_text="Obergrenze für die Antwortlänge pro KI-Aufruf.",
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

    def __str__(self):
        return "KI-Einstellungen"

    def save(self, *args, **kwargs):
        # Ensure only one instance exists (Singleton pattern)
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    def delete(self, *args, **kwargs):
        # Prevent deletion
        pass

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
        """Resolve the API endpoint (explicit override or provider default)."""
        if self.base_url:
            return self.base_url
        return self.PROVIDER_BASE_URLS.get(self.provider, "")
