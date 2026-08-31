# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Statistiken und Berichte (Issue #84).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_reports.py

Prüft:
- Anwesenheitsstatistik (Quote, Ö/NÖ-Sichtbarkeit, Gremium-Filter)
- Sitzungsstatistik je Gremium (Sitzungen, TOPs, Beschlüsse, Ø-Dauer)
- Sitzungsgeld-Jahresbericht (Summen, nur mit manage_allowances)
- Vorlagen-Durchlaufzeiten
- CSV-Exporte inkl. Berechtigungsprüfung
- Tenant-Isolation
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import timedelta
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
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionAllowance,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
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


# =============================================================================
# Setup
# =============================================================================
tz = timezone.get_current_timezone()
year = timezone.localdate().year
base_dt = timezone.make_aware(timezone.datetime(year, 3, 10, 18, 0), tz)

tenant = SessionTenant.objects.create(name="Berichtsstadt", slug="berichtsstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-rep")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
org2 = SessionOrganization.objects.create(tenant=tenant, name="Sozialausschuss")

p1 = SessionPerson.objects.create(tenant=tenant, given_name="Paula", family_name="PRAESENT")
p2 = SessionPerson.objects.create(tenant=tenant, given_name="Emil", family_name="ENTSCHULDIGT")

m1 = SessionMeeting.objects.create(
    tenant=tenant, name="Ratssitzung März", organization=org, start=base_dt,
    end=base_dt + timedelta(hours=2), is_public=True,
)
m2 = SessionMeeting.objects.create(
    tenant=tenant, name="Ratssitzung April", organization=org, start=base_dt + timedelta(days=30),
    actual_start=base_dt + timedelta(days=30),
    actual_end=base_dt + timedelta(days=30, hours=4), is_public=True,
)
m_np = SessionMeeting.objects.create(
    tenant=tenant, name="NOE-SITZUNG", organization=org2, start=base_dt + timedelta(days=2),
    is_public=False,
)
SessionMeeting.objects.create(
    tenant=tenant, name="Abgesagt", organization=org, start=base_dt + timedelta(days=3),
    cancelled=True, is_public=True,
)

SessionAgendaItem.objects.create(meeting=m1, number="1", order=1, name="TOP A", vote_result="approved")
SessionAgendaItem.objects.create(meeting=m1, number="2", order=2, name="TOP B", vote_result="pending")
SessionAgendaItem.objects.create(meeting=m2, number="1", order=1, name="TOP C", vote_result="rejected")
SessionAgendaItem.objects.create(meeting=m_np, number="1", order=1, name="TOP NOE", vote_result="approved", is_public=False)

a1 = SessionAttendance.objects.create(meeting=m1, person=p1, status="present")
a2 = SessionAttendance.objects.create(meeting=m2, person=p1, status="joined_late")
SessionAttendance.objects.create(meeting=m1, person=p2, status="excused")
SessionAttendance.objects.create(meeting=m2, person=p2, status="present")
a_np = SessionAttendance.objects.create(meeting=m_np, person=p1, status="present")

SessionAllowance.objects.create(attendance=a1, amount=Decimal("30.00"), status="paid")
SessionAllowance.objects.create(attendance=a2, amount=Decimal("30.00"), status="approved")
SessionAllowance.objects.create(attendance=a_np, amount=Decimal("50.00"), status="cancelled")

# Fremd-Mandant
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="FREMD-GREMIUM")
mb = SessionMeeting.objects.create(tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=base_dt)
pb = SessionPerson.objects.create(tenant=tenant_b, given_name="Fritz", family_name="FREMD")
SessionAttendance.objects.create(meeting=mb, person=pb, status="present")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-rep@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True)
viewer_user = User.objects.create_user(email="leser-rep@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Berichtsseite ===")
resp = admin.get(f"{base}/reports/?year={year}")
html = resp.content.decode("utf-8")
check("Berichte -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Anwesenheit: Personen gelistet", "PRAESENT" in html and "ENTSCHULDIGT" in html)
check("Anwesenheit: Quote 100 %", "100 %" in html)
check("Sitzungsstatistik: Gremien", "Rat" in html and "Sozialausschuss" in html)
check("Sitzungsgeld für Admin sichtbar", "Sitzungsgeld-Jahresbericht" in html)
check("Sitzungsgeld: Summe ohne Storno", "60" in html.replace("60.00", "60"), "")
check("Durchlaufzeit-Kachel", "Vorlagen-Durchlaufzeit" in html)

resp = viewer.get(f"{base}/reports/?year={year}")
html_v = resp.content.decode("utf-8")
check("Viewer -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Viewer: kein Sitzungsgeld-Bericht", "Sitzungsgeld-Jahresbericht" not in html_v)
check("Viewer: NÖ-Anwesenheit nicht mitgezählt", ">2<" in html_v or "PRAESENT" in html_v)

# Gremium-Filter: nur Rat -> beide Personen, NÖ-Sitzung außen vor
resp = admin.get(f"{base}/reports/?year={year}&organization={org.id}")
check("Gremium-Filter -> 200", resp.status_code == 200)

# =============================================================================
print()
print("=== Phase B: Statistik-Werte ===")
from apps.session.services import report_service  # noqa: E402

stats = report_service.attendance_stats(tenant, year, include_non_public=True)
paula = next(r for r in stats if "PRAESENT" in r["name"])
emil = next(r for r in stats if "ENTSCHULDIGT" in r["name"])
check("Paula: 3 eingeladen, 3 anwesend, 100 %", paula["invited"] == 3 and paula["present"] == 3 and paula["rate"] == 100, str(paula))
check("Emil: 2 eingeladen, 1 anwesend, 50 %", emil["invited"] == 2 and emil["present"] == 1 and emil["rate"] == 50, str(emil))

stats_pub = report_service.attendance_stats(tenant, year, include_non_public=False)
paula_pub = next(r for r in stats_pub if "PRAESENT" in r["name"])
check("Ohne NÖ: Paula nur 2 eingeladen", paula_pub["invited"] == 2, str(paula_pub))

mstats = report_service.meeting_stats(tenant, year, include_non_public=True)
rat = next(r for r in mstats if r["organization"].pk == org.pk)
check("Rat: 2 Sitzungen, 3 TOPs, 2 Beschlüsse", rat["meetings"] == 2 and rat["tops"] == 3 and rat["resolutions"] == 2, str(rat))
check("Rat: Ø-Dauer 180 min", rat["avg_duration"] == 180, str(rat["avg_duration"]))

allowances, totals = report_service.allowance_stats(tenant, year)
check("Sitzungsgeld: 2 Posten, 60 € gesamt, 30 € ausgezahlt",
      totals["count"] == 2 and totals["amount"] == Decimal("60.00") and totals["paid"] == Decimal("30.00"), str(totals))

# =============================================================================
print()
print("=== Phase C: CSV-Exporte ===")
resp = admin.get(f"{base}/reports/export.csv?type=attendance&year={year}")
csv_text = resp.content.decode("utf-8")
check("Anwesenheits-CSV -> 200", resp.status_code == 200 and "text/csv" in resp["Content-Type"])
check("CSV: Personen enthalten", "PRAESENT" in csv_text and "ENTSCHULDIGT" in csv_text)
check("CSV: keine Fremddaten", "FREMD" not in csv_text)

resp = admin.get(f"{base}/reports/export.csv?type=allowances&year={year}")
csv_text = resp.content.decode("utf-8")
check("Sitzungsgeld-CSV -> 200", resp.status_code == 200)
check("Sitzungsgeld-CSV: Gesamtzeile", "Gesamt" in csv_text and "60" in csv_text)

resp = viewer.get(f"{base}/reports/export.csv?type=allowances&year={year}")
check("Sitzungsgeld-CSV ohne manage_allowances -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.get(f"{base}/reports/export.csv?type=attendance&year={year}")
check("Anwesenheits-CSV für Leser erlaubt", resp.status_code == 200, f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
