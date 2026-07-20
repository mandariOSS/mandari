# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Niederschrift-Workflow (Issue #31).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_protocols.py

Prüft:
- Protokoll anlegen (vorbefüllt), TOP-weise Protokolltexte +
  Beschlussergebnisse bearbeiten (Ö/NÖ getrennt, NÖ verschlüsselt)
- Workflow Entwurf -> Prüfung -> genehmigt (Genehmigungsvermerk in
  Folgesitzung) -> veröffentlicht; Zurückweisen mit Kommentar
- Ungültige Statusübergänge werden abgelehnt; genehmigte Niederschriften
  sind nicht mehr bearbeitbar
- Niederschrift-PDF: Ö-Fassung enthält NIEMALS NÖ-Inhalte; interne Fassung
  nur mit Berechtigung
- Teilnehmerverzeichnis aus der Anwesenheitserfassung
- Audit-Einträge (approve/publish/Zurückweisung) und Tenant-Isolation
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
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
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

start = (timezone.now() - timedelta(days=2)).replace(hour=17, minute=0, second=0, microsecond=0)
meeting = SessionMeeting.objects.create(
    tenant=tenant,
    name="Sitzung des Hauptausschusses",
    organization=org,
    start=start,
    location="Rathaus",
    meeting_state="completed",
    is_public=True,
)
followup = SessionMeeting.objects.create(
    tenant=tenant,
    name="Folgesitzung des Hauptausschusses",
    organization=org,
    start=start + timedelta(days=28),
    is_public=True,
)
top1 = SessionAgendaItem.objects.create(meeting=meeting, number="1", order=1, name="OEFFENTLICHER-TOP-EINS")
top_np = SessionAgendaItem.objects.create(
    meeting=meeting, number="N1", order=2, name="GEHEIMER-TOP-XYZ", is_public=False
)

p1 = SessionPerson.objects.create(tenant=tenant, given_name="Vera", family_name="Vorsitz")
p2 = SessionPerson.objects.create(tenant=tenant, given_name="Emil", family_name="Entschuldigt")
SessionAttendance.objects.create(meeting=meeting, person=p1, status="present", role="chair")
SessionAttendance.objects.create(meeting=meeting, person=p2, status="excused")

org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdausschuss")
meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=start, is_public=True
)


def make_client(name, **flags):
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle_{name}", **flags)
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-Test-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    c = Client()
    c.force_login(user)
    return c, su


recorder, su_recorder = make_client(
    "protokollant",
    can_view_meetings=True,
    can_view_non_public_meetings=True,
    can_view_protocols=True,
    can_create_protocols=True,
    can_edit_protocols=True,
)
approver, su_approver = make_client(
    "genehmiger",
    can_view_meetings=True,
    can_view_non_public_meetings=True,
    can_view_protocols=True,
    can_approve_protocols=True,
)
viewer, su_viewer = make_client("leser", can_view_meetings=True, can_view_protocols=True)

base = f"/session/{tenant.slug}"
prot_url = f"{base}/meetings/{meeting.id}/protocol"

# =============================================================================
# Phase A: Anlegen + Bearbeiten
# =============================================================================
print("=== Phase A: Anlegen und Bearbeiten ===")

resp = recorder.get(f"{prot_url}/")
check("Protokollseite ohne Protokoll -> 200 mit Anlegen-Option", resp.status_code == 200 and b"Protokoll anlegen" in resp.content)

resp = recorder.post(f"{prot_url}/create/")
protocol = SessionProtocol.objects.filter(meeting=meeting).first()
check("Protokoll angelegt", protocol is not None)
check("Vorbefüllt mit Sitzungsdaten", protocol is not None and "Sitzung des Hauptausschusses" in protocol.content)
check("Status Entwurf", protocol is not None and protocol.status == "draft")

# Bearbeiten: allgemeiner Teil + TOP-weise Texte (inkl. NÖ verschlüsselt)
resp = recorder.post(
    f"{prot_url}/edit/",
    {
        "content": "Allgemeiner Verlauf der Sitzung.",
        "content_np": "GEHEIME-ALLGEMEINE-NOTIZ",
        "chair_name": "Vera Vorsitz",
        "recorder_name": "Petra Protokoll",
        f"protocol_note_{top1.pk}": "Ausführliche Debatte zum Spielplatz.",
        f"resolution_text_{top1.pk}": "Der Ausschuss stimmt der Vorlage zu.",
        f"vote_result_{top1.pk}": "approved",
        f"votes_yes_{top1.pk}": "7",
        f"votes_no_{top1.pk}": "2",
        f"votes_abstain_{top1.pk}": "1",
        f"protocol_note_{top_np.pk}": "",
        f"protocol_note_np_{top_np.pk}": "GEHEIMER-WORTBEITRAG-XYZ",
        f"vote_result_{top_np.pk}": "noted",
        f"votes_yes_{top_np.pk}": "0",
        f"votes_no_{top_np.pk}": "0",
        f"votes_abstain_{top_np.pk}": "0",
    },
)
check("Bearbeiten -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
protocol.refresh_from_db()
top1.refresh_from_db()
top_np.refresh_from_db()
check("Allgemeiner Teil gespeichert", protocol.content == "Allgemeiner Verlauf der Sitzung.")
check("NÖ-Teil über Accessor lesbar", protocol.get_content_decrypted() == "GEHEIME-ALLGEMEINE-NOTIZ")
check("Unterschriften-Block gespeichert", protocol.chair_name == "Vera Vorsitz" and protocol.recorder_name == "Petra Protokoll")
check("TOP-Protokolltext gespeichert", top1.protocol_note == "Ausführliche Debatte zum Spielplatz.")
check("TOP-Beschlussergebnis gespeichert", top1.vote_result == "approved" and top1.votes_yes == 7 and top1.votes_no == 2)
check("NÖ-Protokolltext verschlüsselt gespeichert", top_np.get_protocol_note_decrypted() == "GEHEIMER-WORTBEITRAG-XYZ")
check("NÖ-Protokolltext nicht im Klartext-Feld", "GEHEIMER-WORTBEITRAG" not in (top_np.protocol_note or ""))

# =============================================================================
# Phase B: Workflow
# =============================================================================
print()
print("=== Phase B: Workflow ===")

# Ungültiger Übergang: publish aus draft
resp = approver.post(f"{prot_url}/publish/")
protocol.refresh_from_db()
check("Publish aus Entwurf abgelehnt", protocol.status == "draft")

# Approve ohne Berechtigung
resp = viewer.post(f"{prot_url}/approve/")
check("Approve ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Entwurf -> Prüfung
resp = recorder.post(f"{prot_url}/submit/")
protocol.refresh_from_db()
check("Zur Prüfung gegeben", protocol.status == "review")
check("Prüfungs-Metadaten gesetzt", protocol.review_requested_by_id == su_recorder.id and protocol.review_requested_at)

# Zurückweisen mit Kommentar
resp = approver.post(f"{prot_url}/reject/", {"comment": "Bitte TOP 1 präzisieren."})
protocol.refresh_from_db()
check("Zurückgewiesen -> Entwurf", protocol.status == "draft")
reject_entry = SessionAuditLog.objects.filter(
    tenant=tenant, model_name="SessionProtocol", changes__has_key="zurueckweisungs_kommentar"
).first()
check("Audit: Zurückweisung mit Kommentar", reject_entry is not None and "präzisieren" in str(reject_entry.changes))

# Erneut einreichen und genehmigen (mit Folgesitzung)
recorder.post(f"{prot_url}/submit/")
resp = approver.post(f"{prot_url}/approve/", {"approval_meeting": str(followup.id)})
protocol.refresh_from_db()
check("Genehmigt", protocol.status == "approved")
check("Genehmigt von/am gesetzt", protocol.approved_by_id == su_approver.id and protocol.approved_at)
check("Genehmigungsvermerk mit Folgesitzung", protocol.approval_meeting_id == followup.id)
check(
    "Genehmigungsvermerk-Text gesetzt",
    "Genehmigt in der Sitzung am" in protocol.approval_note,
    f"note={protocol.approval_note!r}",
)
check(
    "Audit: approve-Aktion",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionProtocol", action="approve").exists(),
)

# Genehmigte Niederschrift nicht mehr bearbeitbar
resp = recorder.get(f"{prot_url}/edit/")
check("Edit nach Genehmigung -> Redirect", resp.status_code == 302)
resp = recorder.post(f"{prot_url}/edit/", {"content": "MANIPULATION"})
protocol.refresh_from_db()
check("Kein Schreiben nach Genehmigung", protocol.content != "MANIPULATION")

# Veröffentlichen
resp = approver.post(f"{prot_url}/publish/")
protocol.refresh_from_db()
check("Veröffentlicht", protocol.status == "published" and protocol.published_at)
check(
    "Audit: publish-Aktion",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionProtocol", action="publish").exists(),
)

# =============================================================================
# Phase C: PDF Ö/NÖ
# =============================================================================
print()
print("=== Phase C: Niederschrift-PDF ===")

resp = viewer.get(f"{base}/meetings/{meeting.id}/niederschrift.pdf")
check("Ö-PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text_public = pdf_text(resp.content)
check("Ö-PDF: öffentlicher TOP enthalten", "OEFFENTLICHER-TOP-EINS" in text_public)
check("Ö-PDF: Abstimmungsergebnis enthalten", "Ja: 7" in text_public)
check("Ö-PDF: Teilnehmerverzeichnis enthalten", "Vorsitz" in text_public and "Entschuldigt" in text_public)
check("Ö-PDF: KEIN NÖ-TOP-Titel", "GEHEIMER-TOP-XYZ" not in text_public)
check("Ö-PDF: KEIN NÖ-Wortbeitrag", "GEHEIMER-WORTBEITRAG-XYZ" not in text_public)
check("Ö-PDF: KEINE NÖ-Allgemeinnotiz", "GEHEIME-ALLGEMEINE-NOTIZ" not in text_public)
check("Ö-PDF: Genehmigungsvermerk", "Genehmigt in der Sitzung am" in text_public)

resp = recorder.get(f"{base}/meetings/{meeting.id}/niederschrift.pdf?fassung=intern")
check("Interne Fassung -> 200", resp.status_code == 200, f"got {resp.status_code}")
text_internal = pdf_text(resp.content)
check("Intern: NÖ-TOP enthalten", "GEHEIMER-TOP-XYZ" in text_internal)
check("Intern: NÖ-Wortbeitrag enthalten", "GEHEIMER-WORTBEITRAG-XYZ" in text_internal)
check("Intern: NÖ-Allgemeinnotiz enthalten", "GEHEIME-ALLGEMEINE-NOTIZ" in text_internal)

resp = viewer.get(f"{base}/meetings/{meeting.id}/niederschrift.pdf?fassung=intern")
check("Interne Fassung ohne NÖ-Recht -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase D: Berechtigungen und Tenant-Isolation
# =============================================================================
print()
print("=== Phase D: Berechtigungen und Isolation ===")

resp = viewer.post(f"{prot_url}/create/")
check("Create ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.get(f"{prot_url}/edit/")
check("Edit ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.get(f"{prot_url}/")
check("Ansicht mit view_protocols -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Ansicht ohne NÖ-Recht: kein NÖ-Inhalt", b"GEHEIMER-WORTBEITRAG-XYZ" not in resp.content)

for url in (f"{base}/meetings/{meeting_b.id}/protocol/", f"{base}/meetings/{meeting_b.id}/niederschrift.pdf"):
    resp = recorder.get(url)
    check(f"Fremde Sitzung {url.rsplit('/', 2)[-2:]} -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = recorder.post(f"{base}/meetings/{meeting_b.id}/protocol/create/")
check("Fremdes Protokoll-Create -> 404", resp.status_code == 404 and not SessionProtocol.objects.filter(meeting=meeting_b).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
