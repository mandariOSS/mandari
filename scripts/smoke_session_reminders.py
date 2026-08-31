# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Fristen-Erinnerungen (Issue #83).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_reminders.py

Prüft:
- Ladungsfrist: Erinnerung vor Ablauf + Eskalation nach Ablauf an edit_meetings
- Vorlagenfrist: Erinnerung an edit_papers (nur offene Status)
- Fehlende Rückmeldung: Erinnerung an die eingeladene Person selbst
- Wiedervorlage Beschlusskontrolle: Erinnerung bei naher/überfälliger Frist,
  erneute Erinnerung nach Fristverschiebung
- Idempotenz: zweiter Lauf versendet nichts
- Mandanten-Konfiguration (An/Aus, Vorlaufzeiten) + Settings-Endpoint
- dry-run versendet und protokolliert nichts
- Tenant-Isolation der Empfänger
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
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
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
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionReminderLog,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import reminder_service  # noqa: E402

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


def outbox():
    return mail.outbox


def clear_outbox():
    mail.outbox = []


def subjects():
    return [m.subject for m in mail.outbox]


# =============================================================================
# Setup
# =============================================================================
now = timezone.now()
today = timezone.localdate()

tenant = SessionTenant.objects.create(name="Erinnerungsstadt", slug="erinnerungsstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-rem")

org = SessionOrganization.objects.create(
    tenant=tenant, name="Hauptausschuss", invitation_period_days=7
)

# Sitzungsdienst-Benutzer (edit_meetings) + Vorlagen-Benutzer (edit_papers)
role_meet = SessionRole.objects.create(
    tenant=tenant, name="Sitzungsdienst", can_view_meetings=True, can_edit_meetings=True
)
role_paper = SessionRole.objects.create(
    tenant=tenant, name="Vorlagen", can_edit_papers=True
)
u_meet = User.objects.create_user(email="sitzungsdienst@example.org", password="pw-Smoke-1!")
su_meet = SessionUser.objects.create(user=u_meet, tenant=tenant)
su_meet.roles.add(role_meet)
u_paper = User.objects.create_user(email="vorlagen@example.org", password="pw-Smoke-1!")
su_paper = SessionUser.objects.create(user=u_paper, tenant=tenant)
su_paper.roles.add(role_paper)

# Fremd-Mandant mit eigenem edit_meetings-Benutzer (darf NIE E-Mails bekommen)
role_b = SessionRole.objects.create(tenant=tenant_b, name="Fremd", can_edit_meetings=True)
u_b = User.objects.create_user(email="fremd@example.org", password="pw-Smoke-1!")
su_b = SessionUser.objects.create(user=u_b, tenant=tenant_b)
su_b.roles.add(role_b)

# Sitzung 1: in 8 Tagen, Ladungsfrist (8-7)=morgen -> "läuft ab" (3 Tage Vorlauf)
meeting_soon = SessionMeeting.objects.create(
    tenant=tenant,
    name="SITZUNG-FRIST-NAH",
    organization=org,
    start=now + timedelta(days=8),
    meeting_state="scheduled",
)
# Sitzung 2: in 3 Tagen, Ladungsfrist vor 4 Tagen -> überfällig
meeting_overdue = SessionMeeting.objects.create(
    tenant=tenant,
    name="SITZUNG-FRIST-VERSTRICHEN",
    organization=org,
    start=now + timedelta(days=3),
    meeting_state="scheduled",
)
# Sitzung 3: weit weg -> keine Erinnerung
meeting_far = SessionMeeting.objects.create(
    tenant=tenant,
    name="SITZUNG-WEIT-WEG",
    organization=org,
    start=now + timedelta(days=30),
    meeting_state="scheduled",
)

# Vorlagen: Frist morgen (offen) / Frist morgen aber freigegeben / Frist fern
paper_due = SessionPaper.objects.create(
    tenant=tenant, reference="V-DUE", name="VORLAGE-FRIST-NAH",
    status="draft", deadline=today + timedelta(days=1),
)
paper_done = SessionPaper.objects.create(
    tenant=tenant, reference="V-OK", name="VORLAGE-FREIGEGEBEN",
    status="approved", deadline=today + timedelta(days=1),
)
paper_far = SessionPaper.objects.create(
    tenant=tenant, reference="V-FERN", name="VORLAGE-FRIST-FERN",
    status="draft", deadline=today + timedelta(days=30),
)

# Rückmeldung: Sitzung in 3 Tagen, Ladung versandt, Person eingeladen ohne Antwort
meeting_rsvp = SessionMeeting.objects.create(
    tenant=tenant,
    name="SITZUNG-RSVP",
    organization=org,
    start=now + timedelta(days=3),
    meeting_state="invitation_sent",
    invitation_sent_at=now - timedelta(days=2),
)
person = SessionPerson.objects.create(
    tenant=tenant, given_name="Rita", family_name="Rückmeldung", email="rita@example.org"
)
person_no_mail = SessionPerson.objects.create(
    tenant=tenant, given_name="Ohne", family_name="Mail"
)
att_open = SessionAttendance.objects.create(
    meeting=meeting_rsvp, person=person, status="invited"
)
SessionAttendance.objects.create(
    meeting=meeting_rsvp, person=person_no_mail, status="invited"
)

# Beschlusskontrolle: Frist in 2 Tagen (nicht erledigt) + erledigt (keine Mail)
res_meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Beschluss-Sitzung", organization=org, start=now - timedelta(days=10)
)
item_due = SessionAgendaItem.objects.create(
    meeting=res_meeting, number="1", order=1, name="BESCHLUSS-WIEDERVORLAGE",
    vote_result="approved", implementation_status="in_progress",
    implementation_recipient="Bauamt", implementation_deadline=today + timedelta(days=2),
)
SessionAgendaItem.objects.create(
    meeting=res_meeting, number="2", order=2, name="BESCHLUSS-ERLEDIGT",
    vote_result="approved", implementation_status="done",
    implementation_deadline=today - timedelta(days=2),
)

# =============================================================================
print("=== Phase A: Erinnerungslauf ===")
clear_outbox()
counts = reminder_service.run_for_tenant(tenant)
subs = "\n".join(subjects())

check("Ladungsfrist nah -> 1 Mail", counts["invitation_upcoming"] == 1, str(counts))
check("Ladungsfrist verstrichen -> 1 Mail", counts["invitation_overdue"] == 1, str(counts))
check("Vorlagenfrist -> 1 Mail", counts["paper_deadline"] == 1, str(counts))
check("Rückmeldung -> 1 Mail", counts["attendance_rsvp"] == 1, str(counts))
check("Wiedervorlage -> 1 Mail", counts["resolution_followup"] == 1, str(counts))
check("Gesamt 5 Mails", len(outbox()) == 5, f"got {len(outbox())}")

check("Nahe Sitzung im Betreff", "SITZUNG-FRIST-NAH" in subs)
check("Verstrichene Frist im Betreff", "SITZUNG-FRIST-VERSTRICHEN" in subs)
check("Weit entfernte Sitzung ohne Mail", "SITZUNG-WEIT-WEG" not in subs)
check("Freigegebene Vorlage ohne Mail", "V-OK" not in subs)
check("Ferne Vorlagenfrist ohne Mail", "V-FERN" not in subs)
check("Erledigter Beschluss ohne Mail", "BESCHLUSS-ERLEDIGT" not in subs)

to_all = [addr for m in outbox() for addr in m.to]
check("Sitzungsdienst als Empfänger", "sitzungsdienst@example.org" in to_all)
check("Vorlagen-Benutzer als Empfänger", "vorlagen@example.org" in to_all)
check("Person als RSVP-Empfänger", "rita@example.org" in to_all)
check("Fremd-Mandant NICHT als Empfänger", "fremd@example.org" not in to_all)

rsvp_mail = [m for m in outbox() if "Rückmeldung" in m.subject]
check("RSVP-Mail nur an die Person", rsvp_mail and rsvp_mail[0].to == ["rita@example.org"])

check("Erinnerungs-Protokoll: 5 Einträge", SessionReminderLog.objects.filter(tenant=tenant).count() == 5)

# =============================================================================
print()
print("=== Phase B: Idempotenz ===")
clear_outbox()
counts2 = reminder_service.run_for_tenant(tenant)
check("Zweiter Lauf versendet nichts", len(outbox()) == 0, f"got {len(outbox())}")
check("Zweiter Lauf zählt 0", not any(counts2.values()), str(counts2))

# Fristverschiebung in der Beschlusskontrolle -> neue Erinnerung
item_due.implementation_deadline = today + timedelta(days=5)
item_due.save(update_fields=["implementation_deadline"])
clear_outbox()
counts3 = reminder_service.run_for_tenant(tenant)
check("Verschobene Frist -> erneute Erinnerung", counts3["resolution_followup"] == 1, str(counts3))

# =============================================================================
print()
print("=== Phase C: Konfiguration ===")
tenant.reminder_settings = {
    "invitation_enabled": False,
    "paper_enabled": False,
    "rsvp_enabled": False,
    "resolution_enabled": False,
}
tenant.save(update_fields=["reminder_settings"])

# Neues erinnerungswürdiges Objekt, damit ohne Deaktivierung etwas käme
SessionPaper.objects.create(
    tenant=tenant, reference="V-NEU", name="VORLAGE-NEU",
    status="draft", deadline=today,
)
clear_outbox()
counts4 = reminder_service.run_for_tenant(tenant)
check("Alles deaktiviert -> keine Mails", len(outbox()) == 0 and not any(counts4.values()), str(counts4))

cfg = tenant.reminder_config()
check("Config: Deaktivierung gelesen", cfg["invitation_enabled"] is False)
check("Config: Default-Vorlauf bleibt", cfg["invitation_days_before"] == 3)

tenant.reminder_settings = {"paper_enabled": True, "paper_days_before": "unsinn"}
tenant.save(update_fields=["reminder_settings"])
cfg = tenant.reminder_config()
check("Config: ungültiger Wert -> Default", cfg["paper_days_before"] == 3)

# Settings-Endpoint
admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-rem@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)
base = f"/session/{tenant.slug}"

resp = admin.get(f"{base}/settings/")
check("Settings-Seite zeigt Erinnerungen", resp.status_code == 200 and "Fristen-Erinnerungen" in resp.content.decode("utf-8"))

resp = admin.post(
    f"{base}/settings/reminders/",
    {
        "invitation_enabled": "on",
        "invitation_days_before": "5",
        "paper_days_before": "2",
        "rsvp_enabled": "on",
        "rsvp_days_before": "99",
        "resolution_enabled": "on",
        "resolution_days_before": "7",
    },
)
tenant.refresh_from_db()
cfg = tenant.reminder_config()
check("Settings-POST -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Settings: Vorlauf gespeichert", cfg["invitation_days_before"] == 5)
check("Settings: Checkbox aus = deaktiviert", cfg["paper_enabled"] is False)
check("Settings: Wert wird auf 60 begrenzt", cfg["rsvp_days_before"] == 60)

viewer_user = User.objects.create_user(email="leser-rem@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True))
viewer = Client()
viewer.force_login(viewer_user)
resp = viewer.post(f"{base}/settings/reminders/", {"invitation_days_before": "1"})
check("Settings ohne manage_settings -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase D: dry-run + Command ===")
tenant.reminder_settings = {}
tenant.save(update_fields=["reminder_settings"])
SessionReminderLog.objects.filter(tenant=tenant).delete()
clear_outbox()

log_count_before = SessionReminderLog.objects.count()
counts_dry = reminder_service.run_for_tenant(tenant, dry_run=True)
check("dry-run: zählt Erinnerungen", any(counts_dry.values()), str(counts_dry))
check("dry-run: versendet nichts", len(outbox()) == 0)
check("dry-run: protokolliert nichts", SessionReminderLog.objects.count() == log_count_before)

clear_outbox()
call_command("send_session_reminders", tenant="erinnerungsstadt")
check("Command versendet Erinnerungen", len(outbox()) > 0, f"got {len(outbox())}")
clear_outbox()
call_command("send_session_reminders", tenant="erinnerungsstadt")
check("Command ist idempotent", len(outbox()) == 0, f"got {len(outbox())}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
