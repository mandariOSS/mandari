# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Vorlagen-Freigabe- und Mitzeichnungslauf (Issue #33).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_paper_workflow.py

Prüft:
- Aktionen „Zur Freigabe vorlegen" (Entwurf -> Prüfung), „Freigeben"
  (Prüfung -> Freigegeben) und „Zurückweisen mit Kommentar" (-> Entwurf)
- Arbeitsvorrat „Meine zu prüfenden Vorlagen" + Badge im Dashboard
- E-Mail-Benachrichtigung an Freigebende bei Vorlage zur Prüfung und an
  die/den Erstellenden bei Zurückweisung
- Audit-Einträge für jede Freigabe/Zurückweisung
- Ungültige Statusübergänge, Berechtigungen, Ö/NÖ und Tenant-Isolation
- Kompletter Zyklus Entwurf -> Prüfung -> Freigabe -> Tagesordnung ohne
  Django-Admin (Akzeptanzkriterium)
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
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
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


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")


def make_client(name, **flags):
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle_{name}", **flags)
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-Test-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    c = Client()
    c.force_login(user)
    return c, su


clerk, su_clerk = make_client(
    "sachbearbeitung",
    can_view_papers=True,
    can_create_papers=True,
    can_edit_papers=True,
    can_view_meetings=True,
    can_edit_meetings=True,
    can_create_meetings=True,
)
approver, su_approver = make_client(
    "amtsleitung",
    can_view_papers=True,
    can_approve_papers=True,
    can_view_meetings=True,
)
approver2, su_approver2 = make_client(
    "vertretung",
    can_view_papers=True,
    can_approve_papers=True,
)
viewer, _ = make_client("leser", can_view_papers=True)

paper = SessionPaper.objects.create(
    has_financial_impact=False,
    tenant=tenant,
    reference="V/2026/0100",
    name="Sanierung des Spielplatzes",
    status="draft",
    is_public=True,
    created_by=su_clerk,
)
paper_np = SessionPaper.objects.create(
    has_financial_impact=False,
    tenant=tenant,
    reference="V/2026/0101",
    name="GEHEIME-VORLAGE-XYZ",
    status="review",
    is_public=False,
)
paper_b = SessionPaper.objects.create(tenant=tenant_b, reference="V/2026/0900", name="Fremdvorlage", status="draft")

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Zur Freigabe vorlegen (mit Benachrichtigung)
# =============================================================================
print("=== Phase A: Zur Freigabe vorlegen ===")

resp = clerk.get(f"{base}/papers/{paper.id}/")
check("Entwurf zeigt 'Zur Freigabe vorlegen'", "Zur Freigabe vorlegen" in resp.content.decode("utf-8"))

mail.outbox = []
resp = clerk.post(f"{base}/papers/{paper.id}/workflow/submit/")
paper.refresh_from_db()
check("Submit -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Status: In Prüfung", paper.status == "review")
check("Benachrichtigung an Freigebende versandt", len(mail.outbox) == 1, f"outbox={len(mail.outbox)}")
if mail.outbox:
    recipients = sorted(mail.outbox[0].to)
    check(
        "Beide Freigabeberechtigten benachrichtigt (Vertreterregelung)",
        recipients == ["amtsleitung@example.org", "vertretung@example.org"],
        str(recipients),
    )
    check("Betreff nennt Aktenzeichen", "V/2026/0100" in mail.outbox[0].subject)

check(
    "Audit: Statuswechsel protokolliert",
    SessionAuditLog.objects.filter(tenant=tenant, object_id=paper.id, action="update").exists(),
)

# Doppelter Submit unzulässig
resp = clerk.post(f"{base}/papers/{paper.id}/workflow/submit/")
paper.refresh_from_db()
check("Erneuter Submit abgelehnt", paper.status == "review")

# =============================================================================
# Phase B: Arbeitsvorrat + Badge
# =============================================================================
print()
print("=== Phase B: Arbeitsvorrat ===")

resp = approver.get(f"{base}/papers/review/")
html = resp.content.decode("utf-8")
check("Arbeitsvorrat -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Vorlage im Arbeitsvorrat", "V/2026/0100" in html)
check("NÖ-Vorlage ohne NÖ-Recht unsichtbar", "GEHEIME-VORLAGE-XYZ" not in html)
import re  # noqa: E402

check(
    "Badge mit Anzahl in der Navigation",
    "Zu prüfen" in html and re.search(r"rounded-full[^>]*\">\s*1\s*</span>", html) is not None,
)

resp = approver.get(f"{base}/dashboard/")
html = resp.content.decode("utf-8")
check("Dashboard: Arbeitsvorrat-Karte", "Meine zu prüfenden Vorlagen" in html and "V/2026/0100" in html)

resp = viewer.get(f"{base}/papers/review/")
check("Arbeitsvorrat ohne approve_papers -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase C: Zurückweisen mit Kommentar
# =============================================================================
print()
print("=== Phase C: Zurückweisen ===")

mail.outbox = []
resp = approver.post(f"{base}/papers/{paper.id}/workflow/reject/", {"comment": "Kostenschätzung fehlt."})
paper.refresh_from_db()
check("Zurückgewiesen -> Entwurf", paper.status == "draft")
check("Keine Freigabe-Metadaten", paper.approved_by_id is None and paper.approved_at is None)
check("Erstellende/r benachrichtigt", len(mail.outbox) == 1 and mail.outbox[0].to == ["sachbearbeitung@example.org"])
check("Kommentar in der E-Mail", mail.outbox and "Kostenschätzung fehlt." in mail.outbox[0].body)
reject_entry = SessionAuditLog.objects.filter(
    tenant=tenant, object_id=paper.id, changes__has_key="zurueckweisungs_kommentar"
).first()
check("Audit: Zurückweisung mit Kommentar", reject_entry is not None and "Kostenschätzung" in str(reject_entry.changes))

# =============================================================================
# Phase D: Freigeben + kompletter Zyklus bis Tagesordnung
# =============================================================================
print()
print("=== Phase D: Freigabe und Tagesordnung ===")

clerk.post(f"{base}/papers/{paper.id}/workflow/submit/")
resp = approver2.post(f"{base}/papers/{paper.id}/workflow/approve/")
paper.refresh_from_db()
check("Freigegeben", paper.status == "approved")
check("Freigegeben von/am gesetzt", paper.approved_by_id == su_approver2.id and paper.approved_at is not None)
check(
    "Audit: approve-Aktion",
    SessionAuditLog.objects.filter(tenant=tenant, object_id=paper.id, action="approve").exists(),
)

# Freigegebene Vorlage auf die Tagesordnung setzen (ohne Django-Admin)
start = (timezone.now() + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
resp = clerk.post(
    f"{base}/meetings/create/",
    {"name": "Sitzung des Hauptausschusses", "organization": str(org.id), "start": start.strftime("%Y-%m-%dT%H:%M")},
)
meeting = SessionMeeting.objects.filter(tenant=tenant).first()
check("Sitzung angelegt", meeting is not None)
resp = clerk.post(
    f"{base}/meetings/{meeting.id}/agenda/add/",
    {"name": "Sanierung des Spielplatzes", "is_public": "on", "paper": str(paper.id)},
)
top = SessionAgendaItem.objects.filter(meeting=meeting, paper=paper).first()
check("Freigegebene Vorlage auf der Tagesordnung", top is not None)

# Ungültig: approve aus approved
resp = approver.post(f"{base}/papers/{paper.id}/workflow/approve/")
paper.refresh_from_db()
check("Erneutes Approve abgelehnt", paper.status == "approved" and paper.approved_by_id == su_approver2.id)

# =============================================================================
# Phase E: Berechtigungen, Ö/NÖ, Tenant-Isolation
# =============================================================================
print()
print("=== Phase E: Berechtigungen und Isolation ===")

paper2 = SessionPaper.objects.create(
    has_financial_impact=False,
    tenant=tenant, reference="V/2026/0102", name="Zweite Vorlage", status="draft", created_by=su_clerk
)
resp = viewer.post(f"{base}/papers/{paper2.id}/workflow/submit/")
paper2.refresh_from_db()
check("Submit ohne edit_papers -> 403", resp.status_code == 403 and paper2.status == "draft")

clerk.post(f"{base}/papers/{paper2.id}/workflow/submit/")
resp = clerk.post(f"{base}/papers/{paper2.id}/workflow/approve/")
paper2.refresh_from_db()
check("Approve ohne approve_papers -> 403", resp.status_code == 403 and paper2.status == "review")

# NÖ-Vorlage: approver2 hat kein view_non_public_papers -> 404
resp = approver2.post(f"{base}/papers/{paper_np.id}/workflow/approve/")
paper_np.refresh_from_db()
check("NÖ-Vorlage ohne NÖ-Recht nicht freigebbar (404)", resp.status_code == 404 and paper_np.status == "review")

# Unbekannte Aktion
resp = approver.post(f"{base}/papers/{paper2.id}/workflow/hackit/")
check("Unbekannte Aktion -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Fremder Tenant
resp = clerk.post(f"{base}/papers/{paper_b.id}/workflow/submit/")
paper_b.refresh_from_db()
check("Fremde Vorlage -> 404, unverändert", resp.status_code == 404 and paper_b.status == "draft")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
