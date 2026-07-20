# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Anwesenheitserfassung aus der Gremienbesetzung (Issue #30).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_attendance.py

Prüft:
- Anwesenheitsliste wird auf Klick aus der aktuellen Besetzung vorbefüllt
  (Funktion + Stimmrecht aus der Besetzung, Vertreter mit Hinweis)
- Erneutes Erzeugen ist idempotent und überschreibt erfasste Stati nicht,
  ergänzt aber neue Besetzungsmitglieder
- Schnellerfassung je Zeile (Status/Zeiten) über AttendanceUpdateView
- Beschlussfähigkeits-Anzeige (Quorum) live im Sitzungsdetail
- Gäste/Verwaltungsvertreter manuell ergänzen (ohne Stimmrecht) + entfernen
- Berechtigungen (manage_attendance) und Tenant-Isolation
- Audit-Einträge für Anwesenheits-Aktionen
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

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
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
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)

org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")

p_chair = SessionPerson.objects.create(tenant=tenant, given_name="Vera", family_name="Vorsitz")
p_m1 = SessionPerson.objects.create(tenant=tenant, given_name="Max", family_name="Mitglied")
p_m2 = SessionPerson.objects.create(tenant=tenant, given_name="Moritz", family_name="Zweitmitglied")
p_sub = SessionPerson.objects.create(tenant=tenant, given_name="Nora", family_name="Nachrueckerin")
p_advisor = SessionPerson.objects.create(tenant=tenant, given_name="Bert", family_name="Berater")
p_guest_pool = SessionPerson.objects.create(tenant=tenant, given_name="Victor", family_name="Verwaltung")
p_ended = SessionPerson.objects.create(tenant=tenant, given_name="Alt", family_name="Ausgeschieden")

SessionOrganizationMembership.objects.create(organization=org, person=p_chair, role="chair")
SessionOrganizationMembership.objects.create(organization=org, person=p_m1, role="member")
SessionOrganizationMembership.objects.create(organization=org, person=p_m2, role="member")
SessionOrganizationMembership.objects.create(organization=org, person=p_sub, role="member", substitute_for=p_m2)
SessionOrganizationMembership.objects.create(organization=org, person=p_advisor, role="advisor", has_voting_rights=False)
SessionOrganizationMembership.objects.create(
    organization=org, person=p_ended, role="member", end_date=timezone.localdate() - timedelta(days=10)
)

start = (timezone.now() + timedelta(days=3)).replace(hour=17, minute=0, second=0, microsecond=0)
meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Sitzung des Hauptausschusses", organization=org, start=start, is_public=True
)

org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdausschuss")
meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=start, is_public=True
)

client = Client()
client.force_login(admin_user)
base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Liste aus Besetzung erzeugen
# =============================================================================
print("=== Phase A: Liste aus Besetzung erzeugen ===")

resp = client.get(f"{base}/meetings/{meeting.id}/")
check("Sitzungsdetail zeigt Erzeugen-Schaltfläche", "Liste aus Besetzung erzeugen" in resp.content.decode("utf-8"))

resp = client.post(f"{base}/meetings/{meeting.id}/attendance/generate/")
check("Generate -> Redirect", resp.status_code == 302, f"got {resp.status_code}")

attendances = {a.person_id: a for a in SessionAttendance.objects.filter(meeting=meeting)}
check("5 aktive Besetzungsmitglieder übernommen", len(attendances) == 5, f"got {len(attendances)}")
check("Ausgeschiedenes Mitglied nicht übernommen", p_ended.id not in attendances)
check("Vorsitz-Funktion übernommen", attendances[p_chair.id].role == "chair")
check("Beratendes Mitglied ohne Stimmrecht", attendances[p_advisor.id].has_voting_rights is False)
check("Mitglieder stimmberechtigt", attendances[p_m1.id].has_voting_rights is True)
check(
    "Vertreterin mit Vertretungs-Hinweis",
    "Vertretung für" in attendances[p_sub.id].notes and "Zweitmitglied" in attendances[p_sub.id].notes,
    f"notes={attendances[p_sub.id].notes!r}",
)
check("Initialstatus 'Eingeladen'", attendances[p_m1.id].status == "invited")
check(
    "Audit: create-Einträge für Anwesenheit",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionAttendance", action="create").count() >= 5,
)

# Idempotenz: erneutes Erzeugen erzeugt keine Duplikate, überschreibt nichts
att_m1 = attendances[p_m1.id]
att_m1.status = "present"
att_m1.save()
resp = client.post(f"{base}/meetings/{meeting.id}/attendance/generate/")
check("Erneutes Erzeugen: keine Duplikate", SessionAttendance.objects.filter(meeting=meeting).count() == 5)
att_m1.refresh_from_db()
check("Erneutes Erzeugen überschreibt Status nicht", att_m1.status == "present")

# Neues Besetzungsmitglied -> Nachziehen ergänzt genau dieses
p_new = SessionPerson.objects.create(tenant=tenant, given_name="Nina", family_name="Neumitglied")
SessionOrganizationMembership.objects.create(organization=org, person=p_new, role="member")
resp = client.post(f"{base}/meetings/{meeting.id}/attendance/generate/")
check("Nachziehen ergänzt neues Mitglied", SessionAttendance.objects.filter(meeting=meeting, person=p_new).exists())
check("Nachziehen: Gesamtzahl 6", SessionAttendance.objects.filter(meeting=meeting).count() == 6)

# =============================================================================
# Phase B: Schnellerfassung + Quorum
# =============================================================================
print()
print("=== Phase B: Schnellerfassung und Quorum ===")

# Stimmberechtigt: chair, m1, m2, sub, new = 5 -> Quorum 3
resp = client.get(f"{base}/meetings/{meeting.id}/")
html = resp.content.decode("utf-8")
check("Quorum-Anzeige vorhanden", "beschlussfähig" in html.lower())
check("Anfangs nicht beschlussfähig (nur 1 anwesend)", "Nicht beschlussfähig" in html)
check("Quorum-Zahlen korrekt (1/5, nötig 3)", "(1/5 stimmberechtigt anwesend, nötig: 3)" in html)

# Zwei weitere anwesend melden (eine davon verspätet — zählt als anwesend)
att_chair = SessionAttendance.objects.get(meeting=meeting, person=p_chair)
resp = client.post(
    f"{base}/attendance/{att_chair.id}/update/",
    {"status": "present", "arrival_time": "", "departure_time": "", "notes": ""},
)
check("Schnellerfassung Update -> OK", resp.status_code in (200, 302), f"got {resp.status_code}")

att_sub = SessionAttendance.objects.get(meeting=meeting, person=p_sub)
client.post(
    f"{base}/attendance/{att_sub.id}/update/",
    {"status": "joined_late", "arrival_time": "17:15", "departure_time": "", "notes": "kam später"},
)
att_sub.refresh_from_db()
check("Zeiten erfasst", str(att_sub.arrival_time) == "17:15:00", f"got {att_sub.arrival_time}")

resp = client.get(f"{base}/meetings/{meeting.id}/")
html = resp.content.decode("utf-8")
check("Jetzt beschlussfähig (3/5)", "Beschlussfähig" in html and "Nicht beschlussfähig" not in html)

# Beratendes Mitglied anwesend -> zählt NICHT fürs Quorum
att_adv = SessionAttendance.objects.get(meeting=meeting, person=p_advisor)
client.post(
    f"{base}/attendance/{att_adv.id}/update/",
    {"status": "present", "arrival_time": "", "departure_time": "", "notes": ""},
)
resp = client.get(f"{base}/meetings/{meeting.id}/")
check("Nicht-Stimmberechtigte zählen nicht", "(3/5 stimmberechtigt anwesend" in resp.content.decode("utf-8"))

# =============================================================================
# Phase C: Gäste manuell ergänzen / entfernen
# =============================================================================
print()
print("=== Phase C: Gäste/Verwaltung ===")

resp = client.post(
    f"{base}/meetings/{meeting.id}/attendance/add/",
    {"person": str(p_guest_pool.id), "role": "guest"},
)
guest_att = SessionAttendance.objects.filter(meeting=meeting, person=p_guest_pool).first()
check("Gast manuell ergänzt", guest_att is not None)
check("Gast ohne Stimmrecht", guest_att is not None and guest_att.has_voting_rights is False)
check("Gast-Funktion", guest_att is not None and guest_att.role == "guest")

count_before = SessionAttendance.objects.filter(meeting=meeting).count()
client.post(f"{base}/meetings/{meeting.id}/attendance/add/", {"person": str(p_guest_pool.id), "role": "guest"})
check("Doppeltes Ergänzen erzeugt kein Duplikat", SessionAttendance.objects.filter(meeting=meeting).count() == count_before)

resp = client.get(f"{base}/meetings/{meeting.id}/")
check("Gast zählt nicht fürs Quorum", "(3/5 stimmberechtigt anwesend" in resp.content.decode("utf-8"))

resp = client.post(f"{base}/attendance/{guest_att.id}/delete/")
check("Gast wieder entfernt", not SessionAttendance.objects.filter(pk=guest_att.pk).exists())
check(
    "Audit: delete-Eintrag für Anwesenheit",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionAttendance", action="delete").exists(),
)

# =============================================================================
# Phase D: Berechtigungen und Tenant-Isolation
# =============================================================================
print()
print("=== Phase D: Berechtigungen und Isolation ===")

viewer_role = SessionRole.objects.create(tenant=tenant, name="Nur-Sitzungen", can_view_meetings=True)
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

before = SessionAttendance.objects.count()
resp = viewer.post(f"{base}/meetings/{meeting.id}/attendance/generate/")
check("Generate ohne manage_attendance -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.post(f"{base}/meetings/{meeting.id}/attendance/add/", {"person": str(p_guest_pool.id)})
check("Add ohne manage_attendance -> 403", resp.status_code == 403, f"got {resp.status_code}")
att_any = SessionAttendance.objects.filter(meeting=meeting).first()
resp = viewer.post(f"{base}/attendance/{att_any.id}/delete/")
check("Delete ohne manage_attendance -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Keine Mutation ohne Berechtigung", SessionAttendance.objects.count() == before)

resp = viewer.get(f"{base}/meetings/{meeting.id}/")
html = resp.content.decode("utf-8")
check("Viewer sieht Anwesenheit read-only (kein Erzeugen-Button)", "Liste aus Besetzung erzeugen" not in html)
check("Viewer sieht Quorum-Anzeige", "beschlussfähig" in html.lower())

resp = client.post(f"/session/{tenant.slug}/meetings/{meeting_b.id}/attendance/generate/")
check("Fremde Sitzung: Generate -> 404", resp.status_code == 404, f"got {resp.status_code}")
check("Fremde Sitzung: keine Zeilen erzeugt", SessionAttendance.objects.filter(meeting=meeting_b).count() == 0)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
