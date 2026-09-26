# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Systemeinstellungen: SMTP-Passwort und Nebius-Schlüssel verschlüsselt speichern.

Beide lagen im Klartext in der Datenbank. Diese Migration

1. benennt die Klartextfelder im Modell in ``*_legacy`` um – nur im Modellzustand; die
   Spalten behalten ihren Namen, damit eine ältere Version sie nach einem Rückfall lesen kann,
2. legt die verschlüsselten Felder an (AES-256-GCM mit dem ENCRYPTION_MASTER_KEY, wie
   ``AISettings.api_key_encrypted``),
3. verschlüsselt vorhandene Werte und leert die Klartextspalten.

Schritt 3 ist wiederholbar: Er fasst nur Klartextspalten mit Inhalt an. Gibt es dort etwas,
aber keinen gültigen Hauptschlüssel, bricht die Migration ab, bevor sich etwas ändert.
Rückwärts schreibt sie die Werte wieder in die Klartextspalten (für eine ältere Version).
Ausgegeben wird nie ein Wert.
"""

import contextlib

from django.db import migrations, models

#: (Klartextfeld, verschlüsseltes Feld)
SECRETS = (
    ("email_host_password_legacy", "email_host_password_encrypted"),
    ("nebius_api_key_legacy", "nebius_api_key_encrypted"),
)
FIELDS = [name for pair in SECRETS for name in pair]
#: SiteSettings.CACHE_KEY
CACHE_KEY = "site_settings"


def _require_master_key() -> None:
    from apps.common.encryption import get_master_key

    try:
        get_master_key()
    except ValueError:
        raise RuntimeError(
            "Die Systemeinstellungen enthalten Geheimnisse, aber ENCRYPTION_MASTER_KEY fehlt oder ist "
            "ungültig. Es wurde nichts geändert; bitte den Schlüssel setzen und die Migration wiederholen."
        ) from None


def _forget_cached_settings() -> None:
    """Die zwischengespeicherte Instanz trägt noch die Klartextwerte. Cache nicht erreichbar: Sie verfällt nach 5 Minuten."""
    from django.core.cache import cache

    with contextlib.suppress(Exception):
        cache.delete(CACHE_KEY)


def encrypt_secrets(apps, schema_editor):
    from apps.common.encryption import encrypt_key

    SiteSettings = apps.get_model("common", "SiteSettings")
    rows = [row for row in SiteSettings.objects.order_by("pk") if any(getattr(row, plain) for plain, _ in SECRETS)]
    if not rows:
        return
    _require_master_key()
    for row in rows:
        for plain, encrypted in SECRETS:
            value = getattr(row, plain)
            if value:
                # Ein Klartextwert ist stets der jüngere (nur eine ältere Version schreibt ihn)
                setattr(row, encrypted, encrypt_key(value.encode("utf-8")))
                setattr(row, plain, "")
        row.save(update_fields=FIELDS)
    _forget_cached_settings()


def restore_plaintext(apps, schema_editor):
    from cryptography.exceptions import InvalidTag

    from apps.common.encryption import decrypt_key

    SiteSettings = apps.get_model("common", "SiteSettings")
    for row in SiteSettings.objects.order_by("pk"):
        changed = False
        for plain, encrypted in SECRETS:
            value = getattr(row, encrypted)
            if not value or getattr(row, plain):
                continue
            _require_master_key()
            try:
                setattr(row, plain, decrypt_key(bytes(value)).decode("utf-8"))
            except (InvalidTag, UnicodeDecodeError):
                raise RuntimeError(
                    f"SiteSettings.{encrypted} lässt sich mit dem Hauptschlüssel nicht lesen. Es wurde nichts geändert."
                ) from None
            setattr(row, encrypted, None)
            changed = True
        if changed:
            row.save(update_fields=FIELDS)
    _forget_cached_settings()


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0005_protokoll_kettenkopf"),
    ]

    operations = [
        # 1) Klartextfelder umbenennen, ohne die Spalten anzufassen
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name="sitesettings",
                    old_name="email_host_password",
                    new_name="email_host_password_legacy",
                ),
                migrations.AlterField(
                    model_name="sitesettings",
                    name="email_host_password_legacy",
                    field=models.CharField(
                        blank=True,
                        db_column="email_host_password",
                        editable=False,
                        help_text="Frühere Klartextspalte, wird nicht mehr beschrieben.",
                        max_length=255,
                        verbose_name="SMTP Passwort (Altbestand)",
                    ),
                ),
                migrations.RenameField(
                    model_name="sitesettings",
                    old_name="nebius_api_key",
                    new_name="nebius_api_key_legacy",
                ),
                migrations.AlterField(
                    model_name="sitesettings",
                    name="nebius_api_key_legacy",
                    field=models.CharField(
                        blank=True,
                        db_column="nebius_api_key",
                        editable=False,
                        help_text="Frühere Klartextspalte, wird nicht mehr beschrieben.",
                        max_length=255,
                        verbose_name="Nebius API Key (Altbestand)",
                    ),
                ),
            ],
            database_operations=[],
        ),
        # 2) Verschlüsselte Felder
        migrations.AddField(
            model_name="sitesettings",
            name="email_host_password_encrypted",
            field=models.BinaryField(
                blank=True,
                editable=False,
                help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
                null=True,
                verbose_name="SMTP Passwort (verschlüsselt)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="nebius_api_key_encrypted",
            field=models.BinaryField(
                blank=True,
                editable=False,
                help_text="AES-256-GCM verschlüsselt mit dem ENCRYPTION_MASTER_KEY.",
                null=True,
                verbose_name="Nebius API Key (verschlüsselt)",
            ),
        ),
        # 3) Bestand verschlüsseln, Klartextspalten leeren
        migrations.RunPython(encrypt_secrets, restore_plaintext),
    ]
