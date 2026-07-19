# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SessionPerson: Kontakt- und Bankdaten verschlüsseln.

Telefon, Adresse, Kontoinhaber, IBAN und BIC lagen trotz gegenteiliger
Kommentare im Klartext in der Datenbank. Diese Migration:

1. legt verschlüsselte Felder (AES-256-GCM, Tenant-Key) an,
2. verschlüsselt vorhandene Klartextwerte (idempotent, in Batches;
   bei leerer Tabelle no-op, es wird dann kein Master-Key benötigt),
3. entfernt die Klartext-Spalten.

Die E-Mail bleibt bewusst Klartext (Suche/Filterung + OParl-API).

Die Migration ist absichtlich irreversibel: Ein Rückwärtslauf würde die
Klartext-Spalten leer wiederherstellen und die Ciphertexte verwerfen.
"""

from django.db import migrations

import apps.common.encryption

BATCH_SIZE = 500

# Klartext-Feld -> verschlüsseltes Feld
FIELD_MAP = {
    "phone": "phone_encrypted",
    "address": "address_encrypted",
    "bank_account_holder": "bank_account_holder_encrypted",
    "bank_iban": "bank_iban_encrypted",
    "bank_bic": "bank_bic_encrypted",
}


def encrypt_existing_data(apps, schema_editor):
    """Bestehende Klartextwerte mit dem Tenant-Key verschlüsseln."""
    from apps.common.encryption import TenantEncryption

    SessionPerson = apps.get_model("session", "SessionPerson")

    # Nur Zeilen anfassen, die überhaupt Klartext enthalten. Bei einer
    # leeren/jungen Installation ist das ein reiner No-op (kein Zugriff
    # auf den Master-Key nötig).
    qs = (
        SessionPerson.objects.exclude(
            phone="",
            address="",
            bank_account_holder="",
            bank_iban="",
            bank_bic="",
        )
        .select_related("tenant")
        .order_by("pk")
    )

    encryptors = {}  # tenant_id -> TenantEncryption (Key nur einmal entschlüsseln)
    batch = []

    for person in qs.iterator(chunk_size=BATCH_SIZE):
        changed = False
        for plain_field, enc_field in FIELD_MAP.items():
            plaintext = getattr(person, plain_field, "") or ""
            if not plaintext:
                continue
            # Idempotenz: bereits verschlüsselte Werte nicht doppelt verschlüsseln
            if getattr(person, enc_field):
                continue
            encryption = encryptors.get(person.tenant_id)
            if encryption is None:
                encryption = TenantEncryption(person.tenant)
                encryptors[person.tenant_id] = encryption
            setattr(person, enc_field, encryption.encrypt(plaintext))
            changed = True
        if changed:
            batch.append(person)
        if len(batch) >= BATCH_SIZE:
            SessionPerson.objects.bulk_update(batch, list(FIELD_MAP.values()))
            batch = []

    if batch:
        SessionPerson.objects.bulk_update(batch, list(FIELD_MAP.values()))


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0001_initial"),
    ]

    operations = [
        # 1) Verschlüsselte Felder anlegen
        migrations.AddField(
            model_name="sessionperson",
            name="phone_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="Telefon (verschlüsselt)"),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="address_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="Adresse (verschlüsselt)"),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="bank_account_holder_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="Kontoinhaber (verschlüsselt)"),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="bank_iban_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="IBAN (verschlüsselt)"),
        ),
        migrations.AddField(
            model_name="sessionperson",
            name="bank_bic_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="BIC (verschlüsselt)"),
        ),
        # 2) Bestehende Klartextwerte verschlüsseln (irreversibel)
        migrations.RunPython(encrypt_existing_data),
        # 3) Klartext-Spalten entfernen
        migrations.RemoveField(model_name="sessionperson", name="phone"),
        migrations.RemoveField(model_name="sessionperson", name="address"),
        migrations.RemoveField(model_name="sessionperson", name="bank_account_holder"),
        migrations.RemoveField(model_name="sessionperson", name="bank_iban"),
        migrations.RemoveField(model_name="sessionperson", name="bank_bic"),
    ]
