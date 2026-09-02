# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Rollen- und Rechteverwaltung.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_roles.py

Prüft:
- Rollenübersicht mit Rechte-Matrix (alle can_*-Felder abgedeckt)
- Rolle anlegen mit frei kombinierten Rechten; Wirkung sofort (403 -> 200)
- Rolle bearbeiten (Rechte entziehen wirkt), Namens-Duplikat abgelehnt
- Schutz: letzte Admin-Rolle nicht entmachtbar/löschbar;
  zugewiesene Rollen nicht löschbar
- Berechtigung manage_users erforderlich; Tenant-Isolation
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

from apps.accounts.models import User  # noqa: E402
from apps.session.models import SessionRole, SessionTenant, SessionUser  # noqa: E402
from apps.session.views.roles import permission_fields  # noqa: E402

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


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Rollenstadt", slug="rollenstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-rl")

admin_role = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
admin_user = User.objects.create_user(email="admin-rl@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

target_user = User.objects.create_user(email="ziel-rl@example.org", password="pw-Smoke-1!")
su_target = SessionUser.objects.create(user=target_user, tenant=tenant)
target = Client()
target.force_login(target_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Matrix-Vollständigkeit ===")
all_can_fields = {f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}
matrix_fields = {n for _g, entries in permission_fields() for n, _l in entries}
check("Alle Rechte in der Matrix", all_can_fields == matrix_fields,
      f"fehlend: {all_can_fields - matrix_fields}")
check("Neues Endgeräte-Recht enthalten", "can_manage_devices" in matrix_fields)

resp = admin.get(f"{base}/settings/roles/")
html = resp.content.decode("utf-8")
check("Rollenseite -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Standardrolle gelistet", "Administrator" in html)
check("Gruppen sichtbar", "Finanzen &amp; Endgeräte" in html or "Finanzen & Endgeräte" in html)

# =============================================================================
print()
print("=== Phase B: Rolle anlegen — Wirkung sofort ===")
# Zielnutzer hat ohne Rolle keinen Zugriff auf Sitzungsgelder
resp = target.get(f"{base}/allowances/")
check("Vorher: kein Zugriff auf Sitzungsgelder", resp.status_code == 403, f"got {resp.status_code}")

resp = admin.post(
    f"{base}/settings/roles/save/",
    {
        "name": "Kämmerei",
        "description": "Nur Entschädigungen",
        "can_view_dashboard": "1",
        "can_manage_allowances": "1",
    },
)
role = SessionRole.objects.filter(tenant=tenant, name="Kämmerei").first()
check("Rolle angelegt", role is not None and role.can_manage_allowances and not role.is_admin)
check("Nicht angehakte Rechte aus", role.can_view_meetings is False and role.can_manage_devices is False)

su_target.roles.add(role)
resp = target.get(f"{base}/allowances/")
check("Nachher: Zugriff auf Sitzungsgelder", resp.status_code == 200, f"got {resp.status_code}")
resp = target.get(f"{base}/devices/")
check("Weiterhin kein Endgeräte-Zugriff", resp.status_code == 403, f"got {resp.status_code}")

# Duplikat
resp = admin.post(f"{base}/settings/roles/save/", {"name": "Kämmerei"})
check("Namens-Duplikat abgelehnt", SessionRole.objects.filter(tenant=tenant, name="Kämmerei").count() == 1)

# =============================================================================
print()
print("=== Phase C: Rolle bearbeiten ===")
resp = admin.get(f"{base}/settings/roles/?edit={role.id}")
html = resp.content.decode("utf-8")
check("Bearbeiten-Formular vorbefüllt", "Kämmerei" in html and "can_manage_allowances" in html)

resp = admin.post(
    f"{base}/settings/roles/save/",
    {"role_id": str(role.id), "name": "Kämmerei", "can_view_dashboard": "1"},
)
role.refresh_from_db()
check("Recht entzogen", role.can_manage_allowances is False)
resp = target.get(f"{base}/allowances/")
check("Entzug wirkt sofort", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase D: Schutzmechanismen ===")
resp = admin.post(
    f"{base}/settings/roles/save/",
    {"role_id": str(admin_role.id), "name": "Administrator"},
)
admin_role.refresh_from_db()
check("Letzte Admin-Rolle nicht entmachtbar", admin_role.is_admin is True)

resp = admin.post(f"{base}/settings/roles/delete/", {"role_id": str(admin_role.id)})
check("Letzte Admin-Rolle nicht löschbar", SessionRole.objects.filter(pk=admin_role.id).exists())

resp = admin.post(f"{base}/settings/roles/delete/", {"role_id": str(role.id)})
check("Zugewiesene Rolle nicht löschbar", SessionRole.objects.filter(pk=role.id).exists())

su_target.roles.remove(role)
resp = admin.post(f"{base}/settings/roles/delete/", {"role_id": str(role.id)})
check("Nicht zugewiesene Rolle löschbar", not SessionRole.objects.filter(pk=role.id).exists())

# Zweite Admin-Rolle -> erste wird entmachtbar
role2 = SessionRole.objects.create(tenant=tenant, name="Zweitadmin", is_admin=True)
su_target.roles.add(role2)
resp = admin.post(
    f"{base}/settings/roles/save/",
    {"role_id": str(admin_role.id), "name": "Administrator", "can_view_dashboard": "1"},
)
admin_role.refresh_from_db()
check("Mit zweitem Admin entmachtbar", admin_role.is_admin is False)
# Zurücksetzen für Isolation
admin_role.is_admin = True
admin_role.save()

# =============================================================================
print()
print("=== Phase E: Rechte + Isolation ===")
su_target.roles.remove(role2)  # Zweitadmin-Rolle wieder entziehen
resp = target.get(f"{base}/settings/roles/")
check("Ohne manage_users -> 403", resp.status_code == 403, f"got {resp.status_code}")

role_b = SessionRole.objects.create(tenant=tenant_b, name="Fremdrolle")
resp = admin.post(f"{base}/settings/roles/save/", {"role_id": str(role_b.id), "name": "Gekapert", "is_admin": "1"})
role_b.refresh_from_db()
check("Fremde Rolle nicht bearbeitbar", role_b.name == "Fremdrolle" and role_b.is_admin is False)
resp = admin.post(f"{base}/settings/roles/delete/", {"role_id": str(role_b.id)})
check("Fremde Rolle nicht löschbar", SessionRole.objects.filter(pk=role_b.id).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
