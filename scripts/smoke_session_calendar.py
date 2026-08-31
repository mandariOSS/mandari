# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Sitzungskalender und Jahresplanung (Issue #82).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_calendar.py

Prüft:
- Monatskalender: Sitzungen im richtigen Monat, Ö/NÖ-Sichtbarkeit, Navigation
- Serientermine: wöchentlich/14-täglich/monatlich (N-ter Wochentag),
  Vorschau mit Kollisionsprüfung, Anlage als Entwürfe mit Audit
- Kollisionsprüfung: gleicher Raum und zeitliche Überschneidung
- Sitzungsplan-PDF: Jahresübersicht, NÖ nur intern
- ICS-Abo-Feed je Gremium: nur öffentliche Sitzungen, ohne Anmeldung
- Berechtigungen und Tenant-Isolation
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import date, time, timedelta
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
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import calendar_service  # noqa: E402

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


def pdf_text(pdf_bytes):
    from pypdf import PdfReader
    import io

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# =============================================================================
# Setup
# =============================================================================
now = timezone.now()
tz = timezone.get_current_timezone()

tenant = SessionTenant.objects.create(name="Kalenderstadt", slug="kalenderstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-cal")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat der Stadt")
org2 = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdausschuss")

# Feste Referenzdaten: Juni 2027 (der 1.6.2027 ist ein Dienstag)
ref = timezone.make_aware(timezone.datetime(2027, 6, 15, 18, 0), tz)
m_public = SessionMeeting.objects.create(
    tenant=tenant, name="KAL-OEFFENTLICH", organization=org, start=ref,
    is_public=True, room="Ratssaal",
)
m_np = SessionMeeting.objects.create(
    tenant=tenant, name="KAL-GEHEIM", organization=org2,
    start=ref + timedelta(days=1), is_public=False,
)
m_other_month = SessionMeeting.objects.create(
    tenant=tenant, name="KAL-JULI", organization=org,
    start=ref + timedelta(days=30), is_public=True,
)
m_cancelled = SessionMeeting.objects.create(
    tenant=tenant, name="KAL-ABGESAGT", organization=org,
    start=ref + timedelta(days=2), is_public=True, cancelled=True,
)
SessionMeeting.objects.create(
    tenant=tenant_b, name="KAL-FREMD", organization=org_b, start=ref, is_public=True
)

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-cal@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True)
viewer_user = User.objects.create_user(email="leser-cal@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

anon = Client()
base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Monatskalender ===")
resp = admin.get(f"{base}/meetings/calendar/?year=2027&month=6")
html = resp.content.decode("utf-8")
check("Kalender -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Ö-Sitzung im Monat", "KAL-OEFFENTLICH" in html and "Rat der Stadt" in html)
check("Monatstitel", "Juni 2027" in html)
check("Juli-Sitzung nicht im Juni", "KAL-JULI" not in html)
check("Admin sieht NÖ-Sitzung (Gremium)", "Bauausschuss" in html)
check("ICS-Feed-Links vorhanden", "sitzungen.ics" in html)

resp = viewer.get(f"{base}/meetings/calendar/?year=2027&month=6")
html_v = resp.content.decode("utf-8")
check("Viewer: NÖ-Gremium-Termin unsichtbar", html_v.count("Bauausschuss") <= 1)

resp = admin.get(f"{base}/meetings/calendar/?year=kaputt&month=99")
check("Ungültige Parameter -> aktueller Monat (200)", resp.status_code == 200)

# =============================================================================
print()
print("=== Phase B: Serientermine ===")
# Monatlich, 2. Dienstag, Juni-Dezember 2027
starts = calendar_service.generate_series(
    rhythm="monthly_2", weekday=1, start_time=time(18, 0),
    date_from=date(2027, 6, 1), date_to=date(2027, 12, 31),
)
check("Monatsserie: 7 Termine", len(starts) == 7, f"got {len(starts)}")
check("Alle am Dienstag", all(s.weekday() == 1 for s in starts))
check("2. Dienstag im Juni = 08.06.2027", starts[0].date() == date(2027, 6, 8), str(starts[0]))

weekly = calendar_service.generate_series(
    rhythm="weekly", weekday=0, start_time=time(17, 0),
    date_from=date(2027, 6, 1), date_to=date(2027, 6, 30),
)
check("Wochenserie Juni: 4 Montage", len(weekly) == 4, f"got {len(weekly)}")

biweekly = calendar_service.generate_series(
    rhythm="biweekly", weekday=0, start_time=time(17, 0),
    date_from=date(2027, 6, 1), date_to=date(2027, 6, 30),
)
check("14-täglich Juni: 2 Termine", len(biweekly) == 2, f"got {len(biweekly)}")

check(
    "Zeitraum > 1 Jahr -> leer",
    calendar_service.generate_series(
        rhythm="weekly", weekday=0, start_time=time(17, 0),
        date_from=date(2027, 1, 1), date_to=date(2028, 6, 1),
    ) == [],
)

# =============================================================================
print()
print("=== Phase C: Kollisionsprüfung ===")
conflicts = calendar_service.find_conflicts(tenant, ref + timedelta(hours=1))
check("Zeitliche Überschneidung erkannt", any(m.pk == m_public.pk for m in conflicts))

same_room = calendar_service.find_conflicts(
    tenant, ref.replace(hour=8), room="Ratssaal"
)
check("Raumkollision erkannt (auch ohne Zeitüberschneidung)", any(m.pk == m_public.pk for m in same_room))

free = calendar_service.find_conflicts(tenant, ref + timedelta(days=10))
check("Freier Tag ohne Kollision", free == [])

# =============================================================================
print()
print("=== Phase D: Jahresplanung (View) ===")
plan_data = {
    "organization": str(org2.id),
    "name": "SERIE-BAUAUSSCHUSS",
    "rhythm": "monthly_2",
    "weekday": "1",
    "time": "18:00",
    "date_from": "2027-06-01",
    "date_to": "2027-08-31",
    "room": "Ratssaal",
    "is_public": "1",
}
resp = admin.post(f"{base}/meetings/plan/", {**plan_data, "action": "preview"})
html = resp.content.decode("utf-8")
check("Vorschau -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Vorschau: 3 Termine", "3 Termine" in html)
check("Vorschau: keine Sitzungen angelegt", not SessionMeeting.objects.filter(name="SERIE-BAUAUSSCHUSS").exists())

before = SessionMeeting.objects.filter(tenant=tenant).count()
resp = admin.post(f"{base}/meetings/plan/", {**plan_data, "action": "create"})
created = SessionMeeting.objects.filter(name="SERIE-BAUAUSSCHUSS")
check("Anlage -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("3 Entwürfe angelegt", created.count() == 3, f"got {created.count()}")
check("Serien-Termine als Entwurf", all(m.meeting_state == "draft" for m in created))
check("Raum übernommen", all(m.room == "Ratssaal" for m in created))
audit_entry = SessionAuditLog.objects.filter(
    tenant=tenant, changes__sitzungsserie="SERIE-BAUAUSSCHUSS"
).first()
check("Audit: Serienanlage protokolliert", audit_entry is not None and audit_entry.changes.get("anzahl") == 3)

resp = admin.post(f"{base}/meetings/plan/", {**plan_data, "organization": "", "action": "preview"})
check("Fehlendes Gremium -> Redirect mit Fehler", resp.status_code == 302)
resp = admin.post(f"{base}/meetings/plan/", {**plan_data, "date_to": "2026-01-01", "action": "preview"})
check("Ende vor Beginn abgelehnt", resp.status_code == 302)

resp = viewer.get(f"{base}/meetings/plan/")
check("Planung ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase E: Sitzungsplan-PDF ===")
resp = admin.get(f"{base}/sitzungsplan.pdf?year=2027")
check("PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text = pdf_text(resp.content)
check("PDF: Jahr im Titel", "Sitzungsplan 2027" in text)
check("PDF: Gremien enthalten", "Rat der Stadt" in text and "Bauausschuss" in text)
check("PDF: abgesagte Sitzung fehlt", "KAL-ABGESAGT" not in text)

resp = viewer.get(f"{base}/sitzungsplan.pdf?year=2027")
text_v = pdf_text(resp.content)
check("Viewer-PDF ohne NÖ-Vermerk", "nichtöffentlich)" not in text_v)

resp = admin.get(f"{base}/sitzungsplan.pdf?year=2027&organization={org.id}")
text_org = pdf_text(resp.content)
check("PDF je Gremium gefiltert", "Rat der Stadt" in text_org and "SERIE" not in text_org)

# =============================================================================
print()
print("=== Phase F: ICS-Feed ===")
resp = anon.get(f"{base}/organizations/{org.id}/sitzungen.ics")
check("Feed ohne Anmeldung -> 200", resp.status_code == 200, f"got {resp.status_code}")
ics = resp.content.decode("utf-8")
check("Feed: Kalenderformat", "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics)
check("Feed: Ö-Sitzung enthalten", "KAL-OEFFENTLICH" in ics)
check("Feed: abgesagte Sitzung fehlt", "KAL-ABGESAGT" not in ics)

resp = anon.get(f"{base}/organizations/{org2.id}/sitzungen.ics")
ics2 = resp.content.decode("utf-8")
check("Feed: NÖ-Sitzung NIE enthalten", "KAL-GEHEIM" not in ics2)

resp = anon.get(f"{base}/organizations/{org_b.id}/sitzungen.ics")
check("Fremdes Gremium unter falschem Mandanten -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase G: Isolation ===")
resp = admin.get(f"{base}/meetings/calendar/?year=2027&month=6")
check("Keine Fremddaten im Kalender", "KAL-FREMD" not in resp.content.decode("utf-8"))
resp = admin.get(f"{base}/sitzungsplan.pdf?year=2027")
check("Keine Fremddaten im PDF", "KAL-FREMD" not in pdf_text(resp.content))

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
