# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Beschlussregister und Beschlussauszüge (Issue #32).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_resolutions.py

Prüft:
- Beschlussregister: gefasste Beschlüsse mit Nummer, Sitzung, TOP,
  Ergebnis, Abstimmungszahlen; Filter nach Gremium/Jahr/Ergebnis
- Sammel-Ausfertigung: Beschlussnummern-Vergabe je Sitzung (B/<Jahr>/<lfd>,
  idempotent, fortlaufend je Mandant)
- Beschlussauszug-PDF je TOP und Sammel-PDF je Sitzung mit
  Ausfertigungsvermerk; NÖ-Beschlusstexte nur in interner Ausfertigung
- Versand-/Übergabevermerk mit Audit-Eintrag
- Ö/NÖ-Sichtbarkeit und Tenant-Isolation
"""

import base64
import io
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

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

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
    SessionResolutionForwarding,
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


def pdf_text(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(bytes(pdf_bytes)))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
org2 = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")

start = (timezone.now() - timedelta(days=3)).replace(hour=17, minute=0, second=0, microsecond=0)
meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Sitzung des Hauptausschusses", organization=org, start=start, is_public=True
)
meeting2 = SessionMeeting.objects.create(
    tenant=tenant, name="Sitzung des Bauausschusses", organization=org2, start=start - timedelta(days=400), is_public=True
)

top1 = SessionAgendaItem.objects.create(
    meeting=meeting,
    number="1",
    order=1,
    name="BESCHLUSS-SPIELPLATZ",
    vote_result="approved",
    votes_yes=8,
    votes_no=1,
    votes_abstain=2,
    resolution_text="Der Ausschuss beschließt die Sanierung des Spielplatzes.",
)
top2 = SessionAgendaItem.objects.create(
    meeting=meeting,
    number="2",
    order=2,
    name="ABGELEHNTER-ANTRAG",
    vote_result="rejected",
    votes_yes=3,
    votes_no=8,
)
top_pending = SessionAgendaItem.objects.create(
    meeting=meeting, number="3", order=3, name="OFFENER-TOP", vote_result="pending"
)
top_np = SessionAgendaItem.objects.create(
    meeting=meeting,
    number="N1",
    order=4,
    name="GEHEIMER-BESCHLUSS-XYZ",
    is_public=False,
    vote_result="approved",
)
top_np.set_resolution_text_encrypted("GEHEIMER-BESCHLUSSTEXT-XYZ")
top_np.save()
top_old = SessionAgendaItem.objects.create(
    meeting=meeting2, number="1", order=1, name="ALTER-BAUBESCHLUSS", vote_result="approved"
)

org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdausschuss")
meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=start, is_public=True
)
top_b = SessionAgendaItem.objects.create(
    meeting=meeting_b, number="1", name="FREMD-BESCHLUSS-XYZ", vote_result="approved"
)


def make_client(name, **flags):
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle_{name}", **flags)
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-Test-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    c = Client()
    c.force_login(user)
    return c, su


admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer, _su_viewer = make_client("leser", can_view_meetings=True)

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Beschlussregister
# =============================================================================
print("=== Phase A: Beschlussregister ===")

resp = admin.get(f"{base}/resolutions/")
html = resp.content.decode("utf-8")
check("Register -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Gefasste Beschlüsse gelistet", "BESCHLUSS-SPIELPLATZ" in html and "ABGELEHNTER-ANTRAG" in html)
check("Offene TOPs nicht im Register", "OFFENER-TOP" not in html)
check("Admin sieht NÖ-Beschluss", "GEHEIMER-BESCHLUSS-XYZ" in html)
check("Abstimmungszahlen im Register", "Ja: 8" in html)
check("Keine Fremddaten", "FREMD-BESCHLUSS" not in html)

# Filter
resp = admin.get(f"{base}/resolutions/", {"organization": str(org2.id)})
html = resp.content.decode("utf-8")
check("Filter Gremium", "ALTER-BAUBESCHLUSS" in html and "BESCHLUSS-SPIELPLATZ" not in html)
resp = admin.get(f"{base}/resolutions/", {"result": "rejected"})
html = resp.content.decode("utf-8")
check("Filter Ergebnis", "ABGELEHNTER-ANTRAG" in html and "BESCHLUSS-SPIELPLATZ" not in html)
year_old = (start - timedelta(days=400)).year
resp = admin.get(f"{base}/resolutions/", {"year": str(year_old)})
html = resp.content.decode("utf-8")
check("Filter Jahr", "ALTER-BAUBESCHLUSS" in html and "BESCHLUSS-SPIELPLATZ" not in html)

# Ö/NÖ im Register
resp = viewer.get(f"{base}/resolutions/")
html = resp.content.decode("utf-8")
check("Viewer: NÖ-Beschluss unsichtbar", "GEHEIMER-BESCHLUSS-XYZ" not in html)
check("Viewer: Ö-Beschlüsse sichtbar", "BESCHLUSS-SPIELPLATZ" in html)

# =============================================================================
# Phase B: Nummernvergabe (Sammel-Ausfertigung)
# =============================================================================
print()
print("=== Phase B: Beschlussnummern ===")

resp = admin.post(f"{base}/meetings/{meeting.id}/resolutions/generate/")
check("Nummernvergabe -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
top1.refresh_from_db()
top2.refresh_from_db()
top_np.refresh_from_db()
top_pending.refresh_from_db()
year = timezone.localtime(meeting.start).year
check("Nummer für TOP 1", top1.resolution_number == f"B/{year}/0001", top1.resolution_number)
check("Fortlaufende Nummern", top2.resolution_number == f"B/{year}/0002", top2.resolution_number)
check("Auch NÖ-Beschluss nummeriert", top_np.resolution_number == f"B/{year}/0003", top_np.resolution_number)
check("Offener TOP ohne Nummer", top_pending.resolution_number == "")

# Idempotenz
admin.post(f"{base}/meetings/{meeting.id}/resolutions/generate/")
top1.refresh_from_db()
check("Erneute Vergabe ändert Nummern nicht", top1.resolution_number == f"B/{year}/0001")

resp = admin.get(f"{base}/resolutions/")
check("Nummern im Register sichtbar", f"B/{year}/0001" in resp.content.decode("utf-8"))

# =============================================================================
# Phase C: Auszug-PDFs
# =============================================================================
print()
print("=== Phase C: Auszug-PDFs ===")

resp = admin.get(f"{base}/agenda/{top1.id}/beschlussauszug.pdf")
check("Einzelauszug -> 200 (PDF)", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text = pdf_text(resp.content)
check("Auszug: Beschlussnummer", f"B/{year}/0001" in text)
check("Auszug: Beschlusstext", "Sanierung des Spielplatzes" in text)
check("Auszug: Abstimmungsergebnis", "Ja-Stimmen: 8" in text)
check("Auszug: Ausfertigungsvermerk", "Richtigkeit der Ausfertigung" in text)
check("Auszug: Auszugsvermerk", "Auszug aus der Niederschrift" in text)

# Sammel-PDF (Admin, inkl. NÖ)
resp = admin.get(f"{base}/meetings/{meeting.id}/beschlussauszuege.pdf")
check("Sammel-PDF -> 200", resp.status_code == 200, f"got {resp.status_code}")
text = pdf_text(resp.content)
check("Sammel-PDF: alle Beschlüsse", "BESCHLUSS-SPIELPLATZ" in text and "ABGELEHNTER-ANTRAG" in text)
check("Sammel-PDF: NÖ-Beschluss intern enthalten", "GEHEIMER-BESCHLUSS-XYZ" in text)
check("Sammel-PDF: NÖ-Beschlusstext intern enthalten", "GEHEIMER-BESCHLUSSTEXT-XYZ" in text)
check("Sammel-PDF: offener TOP fehlt", "OFFENER-TOP" not in text)

# Viewer ohne NÖ-Recht: Sammel-PDF ohne NÖ-Inhalte
resp = viewer.get(f"{base}/meetings/{meeting.id}/beschlussauszuege.pdf")
check("Viewer Sammel-PDF -> 200", resp.status_code == 200, f"got {resp.status_code}")
text = pdf_text(resp.content)
check("Viewer: KEIN NÖ-Beschluss", "GEHEIMER-BESCHLUSS-XYZ" not in text)
check("Viewer: KEIN NÖ-Beschlusstext", "GEHEIMER-BESCHLUSSTEXT-XYZ" not in text)

# NÖ-Einzelauszug ohne NÖ-Recht -> 404
resp = viewer.get(f"{base}/agenda/{top_np.id}/beschlussauszug.pdf")
check("NÖ-Einzelauszug ohne Recht -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Auszug für offenen TOP -> Redirect mit Fehler
resp = admin.get(f"{base}/agenda/{top_pending.id}/beschlussauszug.pdf")
check("Auszug für offenen TOP abgelehnt", resp.status_code == 302)

# =============================================================================
# Phase D: Übergabevermerk + Audit
# =============================================================================
print()
print("=== Phase D: Übergabevermerk ===")

resp = admin.post(
    f"{base}/agenda/{top1.id}/forwarding/add/",
    {"recipient": "Bauamt", "method": "internal", "note": "Zur Umsetzung."},
)
fw = SessionResolutionForwarding.objects.filter(agenda_item=top1).first()
check("Übergabe dokumentiert", fw is not None)
check("Übergabe-Metadaten", fw is not None and fw.recipient == "Bauamt" and fw.sent_by_id == su_admin.id)
audit_entry = SessionAuditLog.objects.filter(
    tenant=tenant, model_name="SessionResolutionForwarding", action="create"
).first()
check("Audit: Übergabe-Eintrag", audit_entry is not None)
check(
    "Audit: Empfänger + Beschlussnummer im Eintrag",
    audit_entry is not None
    and audit_entry.changes.get("empfaenger") == "Bauamt"
    and audit_entry.changes.get("beschluss") == f"B/{year}/0001",
)
resp = admin.get(f"{base}/resolutions/")
check("Übergabe im Register sichtbar", "Bauamt" in resp.content.decode("utf-8"))

resp = admin.post(f"{base}/agenda/{top1.id}/forwarding/add/", {"recipient": ""})
check("Übergabe ohne Empfänger abgelehnt", SessionResolutionForwarding.objects.filter(agenda_item=top1).count() == 1)

# =============================================================================
# Phase E: Berechtigungen und Tenant-Isolation
# =============================================================================
print()
print("=== Phase E: Berechtigungen und Isolation ===")

resp = viewer.post(f"{base}/meetings/{meeting.id}/resolutions/generate/")
check("Nummernvergabe ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.post(f"{base}/agenda/{top2.id}/forwarding/add/", {"recipient": "Amt"})
check("Übergabe ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = admin.get(f"{base}/agenda/{top_b.id}/beschlussauszug.pdf")
check("Fremder Auszug -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = admin.post(f"{base}/agenda/{top_b.id}/forwarding/add/", {"recipient": "Amt"})
check("Fremde Übergabe -> 404", resp.status_code == 404 and not SessionResolutionForwarding.objects.filter(agenda_item=top_b).exists())
resp = admin.post(f"{base}/meetings/{meeting_b.id}/resolutions/generate/")
top_b.refresh_from_db()
check("Fremde Nummernvergabe -> 404, keine Nummer", resp.status_code == 404 and top_b.resolution_number == "")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
