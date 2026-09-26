# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verzeichnis aller verschlüsselt gespeicherten Werte.

Jedes Feld mit verschlüsseltem Inhalt steht hier genau einmal, zusammen mit dem
Schlüssel, der es schützt. Der Schlüsselwechsel (``rotate_encryption``) arbeitet
ausschließlich mit diesem Verzeichnis; was hier fehlt, wäre nach einem Wechsel
unlesbar. ``apps/common/tests/test_crypto_registry.py`` prüft deshalb, dass jedes
``BinaryField`` und jedes ``EncryptedTextField`` aller Modelle entweder hier
eingetragen oder ausdrücklich als unverschlüsselt begründet ist.

Neues verschlüsseltes Feld? Hier eintragen – mit Schlüsselart und, bei
Mandantendaten, dem Weg zum Mandanten (derselbe, den
``get_encryption_organization()`` des Modells geht; auch das prüft der Test).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class KeyKind(StrEnum):
    """Womit ein Wert verschlüsselt ist."""

    #: Mandantenschlüssel einer Organisation (Work) oder eines Session-Mandanten, AES-256-GCM
    TENANT = "mandant"
    #: der Mandantenschlüssel selbst, eingepackt mit dem Hauptschlüssel (AES-256-GCM)
    TENANT_KEY = "mandantenschluessel"
    #: plattformweite Geheimnisse, direkt mit dem Hauptschlüssel verschlüsselt (AES-256-GCM)
    MASTER = "hauptschluessel"
    #: zweiter Faktor, Fernet mit einem aus dem Hauptschlüssel abgeleiteten Schlüssel
    TWO_FACTOR = "zweiter_faktor"
    #: angelegt, aber noch ohne Schreibpfad; der Wechsel bricht ab, sobald hier Werte stehen
    RESERVED = "vorgesehen"


KIND_LABELS: dict[KeyKind, str] = {
    KeyKind.TENANT: "Mandantenschlüssel",
    KeyKind.TENANT_KEY: "Hauptschlüssel (Mandantenschlüssel)",
    KeyKind.MASTER: "Hauptschlüssel",
    KeyKind.TWO_FACTOR: "2FA-Schlüssel (aus dem Hauptschlüssel)",
    KeyKind.RESERVED: "noch nicht festgelegt",
}

#: Modelle, die einen Mandantenschlüssel tragen, mit Kurzname für ``--tenant-type``
TENANT_MODELS: dict[str, str] = {
    "tenants.Organization": "organization",
    "session.SessionTenant": "session",
}

#: Spalten der Mandantenmodelle mit eingepacktem Schlüssel (aktuell und, während eines Wechsels, vorheriger)
TENANT_KEY_FIELDS: tuple[str, ...] = ("encryption_key", "encryption_key_previous")

#: Pfad für Werte, die am Mandanten selbst hängen
SELF = "pk"


@dataclass(frozen=True)
class EncryptedField:
    """Ein verschlüsseltes Feld: Modell (``app_label.Model``), Feldname, Schlüsselart."""

    model: str
    field: str
    kind: KeyKind
    #: nur ``TENANT``: Lookup-Pfade zum Mandanten (Organisation oder Session-Mandant); genau einer ist je Zeile gesetzt
    tenant_paths: tuple[str, ...] = ()
    #: Zeilen je Abruf beim Schlüsselwechsel; kleiner bei Feldern mit großen Werten (Dokumente mit eingebetteten Bildern)
    batch_size: int = 500

    @property
    def label(self) -> str:
        return f"{self.model}.{self.field}"


def _tenant(model: str, *fields: str, paths: tuple[str, ...], batch_size: int = 500) -> tuple[EncryptedField, ...]:
    return tuple(EncryptedField(model, name, KeyKind.TENANT, paths, batch_size) for name in fields)


#: Dokumentinhalte und Editor-Zustände können eingebettete Bilder enthalten und mehrere MB groß sein
_LARGE = 50


_ORG = ("organization",)
_SESSION = ("tenant",)
_RECORDING = (
    "segment__recording__session_meeting__tenant",
    "segment__recording__faction_meeting__organization",
)

ENCRYPTED_FIELDS: tuple[EncryptedField, ...] = (
    # --- Mandantenschlüssel (mit dem Hauptschlüssel eingepackt) -------------------------------
    EncryptedField("tenants.Organization", "encryption_key", KeyKind.TENANT_KEY),
    EncryptedField("tenants.Organization", "encryption_key_previous", KeyKind.TENANT_KEY),
    EncryptedField("session.SessionTenant", "encryption_key", KeyKind.TENANT_KEY),
    EncryptedField("session.SessionTenant", "encryption_key_previous", KeyKind.TENANT_KEY),
    # --- Plattformweite Zugangsdaten (Hauptschlüssel) -------------------------------------------
    EncryptedField("common.SiteSettings", "email_host_password_encrypted", KeyKind.MASTER),
    EncryptedField("common.SiteSettings", "nebius_api_key_encrypted", KeyKind.MASTER),
    EncryptedField("common.AISettings", "api_key_encrypted", KeyKind.MASTER),
    EncryptedField("minutes.ComputeSettings", "client_secret_encrypted", KeyKind.MASTER),
    EncryptedField("minutes.ComputeSettings", "s3_secret_key_encrypted", KeyKind.MASTER),
    # --- Zweiter Faktor ------------------------------------------------------------------------
    EncryptedField("accounts.TwoFactorDevice", "secret_encrypted", KeyKind.TWO_FACTOR),
    EncryptedField("accounts.TwoFactorDevice", "backup_codes_encrypted", KeyKind.TWO_FACTOR),
    # --- Organisation (Work): Zugangsdaten mit eigenem Getter/Setter ----------------------------
    *_tenant("tenants.Organization", "smtp_password_encrypted", "ai_api_key_encrypted", paths=(SELF,)),
    # --- Work ----------------------------------------------------------------------------------
    *_tenant("work.FactionMeeting", "protocol_encrypted", paths=_ORG),
    *_tenant("work.FactionAgendaItem", "description_encrypted", "decision_encrypted", paths=("meeting__organization",)),
    *_tenant("work.FactionProtocolEntry", "content_encrypted", paths=("meeting__organization",)),
    *_tenant("work.MeetingPreparation", "notes_encrypted", paths=_ORG),
    *_tenant("work.AgendaItemPosition", "reasoning_encrypted", paths=_ORG),
    *_tenant("work.AgendaPrivateNote", "content_encrypted", paths=_ORG),
    *_tenant("work.AgendaSpeechNote", "content_encrypted", paths=_ORG),
    *_tenant("work.AgendaItemNote", "content_encrypted", paths=_ORG),
    *_tenant("work.FileAnnotation", "content_encrypted", paths=_ORG),
    *_tenant("work.PaperComment", "content_encrypted", paths=_ORG),
    *_tenant("work.Motion", "content_encrypted", "yjs_document_encrypted", paths=_ORG, batch_size=_LARGE),
    *_tenant("work.MotionRevision", "content_encrypted", paths=("motion__organization",), batch_size=_LARGE),
    *_tenant("work.SupportTicket", "description_encrypted", paths=_ORG),
    *_tenant("work.SupportTicketMessage", "content_encrypted", paths=("ticket__organization",)),
    # --- Session (Verwaltungs-RIS) ---------------------------------------------------------------
    *_tenant(
        "session.SessionPerson",
        "phone_encrypted",
        "address_encrypted",
        "bank_account_holder_encrypted",
        "bank_iban_encrypted",
        "bank_bic_encrypted",
        paths=_SESSION,
    ),
    *_tenant("session.SessionMeeting", "internal_notes_encrypted", paths=_SESSION),
    *_tenant(
        "session.SessionAgendaItem",
        "resolution_text_encrypted",
        "protocol_note_encrypted",
        paths=("meeting__tenant",),
    ),
    *_tenant("session.SessionPaper", "confidential_text_encrypted", paths=_SESSION),
    *_tenant("session.SessionApplication", "additional_info_encrypted", paths=_SESSION),
    *_tenant("session.SessionProtocol", "content_encrypted", paths=("meeting__tenant",)),
    *_tenant(
        "session.SessionProtocolCorrection",
        "reason_encrypted",
        "payload_encrypted",
        paths=("protocol__meeting__tenant",),
    ),
    *_tenant("session.SessionAttendance", "response_reason_encrypted", paths=("meeting__tenant",)),
    # --- Protokollassistenz (Aufzeichnung gehört zu einer Session- oder Fraktionssitzung) -------
    *_tenant("minutes.TranscriptSegment", "text_encrypted", paths=_RECORDING),
    *_tenant("minutes.ProtocolDraft", "text_encrypted", paths=_RECORDING),
    # --- Vorgesehen, noch ohne Schreibpfad -----------------------------------------------------
    EncryptedField("minutes.VoiceProfile", "embedding_encrypted", KeyKind.RESERVED),
)

#: Binärfelder ohne verschlüsselten Inhalt – jeweils mit Begründung
UNENCRYPTED_BINARY_FIELDS: dict[str, str] = {
    "insight_core.TileCache.tile_data": "Kartenkacheln aus öffentlichen Geodaten",
    "accounts.WebAuthnCredential.public_key": "öffentlicher Schlüssel eines Sicherheitsschlüssels, kein Geheimnis",
    "work.Motion.yjs_document_legacy": (
        "frühere Klartextspalte des Editor-Zustands; die Migration work/0057 verschlüsselt sie in "
        "yjs_document_encrypted, danach enthält sie nur eine Markierung (leer). Entfällt mit einer Folgeversion"
    ),
}


def fields_of_kind(*kinds: KeyKind) -> tuple[EncryptedField, ...]:
    return tuple(entry for entry in ENCRYPTED_FIELDS if entry.kind in kinds)
