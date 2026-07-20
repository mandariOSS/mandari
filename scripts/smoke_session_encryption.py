# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: SessionPerson-Verschlüsselung (P1-Fix, DSGVO).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_encryption.py

Hintergrund: SessionPerson speicherte Telefon, Adresse, Kontoinhaber,
IBAN und BIC im Klartext, obwohl Kommentare "(encrypted)" behaupteten.
Migration session.0002 stellt auf EncryptedTextField (AES-256-GCM mit
Tenant-Key des SessionTenant) um und verschlüsselt Altbestand.

Prüft:
- Datenmigration: Klartext-Altbestand (angelegt auf Stand 0001) wird
  korrekt verschlüsselt; Klartext-Spalten verschwinden; leere Werte
  bleiben leer; Tenant erhält einen encryption_key
- DB-Rohwerte (raw SQL) enthalten keinen Klartext
- Accessoren (set_*_encrypted / get_*_decrypted) liefern Klartext zurück
- E-Mail bleibt bewusst Klartext (Suche + OParl-API) und ist filterbar
- Personen-Detailseite rendert entschlüsselte Werte über die Accessoren
"""

import base64
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.migrations.executor import MigrationExecutor  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


IBAN = "DE89370400440532013000"
BIC = "COBADEFFXXX"
PHONE = "+49 251 4920"
ADDRESS = "Prinzipalmarkt 10, 48143 Münster"
HOLDER = "Erika Mustermann"
EMAIL = "erika.mustermann@example.org"

# =============================================================================
# Phase A: Alt-Klartext auf Migrationsstand 0001 anlegen, dann migrieren
# =============================================================================
print("=== Phase A: Datenmigration verschlüsselt Alt-Klartext ===")

# Nur bis session.0001 migrieren (inkl. Abhängigkeiten)
call_command("migrate", "session", "0001_initial", verbosity=0, interactive=False)

executor = MigrationExecutor(connection)
old_state = executor.loader.project_state(("session", "0001_initial"))
OldTenant = old_state.apps.get_model("session", "SessionTenant")
OldPerson = old_state.apps.get_model("session", "SessionPerson")

tenant_id = uuid.uuid4()
OldTenant.objects.create(id=tenant_id, name="Stadt Musterstadt", slug="musterstadt")

person_full_id = uuid.uuid4()
OldPerson.objects.create(
    id=person_full_id,
    tenant_id=tenant_id,
    given_name="Erika",
    family_name="Mustermann",
    email=EMAIL,
    phone=PHONE,
    address=ADDRESS,
    bank_account_holder=HOLDER,
    bank_iban=IBAN,
    bank_bic=BIC,
)
person_empty_id = uuid.uuid4()
OldPerson.objects.create(
    id=person_empty_id,
    tenant_id=tenant_id,
    given_name="Max",
    family_name="Leerfeld",
    email="",
    phone="",
    address="",
    bank_account_holder="",
    bank_iban="",
    bank_bic="",
)

# Restliche Migrationen anwenden (inkl. session.0002 mit Datenmigration)
call_command("migrate", verbosity=0, interactive=False)

from apps.session.models import (  # noqa: E402
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)

tenant = SessionTenant.objects.get(id=tenant_id)
person = SessionPerson.objects.get(id=person_full_id)
person_empty = SessionPerson.objects.get(id=person_empty_id)

check("Tenant hat nach Migration einen encryption_key", bool(tenant.encryption_key))
check("Migrierte IBAN entschlüsselbar", person.get_bank_iban_decrypted() == IBAN)
check("Migrierte BIC entschlüsselbar", person.get_bank_bic_decrypted() == BIC)
check("Migrierter Kontoinhaber entschlüsselbar", person.get_bank_account_holder_decrypted() == HOLDER)
check("Migriertes Telefon entschlüsselbar", person.get_phone_decrypted() == PHONE)
check("Migrierte Adresse entschlüsselbar", person.get_address_decrypted() == ADDRESS)
check("E-Mail bleibt Klartext erhalten", person.email == EMAIL)

check("Leere Felder bleiben leer (IBAN)", person_empty.get_bank_iban_decrypted() == "")
check("Leere Felder bleiben leer (Telefon)", person_empty.get_phone_decrypted() == "")

# DB-Rohwerte prüfen: kein Klartext, Klartext-Spalten entfernt
with connection.cursor() as cur:
    cur.execute("PRAGMA table_info(session_persons)")
    columns = {row[1] for row in cur.fetchall()}
check(
    "Klartext-Spalten entfernt",
    not ({"phone", "address", "bank_account_holder", "bank_iban", "bank_bic"} & columns),
    f"columns={sorted(columns)}",
)
check(
    "Verschlüsselte Spalten vorhanden",
    {
        "phone_encrypted",
        "address_encrypted",
        "bank_account_holder_encrypted",
        "bank_iban_encrypted",
        "bank_bic_encrypted",
    }
    <= columns,
    f"columns={sorted(columns)}",
)

with connection.cursor() as cur:
    cur.execute(
        "SELECT phone_encrypted, address_encrypted, bank_account_holder_encrypted, "
        "bank_iban_encrypted, bank_bic_encrypted FROM session_persons WHERE id = %s",
        [person_full_id.hex],
    )
    raw_row = cur.fetchone()

for label, raw_value, plain in [
    ("Telefon", raw_row[0], PHONE),
    ("Adresse", raw_row[1], ADDRESS),
    ("Kontoinhaber", raw_row[2], HOLDER),
    ("IBAN", raw_row[3], IBAN),
    ("BIC", raw_row[4], BIC),
]:
    raw_bytes = bytes(raw_value) if raw_value is not None else b""
    check(
        f"DB-Rohwert {label} ist nicht der Klartext",
        raw_bytes and plain.encode("utf-8") not in raw_bytes,
        f"raw={raw_bytes[:40]!r}",
    )
    check(
        f"DB-Rohwert {label} hat GCM-Mindestlänge (Nonce+Tag)",
        len(raw_bytes) >= 28,
        f"len={len(raw_bytes)}",
    )

# =============================================================================
# Phase B: Accessor-Roundtrip auf neuem Objekt
# =============================================================================
print()
print("=== Phase B: Accessor-Roundtrip (set/save/reload/get) ===")

new_person = SessionPerson(tenant=tenant, given_name="Neu", family_name="Angelegt")
new_person.set_bank_iban_encrypted("DE02120300000000202051")
new_person.set_bank_bic_encrypted("BYLADEM1001")
new_person.set_phone_encrypted("0251 123456")
new_person.save()

reloaded = SessionPerson.objects.get(id=new_person.id)
check("IBAN-Roundtrip", reloaded.get_bank_iban_decrypted() == "DE02120300000000202051")
check("BIC-Roundtrip", reloaded.get_bank_bic_decrypted() == "BYLADEM1001")
check("Telefon-Roundtrip", reloaded.get_phone_decrypted() == "0251 123456")
check("Nicht gesetzte Adresse -> ''", reloaded.get_address_decrypted() == "")

with connection.cursor() as cur:
    cur.execute("SELECT bank_iban_encrypted FROM session_persons WHERE id = %s", [new_person.id.hex])
    raw_iban = bytes(cur.fetchone()[0])
check(
    "Neuer Datensatz: DB-Rohwert IBAN ist Ciphertext",
    b"DE02120300000000202051" not in raw_iban and len(raw_iban) >= 28,
    f"raw={raw_iban[:40]!r}",
)

# Leeren Wert setzen -> Feld wird None
reloaded.set_bank_iban_encrypted("")
reloaded.save()
check(
    "Leerer Wert setzt Feld auf None",
    SessionPerson.objects.get(id=new_person.id).bank_iban_encrypted is None,
)

# =============================================================================
# Phase C: Views — Suche über Klartext-E-Mail und Detailseite mit Entschlüsselung
# =============================================================================
print()
print("=== Phase C: Personen-Liste (E-Mail-Suche) und Detailseite ===")

from apps.accounts.models import User  # noqa: E402

user = User.objects.create_user(email="smoke-admin@example.org", password="pw-Smoke-Test-1!")
role = SessionRole.objects.create(tenant=tenant, name="Sachbearbeitung")  # can_view_meetings default True
session_user = SessionUser.objects.create(user=user, tenant=tenant)
session_user.roles.add(role)

client = Client()
client.force_login(user)

resp = client.get(f"/session/{tenant.slug}/persons/", {"q": "erika.mustermann"})
check("Personen-Suche per E-Mail (Klartext-Filter) -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Suchtreffer enthält Person", b"Mustermann" in resp.content)

resp = client.get(f"/session/{tenant.slug}/persons/{person_full_id}/")
check("Personen-Detailseite -> 200", resp.status_code == 200, f"got {resp.status_code}")
html = resp.content.decode("utf-8")
check("Detailseite zeigt entschlüsseltes Telefon", PHONE in html)
check("Detailseite zeigt entschlüsselte Adresse", ADDRESS in html)
check("Detailseite zeigt E-Mail", EMAIL in html)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
