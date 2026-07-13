# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Zugriffsschutz Sitzungsvorbereitung (Consumer + Broadcast).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_prepare_ws_guard.py

Prüft die in dieser Runde gefixten Bugs:
- PreparationConsumer._check_access verlangt meetings.prepare UND schließt
  Gäste aus (Divergenz WS vs. HTTP behoben).
- PaperCommentAPIView.post broadcastet PRIVATE Kommentare NICHT (Leak behoben),
  organisationsweite Kommentare aber schon.
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_wsguard_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_prepare_ws_guard"]
django.setup()

from asgiref.sync import async_to_sync  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.meetings import consumers as meetings_consumers  # noqa: E402
from apps.work.meetings.consumers import PreparationConsumer  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
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


print("=== Setup ===")
source = OParlSource.objects.create(name="Q", url="https://oparl.example.org/system")
body = OParlBody.objects.create(source=source, external_id="https://oparl.example.org/body/1", name="Musterstadt")
org = Organization.objects.create(name="Fraktion WS", slug="fraktion-ws", body=body)
TenantEncryption(org).key

admin_role = Role.objects.filter(organization=org, is_admin=True).first()
if admin_role is None:
    admin_role = Role.objects.create(organization=org, name="Administrator", is_admin=True)

# Admin (hat meetings.prepare via is_admin)
admin_user = User.objects.create_user(email="admin-ws@example.org", password="test1234!")
admin_ms = Membership.objects.create(user=admin_user, organization=org)
admin_ms.roles.add(admin_role)

# Mitglied ohne Rollen -> keine meetings.prepare
plain_user = User.objects.create_user(email="plain-ws@example.org", password="test1234!")
plain_ms = Membership.objects.create(user=plain_user, organization=org)

# Gast (selbst mit Admin-Rolle muss der Zugriff verweigert werden)
guest_user = User.objects.create_user(email="guest-ws@example.org", password="test1234!")
guest_ms = Membership.objects.create(user=guest_user, organization=org, is_guest=True)

now = timezone.now()
committee = OParlOrganization.objects.create(
    external_id="https://oparl.example.org/organization/ws-1",
    body=body,
    name="Hauptausschuss",
    organization_type="committee",
)
meeting = OParlMeeting.objects.create(
    external_id="https://oparl.example.org/meeting/ws-1",
    body=body,
    name="Sitzung",
    start=now + timezone.timedelta(days=2),
)
meeting.organizations.add(committee)
paper = OParlPaper.objects.create(
    external_id="https://oparl.example.org/paper/ws-1",
    body=body,
    name="Vorlage",
    reference="V/1",
)
item = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/ws-1",
    meeting=meeting,
    number="1",
    name="Beratung",
)
OParlConsultation.objects.create(
    external_id="https://oparl.example.org/consultation/ws-1",
    body=body,
    paper=paper,
    agenda_item_external_id=item.external_id,
    meeting_external_id=meeting.external_id,
    role="Vorberatung",
)


def check_access(user):
    consumer = PreparationConsumer()
    consumer.org_slug = org.slug
    consumer.scope_type = "paper"
    consumer.object_id = str(paper.id)
    return async_to_sync(consumer._check_access)(user)


print("=== 1. Consumer-Zugriffsprüfung (Divergenz WS vs. HTTP) ===")
check("Admin mit meetings.prepare wird zugelassen", check_access(admin_user) == org.id)
check("Mitglied ohne meetings.prepare wird abgewiesen", check_access(plain_user) is None)
check("Gast wird abgewiesen (auch ohne fehlende Rolle)", check_access(guest_user) is None)

print("=== 2. Broadcast-Leak privater Kommentare ===")
calls = []
_orig = meetings_consumers.broadcast_preparation_event


def _spy(organization_id, payload, **kwargs):
    calls.append((organization_id, payload, kwargs))


meetings_consumers.broadcast_preparation_event = _spy
try:
    client = Client()
    client.force_login(admin_user)
    import json as _json

    url = f"/work/{org.slug}/paper/{paper.id}/comments/"

    calls.clear()
    r1 = client.post(url, _json.dumps({"content": "geheim", "visibility": "private"}), content_type="application/json")
    check("Privater Kommentar gespeichert (200)", r1.status_code == 200, f"status={r1.status_code}")
    check("Privater Kommentar wird NICHT broadcastet", len(calls) == 0, f"calls={len(calls)}")

    calls.clear()
    r2 = client.post(
        url, _json.dumps({"content": "sichtbar", "visibility": "organization"}), content_type="application/json"
    )
    check("Org-Kommentar gespeichert (200)", r2.status_code == 200, f"status={r2.status_code}")
    check("Org-Kommentar wird broadcastet", len(calls) == 1, f"calls={len(calls)}")
finally:
    meetings_consumers.broadcast_preparation_event = _orig

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
