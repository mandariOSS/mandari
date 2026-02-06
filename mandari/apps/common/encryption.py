# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Encryption utilities for tenant-specific data protection.

Uses AES-256-GCM for authenticated encryption with per-tenant keys.
The master key encrypts tenant keys, which in turn encrypt sensitive data.

Key Hierarchy:
    ENCRYPTION_MASTER_KEY (env var)
        └── Organization encryption_key (per tenant)
                └── Encrypted fields (notes, protocols, etc.)
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import models


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


def generate_key() -> bytes:
    """Generate a new 256-bit AES key."""
    return AESGCM.generate_key(bit_length=256)


def encrypt_key(key: bytes, master_key: bytes | None = None) -> bytes:
    """
    Encrypt a key with the master key.

    Returns the encrypted key with nonce prepended.
    """
    if master_key is None:
        master_key = get_master_key()

    aesgcm = AESGCM(master_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, key, None)
    return nonce + ciphertext


def decrypt_key(encrypted_key: bytes, master_key: bytes | None = None) -> bytes:
    """
    Decrypt a key with the master key.

    Expects the nonce to be prepended to the ciphertext.
    """
    if master_key is None:
        master_key = get_master_key()

    aesgcm = AESGCM(master_key)
    nonce = encrypted_key[:12]
    ciphertext = encrypted_key[12:]
    return aesgcm.decrypt(nonce, ciphertext, None)


class TenantEncryption:
    """
    Encryption helper for tenant-specific data.

    Usage:
        encryption = TenantEncryption(organization)
        ciphertext = encryption.encrypt("sensitive data")
        plaintext = encryption.decrypt(ciphertext)
    """

    def __init__(self, organization):
        """
        Initialize with an organization instance.

        Args:
            organization: Organization model instance with encryption_key field
        """
        import logging

        self.logger = logging.getLogger("apps.common.encryption")
        self.organization = organization
        self._key: bytes | None = None

    @property
    def key(self) -> bytes:
        """
        Get the decrypted tenant key.

        Generates a new key if none exists.
        """
        if self._key is None:
            if not self.organization.encryption_key:
                # Generate new key for this tenant
                self.logger.info(f"[Encryption] Generating new key for org {self.organization.slug}")
                new_key = generate_key()
                self.organization.encryption_key = encrypt_key(new_key)
                self.organization.save(update_fields=["encryption_key"])
                self._key = new_key
                self.logger.info("[Encryption] New key generated and saved")
            else:
                # Decrypt existing key
                try:
                    self._key = decrypt_key(self.organization.encryption_key)
                    self.logger.debug(f"[Encryption] Key decrypted for org {self.organization.slug}")
                except Exception as e:
                    self.logger.exception(f"[Encryption] KEY DECRYPTION FAILED for org {self.organization.slug}: {e}")
                    raise

        return self._key

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

        self.logger.debug(f"[Encryption] Decrypting {len(ciphertext)} bytes")

        # Security: Validate minimum ciphertext length (12 bytes nonce + at least 16 bytes auth tag)
        if len(ciphertext) < 28:
            self.logger.error(f"[Encryption] Ciphertext too short: {len(ciphertext)} bytes")
            raise DecryptionError("Invalid ciphertext: too short")

        try:
            aesgcm = AESGCM(self.key)
            nonce = ciphertext[:12]
            encrypted = ciphertext[12:]
            plaintext = aesgcm.decrypt(nonce, encrypted, None)
            self.logger.debug(f"[Encryption] Decrypted to {len(plaintext)} bytes")
            return plaintext.decode("utf-8")
        except Exception as e:
            # Security: Don't leak specific error details
            raise DecryptionError("Decryption failed: data may be corrupted or tampered") from e


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

    def __init__(self, *args, **kwargs):
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
