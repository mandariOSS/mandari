# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Endgeräte für die digitale Ratsarbeit.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_devices.py

Prüft:
- Gerätebestand: aufnehmen, ausgeben, zurücknehmen, Defekt, Ausmusterung
  (inkl. Statuswechsel-Guards und Historie)
- Übergabeprotokoll-PDF
- Endgeräte-Zuschüsse: erfassen, Doppel-Warnung, genehmigen, auszahlen,
  Storno-Guards, CSV-Export mit Bankdaten
- Neue Berechtigung manage_devices (403 ohne, Admin implizit)
- Tenant-Isolation
"""

import base64
import io
import os
import secrets
import sys
import tempfile
from decimal import Decimal
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

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionDevice,
    SessionDeviceGrant,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def pdf_text(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Gerätestadt", slug="geraetestadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-dev")

person = SessionPerson.objects.create(tenant=tenant, given_name="Rita", family_name="RATSMITGLIED")
person.set_bank_iban_encrypted("DE02120300000000202051")
person.save()

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-dev@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

device_role = SessionRole.objects.create(tenant=tenant, name="Geräteverwaltung", can_manage_devices=True)
device_user = User.objects.create_user(email="geraete-dev@example.org", password="pw-Smoke-1!")
su_device = SessionUser.objects.create(user=device_user, tenant=tenant)
su_device.roles.add(device_role)
device_client = Client()
device_client.force_login(device_user)

viewer_role = SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True)
viewer_user = User.objects.create_user(email="leser-dev@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Berechtigung ===")
check("Admin erreicht Seite", admin.get(f"{base}/devices/").status_code == 200)
check("Rolle mit manage_devices erreicht Seite", device_client.get(f"{base}/devices/").status_code == 200)
resp = viewer.get(f"{base}/devices/")
check("Ohne manage_devices -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Nav zeigt Endgeräte nur mit Recht", "Endgeräte" not in viewer.get(f"{base}/").content.decode("utf-8"))

# =============================================================================
print()
print("=== Phase B: Gerätebestand ===")
resp = device_client.post(
    f"{base}/devices/add/",
    {"label": "iPad 10. Gen, 64 GB", "serial_number": "SN-TEST-123", "inventory_number": "INV-001", "accessories": "Hülle, Netzteil"},
)
device = SessionDevice.objects.filter(tenant=tenant).first()
check("Gerät aufgenommen", device is not None and device.status == "in_stock")
check("Anlage-Log", device.logs.filter(action="created").exists())

resp = device_client.post(f"{base}/devices/add/", {"label": ""})
check("Ohne Bezeichnung abgelehnt", SessionDevice.objects.filter(tenant=tenant).count() == 1)

# Ausgabe
resp = device_client.post(f"{base}/devices/{device.id}/issue/", {"person": str(person.id), "note": "Übergabe im Rathaus"})
device.refresh_from_db()
check("Ausgegeben an Person", device.status == "issued" and device.issued_to_id == person.id and device.issued_at is not None)
check("Ausgabe-Log mit Person", device.logs.filter(action="issued", person=person).exists())

# Doppelte Ausgabe blockiert
resp = device_client.post(f"{base}/devices/{device.id}/issue/", {"person": str(person.id)})
check("Erneute Ausgabe blockiert", device.logs.filter(action="issued").count() == 1)

# Übergabeprotokoll-PDF
resp = device_client.get(f"{base}/devices/{device.id}/protokoll.pdf")
check("Protokoll-PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text = pdf_text(resp.content)
check("PDF: Gerät + Person", "iPad" in text and "RATSMITGLIED" in text)
check("PDF: Unterschriftenzeile", "Für die Verwaltung" in text)
check("PDF: Nutzungsbedingungen", "Eigentum der" in text)

# Rückgabe
resp = device_client.post(f"{base}/devices/{device.id}/return/", {"note": "Mandatsende"})
device.refresh_from_db()
check("Zurückgenommen", device.status == "in_stock" and device.issued_to is None)
check("Rückgabe-Log", device.logs.filter(action="returned").exists())

# Defekt + Ausmusterung
resp = device_client.post(f"{base}/devices/{device.id}/defect/", {"note": "Display gebrochen"})
device.refresh_from_db()
check("Defekt erfasst", device.status == "defect")
resp = device_client.post(f"{base}/devices/{device.id}/retire/")
device.refresh_from_db()
check("Ausgemustert", device.status == "retired")

# =============================================================================
print()
print("=== Phase C: Zuschüsse ===")
resp = device_client.post(
    f"{base}/device-grants/add/",
    {"person": str(person.id), "amount": "300,00", "note": "Ratsbeschluss digitale Gremienarbeit"},
)
grant = SessionDeviceGrant.objects.filter(tenant=tenant).first()
check("Zuschuss erfasst", grant is not None and grant.amount == Decimal("300.00") and grant.status == "pending")

resp = device_client.post(f"{base}/device-grants/add/", {"person": str(person.id), "amount": "abc"})
check("Ungültiger Betrag abgelehnt", SessionDeviceGrant.objects.filter(tenant=tenant).count() == 1)

# Auszahlung erst nach Genehmigung
resp = device_client.post(f"{base}/device-grants/{grant.id}/pay/")
grant.refresh_from_db()
check("Auszahlung vor Genehmigung blockiert", grant.status == "pending")
resp = device_client.post(f"{base}/device-grants/{grant.id}/approve/")
grant.refresh_from_db()
check("Genehmigt", grant.status == "approved" and grant.approved_by_id == su_device.id)
resp = device_client.post(f"{base}/device-grants/{grant.id}/pay/")
grant.refresh_from_db()
check("Ausgezahlt", grant.status == "paid" and grant.paid_at is not None)
resp = device_client.post(f"{base}/device-grants/{grant.id}/cancel/")
grant.refresh_from_db()
check("Storno nach Auszahlung blockiert", grant.status == "paid")

# Doppel-Warnung (zweiter Zuschuss wird angelegt, aber gewarnt)
resp = device_client.post(
    f"{base}/device-grants/add/", {"person": str(person.id), "amount": "200"}, follow=True
)
check("Doppel-Zuschuss mit Warnhinweis", "bereits ein Zuschuss" in resp.content.decode("utf-8"))

# CSV
resp = device_client.post(f"{base}/device-grants/export/csv/")
csv_text = resp.content.decode("utf-8")
check("CSV -> 200 mit IBAN", resp.status_code == 200 and "DE02120300000000202051" in csv_text)
check("CSV: Beträge", "300,00" in csv_text)

# =============================================================================
print()
print("=== Phase D: Tenant-Isolation ===")
person_b = SessionPerson.objects.create(tenant=tenant_b, given_name="Fritz", family_name="FREMD")
device_b = SessionDevice.objects.create(tenant=tenant_b, label="Fremd-iPad")
resp = admin.post(f"{base}/devices/{device_b.id}/issue/", {"person": str(person.id)})
device_b.refresh_from_db()
check("Fremdes Gerät -> 404", resp.status_code == 404 and device_b.status == "in_stock")
resp = admin.post(f"{base}/device-grants/add/", {"person": str(person_b.id), "amount": "100"})
check("Fremde Person kein Zuschuss", not SessionDeviceGrant.objects.filter(person=person_b).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
