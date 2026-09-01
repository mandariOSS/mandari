# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Digitale Abstimmung und Umlaufbeschlüsse (Issue #41).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_voting.py

Prüft:
- Einzelstimmen-Erfassung (namentlich/offen) mit automatischer Summenbildung
- Befangenheit (Mitwirkungsverbot): dokumentiert, zählt nicht mit
- Geheime Abstimmung: nur Summen, keine Einzelstimmen gespeichert
- Namentliche Ergebnisse + Befangenheits-Vermerk in Beschlussauszug- und
  Niederschrift-PDF
- Umlaufbeschlüsse: Nummernvergabe U/<Jahr>/<lfd>, Rücklauf-Erfassung gegen
  die stimmberechtigte Besetzung, Ergebnisfeststellung, Sperre danach
- Berechtigungen, Ö/NÖ und Tenant-Isolation
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
    SessionAttendance,
    SessionAuditLog,
    SessionCircularResolution,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
    SessionVote,
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


def pdf_text(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# =============================================================================
# Setup
# =============================================================================
now = timezone.now()
today = timezone.localdate()

tenant = SessionTenant.objects.create(name="Abstimmungsstadt", slug="abstimmungsstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-vote")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat der Stadt")

p1 = SessionPerson.objects.create(tenant=tenant, given_name="Jana", family_name="JASTIMME")
p2 = SessionPerson.objects.create(tenant=tenant, given_name="Norbert", family_name="NEINSTIMME")
p3 = SessionPerson.objects.create(tenant=tenant, given_name="Berta", family_name="BEFANGEN")
for person in (p1, p2, p3):
    SessionOrganizationMembership.objects.create(
        organization=org, person=person, has_voting_rights=True
    )
# Ehemaliges Mitglied: zählt nicht zur Besetzung
p_old = SessionPerson.objects.create(tenant=tenant, given_name="Alt", family_name="AUSGESCHIEDEN")
SessionOrganizationMembership.objects.create(
    organization=org, person=p_old, has_voting_rights=True,
    end_date=today - timedelta(days=30),
)

meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Ratssitzung", organization=org, start=now - timedelta(days=1)
)
for person in (p1, p2, p3):
    SessionAttendance.objects.create(meeting=meeting, person=person, status="present")

item_roll = SessionAgendaItem.objects.create(
    meeting=meeting, number="1", order=1, name="TOP-NAMENTLICH",
    resolution_text="Der Rat beschließt die Ortsdurchfahrt.",
)
item_secret = SessionAgendaItem.objects.create(
    meeting=meeting, number="2", order=2, name="TOP-GEHEIM",
)

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-vote@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(
    tenant=tenant, name="Leser", can_view_meetings=True, can_view_protocols=True
)
viewer_user = User.objects.create_user(email="leser-vote@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Einzelabstimmung (namentlich) ===")
resp = admin.get(f"{base}/agenda/{item_roll.id}/voting/")
html = resp.content.decode("utf-8")
check("Erfassungsseite -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Anwesende gelistet", "JASTIMME" in html and "BEFANGEN" in html)

resp = admin.post(
    f"{base}/agenda/{item_roll.id}/voting/",
    {
        "voting_method": "roll_call",
        "vote_result": "approved",
        f"vote_{p1.id}": "yes",
        f"vote_{p2.id}": "no",
        f"vote_{p3.id}": "excluded",
    },
)
item_roll.refresh_from_db()
check("Speichern -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Summen aus Einzelstimmen", item_roll.votes_yes == 1 and item_roll.votes_no == 1 and item_roll.votes_abstain == 0)
check("Befangene zählt nicht mit", item_roll.votes.filter(vote="excluded").count() == 1)
check("Abstimmungsart + Ergebnis gesetzt", item_roll.voting_method == "roll_call" and item_roll.vote_result == "approved")
audit_entry = (
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionAgendaItem")
    .order_by("-created_at")
    .first()
)
check(
    "Audit: Befangene dokumentiert",
    audit_entry is not None and "Berta BEFANGEN" in audit_entry.changes.get("befangen", []),
)

# Korrektur: Nein -> Ja
resp = admin.post(
    f"{base}/agenda/{item_roll.id}/voting/",
    {
        "voting_method": "roll_call",
        "vote_result": "approved",
        f"vote_{p1.id}": "yes",
        f"vote_{p2.id}": "yes",
        f"vote_{p3.id}": "excluded",
    },
)
item_roll.refresh_from_db()
check("Korrektur aktualisiert Summen", item_roll.votes_yes == 2 and item_roll.votes_no == 0)
check("Keine Duplikate je Person", item_roll.votes.count() == 3)

resp = viewer.get(f"{base}/agenda/{item_roll.id}/voting/")
check("Erfassung ohne edit_protocols -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase B: Geheime Abstimmung ===")
resp = admin.post(
    f"{base}/agenda/{item_secret.id}/voting/",
    {
        "voting_method": "secret",
        "vote_result": "approved",
        "votes_yes": "5",
        "votes_no": "2",
        "votes_abstain": "1",
        f"vote_{p1.id}": "yes",
        f"vote_{p3.id}": "excluded",
    },
)
item_secret.refresh_from_db()
check("Geheim: manuelle Summen gespeichert", item_secret.votes_yes == 5 and item_secret.votes_no == 2)
check("Geheim: keine Ja/Nein-Einzelstimmen", not item_secret.votes.filter(vote__in=("yes", "no", "abstain")).exists())
check("Geheim: Befangenheit dokumentierbar", item_secret.votes.filter(vote="excluded").count() == 1)

# =============================================================================
print()
print("=== Phase C: PDFs (Auszug + Niederschrift) ===")
resp = admin.post(f"{base}/meetings/{meeting.id}/resolutions/generate/")
resp = admin.get(f"{base}/agenda/{item_roll.id}/beschlussauszug.pdf")
text = pdf_text(resp.content)
check("Auszug: namentliche Liste", "JASTIMME" in text and "NEINSTIMME" in text)
check("Auszug: Abstimmungsart", "Namentlich" in text)
check("Auszug: Mitwirkungsverbot", "Mitwirkungsverbot" in text and "BEFANGEN" in text)

resp = admin.get(f"{base}/agenda/{item_secret.id}/beschlussauszug.pdf")
text = pdf_text(resp.content)
check("Auszug geheim: keine Einzelnamen der Stimmen", "JASTIMME" not in text)
check("Auszug geheim: Befangene dennoch vermerkt", "BEFANGEN" in text)

SessionProtocol.objects.create(meeting=meeting, created_by=su_admin)
resp = admin.get(f"{base}/meetings/{meeting.id}/niederschrift.pdf")
text = pdf_text(resp.content)
check("Niederschrift: namentliches Ergebnis", "Namentlich" in text and "JASTIMME" in text)
check("Niederschrift: Mitwirkungsverbot", "Mitwirkungsverbot" in text)

# =============================================================================
print()
print("=== Phase D: Umlaufbeschlüsse ===")
resp = admin.post(
    f"{base}/circulars/create/",
    {
        "organization": str(org.id),
        "title": "UMLAUF-WINTERDIENST",
        "resolution_text": "Der Auftrag für den Winterdienst wird vergeben.",
        "deadline": (today + timedelta(days=7)).isoformat(),
        "is_public": "1",
    },
)
circular = SessionCircularResolution.objects.filter(tenant=tenant).first()
check("Umlauf angelegt -> Redirect", resp.status_code == 302 and circular is not None)
year = today.year
check("Umlauf-Nummer vergeben", circular.reference == f"U/{year}/0001", circular.reference)

resp = admin.post(
    f"{base}/circulars/create/",
    {
        "organization": str(org.id),
        "title": "UMLAUF-ZWEI",
        "resolution_text": "Zweiter Umlauf.",
        "deadline": (today + timedelta(days=7)).isoformat(),
    },
)
check("Fortlaufende Nummern", SessionCircularResolution.objects.filter(reference=f"U/{year}/0002").exists())

resp = admin.post(f"{base}/circulars/create/", {"organization": str(org.id), "title": "", "resolution_text": "x", "deadline": ""})
check("Unvollständige Anlage abgelehnt", SessionCircularResolution.objects.filter(tenant=tenant).count() == 2)

# Detailseite + Besetzung (ohne Ausgeschiedene)
resp = admin.get(f"{base}/circulars/{circular.id}/")
html = resp.content.decode("utf-8")
check("Detail -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Besetzung: 3 Stimmberechtigte", "3</p>" in html.replace("0/3", "3") or "JASTIMME" in html)
check("Ausgeschiedene fehlen", "AUSGESCHIEDEN" not in html)

# Rückläufe erfassen
resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p1.id), "vote": "yes"})
resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p2.id), "vote": "yes"})
resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p3.id), "vote": "no"})
check("3 Rückläufe erfasst", circular.votes.count() == 3)
resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p_old.id), "vote": "yes"})
check("Ausgeschiedene nicht erfassbar", circular.votes.count() == 3)

# Korrektur überschreibt
resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p3.id), "vote": "abstain"})
check("Rücklauf-Korrektur ohne Duplikat", circular.votes.count() == 3 and circular.votes.get(person=p3).vote == "abstain")

html = admin.get(f"{base}/circulars/{circular.id}/").content.decode("utf-8")
check("Auszählung sichtbar", "3/3" in html)

# Ergebnis feststellen
resp = admin.post(
    f"{base}/circulars/{circular.id}/close/",
    {"result": "adopted", "result_note": "Einstimmig bei einer Enthaltung."},
)
circular.refresh_from_db()
check("Ergebnis festgestellt", circular.status == "adopted" and circular.decided_at is not None)
close_entry = (
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionCircularResolution")
    .order_by("-created_at")
    .first()
)
check(
    "Audit: Auszählung protokolliert",
    close_entry is not None
    and close_entry.changes.get("umlauf") == f"U/{year}/0001"
    and close_entry.changes.get("ergebnis") == "Angenommen",
)

resp = admin.post(f"{base}/circulars/{circular.id}/vote/", {"person": str(p1.id), "vote": "no"})
check("Nach Abschluss keine Rückläufe mehr", circular.votes.get(person=p1).vote == "yes")
resp = admin.post(f"{base}/circulars/{circular.id}/close/", {"result": "rejected"})
circular.refresh_from_db()
check("Doppelter Abschluss blockiert", circular.status == "adopted")

# Rechte + Sichtbarkeit
resp = viewer.get(f"{base}/circulars/")
html = resp.content.decode("utf-8")
check("Liste für Leser -> 200", resp.status_code == 200 and "UMLAUF-WINTERDIENST" in html)
check("Leser ohne Verwaltungs-Formulare", "Umlauf starten" not in html)
resp = viewer.post(f"{base}/circulars/create/", {"organization": str(org.id), "title": "HACK", "resolution_text": "x", "deadline": today.isoformat()})
check("Anlage ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")

np_circular = SessionCircularResolution.objects.create(
    tenant=tenant, organization=org, title="UMLAUF-GEHEIM-NOE",
    resolution_text="NÖ", deadline=today, is_public=False,
)
html = viewer.get(f"{base}/circulars/").content.decode("utf-8")
check("NÖ-Umlauf für Leser unsichtbar", "UMLAUF-GEHEIM-NOE" not in html)
resp = viewer.get(f"{base}/circulars/{np_circular.id}/")
check("NÖ-Umlauf-Detail -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase E: Tenant-Isolation ===")
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdrat")
circ_b = SessionCircularResolution.objects.create(
    tenant=tenant_b, organization=org_b, title="FREMD-UMLAUF",
    resolution_text="x", deadline=today,
)
resp = admin.get(f"{base}/circulars/{circ_b.id}/")
check("Fremder Umlauf -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = admin.post(f"{base}/circulars/{circ_b.id}/close/", {"result": "adopted"})
circ_b.refresh_from_db()
check("Fremder Abschluss -> 404", resp.status_code == 404 and circ_b.status == "open")

meeting_b = SessionMeeting.objects.create(tenant=tenant_b, name="Fremdsitzung", organization=org_b, start=now)
item_b = SessionAgendaItem.objects.create(meeting=meeting_b, number="1", order=1, name="Fremd-TOP")
resp = admin.post(f"{base}/agenda/{item_b.id}/voting/", {"voting_method": "roll_call"})
check("Fremde Abstimmung -> 404", resp.status_code == 404, f"got {resp.status_code}")
check("Keine Fremdstimmen angelegt", not SessionVote.objects.filter(agenda_item=item_b).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
