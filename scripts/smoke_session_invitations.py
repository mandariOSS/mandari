# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Ladungs-/Einladungsversand mit Fristen, PDF und ICS (Issue #29).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_invitations.py

Prüft:
- Ladungsfrist aus dem Gremium: Deadline-Berechnung + Fristwarnung im Dashboard
- Empfängerkreis automatisch aus der Gremienbesetzung (Gast nur Ö-Teil)
- Versand: je Empfänger E-Mail mit PDF-Tagesordnung + ICS-Kalenderanhang
- Ö/NÖ: Gäste erhalten die Ö-Fassung (NÖ-TOP-Titel nicht enthalten)
- Versand-Protokollierung: Dispatch + Empfänger + Audit-Eintrag
- Statuswechsel: meeting_state=invitation_sent, invitation_sent_at gesetzt
- Nachtrags-TOPs (nach Erstladung) + Nachladung als eigener Versandtyp
- PDF-/ICS-Downloads (Ö-Variante ohne NÖ-Recht), Tenant-Isolation
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
    SessionInvitationDispatch,
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
# Setup: Tenant, Gremium mit Ladungsfrist, Besetzung, Sitzung mit Ö/NÖ-TOPs
# =============================================================================
tenant = SessionTenant.objects.create(
    name="Stadt Musterstadt",
    slug="musterstadt",
    address="Rathausplatz 1\n12345 Musterstadt",
    contact_email="rathaus@musterstadt.example",
)
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)

org = SessionOrganization.objects.create(
    tenant=tenant,
    name="Hauptausschuss",
    invitation_period_days=7,
    default_meeting_location="Rathaus",
)

# Besetzung: Mitglied, Vorsitz, beratendes Mitglied, Gast, Person ohne E-Mail
p_member = SessionPerson.objects.create(
    tenant=tenant, given_name="Max", family_name="Mitglied", email="mitglied@example.org"
)
p_chair = SessionPerson.objects.create(
    tenant=tenant, given_name="Vera", family_name="Vorsitz", email="vorsitz@example.org"
)
p_advisor = SessionPerson.objects.create(
    tenant=tenant, given_name="Bert", family_name="Berater", email="berater@example.org"
)
p_guest = SessionPerson.objects.create(tenant=tenant, given_name="Gustav", family_name="Gast", email="gast@example.org")
p_no_mail = SessionPerson.objects.create(tenant=tenant, given_name="Olga", family_name="OhneMail", email="")
p_ended = SessionPerson.objects.create(
    tenant=tenant, given_name="Alt", family_name="Ausgeschieden", email="alt@example.org"
)

SessionOrganizationMembership.objects.create(organization=org, person=p_member, role="member")
SessionOrganizationMembership.objects.create(organization=org, person=p_chair, role="chair")
SessionOrganizationMembership.objects.create(organization=org, person=p_advisor, role="advisor", has_voting_rights=False)
SessionOrganizationMembership.objects.create(organization=org, person=p_guest, role="guest", has_voting_rights=False)
SessionOrganizationMembership.objects.create(organization=org, person=p_no_mail, role="member")
SessionOrganizationMembership.objects.create(
    organization=org,
    person=p_ended,
    role="member",
    end_date=timezone.localdate() - timedelta(days=30),
)

start = (timezone.now() + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
meeting = SessionMeeting.objects.create(
    tenant=tenant,
    name="Sitzung des Hauptausschusses",
    organization=org,
    start=start,
    end=start + timedelta(hours=2),
    location="Rathaus Musterstadt",
    room="Sitzungssaal 1",
    meeting_state="scheduled",
    is_public=True,
)
SessionAgendaItem.objects.create(meeting=meeting, number="1", order=1, name="OEFFENTLICHER-TOP-EINS", is_public=True)
SessionAgendaItem.objects.create(meeting=meeting, number="2", order=2, name="OEFFENTLICHER-TOP-ZWEI", is_public=True)
SessionAgendaItem.objects.create(meeting=meeting, number="N1", order=3, name="GEHEIMER-TOP-XYZ", is_public=False)

# Fremd-Tenant-Sitzung für Isolationstest
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdausschuss")
meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=start, is_public=True
)

client = Client()
client.force_login(admin_user)
base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Fristlogik und Versandseite
# =============================================================================
print("=== Phase A: Ladungsfrist und Versandseite ===")

expected_deadline = (timezone.localtime(meeting.start) - timedelta(days=7)).date()
check("Deadline = Sitzung - Ladungsfrist", meeting.invitation_deadline == expected_deadline)
check("Frist noch nicht überfällig", meeting.invitation_overdue is False)

resp = client.get(f"{base}/dashboard/")
check("Dashboard -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Dashboard zeigt Fristwarnung", "Ladung muss bis" in resp.content.decode("utf-8"))

resp = client.get(f"{base}/meetings/{meeting.id}/invitation/")
html = resp.content.decode("utf-8")
check("Versandseite -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Empfänger aus Besetzung gelistet", "Max Mitglied" in html and "Gustav Gast" in html)
check("Person ohne E-Mail wird ausgewiesen", "keine E-Mail hinterlegt" in html)
check("Ausgeschiedenes Mitglied nicht im Empfängerkreis", "Ausgeschieden" not in html)

# Überfällige Sitzung: Frist verstrichen
overdue_meeting = SessionMeeting.objects.create(
    tenant=tenant,
    name="Kurzfristige Sitzung",
    organization=org,
    start=timezone.now() + timedelta(days=2),
    meeting_state="scheduled",
    is_public=True,
)
check("Kurzfristige Sitzung ist überfällig", overdue_meeting.invitation_overdue is True)
resp = client.get(f"{base}/dashboard/")
check("Dashboard zeigt Überfälligkeit", "verstrichen" in resp.content.decode("utf-8"))

# =============================================================================
# Phase B: Erstladung versenden
# =============================================================================
print()
print("=== Phase B: Erstladung ===")

mail.outbox = []
resp = client.post(
    f"{base}/meetings/{meeting.id}/invitation/",
    {"dispatch_type": "invitation", "subject": "", "message": "Bitte pünktlich erscheinen."},
)
check("Versand -> Redirect", resp.status_code == 302, f"got {resp.status_code}")

# 4 Empfänger mit E-Mail (Mitglied, Vorsitz, Berater, Gast); OhneMail + Ausgeschieden nicht
check("4 E-Mails versandt", len(mail.outbox) == 4, f"outbox={len(mail.outbox)}")
recipients = sorted(m.to[0] for m in mail.outbox)
check(
    "Empfängerkreis korrekt",
    recipients == ["berater@example.org", "gast@example.org", "mitglied@example.org", "vorsitz@example.org"],
    str(recipients),
)

by_recipient = {m.to[0]: m for m in mail.outbox}
member_mail = by_recipient["mitglied@example.org"]
guest_mail = by_recipient["gast@example.org"]

check("Betreff enthält Sitzungsnamen", "Sitzung des Hauptausschusses" in member_mail.subject)
check("Anschreiben in E-Mail", "Bitte pünktlich erscheinen." in member_mail.body)

att_names = sorted(name for name, _content, _mime in member_mail.attachments)
check("Anhänge: PDF + ICS", att_names == ["einladung-tagesordnung.pdf", "sitzung.ics"], str(att_names))

pdf_member = next(c for n, c, _m in member_mail.attachments if n.endswith(".pdf"))
pdf_guest = next(c for n, c, _m in guest_mail.attachments if n.endswith(".pdf"))
check("PDF ist gültig (Magic Bytes)", bytes(pdf_member[:4]) == b"%PDF")


def pdf_contains(pdf_bytes, text):
    """Text in PDF-Content-Streams suchen (pypdf-Textextraktion)."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(bytes(pdf_bytes)))
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return text in full_text


check("Mitglied-PDF enthält Ö-TOP", pdf_contains(pdf_member, "OEFFENTLICHER-TOP-EINS"))
check("Mitglied-PDF enthält NÖ-TOP", pdf_contains(pdf_member, "GEHEIMER-TOP-XYZ"))
check("Gast-PDF enthält Ö-TOP", pdf_contains(pdf_guest, "OEFFENTLICHER-TOP-EINS"))
check("Gast-PDF enthält KEINEN NÖ-TOP", not pdf_contains(pdf_guest, "GEHEIMER-TOP-XYZ"))

ics_bytes = next(c for n, c, _m in member_mail.attachments if n.endswith(".ics"))
# Django dekodiert text/*-Anhänge im Outbox-Objekt bereits zu str
ics_text = ics_bytes if isinstance(ics_bytes, str) else bytes(ics_bytes).decode("utf-8")
check("ICS: VCALENDAR/VEVENT", "BEGIN:VCALENDAR" in ics_text and "BEGIN:VEVENT" in ics_text)
check("ICS: DTSTART vorhanden", "DTSTART:" in ics_text)
check("ICS: Ort enthalten", "Rathaus Musterstadt" in ics_text)

meeting.refresh_from_db()
check("meeting_state = invitation_sent", meeting.meeting_state == "invitation_sent")
check("invitation_sent_at gesetzt", meeting.invitation_sent_at is not None)

dispatch = SessionInvitationDispatch.objects.filter(meeting=meeting, dispatch_type="invitation").first()
check("Dispatch protokolliert", dispatch is not None)
check(
    "Empfänger mit Zustellstatus protokolliert",
    dispatch is not None and dispatch.recipients.filter(status="sent").count() == 4,
)
check(
    "Gast als Ö-only protokolliert",
    dispatch is not None
    and dispatch.recipients.get(email="gast@example.org").includes_non_public is False
    and dispatch.recipients.get(email="mitglied@example.org").includes_non_public is True,
)
audit_entry = SessionAuditLog.objects.filter(
    tenant=tenant, action="invitation_sent", object_id=meeting.id, changes__has_key="versandart"
).first()
check("Audit-Eintrag mit Versandzusammenfassung", audit_entry is not None)
check(
    "Audit: Nutzer des Versands erfasst",
    audit_entry is not None and audit_entry.user_id == su_admin.id,
)

# Fristwarnung verschwindet nach Versand
resp = client.get(f"{base}/dashboard/")
check(
    "Fristwarnung nach Versand nur noch für kurzfristige Sitzung",
    "Sitzung des Hauptausschusses" not in resp.content.decode("utf-8").split("Ladungsfristen im Blick")[-1].split("Stats")[0]
    if "Ladungsfristen im Blick" in resp.content.decode("utf-8")
    else True,
)

# =============================================================================
# Phase C: Nachtrags-TOP + Nachladung
# =============================================================================
print()
print("=== Phase C: Nachtrag ===")

# Neuer TOP nach Ladungsversand -> automatisch als Nachtrag markiert (#26-Service)
resp = client.post(f"{base}/meetings/{meeting.id}/agenda/add/", {"name": "NACHTRAGS-TOP-NEU", "is_public": "on"})
supp_item = SessionAgendaItem.objects.filter(meeting=meeting, name="NACHTRAGS-TOP-NEU").first()
check("Neuer TOP nach Ladung ist Nachtrag", supp_item is not None and supp_item.is_supplementary is True)

mail.outbox = []
resp = client.post(f"{base}/meetings/{meeting.id}/invitation/", {"dispatch_type": "supplementary"})
check("Nachladung -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Nachladung: 4 E-Mails", len(mail.outbox) == 4, f"outbox={len(mail.outbox)}")

supp_mail = mail.outbox[0]
check("Nachladung: Betreff 'Nachtrag'", "Nachtrag" in supp_mail.subject)
supp_pdf = next(c for n, c, _m in supp_mail.attachments if n.endswith(".pdf"))
check("Nachtrags-PDF enthält Nachtrags-TOP", pdf_contains(supp_pdf, "NACHTRAGS-TOP-NEU"))
check("Nachtrags-PDF ohne Alt-TOPs", not pdf_contains(supp_pdf, "OEFFENTLICHER-TOP-EINS"))
check(
    "Nachladung als eigener Versandtyp protokolliert",
    SessionInvitationDispatch.objects.filter(meeting=meeting, dispatch_type="supplementary").exists(),
)

# Nachladung vor Erstladung ist gesperrt
mail.outbox = []
resp = client.post(f"{base}/meetings/{overdue_meeting.id}/invitation/", {"dispatch_type": "supplementary"})
check("Nachladung ohne Erstladung abgelehnt", len(mail.outbox) == 0 and resp.status_code == 302)

# =============================================================================
# Phase D: Downloads (PDF/ICS) und Ö/NÖ-Berechtigung
# =============================================================================
print()
print("=== Phase D: Downloads und Ö/NÖ ===")

resp = client.get(f"{base}/meetings/{meeting.id}/agenda.pdf")
check("Agenda-PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
check("Admin-PDF enthält NÖ-TOP", pdf_contains(resp.content, "GEHEIMER-TOP-XYZ"))

resp = client.get(f"{base}/meetings/{meeting.id}/sitzung.ics")
check("ICS-Download -> 200", resp.status_code == 200 and "text/calendar" in resp["Content-Type"])

# Nutzer nur mit view_meetings: PDF ohne NÖ-Teil
viewer_role = SessionRole.objects.create(tenant=tenant, name="Nur-Sitzungen", can_view_meetings=True)
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

resp = viewer.get(f"{base}/meetings/{meeting.id}/agenda.pdf")
check("Viewer-PDF -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Viewer-PDF ohne NÖ-TOP", not pdf_contains(resp.content, "GEHEIMER-TOP-XYZ"))

resp = viewer.get(f"{base}/meetings/{meeting.id}/invitation/")
check("Versandseite ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase E: Tenant-Isolation
# =============================================================================
print()
print("=== Phase E: Tenant-Isolation ===")

for url in (
    f"{base}/meetings/{meeting_b.id}/invitation/",
    f"{base}/meetings/{meeting_b.id}/agenda.pdf",
    f"{base}/meetings/{meeting_b.id}/sitzung.ics",
):
    resp = client.get(url)
    check(f"Fremde Sitzung {url.rsplit('/', 1)[-1] or 'invitation'} -> 404", resp.status_code == 404, f"got {resp.status_code}")

mail.outbox = []
resp = client.post(f"{base}/meetings/{meeting_b.id}/invitation/", {"dispatch_type": "invitation"})
check("Fremder Versand-POST -> 404, keine Mail", resp.status_code == 404 and len(mail.outbox) == 0)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
