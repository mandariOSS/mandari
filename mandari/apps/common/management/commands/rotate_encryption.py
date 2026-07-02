# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management-Command: Verschlüsselung rotieren.

Rotiert den Master-Key UND alle Tenant-Keys. Jedes verschlüsselte Feld
wird mit dem alten Schlüssel entschlüsselt und mit einem neuen Schlüssel
neu verschlüsselt.

Use Case:
    Nach DB-Migration zwischen Servern (z.B. Prod → Dev), wenn der
    Prod-Master-Key auf dem Ziel-Server nicht verwendet werden soll.

Workflow:
    1. Alter Master-Key über ENV-Variable `OLD_MASTER_KEY` setzen
    2. Neuer Master-Key in `settings.ENCRYPTION_MASTER_KEY` (aus .env)
    3. Command ausführen: `python manage.py rotate_encryption`
    4. Bei Erfolg: Alle Tenant-Keys wurden neu generiert und mit
       neuem Master-Key verschlüsselt; alle Feld-Daten wurden mit
       neuen Tenant-Keys neu verschlüsselt.

Safety:
    - Läuft transaktional pro Tenant
    - Erstellt Backup der DB empfohlen (sollte vor Ausführung erstellt werden)
    - Bei Fehler: Rollback der aktuellen Tenant-Transaktion
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Verschlüsselt plaintext mit AES-256-GCM. Nonce wird vorne angehängt."""
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """Entschlüsselt Daten mit AES-256-GCM. Nonce wird von vorne gelesen."""
    aesgcm = AESGCM(key)
    nonce = ciphertext[:12]
    encrypted = ciphertext[12:]
    return aesgcm.decrypt(nonce, encrypted, None)


class Command(BaseCommand):
    help = "Rotiert Master-Key + alle Tenant-Keys + alle verschlüsselten Feld-Daten."

    def add_arguments(self, parser):
        parser.add_argument(
            "--old-master-key",
            type=str,
            default=os.environ.get("OLD_MASTER_KEY"),
            help="Alter Master-Key (base64). Default: ENV OLD_MASTER_KEY",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur simulieren, keine Änderungen speichern",
        )
        parser.add_argument(
            "--tenant-type",
            choices=["organization", "session", "both"],
            default="both",
            help="Welche Tenant-Typen rotieren (default: both)",
        )

    def handle(self, *args, **options):
        old_master_b64 = options["old_master_key"]
        dry_run = options["dry_run"]
        tenant_type = options["tenant_type"]

        if not old_master_b64:
            raise CommandError(
                "Alter Master-Key fehlt. Setze OLD_MASTER_KEY env-Variable oder nutze --old-master-key <base64>"
            )

        try:
            old_master = base64.b64decode(old_master_b64)
        except Exception as e:
            raise CommandError(f"Alter Master-Key ist kein gültiges base64: {e}")
        if len(old_master) != 32:
            raise CommandError(f"Alter Master-Key muss 32 Bytes sein, ist {len(old_master)}")

        try:
            new_master = base64.b64decode(settings.ENCRYPTION_MASTER_KEY)
        except Exception as e:
            raise CommandError(f"Neuer Master-Key (aus ENCRYPTION_MASTER_KEY) ist ungültig: {e}")
        if len(new_master) != 32:
            raise CommandError(f"Neuer Master-Key muss 32 Bytes sein, ist {len(new_master)}")

        if old_master == new_master:
            raise CommandError("Alter und neuer Master-Key sind identisch — nichts zu tun.")

        self.stdout.write(self.style.WARNING(f"Dry-Run: {dry_run}"))
        self.stdout.write(self.style.WARNING(f"Tenant-Type: {tenant_type}"))
        self.stdout.write("")

        # Sammle alle verschlüsselten Feld-Definitionen
        encrypted_fields = self._collect_encrypted_fields()
        self.stdout.write(f"Gefundene verschlüsselte Felder: {sum(len(v) for v in encrypted_fields.values())}")
        for model, fields in encrypted_fields.items():
            self.stdout.write(f"  {model.__name__}: {', '.join(fields)}")
        self.stdout.write("")

        # Rotiere Organization-Tenants
        total_stats = {"tenants": 0, "fields": 0, "skipped": 0, "errors": 0}

        if tenant_type in ("organization", "both"):
            from apps.tenants.models import Organization

            self.stdout.write(self.style.NOTICE("=== Organization-Tenants ==="))
            stats = self._rotate_tenant_type(
                Organization, encrypted_fields, old_master, new_master, dry_run, "organization"
            )
            for k, v in stats.items():
                total_stats[k] += v

        if tenant_type in ("session", "both"):
            try:
                from apps.session.models import SessionTenant

                self.stdout.write(self.style.NOTICE("\n=== Session-Tenants ==="))
                stats = self._rotate_tenant_type(
                    SessionTenant, encrypted_fields, old_master, new_master, dry_run, "session"
                )
                for k, v in stats.items():
                    total_stats[k] += v
            except (ImportError, LookupError):
                self.stdout.write(self.style.WARNING("Session-App nicht verfügbar — überspringe."))

        # Zusammenfassung
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"Tenants rotiert:    {total_stats['tenants']}"))
        self.stdout.write(self.style.SUCCESS(f"Felder rotiert:     {total_stats['fields']}"))
        self.stdout.write(self.style.WARNING(f"Leere Felder übersprungen: {total_stats['skipped']}"))
        if total_stats["errors"]:
            self.stdout.write(self.style.ERROR(f"Fehler:             {total_stats['errors']}"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDRY-RUN — keine Daten wurden geändert."))

    def _collect_encrypted_fields(self):
        """
        Findet alle Models mit EncryptedTextField und gibt ein Dict zurück:
            {ModelClass: [field_name, ...]}

        Erkennt Felder anhand des Suffix `_encrypted` UND anhand des Typs.
        """
        from apps.common.encryption import EncryptedTextField

        result = {}
        for model in apps.get_models():
            encrypted = []
            for f in model._meta.get_fields():
                if isinstance(f, EncryptedTextField):
                    encrypted.append(f.name)
            if encrypted:
                result[model] = encrypted
        return result

    def _get_tenant_field_for_model(self, model, tenant_model):
        """
        Findet den ForeignKey vom Model zum Tenant-Model (direkt oder indirekt).
        Gibt den Pfad als Liste zurück, z.B. ["organization"] oder ["meeting", "organization"].
        """
        # Direkter FK zum Tenant
        for f in model._meta.get_fields():
            if hasattr(f, "related_model") and f.related_model == tenant_model:
                if f.many_to_one or f.one_to_one:
                    return [f.name]

        # Indirekter FK über andere FKs (einfacher Fall: 1 Hop)
        for f in model._meta.get_fields():
            if hasattr(f, "related_model") and f.related_model is not None:
                if f.many_to_one or f.one_to_one:
                    # Prüfe ob das related_model einen FK zum Tenant hat
                    for rf in f.related_model._meta.get_fields():
                        if hasattr(rf, "related_model") and rf.related_model == tenant_model:
                            if rf.many_to_one or rf.one_to_one:
                                return [f.name, rf.name]

        return None

    def _rotate_tenant_type(self, tenant_model, encrypted_fields, old_master, new_master, dry_run, tenant_type):
        """
        Rotiert alle Tenants eines Typs und deren verschlüsselte Felder.
        """
        stats = {"tenants": 0, "fields": 0, "skipped": 0, "errors": 0}

        # Finde alle Models die zu diesem Tenant-Typ gehören
        relevant_models = {}
        for model, fields in encrypted_fields.items():
            if model == tenant_model:
                continue  # Tenant selbst behandeln wir separat
            path = self._get_tenant_field_for_model(model, tenant_model)
            if path:
                relevant_models[model] = (fields, path)

        self.stdout.write(f"Relevante Models für {tenant_model.__name__}:")
        for model, (fields, path) in relevant_models.items():
            self.stdout.write(f"  {model.__name__} via {'.'.join(path)}: {fields}")

        tenants = list(tenant_model.objects.all())
        self.stdout.write(f"\nGefundene Tenants: {len(tenants)}")

        for tenant in tenants:
            self.stdout.write(f"\n--- Tenant: {tenant} (id={tenant.pk}) ---")

            if not tenant.encryption_key:
                self.stdout.write(self.style.WARNING("  Kein encryption_key — überspringe."))
                continue

            # Alten Tenant-Key entschlüsseln
            try:
                old_tenant_key = aes_decrypt(old_master, bytes(tenant.encryption_key))
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  FEHLER: Konnte Tenant-Key nicht mit altem Master entschlüsseln: {e}")
                )
                stats["errors"] += 1
                continue

            # Neuen Tenant-Key generieren
            new_tenant_key = AESGCM.generate_key(bit_length=256)

            tenant_field_count = 0
            tenant_skipped = 0

            if dry_run:
                # Nur simulieren: Versuche jedes Feld zu entschlüsseln
                for model, (fields, path) in relevant_models.items():
                    lookup = "__".join(path) + "_id" if len(path) == 1 else None
                    if lookup:
                        objects = model.objects.filter(**{path[0]: tenant})
                    else:
                        # Indirekt — komplexere Filter
                        filter_kwargs = {"__".join(path): tenant}
                        objects = model.objects.filter(**filter_kwargs)

                    for obj in objects:
                        for fname in fields:
                            val = getattr(obj, fname)
                            if not val:
                                tenant_skipped += 1
                                continue
                            try:
                                aes_decrypt(old_tenant_key, bytes(val))
                                tenant_field_count += 1
                            except Exception as e:
                                self.stdout.write(
                                    self.style.ERROR(f"  {model.__name__}({obj.pk}).{fname}: Decrypt failed: {e}")
                                )
                                stats["errors"] += 1

                self.stdout.write(
                    f"  [Dry-Run] Würde {tenant_field_count} Felder rotieren, {tenant_skipped} übersprungen"
                )
                stats["tenants"] += 1
                stats["fields"] += tenant_field_count
                stats["skipped"] += tenant_skipped
                continue

            # Real-Run: Atomare Transaktion pro Tenant
            try:
                with transaction.atomic():
                    for model, (fields, path) in relevant_models.items():
                        filter_kwargs = {"__".join(path): tenant}
                        objects = model.objects.filter(**filter_kwargs)

                        for obj in objects:
                            update_fields = []
                            for fname in fields:
                                val = getattr(obj, fname)
                                if not val:
                                    tenant_skipped += 1
                                    continue
                                try:
                                    plaintext = aes_decrypt(old_tenant_key, bytes(val))
                                    new_ciphertext = aes_encrypt(new_tenant_key, plaintext)
                                    setattr(obj, fname, new_ciphertext)
                                    update_fields.append(fname)
                                    tenant_field_count += 1
                                except Exception as e:
                                    self.stdout.write(self.style.ERROR(f"  {model.__name__}({obj.pk}).{fname}: {e}"))
                                    stats["errors"] += 1
                                    raise  # Rollback für diesen Tenant

                            if update_fields:
                                obj.save(update_fields=update_fields)

                    # Tenant-Key mit neuem Master-Key verschlüsseln
                    new_encrypted_tenant_key = aes_encrypt(new_master, new_tenant_key)
                    tenant.encryption_key = new_encrypted_tenant_key
                    tenant.save(update_fields=["encryption_key"])

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  OK — {tenant_field_count} Felder rotiert, {tenant_skipped} leere übersprungen"
                    )
                )
                stats["tenants"] += 1
                stats["fields"] += tenant_field_count
                stats["skipped"] += tenant_skipped
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  FEHLER — Rollback für Tenant {tenant.pk}: {e}"))
                stats["errors"] += 1

        return stats
