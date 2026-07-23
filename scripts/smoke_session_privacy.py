# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: DSGVO-Paket im Session RIS (Issue #43).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_privacy.py

Prüft:
- Aufbewahrungsfrist-Einstellungen je Datenart (UI, auditiert)
- Anonymisierungs-/Löschlauf (UI + Management-Command, Dry-Run):
  Kontakt-/Bankdaten ausgeschiedener Personen weg, Name bleibt;
  NÖ-Protokollteil/interne Notizen geleert; alte Audit-Einträge gelöscht;
  Lauf selbst nachweisbar im Audit-Log
- Betroffenenauskunft: JSON-Export mit allen Datenarten; Bankdaten nur
  mit manage_allowances entschlüsselt; Export auditiert
- Öffentliche Hinweisseite "Datenschutz im RIS" (ohne Login, je Tenant)
- Permission-Checks (manage_settings) und Tenant-Isolation
"""

import base64
import os
import secrets
import sys
import tempfile
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
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

import json  # noqa: E402
from datetime import timedelta  # noqa: E402
from io import StringIO  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from django.utils import timezone  # noqa: E402

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


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
other_tenant = SessionTenant.objects.create(name="Stadt Anderswo", slug="anderswo")

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
clerk_user = User.objects.create_user(email="clerk@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(roles["admin"])
su_clerk = SessionUser.objects.create(user=clerk_user, tenant=tenant)
su_clerk.roles.add(roles["clerk"])

admin = Client()
admin.force_login(admin_user)
clerk = Client()
clerk.force_login(clerk_user)
anonymous = Client()

base = f"/session/{tenant.slug}"

org = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")

# Ausgeschiedene Person (Mandatsende vor 3 Jahren) mit Kontakt- + Bankdaten
old_person = SessionPerson.objects.create(
    tenant=tenant,
    given_name="Alma",
    family_name="Altrat",
    email="alma@example.org",
    is_active=False,
    end_date=timezone.localdate() - timedelta(days=365 * 3),
)
old_person.set_phone_encrypted("0251 123456")
old_person.set_address_encrypted("Altweg 1, Musterstadt")
old_person.set_bank_iban_encrypted("DE02120300000000202051")
old_person.set_bank_account_holder_encrypted("Alma Altrat")
old_person.save()

# Aktive Person mit Daten (darf NICHT anonymisiert werden)
active_person = SessionPerson.objects.create(
    tenant=tenant, given_name="Bernd", family_name="Bleibt", email="bernd@example.org"
)
active_person.set_phone_encrypted("0251 654321")
active_person.set_bank_iban_encrypted("DE02120300000000202052")
active_person.save()

# Alte Sitzung mit NÖ-Protokollteil + internen Notizen
old_meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Alte Sitzung", organization=org, start=timezone.now() - timedelta(days=365 * 6)
)
old_meeting.set_internal_notes_encrypted("Interne Notiz")
old_meeting.save()
protocol = SessionProtocol.objects.create(meeting=old_meeting, content="Öffentlicher Teil")
protocol.set_content_encrypted("Geheimer NÖ-Teil")
protocol.save()

new_meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Neue Sitzung", organization=org, start=timezone.now() - timedelta(days=30)
)
new_meeting.set_internal_notes_encrypted("Aktuelle Notiz")
new_meeting.save()

# Alter Audit-Eintrag (Frist überschritten)
old_audit = SessionAuditLog.objects.create(
    tenant=tenant, action="update", model_name="SessionMeeting", object_id=old_meeting.pk, object_repr="alt"
)
SessionAuditLog.objects.filter(pk=old_audit.pk).update(created_at=timezone.now() - timedelta(days=365 * 12))

# =============================================================================
# Phase A: Einstellungen
# =============================================================================
print("=== Phase A: Datenschutz-Einstellungen ===")

resp = admin.get(f"{base}/settings/privacy/")
check("Datenschutz-Seite (Admin) -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = clerk.get(f"{base}/settings/privacy/")
check("Datenschutz-Seite (Clerk ohne manage_settings) -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = admin.post(
    f"{base}/settings/privacy/",
    {"persons_years": "2", "np_content_years": "5", "audit_years": "10", "notice": "Unsere Datenschutzhinweise."},
)
tenant.refresh_from_db()
privacy = tenant.settings.get("privacy", {})
check(
    "Fristen gespeichert (2/5/10 Jahre + Hinweistext)",
    privacy.get("persons_years") == 2
    and privacy.get("np_content_years") == 5
    and privacy.get("audit_years") == 10
    and privacy.get("notice") == "Unsere Datenschutzhinweise.",
)
check(
    "Einstellungs-Änderung auditiert",
    SessionAuditLog.objects.filter(tenant=tenant, changes__has_key="dsgvo_einstellungen").exists(),
)

# =============================================================================
# Phase B: Löschlauf (Dry-Run + echt, UI + Command)
# =============================================================================
print("=== Phase B: Anonymisierungs-/Löschlauf ===")

resp = admin.post(f"{base}/settings/privacy/purge/", {"dry_run": "1"})
old_person.refresh_from_db()
check("Dry-Run verändert nichts", resp.status_code == 302 and old_person.email == "alma@example.org")

out = StringIO()
call_command("session_privacy_purge", tenant=tenant.slug, stdout=out)
output = out.getvalue()
check("Command läuft und meldet Zahlen", "anonymisiert" in output and tenant.name in output, output.strip())

old_person.refresh_from_db()
check(
    "Ausgeschiedene Person anonymisiert (Kontakt+Bank weg)",
    old_person.email == ""
    and not old_person.get_phone_decrypted()
    and not old_person.get_address_decrypted()
    and not old_person.get_bank_iban_decrypted(),
)
check("Name bleibt erhalten (historische Beschlüsse)", old_person.family_name == "Altrat")

active_person.refresh_from_db()
check(
    "Aktive Person unangetastet",
    active_person.email == "bernd@example.org" and active_person.get_bank_iban_decrypted() == "DE02120300000000202052",
)

protocol.refresh_from_db()
old_meeting.refresh_from_db()
new_meeting.refresh_from_db()
check(
    "NÖ-Protokollteil + interne Notizen der alten Sitzung geleert",
    not protocol.get_content_decrypted() and not old_meeting.get_internal_notes_decrypted(),
)
check("Öffentlicher Protokollteil bleibt", protocol.content == "Öffentlicher Teil")
check("Junge Sitzung unangetastet", new_meeting.get_internal_notes_decrypted() == "Aktuelle Notiz")

check("Alter Audit-Eintrag gelöscht", not SessionAuditLog.objects.filter(pk=old_audit.pk).exists())
check(
    "Anonymisierung je Person auditiert (ohne Klartext)",
    SessionAuditLog.objects.filter(tenant=tenant, changes__has_key="dsgvo_anonymisiert").exists(),
)
check(
    "Löschlauf selbst nachweisbar im Audit-Log",
    SessionAuditLog.objects.filter(tenant=tenant, changes__has_key="dsgvo_loeschlauf").exists(),
)

# Zweiter Lauf: idempotent
out = StringIO()
call_command("session_privacy_purge", tenant=tenant.slug, stdout=out)
check("Zweiter Lauf anonymisiert nichts erneut", "0 Person(en) anonymisiert" in out.getvalue(), out.getvalue().strip())

# =============================================================================
# Phase C: Betroffenenauskunft
# =============================================================================
print("=== Phase C: Betroffenenauskunft ===")

resp = admin.get(f"{base}/persons/{active_person.id}/auskunft.json")
check("Auskunfts-Export -> 200", resp.status_code == 200, f"got {resp.status_code}")
data = json.loads(resp.content)
check(
    "Auskunft enthält Stammdaten + Datenarten",
    data["stammdaten"]["nachname"] == "Bleibt"
    and "gremienmitgliedschaften" in data
    and "anwesenheiten" in data
    and "sitzungsgelder" in data,
)
check(
    "Bankdaten entschlüsselt (Admin hat manage_allowances)", data["bankdaten"].get("iban") == "DE02120300000000202052"
)
check(
    "Auskunfts-Export auditiert",
    SessionAuditLog.objects.filter(tenant=tenant, action="download", changes__has_key="dsgvo_auskunft").exists(),
)

resp = clerk.get(f"{base}/persons/{active_person.id}/auskunft.json")
check("Auskunft ohne manage_settings -> 403", resp.status_code == 403, f"got {resp.status_code}")

foreign_person = SessionPerson.objects.create(tenant=other_tenant, given_name="Frida", family_name="Fremd")
resp = admin.get(f"{base}/persons/{foreign_person.id}/auskunft.json")
check("Auskunft für fremde Person -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
# Phase D: Öffentliche Hinweisseite
# =============================================================================
print("=== Phase D: Hinweisseite ===")

resp = anonymous.get(f"{base}/datenschutz/")
check("Hinweisseite ohne Login -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Konfigurierter Text erscheint", b"Unsere Datenschutzhinweise." in resp.content)

resp = anonymous.get(f"/session/{other_tenant.slug}/datenschutz/")
check("Hinweisseite anderer Tenant mit Standardtext", resp.status_code == 200 and b"Auskunftsersuchen" in resp.content)

resp = anonymous.get("/session/gibtsnicht/datenschutz/")
check("Unbekannter Tenant -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
