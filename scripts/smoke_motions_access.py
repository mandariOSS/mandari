# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Zugriffsschutz Anträge (motions) - Freigabe-Leak + Yjs-Persistenz.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_motions_access.py

Beweist die in dieser Runde gefixten Bugs:
- MotionApprovalRequestView: ein motions.edit-Mitglied ohne Zugriff auf ein
  PRIVATES Dokument kann sich (oder anderen) darüber KEINEN Zugriff mehr
  verschaffen; die Sichtbarkeit bleibt privat.
- DocumentCollaborationConsumer._persist_yjs_state: ein Client mit
  view/comment-Level überschreibt den gemeinsamen Yjs-Zustand nicht mehr.
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_maccess_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_motions_access"]
django.setup()

from asgiref.sync import async_to_sync  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.motions.consumers import DocumentCollaborationConsumer  # noqa: E402
from apps.work.motions.models import Motion, MotionShare  # noqa: E402

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
org = Organization.objects.create(name="Fraktion MA", slug="fraktion-ma")
TenantEncryption(org).key


def perm(code, name, cat):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": name, "category": cat})
    return obj


edit_role = Role.objects.create(organization=org, name="Test-Antragsteller", is_admin=False)
edit_role.permissions.add(
    perm("motions.view", "Anträge sehen", "motions"),
    perm("motions.edit", "Anträge bearbeiten", "motions"),
    perm("motions.comment", "Anträge kommentieren", "motions"),
)

author_user = User.objects.create_user(email="author-ma@example.org", password="test1234!")
author_ms = Membership.objects.create(user=author_user, organization=org)
author_ms.roles.add(edit_role)

attacker_user = User.objects.create_user(email="attacker-ma@example.org", password="test1234!")
attacker_ms = Membership.objects.create(user=attacker_user, organization=org)
attacker_ms.roles.add(edit_role)

private_motion = Motion.objects.create(
    organization=org, author=author_ms, title="Geheimer Antrag", visibility="private"
)

BASE = f"/work/{org.slug}/documents"
attacker_client = Client()
attacker_client.force_login(attacker_user)

print("=== 1. Freigabe-Leak auf privates Dokument ===")
resp = attacker_client.post(
    f"{BASE}/{private_motion.id}/approvals/request/",
    {"approver": str(attacker_ms.id), "approval_type": "chair"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
private_motion.refresh_from_db()
check("Anfrage ohne Zugriff wird abgewiesen (403)", resp.status_code == 403, f"status={resp.status_code}")
check("Sichtbarkeit bleibt privat", private_motion.visibility == "private")
check(
    "Kein Self-Share angelegt",
    not MotionShare.objects.filter(motion=private_motion, user=attacker_user).exists(),
)
check("Angreifer hat weiterhin keinen Zugriff", not private_motion.can_access(attacker_ms))

print("=== 2. Positivkontrolle: Autor darf Freigabe anfragen ===")
author_client = Client()
author_client.force_login(author_user)
# Autor braucht eine zweite Person als Genehmiger
approver_user = User.objects.create_user(email="approver-ma@example.org", password="test1234!")
approver_ms = Membership.objects.create(user=approver_user, organization=org)
approver_ms.roles.add(edit_role)
resp = author_client.post(
    f"{BASE}/{private_motion.id}/approvals/request/",
    {"approver": str(approver_ms.id), "approval_type": "chair"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Autor darf Freigabe anfragen (200)", resp.status_code == 200, f"status={resp.status_code}")

print("=== 3. Yjs-Persistenz nur mit Schreibrecht ===")
sample = base64.b64encode(b"\x01\x02\x03yjs-state").decode()


def persist(access_level):
    consumer = DocumentCollaborationConsumer()
    consumer.document_id = str(private_motion.id)
    consumer.user_info = {"access_level": access_level}
    async_to_sync(consumer._persist_yjs_state)(sample, None)


persist("comment")
private_motion.refresh_from_db()
check("view/comment-Client schreibt yjs_document NICHT", not private_motion.yjs_document)

persist("edit")
private_motion.refresh_from_db()
check("edit-Client schreibt yjs_document", bool(private_motion.yjs_document))

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
