# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Encryption utilities for tenant-specific data protection.

Uses AES-256-GCM for authenticated encryption with per-tenant keys.
The master key encrypts tenant keys, which in turn encrypt sensitive data.

Key Hierarchy:
    ENCRYPTION_MASTER_KEY (env var)
        ├── Organization / SessionTenant encryption_key (per tenant)
        │       └── Encrypted fields (notes, protocols, etc.)
        ├── platform secrets (AISettings, ComputeSettings)
        └── derived 2FA key (Fernet: TOTP secrets, backup codes)

Schlüsselwechsel: Während eines Wechsels gelten alter und neuer Schlüssel nebeneinander.
Gelesen wird mit beiden, geschrieben nur mit dem neuen. Für den Hauptschlüssel steht
der alte in ``ENCRYPTION_MASTER_KEY_PREVIOUS``, für Mandantenschlüssel in
``encryption_key_previous``. Weil AES-GCM authentifiziert ist, schlägt ein falscher
Schlüssel sauber fehl; der passende wird ausprobiert. Ablauf: docs/KRYPTOKONZEPT.md,
Verzeichnis aller verschlüsselten Felder: apps/common/crypto_registry.py.
"""

import base64
import binascii
import contextlib
import hashlib
import os
from collections.abc import Iterator
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import models

from apps.common.crypto_registry import TENANT_KEY_FIELDS

#: Setting (aus der gleichnamigen Umgebungsvariable) mit vorherigen Hauptschlüsseln während eines Wechsels
PREVIOUS_MASTER_KEY_SETTING = "ENCRYPTION_MASTER_KEY_PREVIOUS"

#: Zweckbindung des aus dem Hauptschlüssel abgeleiteten 2FA-Schlüssels
TWO_FACTOR_CONTEXT = b"mandari-2fa-v1:"


def get_master_key() -> bytes:
    """
    Get the master encryption key from settings.

    The master key should be a 32-byte (256-bit) key encoded as base64.
    Generate with: python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

    Security: Validates key length to ensure proper AES-256 encryption.
    """
    master_key_b64 = getattr(settings, "ENCRYPTION_MASTER_KEY", None)

    if not master_key_b64:
        raise ValueError(
            "ENCRYPTION_MASTER_KEY not configured. "
            'Generate with: python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"'
        )

    key = base64.b64decode(master_key_b64)

    # Security: Validate key length
    if len(key) != 32:
        raise ValueError(
            f"ENCRYPTION_MASTER_KEY must be exactly 32 bytes (256 bits), got {len(key)} bytes. "
            "Generate a new key with the command above."
        )

    return key


def decode_master_key(material: str, name: str = "ENCRYPTION_MASTER_KEY") -> bytes:
    """Base64-Text in einen 32-Byte-Schlüssel wandeln. Fehlermeldungen enthalten nie den Schlüssel."""
    try:
        key = base64.b64decode(material)
    except (binascii.Error, ValueError):
        raise ValueError(f"{name} ist kein gültiges Base64.") from None
    if len(key) != 32:
        raise ValueError(f"{name} muss genau 32 Byte (256 Bit) lang sein, hat aber {len(key)} Byte.")
    return key


def previous_master_key_materials() -> list[str]:
    """Vorherige Hauptschlüssel (Base64) aus ``ENCRYPTION_MASTER_KEY_PREVIOUS``, mehrere durch Komma getrennt."""
    raw = str(getattr(settings, PREVIOUS_MASTER_KEY_SETTING, "") or "")
    return [part for part in raw.replace(",", " ").split() if part]


def previous_master_keys() -> list[bytes]:
    """Vorherige Hauptschlüssel – nur zum Lesen während eines Wechsels, nie zum Schreiben."""
    return [decode_master_key(material, PREVIOUS_MASTER_KEY_SETTING) for material in previous_master_key_materials()]


def generate_key() -> bytes:
    """Generate a new 256-bit AES key."""
    return AESGCM.generate_key(bit_length=256)


def aes_seal(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM mit frischer 96-Bit-Nonce; die Nonce steht vor dem Geheimtext."""
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def aes_open(key: bytes, data: bytes) -> bytes:
    """Gegenstück zu ``aes_seal``. Falscher Schlüssel oder veränderte Daten: ``InvalidTag``."""
    if len(data) < 28:  # 12 Byte Nonce + 16 Byte Authentifizierungs-Tag
        raise InvalidTag
    return AESGCM(key).decrypt(data[:12], data[12:], None)


def encrypt_key(key: bytes, master_key: bytes | None = None) -> bytes:
    """
    Encrypt a key (or platform secret) with the master key.

    Returns the encrypted key with nonce prepended. Always uses the current master key
    unless one is given explicitly.
    """
    return aes_seal(get_master_key() if master_key is None else master_key, key)


def decrypt_key(encrypted_key: bytes | memoryview, master_key: bytes | None = None) -> bytes:
    """
    Decrypt a key (or platform secret) with the master key.

    Expects the nonce to be prepended to the ciphertext. Without an explicit key, the
    current master key is tried first, then any previous one from
    ``ENCRYPTION_MASTER_KEY_PREVIOUS`` (only set during a key change).
    """
    data = bytes(encrypted_key)
    if master_key is not None:
        return aes_open(master_key, data)
    try:
        return aes_open(get_master_key(), data)
    except InvalidTag:
        for previous in previous_master_keys():
            try:
                return aes_open(previous, data)
            except InvalidTag:
                continue
        raise


def two_factor_fernet(material: str) -> Fernet:
    """2FA-Schlüssel, zweckgebunden aus dem Hauptschlüssel (als konfigurierter Base64-Text) abgeleitet."""
    derived = hashlib.sha256(TWO_FACTOR_CONTEXT + material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def two_factor_material_variants(material: str) -> list[str]:
    """
    Schreibweisen desselben Hauptschlüssels.

    Der 2FA-Schlüssel wird aus dem Text abgeleitet, nicht aus den Bytes. Ein beim Umkopieren
    verändertes Leerzeichen oder Padding soll einen vorherigen Schlüssel nicht unbrauchbar machen.
    """
    variants = [material, material.strip()]
    with contextlib.suppress(binascii.Error, ValueError):
        variants.append(base64.b64encode(base64.b64decode(material)).decode("ascii"))
    return list(dict.fromkeys(variant for variant in variants if variant))


def two_factor_keys() -> MultiFernet | None:
    """
    Schlüsselbund für den zweiten Faktor: verschlüsselt mit dem aktuellen, liest auch mit vorherigen.

    Ohne ``ENCRYPTION_MASTER_KEY`` (Fallback ``ENCRYPTION_KEY``) gibt es keinen Schlüssel.
    """
    material = str(getattr(settings, "ENCRYPTION_MASTER_KEY", "") or getattr(settings, "ENCRYPTION_KEY", "") or "")
    if not material:
        return None
    fernets = [two_factor_fernet(material)]
    for previous in previous_master_key_materials():
        fernets.extend(two_factor_fernet(variant) for variant in two_factor_material_variants(previous))
    return MultiFernet(fernets)


def exclude_key_fields_from_save(instance: models.Model, kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    ``save()``-Argumente eines Mandantenmodells ohne dessen Schlüsselspalten.

    Ein allgemeines ``save()`` einer Instanz, die vor einem Schlüsselwechsel geladen wurde
    (etwa ein offenes Admin-Formular), würde sonst den alten Schlüssel zurückschreiben –
    die inzwischen neu verschlüsselten Inhalte wären unlesbar. Schlüssel ändern nur
    ``TenantEncryption`` und ``rotate_encryption``, jeweils mit gezielten Updates.
    Neue Zeilen und ausdrückliche ``update_fields`` bleiben unberührt.
    """
    if instance._state.adding or kwargs.get("force_insert") or kwargs.get("update_fields") is not None:
        return kwargs
    deferred = instance.get_deferred_fields()
    names = [
        field.name
        for field in instance._meta.concrete_fields
        if not field.primary_key and field.name not in TENANT_KEY_FIELDS and field.attname not in deferred
    ]
    return {**kwargs, "update_fields": names}


class TenantEncryption:
    """
    Encryption helper for tenant-specific data.

    Usage:
        encryption = TenantEncryption(organization)
        ciphertext = encryption.encrypt("sensitive data")
        plaintext = encryption.decrypt(ciphertext)
    """

    def __init__(self, organization: Any) -> None:
        """
        Initialize with an organization instance.

        Args:
            organization: Organization model instance with encryption_key field
        """
        import logging

        self.logger = logging.getLogger("apps.common.encryption")
        self.organization = organization
        self._key: bytes | None = None
        self._previous_key: bytes | None = None
        self._previous_loaded = False

    @property
    def key(self) -> bytes:
        """
        Get the decrypted tenant key.

        Generates a new key if none exists.
        """
        if self._key is None:
            if not self.organization.encryption_key and self.organization.pk:
                # Schutz vor Datenverlust: Eine veraltete (stale) Instanz mit
                # leerem encryption_key darf NIEMALS den DB-Key überschreiben,
                # sonst werden alle bestehenden Ciphertexte des Tenants
                # unlesbar. Vor der Generierung deshalb den aktuellen
                # DB-Stand nachladen.
                self.organization.refresh_from_db(fields=["encryption_key"])
            if not self.organization.encryption_key:
                # Generate new key for this tenant
                self.logger.info(f"[Encryption] Generating new key for org {self.organization.slug}")
                new_key = generate_key()
                encrypted = encrypt_key(new_key)
                if self.organization.pk is None:
                    self.organization.encryption_key = encrypted
                    self.organization.save(update_fields=["encryption_key"])
                    self._key = new_key
                elif self._store_if_empty(encrypted):
                    self.organization.encryption_key = encrypted
                    self._key = new_key
                    self.logger.info("[Encryption] New key generated and saved")
                else:
                    # Eine gleichzeitige Anfrage war schneller: deren Schlüssel gilt, nie überschreiben
                    self.organization.encryption_key = self._stored_key()
                    self._key = decrypt_key(bytes(self.organization.encryption_key))
            else:
                # Decrypt existing key
                try:
                    self._key = decrypt_key(self.organization.encryption_key)
                    self.logger.debug(f"[Encryption] Key decrypted for org {self.organization.slug}")
                except Exception as e:
                    self.logger.exception(f"[Encryption] KEY DECRYPTION FAILED for org {self.organization.slug}: {e}")
                    raise

        return self._key

    def _store_if_empty(self, encrypted: bytes) -> bool:
        """Schlüssel nur speichern, wenn in der Datenbank noch keiner steht (atomar, ohne Sperre)."""
        manager = type(self.organization)._default_manager
        leer = models.Q(encryption_key__isnull=True) | models.Q(encryption_key=b"")
        return bool(manager.filter(leer, pk=self.organization.pk).update(encryption_key=encrypted))

    def _stored_key(self) -> bytes:
        manager = type(self.organization)._default_manager
        return bytes(manager.filter(pk=self.organization.pk).values_list("encryption_key", flat=True).get())

    @property
    def previous_key(self) -> bytes | None:
        """Vorheriger Mandantenschlüssel – nur während eines Schlüsselwechsels gesetzt, nur zum Lesen."""
        if not self._previous_loaded:
            wrapped = getattr(self.organization, "encryption_key_previous", None)
            self._previous_key = None
            if wrapped:
                try:
                    self._previous_key = decrypt_key(wrapped)
                except InvalidTag:
                    org_id = self.organization.pk
                    self.logger.error("[Encryption] Vorheriger Mandantenschlüssel nicht lesbar (Mandant %s)", org_id)
            self._previous_loaded = True
        return self._previous_key

    def _read_keys(self) -> Iterator[bytes]:
        yield self.key
        previous = self.previous_key
        if previous is not None:
            yield previous

    def _reload_keys(self) -> bool:
        """
        Schlüsselspalten neu aus der Datenbank lesen.

        Eine Instanz, die vor einem Schlüsselwechsel geladen wurde, kennt den neuen Schlüssel
        noch nicht. Liefert ``True``, wenn sich etwas geändert hat.
        """
        if self.organization.pk is None:
            return False
        names = [name for name in TENANT_KEY_FIELDS if hasattr(self.organization, name)]
        manager = type(self.organization)._base_manager
        stored = manager.filter(pk=self.organization.pk).values_list(*names).first()
        if stored is None:
            return False
        changed = False
        for name, value in zip(names, stored, strict=True):
            current = getattr(self.organization, name)
            if (bytes(value) if value else None) != (bytes(current) if current else None):
                setattr(self.organization, name, value)
                changed = True
        if changed:
            self._key = None
            self._previous_key = None
            self._previous_loaded = False
        return changed

    def encrypt(self, plaintext: str) -> bytes:
        """
        Encrypt a string with AES-256-GCM.

        Args:
            plaintext: The string to encrypt

        Returns:
            Encrypted bytes with nonce prepended
        """
        if not plaintext:
            return b""

        try:
            self.logger.debug(f"[Encryption] Encrypting {len(plaintext)} chars")
            aesgcm = AESGCM(self.key)
            nonce = os.urandom(12)
            ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
            self.logger.debug(f"[Encryption] Encrypted to {len(ciphertext)} bytes")
            return nonce + ciphertext
        except Exception as e:
            self.logger.exception(f"[Encryption] ENCRYPT FAILED: {e}")
            raise

    def decrypt(self, ciphertext: bytes) -> str:
        """
        Decrypt bytes with AES-256-GCM.

        Args:
            ciphertext: Encrypted bytes with nonce prepended

        Returns:
            Decrypted string

        Raises:
            DecryptionError: If decryption fails (tampered data, wrong key)

        Security: Uses authenticated encryption (GCM) to detect tampering.
        """
        if not ciphertext:
            self.logger.debug("[Encryption] Decrypt called with empty ciphertext")
            return ""

        data = bytes(ciphertext)
        self.logger.debug(f"[Encryption] Decrypting {len(data)} bytes")

        # Security: Validate minimum ciphertext length (12 bytes nonce + at least 16 bytes auth tag)
        if len(data) < 28:
            self.logger.error(f"[Encryption] Ciphertext too short: {len(data)} bytes")
            raise DecryptionError("Invalid ciphertext: too short")

        try:
            # Aktueller, dann (während eines Wechsels) vorheriger Mandantenschlüssel. Passt keiner,
            # einmal die Schlüssel neu lesen: Die Instanz kann aus der Zeit vor dem Wechsel stammen.
            for attempt in range(2):
                for key in self._read_keys():
                    try:
                        plaintext = aes_open(key, data)
                    except InvalidTag:
                        continue
                    self.logger.debug(f"[Encryption] Decrypted to {len(plaintext)} bytes")
                    return plaintext.decode("utf-8")
                if attempt or not self._reload_keys():
                    break
        except Exception as e:
            # Security: Don't leak specific error details
            raise DecryptionError("Decryption failed: data may be corrupted or tampered") from e
        raise DecryptionError("Decryption failed: data may be corrupted or tampered")


class DecryptionError(Exception):
    """Raised when decryption fails due to invalid data or tampering."""

    pass


class EncryptedTextField(models.BinaryField):
    """
    Django model field for storing encrypted text.

    The field stores binary data but provides a string interface
    through the `get_<field>_decrypted` and `set_<field>_encrypted` methods.

    Usage:
        class MyModel(models.Model):
            organization = models.ForeignKey(Organization, ...)
            notes_encrypted = EncryptedTextField(blank=True, null=True)

        # Access:
        obj.set_notes_encrypted("sensitive data")
        plaintext = obj.get_notes_decrypted()
    """

    description = "Encrypted text field using AES-256-GCM"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("editable", True)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("null", True)
        super().__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name):
        """Add helper methods to the model class."""
        super().contribute_to_class(cls, name)

        # Remove _encrypted suffix for method names
        base_name = name.replace("_encrypted", "")

        def get_decrypted(self_model):
            """Get decrypted value."""
            value = getattr(self_model, name)
            if not value:
                return ""

            # Get organization from model
            org = self_model.get_encryption_organization()
            if not org:
                raise ValueError(
                    f"Cannot decrypt {name}: no organization found. "
                    "Implement get_encryption_organization() on your model."
                )

            encryption = TenantEncryption(org)
            return encryption.decrypt(value)

        def set_encrypted(self_model, plaintext):
            """Set encrypted value."""
            if not plaintext:
                setattr(self_model, name, None)
                return

            # Get organization from model
            org = self_model.get_encryption_organization()
            if not org:
                raise ValueError(
                    f"Cannot encrypt {name}: no organization found. "
                    "Implement get_encryption_organization() on your model."
                )

            encryption = TenantEncryption(org)
            setattr(self_model, name, encryption.encrypt(plaintext))

        # Add methods to model
        setattr(cls, f"get_{base_name}_decrypted", get_decrypted)
        setattr(cls, f"set_{base_name}_encrypted", set_encrypted)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Remove default values that we set
        if kwargs.get("editable") is True:
            del kwargs["editable"]
        if kwargs.get("blank") is True:
            del kwargs["blank"]
        if kwargs.get("null") is True:
            del kwargs["null"]
        return name, path, args, kwargs


class EncryptionMixin(models.Model):
    """
    Mixin for models that need encryption.

    Provides the get_encryption_organization() method required by EncryptedTextField.

    Usage:
        class MyModel(EncryptionMixin, models.Model):
            organization = models.ForeignKey('tenants.Organization', ...)
            notes_encrypted = EncryptedTextField()

            # If using a different field name for organization:
            def get_encryption_organization(self):
                return self.tenant
    """

    class Meta:
        abstract = True

    def get_encryption_organization(self):
        """
        Get the organization for encryption.

        Override this method if your model uses a different field name
        for the organization relationship.
        """
        if hasattr(self, "organization"):
            return self.organization
        if hasattr(self, "tenant"):
            return self.tenant
        if hasattr(self, "membership") and hasattr(self.membership, "organization"):
            return self.membership.organization
        return None
