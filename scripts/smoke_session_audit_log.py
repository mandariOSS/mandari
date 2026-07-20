# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Session-Audit-Log (Issue #23).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_audit_log.py

Prüft:
- Jede Mutation (create/update/delete) an den zentralen Session-Models
  erzeugt einen Audit-Eintrag mit Nutzer, Zeitpunkt und Aktion
- Spezial-Ereignisse: Freigabe (approve), Veröffentlichung (publish),
  Einladungsversand (invitation_sent) werden als eigene Aktionen erfasst
- Verschlüsselte Felder erscheinen nie im Klartext im Änderungsprotokoll
- Einträge sind unveränderbar (Save-/Delete-Guard)
- Log-Ansicht: nur mit can_view_audit_log erreichbar, zeigt nur Einträge
  des eigenen Tenants, Filter funktionieren
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import timedelta
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
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
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
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def entries(instance=None, action=None, tenant=None):
    qs = SessionAuditLog.objects.all()
    if instance is not None:
        qs = qs.filter(object_id=instance.pk)
    if action:
        qs = qs.filter(action=action)
    if tenant is not None:
        qs = qs.filter(tenant=tenant)
    return qs


# =============================================================================
# Setup: Tenant, Rollen, Nutzer
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant2 = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
viewer_role = SessionRole.objects.create(tenant=tenant, name="Nur-Lesen")

su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)

org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")

client = Client()
client.force_login(admin_user)

# =============================================================================
# Phase A: create/update/delete über Views erzeugen Einträge mit Nutzer
# =============================================================================
print("=== Phase A: Mutationen erzeugen Audit-Einträge ===")

start = (timezone.now() + timedelta(days=7)).strftime("%Y-%m-%dT10:00")
resp = client.post(
    f"/session/{tenant.slug}/meetings/create/",
    {
        "name": "Sitzung des Hauptausschusses",
        "organization": str(org.id),
        "start": start,
        "is_public": "on",
    },
)
check("Meeting-Create-View -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
meeting = SessionMeeting.objects.filter(tenant=tenant).first()
check("Audit: create-Eintrag für Sitzung", entries(meeting, "create").exists())
entry = entries(meeting, "create").first()
check("Audit: Nutzer erfasst", entry is not None and entry.user_id == su_admin.id)
check("Audit: Zeitpunkt erfasst", entry is not None and entry.created_at is not None)
check("Audit: Tenant korrekt", entry is not None and entry.tenant_id == tenant.id)

resp = client.post(
    f"/session/{tenant.slug}/meetings/{meeting.id}/edit/",
    {
        "name": "Umbenannte Sitzung",
        "organization": str(org.id),
        "start": start,
        "meeting_state": "scheduled",
        "is_public": "on",
    },
)
check("Meeting-Edit-View -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
update_entry = entries(meeting, "update").order_by("-created_at").first()
check("Audit: update-Eintrag für Sitzung", update_entry is not None)
check(
    "Audit: Namensänderung im Diff",
    update_entry is not None and update_entry.changes.get("name", {}).get("neu") == "Umbenannte Sitzung",
    f"changes={update_entry.changes if update_entry else None}",
)

# Paper anlegen + Freigabe-Statuswechsel
paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0001", name="Testvorlage", created_by=su_admin)
check("Audit: create-Eintrag für Vorlage (ORM)", entries(paper, "create").exists())

paper.resolution_text = "Der Rat möge beschließen."
paper.save()
check(
    "Audit: Beschlusstext-Änderung erzeugt Eintrag",
    entries(paper, "update").filter(changes__has_key="resolution_text").exists()
    or any("resolution_text" in e.changes for e in entries(paper, "update")),
)

paper.status = "approved"
paper.save()
check("Audit: Freigabe als approve-Aktion", entries(paper, "approve").exists())

# Protokoll: Veröffentlichung
protocol = SessionProtocol.objects.create(meeting=meeting, content="Niederschrift")
protocol.status = "published"
protocol.save()
check("Audit: Veröffentlichung als publish-Aktion", entries(protocol, "publish").exists())

# Einladungsversand
meeting.meeting_state = "invitation_sent"
meeting.invitation_sent_at = timezone.now()
meeting.save()
check("Audit: Einladungsversand als invitation_sent-Aktion", entries(meeting, "invitation_sent").exists())

# Delete
paper_id = paper.id
paper.delete()
check("Audit: delete-Eintrag für Vorlage", SessionAuditLog.objects.filter(object_id=paper_id, action="delete").exists())

# =============================================================================
# Phase B: Verschlüsselte Felder werden maskiert
# =============================================================================
print()
print("=== Phase B: Keine Klartext-Sensibeldaten im Log ===")

person = SessionPerson.objects.create(tenant=tenant, given_name="Erika", family_name="Mustermann")
person.set_bank_iban_encrypted("DE89370400440532013000")
person.save()

iban_entries = [e for e in entries(person) if "bank_iban_encrypted" in e.changes]
check("Audit: IBAN-Änderung erzeugt Eintrag", bool(iban_entries))
all_changes_text = " ".join(str(e.changes) for e in entries(person))
check("Audit: IBAN erscheint nirgends im Klartext", "DE89370400440532013000" not in all_changes_text)
check(
    "Audit: verschlüsselte Felder als maskiert markiert",
    bool(iban_entries) and iban_entries[0].changes["bank_iban_encrypted"]["neu"] == "[verschlüsselt geändert]",
)

# =============================================================================
# Phase C: Unveränderbarkeit
# =============================================================================
print()
print("=== Phase C: Einträge sind unveränderbar ===")

entry = SessionAuditLog.objects.first()
try:
    entry.action = "delete"
    entry.save()
    immutable_save = False
except ValueError:
    immutable_save = True
check("Audit: Update wird verweigert", immutable_save)

try:
    entry.delete()
    immutable_delete = False
except ValueError:
    immutable_delete = True
check("Audit: Delete wird verweigert", immutable_delete)

# =============================================================================
# Phase D: Log-Ansicht — Zugriffsschutz, Tenant-Isolation, Filter
# =============================================================================
print()
print("=== Phase D: Log-Ansicht ===")

# Fremd-Tenant-Eintrag anlegen
foreign_org = SessionOrganization.objects.create(tenant=tenant2, name="Geheimer Fremdausschuss XYZ")
check("Audit: Fremd-Tenant-Eintrag existiert", entries(tenant=tenant2).exists())

resp = client.get(f"/session/{tenant.slug}/audit/")
check("Audit-Ansicht für Admin -> 200", resp.status_code == 200, f"got {resp.status_code}")
html = resp.content.decode("utf-8")
check("Audit-Ansicht zeigt eigene Einträge", "Umbenannte Sitzung" in html)
check("Audit-Ansicht zeigt KEINE fremden Einträge", "Geheimer Fremdausschuss XYZ" not in html)

# Filter: Aktion
resp = client.get(f"/session/{tenant.slug}/audit/", {"action": "approve"})
check("Filter action=approve -> 200", resp.status_code == 200)
check(
    "Filter liefert nur approve-Einträge",
    b"Testvorlage" in resp.content and b"Umbenannte Sitzung" not in resp.content,
)

# Filter: Objekt-Typ
resp = client.get(f"/session/{tenant.slug}/audit/", {"model": "SessionPerson"})
check("Filter model=SessionPerson -> 200", resp.status_code == 200)
check("Filter model liefert Personen-Eintrag", b"Mustermann" in resp.content)

# Ohne Berechtigung: 403
viewer_client = Client()
viewer_client.force_login(viewer_user)
resp = viewer_client.get(f"/session/{tenant.slug}/audit/")
check("Audit-Ansicht ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Nicht-Mitglied des Tenants: kein Zugriff
foreign_user = User.objects.create_user(email="fremd@example.org", password="pw-Smoke-Test-1!")
foreign_client = Client()
foreign_client.force_login(foreign_user)
resp = foreign_client.get(f"/session/{tenant.slug}/audit/")
check("Audit-Ansicht als Nicht-Mitglied -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
