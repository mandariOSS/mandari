# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Zugriffs-/IDOR-Schutz quer über work-Endpunkte.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_work_idor.py

Beweist die in dieser Runde gefixten Zugriffsfehler:
- Fraktionssitzung: Protokollant in Org A kann NICHT die Abstimmung eines
  TOP einer fremden (Org-B-)Sitzung überschreiben.
- Support: Mitglied ohne support.manage kann ein fremdes Ticket weder
  beantworten noch schließen.
- Aufgaben: Mitglied ohne Zugriff kann eine PRIVATE Aufgabe weder öffnen
  noch kommentieren.
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_idor_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_work_idor"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.faction.models import FactionAgendaItem, FactionMeeting  # noqa: E402
from apps.work.support.models import SupportTicket  # noqa: E402
from apps.work.tasks.models import Task, TaskComment  # noqa: E402

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


def perm(code, name, cat):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": name, "category": cat})
    return obj


def make_member(org, email, perms):
    user = User.objects.create_user(email=email, password="test1234!")
    ms = Membership.objects.create(user=user, organization=org)
    if perms:
        role = Role.objects.create(organization=org, name=f"R-{email}", is_admin=False)
        role.permissions.add(*perms)
        ms.roles.add(role)
    c = Client()
    c.force_login(user)
    return user, ms, c


print("=== Setup ===")
org_a = Organization.objects.create(name="Org A", slug="org-a")
org_b = Organization.objects.create(name="Org B", slug="org-b")
TenantEncryption(org_a).key
TenantEncryption(org_b).key

now = timezone.now()

# --- Fraktion: Angreifer ist Protokollant in Org A, Opfer-TOP liegt in Org B ---
attacker_user, attacker_ms, attacker_c = make_member(
    org_a,
    "fac-attacker@example.org",
    [perm("faction.view_public", "Sitzungen sehen", "faction"), perm("protocols.edit", "Protokoll", "protocols")],
)
mtg_a = FactionMeeting.objects.create(
    organization=org_a, title="Sitzung A", start=now, created_by=attacker_ms
)
victim_owner, victim_ms, _ = make_member(org_b, "fac-victim@example.org", [])
mtg_b = FactionMeeting.objects.create(
    organization=org_b, title="Sitzung B", start=now, created_by=victim_ms
)
foreign_item = FactionAgendaItem.objects.create(
    meeting=mtg_b, title="Fremder TOP", votes_for=1, votes_against=2, votes_abstain=0
)

print("=== 1. Fraktion: Cross-Org-Abstimmung überschreiben ===")
attacker_c.post(
    f"/work/{org_a.slug}/faction/{mtg_a.id}/action/",
    {
        "action": "add_entry",
        "entry_type": "decision",
        "content": "Beschluss",
        "agenda_item_id": str(foreign_item.id),
        "votes_yes": "99",
        "votes_no": "0",
        "votes_abstain": "0",
    },
)
foreign_item.refresh_from_db()
check(
    "Fremder TOP behält seine Abstimmung",
    foreign_item.votes_for == 1 and foreign_item.votes_against == 2 and foreign_item.has_decision is False,
    f"for={foreign_item.votes_for}",
)

# --- Support: Ticket von A-Ersteller, Angreifer ohne support.manage ---
print("=== 2. Support: fremdes Ticket manipulieren ===")
owner_user, owner_ms, _ = make_member(org_a, "sup-owner@example.org", [perm("support.view", "Support", "support")])
sup_attacker_user, sup_attacker_ms, sup_attacker_c = make_member(
    org_a, "sup-attacker@example.org", [perm("support.view", "Support", "support")]
)
ticket = SupportTicket.objects.create(organization=org_a, subject="Mein Ticket", created_by=owner_ms, status="open")
resp = sup_attacker_c.post(
    f"/work/{org_a.slug}/support/{ticket.id}/",
    {"action": "close"},
)
ticket.refresh_from_db()
check("Fremdes Ticket wird NICHT geschlossen", ticket.status == "open", f"status={ticket.status}")
resp = sup_attacker_c.post(
    f"/work/{org_a.slug}/support/{ticket.id}/",
    {"action": "reply", "content": "Fremde Antwort"},
)
check("Keine Nachricht im fremden Ticket", ticket.messages.count() == 0)

# --- Tasks: private Aufgabe eines Kollegen ---
print("=== 3. Aufgaben: private Aufgabe öffnen/kommentieren ===")
task_owner_user, task_owner_ms, _ = make_member(
    org_a, "task-owner@example.org", [perm("tasks.view", "Aufgaben", "tasks")]
)
task_attacker_user, task_attacker_ms, task_attacker_c = make_member(
    org_a, "task-attacker@example.org", [perm("tasks.view", "Aufgaben", "tasks")]
)
private_task = Task.objects.create(
    organization=org_a, created_by=task_owner_ms, title="Geheime Aufgabe", visibility="private"
)
resp = task_attacker_c.get(f"/work/{org_a.slug}/tasks/{private_task.id}/panel/")
check("Private Aufgabe: Panel verweigert (403)", resp.status_code == 403, f"status={resp.status_code}")
resp = task_attacker_c.post(
    f"/work/{org_a.slug}/tasks/{private_task.id}/panel/action/",
    {"action": "add_comment", "content": "Fremder Kommentar"},
)
check("Private Aufgabe: Kommentar verweigert (403)", resp.status_code == 403, f"status={resp.status_code}")
check("Kein Kommentar an fremder Aufgabe", TaskComment.objects.filter(task=private_task).count() == 0)

print("=== 4. Positivkontrolle: eigene Aufgabe bleibt zugänglich ===")
resp = task_owner_ms and Client()
oc = Client()
oc.force_login(task_owner_user)
resp = oc.get(f"/work/{org_a.slug}/tasks/{private_task.id}/panel/")
check("Eigene Aufgabe: Panel lädt (200)", resp.status_code == 200, f"status={resp.status_code}")

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
