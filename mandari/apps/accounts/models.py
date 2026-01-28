# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Account models for user management and authentication.

Provides:
- Custom User model with email-based authentication
- Two-factor authentication (2FA) with TOTP
- Trusted device management
- Session management
- Security logging
"""

import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Custom user manager with email as the unique identifier."""

    def create_user(self, email, password=None, **extra_fields):
        """Create and return a regular user."""
        if not email:
            raise ValueError("E-Mail-Adresse ist erforderlich")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create and return a superuser."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser muss is_staff=True haben")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser muss is_superuser=True haben")

        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model with email-based authentication.

    Uses email instead of username for login.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Basic info
    email = models.EmailField(unique=True, verbose_name="E-Mail")
    first_name = models.CharField(max_length=150, blank=True, verbose_name="Vorname")
    last_name = models.CharField(max_length=150, blank=True, verbose_name="Nachname")

    # Profile
    avatar = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        verbose_name="Profilbild"
    )
    phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    is_staff = models.BooleanField(default=False, verbose_name="Mitarbeiter")
    email_verified = models.BooleanField(default=False, verbose_name="E-Mail verifiziert")

    # Timestamps
    date_joined = models.DateTimeField(default=timezone.now, verbose_name="Beigetreten")
    last_login = models.DateTimeField(blank=True, null=True, verbose_name="Letzter Login")

    # Settings
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "Benutzer"
        verbose_name_plural = "Benutzer"
        ordering = ["email"]

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        """Return the user's full name."""
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return self.email.split("@")[0]

    def get_full_name(self) -> str:
        """Return the user's full name (Django AbstractUser compatibility)."""
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return ""

    def get_short_name(self) -> str:
        """Return the user's first name (Django AbstractUser compatibility)."""
        return self.first_name

    @property
    def display_name(self) -> str:
        """Return a display name for the user."""
        return self.full_name

    def get_display_name(self) -> str:
        """Return a display name for the user (method version)."""
        return self.full_name

    def get_initials(self) -> str:
        """Return user initials for avatar placeholder."""
        if self.first_name and self.last_name:
            return f"{self.first_name[0]}{self.last_name[0]}".upper()
        return self.email[:2].upper()


class TwoFactorDevice(models.Model):
    """
    TOTP-based two-factor authentication device.

    Stores the encrypted TOTP secret for each user.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="totp_device"
    )

    # TOTP secret (encrypted)
    secret_encrypted = models.BinaryField(
        verbose_name="Verschlüsseltes Secret"
    )

    # Status
    is_confirmed = models.BooleanField(default=False, verbose_name="Bestätigt")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    last_used_at = models.DateTimeField(blank=True, null=True)

    # Backup codes (encrypted JSON list)
    backup_codes_encrypted = models.BinaryField(
        blank=True,
        null=True,
        verbose_name="Backup-Codes"
    )

    class Meta:
        verbose_name = "2FA-Gerät"
        verbose_name_plural = "2FA-Geräte"

    def __str__(self):
        return f"2FA für {self.user.email}"


class TrustedDevice(models.Model):
    """
    Trusted device for reduced 2FA prompts.

    When a user marks a device as trusted, they won't need to
    enter their 2FA code for a specified duration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="trusted_devices"
    )

    # Device identification
    device_token = models.CharField(max_length=64, unique=True)
    device_name = models.CharField(max_length=200, blank=True)

    # Browser info
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "Vertrauenswürdiges Gerät"
        verbose_name_plural = "Vertrauenswürdige Geräte"
        ordering = ["-last_used_at"]

    def __str__(self):
        return f"{self.device_name or 'Unbenannt'} ({self.user.email})"

    @property
    def is_valid(self) -> bool:
        """Check if the trusted device is still valid."""
        return timezone.now() < self.expires_at

    @classmethod
    def create_for_user(
        cls,
        user,
        request,
        device_name: str = "",
        valid_days: int = 30
    ):
        """Create a new trusted device for a user."""
        token = secrets.token_hex(32)
        expires_at = timezone.now() + timedelta(days=valid_days)

        return cls.objects.create(
            user=user,
            device_token=token,
            device_name=device_name or cls._get_device_name(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            ip_address=cls._get_ip_address(request),
            expires_at=expires_at,
        )

    @staticmethod
    def _get_device_name(request) -> str:
        """Extract device name from user agent."""
        ua = request.META.get("HTTP_USER_AGENT", "")
        # Simple extraction - can be enhanced with user-agents library
        if "Windows" in ua:
            return "Windows PC"
        elif "Mac" in ua:
            return "Mac"
        elif "Linux" in ua:
            return "Linux PC"
        elif "iPhone" in ua:
            return "iPhone"
        elif "Android" in ua:
            return "Android"
        return "Unbekanntes Gerät"

    @staticmethod
    def _get_ip_address(request) -> str:
        """Get client IP address from request."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")


class UserSession(models.Model):
    """
    Track user sessions for security and management.

    Allows users to see and revoke their active sessions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sessions"
    )

    # Session identification
    session_key = models.CharField(max_length=40, unique=True)

    # Device info
    device_name = models.CharField(max_length=200, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    # Location (optional, from IP)
    location = models.CharField(max_length=200, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    # Status
    is_current = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Benutzersitzung"
        verbose_name_plural = "Benutzersitzungen"
        ordering = ["-last_activity"]

    def __str__(self):
        return f"Session {self.session_key[:8]}... ({self.user.email})"


class LoginAttempt(models.Model):
    """
    Track login attempts for security monitoring.

    Used for rate limiting and detecting brute-force attacks.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Identification
    email = models.EmailField(db_index=True)
    ip_address = models.GenericIPAddressField(db_index=True)
    user_agent = models.CharField(max_length=500, blank=True)

    # Result
    was_successful = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=100, blank=True)

    # Timestamp
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Login-Versuch"
        verbose_name_plural = "Login-Versuche"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["email", "timestamp"]),
            models.Index(fields=["ip_address", "timestamp"]),
        ]

    @classmethod
    def get_recent_failures(cls, email: str, minutes: int = 30) -> int:
        """Count recent failed login attempts for an email."""
        cutoff = timezone.now() - timedelta(minutes=minutes)
        return cls.objects.filter(
            email=email,
            was_successful=False,
            timestamp__gte=cutoff
        ).count()

    @classmethod
    def is_rate_limited(cls, email: str, max_attempts: int = 5) -> bool:
        """Check if login should be rate limited."""
        return cls.get_recent_failures(email) >= max_attempts


class PasswordResetToken(models.Model):
    """
    Secure password reset token.

    Tokens are single-use and expire after a short period.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_tokens"
    )

    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "Passwort-Reset-Token"
        verbose_name_plural = "Passwort-Reset-Tokens"

    @property
    def is_valid(self) -> bool:
        """Check if the token is still valid."""
        return self.used_at is None and timezone.now() < self.expires_at

    @classmethod
    def create_for_user(cls, user, valid_hours: int = 24):
        """Create a new password reset token."""
        token = secrets.token_urlsafe(48)
        expires_at = timezone.now() + timedelta(hours=valid_hours)

        # Invalidate previous tokens
        cls.objects.filter(user=user, used_at__isnull=True).update(
            used_at=timezone.now()
        )

        return cls.objects.create(
            user=user,
            token=token,
            expires_at=expires_at,
        )


class EmailVerificationToken(models.Model):
    """
    Email verification token for new accounts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="email_verification_tokens"
    )

    token = models.CharField(max_length=64, unique=True)
    email = models.EmailField()  # The email being verified
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "E-Mail-Verifizierungstoken"
        verbose_name_plural = "E-Mail-Verifizierungstokens"

    @property
    def is_valid(self) -> bool:
        """Check if the token is still valid."""
        return self.verified_at is None and timezone.now() < self.expires_at

    @classmethod
    def create_for_user(cls, user, valid_hours: int = 48):
        """Create a new email verification token."""
        token = secrets.token_urlsafe(48)
        expires_at = timezone.now() + timedelta(hours=valid_hours)

        return cls.objects.create(
            user=user,
            token=token,
            email=user.email,
            expires_at=expires_at,
        )


class SecurityNotification(models.Model):
    """
    Security notifications for users.

    Alerts users to suspicious activity on their account.
    """

    NOTIFICATION_TYPES = [
        ("new_login", "Neuer Login"),
        ("password_changed", "Passwort geändert"),
        ("2fa_enabled", "2FA aktiviert"),
        ("2fa_disabled", "2FA deaktiviert"),
        ("device_added", "Gerät hinzugefügt"),
        ("device_removed", "Gerät entfernt"),
        ("session_revoked", "Sitzung beendet"),
        ("suspicious_activity", "Verdächtige Aktivität"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="security_notifications"
    )

    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES)
    title = models.CharField(max_length=200)
    message = models.TextField()

    # Context
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    device_info = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=200, blank=True)

    # Status
    is_read = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "Sicherheitsbenachrichtigung"
        verbose_name_plural = "Sicherheitsbenachrichtigungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.user.email})"
