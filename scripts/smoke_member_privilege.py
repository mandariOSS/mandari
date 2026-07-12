# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Rechte-Eskalation im Mitglieder-Management (organization).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_member_privilege.py

Beweist die in dieser Runde gefixte Privilege-Escalation: Ein Mitglied mit
members.edit/members.manage_roles (aber NICHT is_admin) darf sich selbst oder
andere nicht zum Administrator machen — weder über das Mitglieder-Detail
(update_roles/update_permissions/update_sworn_in) noch über den Antragsweg
(approve_request). Normale (nicht-admin) Rollen bleiben delegierbar.
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_priv_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_member_privilege"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.organization.models import MemberChangeRequest  # noqa: E402

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


print("=== Setup ===")
org = Organization.objects.create(name="Fraktion Priv", slug="fraktion-priv")
TenantEncryption(org).key

admin_role = Role.objects.filter(organization=org, is_admin=True).first()
if admin_role is None:
    admin_role = Role.objects.create(organization=org, name="Administrator", is_admin=True)

# Nicht-Admin-Manager-Rolle (wie Fraktionsvorsitz): darf Mitglieder/Rollen verwalten
def perm(code, name, cat):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": name, "category": cat})
    return obj


# members.view/dashboard.view sind Voraussetzung, um die jeweiligen Views
# überhaupt zu erreichen (permission_required) — ein echter Manager hat sie.
manager_role = Role.objects.create(organization=org, name="Test-Manager", is_admin=False)
manager_role.permissions.add(
    perm("members.view", "Mitglieder sehen", "members"),
    perm("members.edit", "Mitglieder bearbeiten", "members"),
    perm("members.manage_roles", "Rollen zuweisen", "members"),
    perm("dashboard.view", "Dashboard", "dashboard"),
)

normal_role = Role.objects.create(organization=org, name="Test-Normal", is_admin=False)

# Angreifer: Nicht-Admin mit Manager-Rolle
attacker = User.objects.create_user(email="attacker@example.org", password="test1234!")
attacker_ms = Membership.objects.create(user=attacker, organization=org)
attacker_ms.roles.add(manager_role)

# Opfer/anderes Mitglied
victim = User.objects.create_user(email="victim@example.org", password="test1234!")
victim_ms = Membership.objects.create(user=victim, organization=org)

# Echter Admin
admin = User.objects.create_user(email="admin@example.org", password="test1234!")
admin_ms = Membership.objects.create(user=admin, organization=org)
admin_ms.roles.add(admin_role)

BASE = f"/work/{org.slug}/organization/members"
REQ_URL = f"/work/{org.slug}/profile/requests/"

attacker_client = Client()
attacker_client.force_login(attacker)
admin_client = Client()
admin_client.force_login(admin)


def has_admin(ms):
    ms.refresh_from_db()
    return ms.roles.filter(is_admin=True).exists()


print("=== 1. Selbst-Eskalation via update_roles ===")
attacker_client.post(
    f"{BASE}/{attacker_ms.id}/",
    {"action": "update_roles", "roles": [str(admin_role.id), str(manager_role.id)]},
)
check("Nicht-Admin macht sich NICHT selbst zum Admin", not has_admin(attacker_ms))

print("=== 2. Fremd-Eskalation via update_roles ===")
attacker_client.post(
    f"{BASE}/{victim_ms.id}/",
    {"action": "update_roles", "roles": [str(admin_role.id)]},
)
check("Nicht-Admin macht Opfer NICHT zum Admin", not has_admin(victim_ms))

print("=== 3. Normale Rolle bleibt delegierbar ===")
attacker_client.post(
    f"{BASE}/{victim_ms.id}/",
    {"action": "update_roles", "roles": [str(normal_role.id)]},
)
victim_ms.refresh_from_db()
check(
    "Nicht-Admin darf normale Rolle vergeben",
    victim_ms.roles.filter(id=normal_role.id).exists() and not has_admin(victim_ms),
)

print("=== 4. Selbst-Rechtevergabe via update_permissions ===")
perm_admin, _ = Permission.objects.get_or_create(
    codename="organization.admin", defaults={"name": "Voller Admin-Zugriff", "category": "organization"}
)
attacker_client.post(
    f"{BASE}/{attacker_ms.id}/",
    {"action": "update_permissions", "individual_permissions": ["organization.admin"], "denied_permissions": []},
)
attacker_ms.refresh_from_db()
check(
    "Nicht-Admin gewährt sich KEINE Einzelrechte",
    not attacker_ms.individual_permissions.filter(codename="organization.admin").exists(),
)

print("=== 5. Selbst-Vereidigung via update_sworn_in ===")
attacker_client.post(f"{BASE}/{attacker_ms.id}/", {"action": "update_sworn_in", "is_sworn_in": "1"})
attacker_ms.refresh_from_db()
check("Nicht-Admin vereidigt sich NICHT selbst", attacker_ms.is_sworn_in is False)

print("=== 6. Selbstgenehmigung Antrag (role_change -> Admin) ===")
cr = MemberChangeRequest.objects.create(
    organization=org,
    requester=attacker_ms,
    request_type="role_change",
    request_data={"requested_roles": [str(admin_role.id)]},
    status="pending",
)
attacker_client.post(REQ_URL, {"action": "approve_request", "request_id": str(cr.id)})
cr.refresh_from_db()
check("Eigener Admin-Antrag wird NICHT selbst genehmigt", cr.status == "pending")
check("Angreifer ist weiterhin kein Admin", not has_admin(attacker_ms))

print("=== 7. Positivkontrolle: Admin darf Admin-Rolle vergeben ===")
admin_client.post(
    f"{BASE}/{victim_ms.id}/",
    {"action": "update_roles", "roles": [str(admin_role.id), str(normal_role.id)]},
)
check("Admin macht Opfer zum Admin", has_admin(victim_ms))

print("=== 8. Positivkontrolle: Admin genehmigt Antrag ===")
cr2 = MemberChangeRequest.objects.create(
    organization=org,
    requester=attacker_ms,
    request_type="role_change",
    request_data={"requested_roles": [str(admin_role.id)]},
    status="pending",
)
admin_client.post(REQ_URL, {"action": "approve_request", "request_id": str(cr2.id)})
cr2.refresh_from_db()
check("Admin genehmigt Antrag", cr2.status == "approved")

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
