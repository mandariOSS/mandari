# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Security services for authentication and 2FA.

Provides:
- TOTP-based 2FA setup and verification
- Secure backup code generation
- Session management
- Password strength validation
"""

import base64
import hashlib
import hmac
import io
import json
import secrets
import struct
import time

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

try:
    import qrcode

    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

import contextlib

from .models import (
    SecurityNotification,
    TrustedDevice,
    TwoFactorDevice,
    UserSession,
    WebAuthnCredential,
)


class TwoFactorService:
    """
    TOTP-based two-factor authentication service.

    Implements RFC 6238 TOTP with secure key encryption.
    """

    ISSUER_NAME = "Mandari Work"
    TOTP_DIGITS = 6
    TOTP_INTERVAL = 30  # seconds
    BACKUP_CODE_COUNT = 10
    BACKUP_CODE_LENGTH = 8

    def __init__(self) -> None:
        """Schlüssel für Secret und Backup-Codes aus dem Master-Key ableiten."""
        self._fernet = self._build_fernet()

    @staticmethod
    def _build_fernet() -> Fernet | None:
        """Zweckgebundener Fernet-Schlüssel aus ENCRYPTION_MASTER_KEY (Fallback: ENCRYPTION_KEY)."""
        material = getattr(settings, "ENCRYPTION_MASTER_KEY", "") or getattr(settings, "ENCRYPTION_KEY", "") or ""
        if not material:
            return None
        derived_key = hashlib.sha256(b"mandari-2fa-v1:" + str(material).encode()).digest()
        return Fernet(base64.urlsafe_b64encode(derived_key))

    def _encrypt(self, data: str) -> bytes:
        """Verschlüsseln; ohne Schlüssel nur im DEBUG-Betrieb im Klartext."""
        if not self._fernet:
            if not settings.DEBUG:
                raise ImproperlyConfigured("ENCRYPTION_MASTER_KEY fehlt – 2FA-Daten können nicht verschlüsselt werden.")
            return data.encode()
        return self._fernet.encrypt(data.encode())

    def _decrypt(self, data) -> str:
        """Entschlüsseln; früher im Klartext gespeicherter Altbestand bleibt lesbar."""
        raw = bytes(data) if data is not None else b""
        if self._fernet:
            try:
                return self._fernet.decrypt(raw).decode()
            except InvalidToken:
                pass
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return ""

    def _is_encrypted(self, data) -> bool:
        """True, wenn die Daten mit dem aktuellen Schlüssel entschlüsselbar sind."""
        if not self._fernet or data is None:
            return False
        try:
            self._fernet.decrypt(bytes(data))
        except InvalidToken:
            return False
        return True

    def _reencrypt_legacy(self, device: TwoFactorDevice) -> None:
        """Im Klartext gespeicherte Altdaten beim nächsten erfolgreichen Einsatz verschlüsseln."""
        if not self._fernet:
            return
        fields = []
        if device.secret_encrypted is not None and not self._is_encrypted(device.secret_encrypted):
            device.secret_encrypted = self._encrypt(self._decrypt(device.secret_encrypted))
            fields.append("secret_encrypted")
        if device.backup_codes_encrypted and not self._is_encrypted(device.backup_codes_encrypted):
            device.backup_codes_encrypted = self._encrypt(self._decrypt(device.backup_codes_encrypted))
            fields.append("backup_codes_encrypted")
        if fields:
            device.save(update_fields=fields)

    def generate_secret(self) -> str:
        """Generate a new TOTP secret (base32 encoded)."""
        # Generate 160 bits of randomness
        random_bytes = secrets.token_bytes(20)
        # Base32 encode for TOTP compatibility
        return base64.b32encode(random_bytes).decode("ascii")

    def get_totp_uri(self, user, secret: str) -> str:
        """Generate otpauth:// URI for QR code scanning."""
        from urllib.parse import quote

        label = quote(user.email)
        issuer = quote(self.ISSUER_NAME)

        return (
            f"otpauth://totp/{issuer}:{label}"
            f"?secret={secret}"
            f"&issuer={issuer}"
            f"&algorithm=SHA1"
            f"&digits={self.TOTP_DIGITS}"
            f"&period={self.TOTP_INTERVAL}"
        )

    def generate_qr_code(self, uri: str) -> str | None:
        """Generate QR code as base64-encoded PNG."""
        if not HAS_QRCODE:
            return None

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(uri)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def _get_totp_code(self, secret: str, counter: int) -> str:
        """Generate TOTP code for a given counter value."""
        # Decode the base32 secret
        key = base64.b32decode(secret, casefold=True)

        # Pack counter as 8-byte big-endian
        counter_bytes = struct.pack(">Q", counter)

        # HMAC-SHA1
        hmac_hash = hmac.new(key, counter_bytes, hashlib.sha1).digest()

        # Dynamic truncation
        offset = hmac_hash[-1] & 0x0F
        code_int = struct.unpack(">I", hmac_hash[offset : offset + 4])[0]
        code_int &= 0x7FFFFFFF  # Remove sign bit

        # Get the specified number of digits
        code = code_int % (10**self.TOTP_DIGITS)

        return str(code).zfill(self.TOTP_DIGITS)

    def _matching_counter(self, secret: str, code: str, window: int = 1) -> int | None:
        """Zeitschritt, zu dem der Code passt, oder None (ohne gültiges Secret nie ein Treffer)."""
        if not secret:
            return None
        code = (code or "").replace(" ", "").strip()
        if len(code) != self.TOTP_DIGITS or not code.isdigit():
            return None
        try:
            current_counter = int(time.time() // self.TOTP_INTERVAL)
            for offset in range(-window, window + 1):
                counter = current_counter + offset
                if hmac.compare_digest(code, self._get_totp_code(secret, counter)):
                    return counter
        except Exception:
            return None
        return None

    def verify_code(self, secret: str, code: str, window: int = 1) -> bool:
        """
        Verify a TOTP code.

        Args:
            secret: The base32-encoded TOTP secret
            code: The code to verify
            window: Number of intervals to check before/after current

        Returns:
            True if the code is valid
        """
        return self._matching_counter(secret, code, window) is not None

    def setup_2fa(self, user) -> dict:
        """
        Begin 2FA setup for a user.

        Returns dict with secret, QR code, and backup codes.
        """
        # Generate new secret
        secret = self.generate_secret()

        # Generate QR code
        uri = self.get_totp_uri(user, secret)
        qr_code = self.generate_qr_code(uri)

        # Generate backup codes
        backup_codes = self.generate_backup_codes()

        # Create or update the device (not confirmed yet)
        device, created = TwoFactorDevice.objects.update_or_create(
            user=user,
            defaults={
                "secret_encrypted": self._encrypt(secret),
                "backup_codes_encrypted": self._encrypt(json.dumps(backup_codes)),
                "is_confirmed": False,
                "is_active": True,
            },
        )

        return {
            "secret": secret,
            "qr_code": qr_code,
            "uri": uri,
            "backup_codes": backup_codes,
        }

    def confirm_2fa(self, user, code: str) -> bool:
        """
        Confirm 2FA setup by verifying initial code.

        Returns True if the code is valid and 2FA is now active.
        """
        try:
            device = user.totp_device
        except TwoFactorDevice.DoesNotExist:
            return False

        if device.is_confirmed:
            return False  # Already confirmed

        # Get the secret
        secret = self._decrypt(device.secret_encrypted)

        # Verify the code
        if self.verify_code(secret, code):
            device.is_confirmed = True
            device.confirmed_at = timezone.now()
            device.save()

            # Create security notification
            SecurityNotification.objects.create(
                user=user,
                notification_type="2fa_enabled",
                title="2FA aktiviert",
                message="Die Zwei-Faktor-Authentifizierung wurde für Ihr Konto aktiviert.",
            )

            return True

        return False

    def verify_2fa(self, user, code: str) -> bool:
        """
        Verify a 2FA code during login.

        Also checks backup codes if TOTP fails.
        """
        try:
            device = user.totp_device
        except TwoFactorDevice.DoesNotExist:
            return False

        if not device.is_confirmed or not device.is_active:
            return False

        secret = self._decrypt(device.secret_encrypted)

        # Zuerst TOTP; jeder Zeitschritt ist je Nutzer nur einmal verwendbar (Replay-Schutz)
        counter = self._matching_counter(secret, code)
        if counter is not None:
            if not cache.add(f"accounts:2fa-used:{user.pk}:{counter}", True, timeout=self.TOTP_INTERVAL * 3):
                return False
            device.last_used_at = timezone.now()
            device.save(update_fields=["last_used_at"])
            self._reencrypt_legacy(device)
            return True

        # Danach Backup-Codes (jeweils einmalig)
        if self._use_backup_code(device, code):
            self._reencrypt_legacy(device)
            return True
        return False

    def _use_backup_code(self, device: TwoFactorDevice, code: str) -> bool:
        """Try to use a backup code."""
        if not device.backup_codes_encrypted:
            return False

        try:
            codes = json.loads(self._decrypt(device.backup_codes_encrypted))
        except (json.JSONDecodeError, Exception):
            return False

        # Normalize code (remove dashes/spaces)
        code = self._normalize_backup_code(code)
        if not code:
            return False

        for i, stored_code in enumerate(codes):
            if stored_code and hmac.compare_digest(code, self._normalize_backup_code(stored_code)):
                # Mark code as used
                codes[i] = None
                device.backup_codes_encrypted = self._encrypt(json.dumps(codes))
                device.last_used_at = timezone.now()
                device.save()
                return True

        return False

    @staticmethod
    def _normalize_backup_code(code: str) -> str:
        """Backup-Codes werden mit Bindestrich ausgegeben – Vergleich ohne Trenner und Groß-/Kleinschreibung."""
        return (code or "").replace("-", "").replace(" ", "").strip().lower()

    def generate_backup_codes(self) -> list[str]:
        """Generate a list of backup codes."""
        codes = []
        for _ in range(self.BACKUP_CODE_COUNT):
            # Generate readable code with dashes
            code = secrets.token_hex(self.BACKUP_CODE_LENGTH // 2)
            formatted = f"{code[:4]}-{code[4:]}"
            codes.append(formatted)
        return codes

    def regenerate_backup_codes(self, user) -> list[str]:
        """Regenerate backup codes for a user."""
        try:
            device = user.totp_device
        except TwoFactorDevice.DoesNotExist:
            return []

        if not device.is_confirmed:
            return []

        codes = self.generate_backup_codes()
        device.backup_codes_encrypted = self._encrypt(json.dumps(codes))
        device.save()

        return codes

    def disable_2fa(self, user) -> bool:
        """Disable 2FA for a user."""
        try:
            device = user.totp_device
            device.delete()
            # Sicherheitsschlüssel setzen die Authenticator-App als Rückfall voraus
            WebAuthnCredential.objects.filter(user=user).delete()

            # Create security notification
            SecurityNotification.objects.create(
                user=user,
                notification_type="2fa_disabled",
                title="2FA deaktiviert",
                message="Die Zwei-Faktor-Authentifizierung wurde für Ihr Konto deaktiviert.",
            )

            return True
        except TwoFactorDevice.DoesNotExist:
            return False

    def is_2fa_enabled(self, user) -> bool:
        """Check if a user has 2FA enabled."""
        try:
            device = user.totp_device
            return device.is_confirmed and device.is_active
        except TwoFactorDevice.DoesNotExist:
            return False


class SessionService:
    """Service for managing user sessions."""

    @classmethod
    def get_user_sessions(cls, user):
        """Get all active sessions for a user."""
        return UserSession.objects.filter(user=user, expires_at__gt=timezone.now()).order_by("-last_activity")

    @classmethod
    def create_session(cls, user, request, session_key: str):
        """Record a new user session."""
        # Match expires_at to actual session expiry
        expiry_age = request.session.get_expiry_age()
        if expiry_age and expiry_age > 0:
            expires_at = timezone.now() + timezone.timedelta(seconds=expiry_age)
        else:
            # Browser-close sessions: default to 24h for display purposes
            expires_at = timezone.now() + timezone.timedelta(hours=24)

        session, created = UserSession.objects.update_or_create(
            session_key=session_key,
            defaults={
                "user": user,
                "device_name": TrustedDevice._get_device_name(request),
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
                "ip_address": TrustedDevice._get_ip_address(request),
                "expires_at": expires_at,
                "is_current": True,
            },
        )
        return session

    @classmethod
    def revoke_session(cls, user, session_key: str) -> bool:
        """Revoke a specific session."""
        from django.contrib.sessions.models import Session

        try:
            session = UserSession.objects.get(user=user, session_key=session_key)

            # Delete Django session
            with contextlib.suppress(Session.DoesNotExist):
                Session.objects.get(session_key=session_key).delete()

            session.delete()

            # Create security notification
            SecurityNotification.objects.create(
                user=user,
                notification_type="session_revoked",
                title="Sitzung beendet",
                message="Eine Ihrer Sitzungen wurde beendet.",
            )

            return True
        except UserSession.DoesNotExist:
            return False

    @classmethod
    def revoke_all_sessions(cls, user, except_current: str = None):
        """Revoke all sessions for a user except optionally the current one."""
        from django.contrib.sessions.models import Session

        sessions = UserSession.objects.filter(user=user)
        if except_current:
            sessions = sessions.exclude(session_key=except_current)

        for session in sessions:
            with contextlib.suppress(Session.DoesNotExist):
                Session.objects.get(session_key=session.session_key).delete()

        count = sessions.count()
        sessions.delete()

        if count > 0:
            SecurityNotification.objects.create(
                user=user,
                notification_type="session_revoked",
                title="Sitzungen beendet",
                message=f"{count} Sitzung(en) wurden beendet.",
            )

        return count


class PasswordService:
    """Service for password management."""

    MIN_LENGTH = 8
    REQUIRE_UPPERCASE = True
    REQUIRE_LOWERCASE = True
    REQUIRE_DIGIT = True
    REQUIRE_SPECIAL = False

    @classmethod
    def check_strength(cls, password: str) -> dict:
        """
        Check password strength.

        Returns dict with score (0-4), issues, and is_valid.
        """
        issues = []
        score = 0

        if len(password) < cls.MIN_LENGTH:
            issues.append(f"Mindestens {cls.MIN_LENGTH} Zeichen erforderlich")
        else:
            score += 1

        if len(password) >= 12:
            score += 1

        has_upper = any(c.isupper() for c in password)
        has_lower = any(c.islower() for c in password)
        has_digit = any(c.isdigit() for c in password)
        has_special = any(not c.isalnum() for c in password)

        if cls.REQUIRE_UPPERCASE and not has_upper:
            issues.append("Mindestens ein Großbuchstabe erforderlich")
        elif has_upper:
            score += 0.5

        if cls.REQUIRE_LOWERCASE and not has_lower:
            issues.append("Mindestens ein Kleinbuchstabe erforderlich")
        elif has_lower:
            score += 0.5

        if cls.REQUIRE_DIGIT and not has_digit:
            issues.append("Mindestens eine Zahl erforderlich")
        elif has_digit:
            score += 0.5

        if cls.REQUIRE_SPECIAL and not has_special:
            issues.append("Mindestens ein Sonderzeichen erforderlich")
        elif has_special:
            score += 0.5

        # Cap score at 4
        score = min(int(score), 4)

        return {
            "score": score,
            "issues": issues,
            "is_valid": len(issues) == 0,
            "strength_label": ["Sehr schwach", "Schwach", "Mittel", "Stark", "Sehr stark"][score],
        }

    @classmethod
    def change_password(cls, user, old_password: str, new_password: str) -> tuple[bool, str]:
        """
        Change user password.

        Returns (success, message).
        """
        # Verify old password
        if not user.check_password(old_password):
            return False, "Das aktuelle Passwort ist nicht korrekt."

        # Check new password strength
        strength = cls.check_strength(new_password)
        if not strength["is_valid"]:
            return False, "; ".join(strength["issues"])

        # Set new password
        user.set_password(new_password)
        user.save()

        # Create security notification
        SecurityNotification.objects.create(
            user=user,
            notification_type="password_changed",
            title="Passwort geändert",
            message="Ihr Passwort wurde erfolgreich geändert.",
        )

        return True, "Passwort erfolgreich geändert."
