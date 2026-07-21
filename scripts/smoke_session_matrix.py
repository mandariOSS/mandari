# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Tenant-Isolations-, Ö/NÖ-Sichtbarkeits- und Permission-Matrix
für das Session RIS (Issue #28, Vorbild: smoke_guest_isolation).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_matrix.py

Prüft:
- Permission-Matrix: alle Session-GET-Views gegen Nutzer mit exakt einer
  Berechtigung (erlaubt/verboten), plus Admin und Nutzer ohne Rechte
- Mutations-Endpunkte verweigern ohne Berechtigung (403) und mutieren nicht
- Tenant-Isolation: Listen/Detail-/API-Views liefern ausschließlich Daten
  des eigenen Tenants; fremde Objekt-IDs unter eigenem Slug -> 404;
  fremder Tenant-Slug -> 403
- Ö/NÖ-Sichtbarkeit: NÖ-Sitzungen, NÖ-Vorlagen, NÖ-TOPs und NÖ-Anlagen
  sind für unberechtigte Rollen unsichtbar — UI und Session-API
- OParl-API liefert ausschließlich is_public-Daten (anonym)

Die Suite schlägt fehl, sobald eine View die Tenant- oder Ö/NÖ-Filterung
oder die Berechtigungsprüfung vergisst.
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
_db_path = _tmp_dir / "smoke.sqlite3"
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

# SQLite-Robustheit unter Windows: laengere Busy-Timeouts gegen
# transiente "database is locked"-Fehler (Virenscanner/Indexer).
from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
_dj_settings.MEDIA_ROOT = str(_tmp_dir / "media")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionFile,
    SessionInvitationDispatch,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
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


# =============================================================================
# Setup: Zwei Tenants mit Ö/NÖ-Daten
# =============================================================================
tenant_a = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

# Tenant A: Daten mit Ö/NÖ-Trennung
org_a = SessionOrganization.objects.create(tenant=tenant_a, name="Hauptausschuss A")
person_a = SessionPerson.objects.create(tenant=tenant_a, given_name="Anna", family_name="Eigen")
meeting_pub = SessionMeeting.objects.create(
    tenant=tenant_a, name="OEFFENTLICHE-SITZUNG-A", organization=org_a, start=timezone.now(), is_public=True
)
meeting_np = SessionMeeting.objects.create(
    tenant=tenant_a, name="GEHEIME-SITZUNG-A", organization=org_a, start=timezone.now(), is_public=False
)
paper_pub = SessionPaper.objects.create(
    tenant=tenant_a, reference="V/2026/1001", name="OEFFENTLICHE-VORLAGE-A", is_public=True
)
paper_np = SessionPaper.objects.create(
    tenant=tenant_a, reference="V/2026/1002", name="GEHEIME-VORLAGE-A", is_public=False
)
top_pub = SessionAgendaItem.objects.create(meeting=meeting_pub, number="1", name="OEFFENTLICHER-TOP-A", is_public=True)
top_np = SessionAgendaItem.objects.create(meeting=meeting_pub, number="N1", name="GEHEIMER-TOP-A", is_public=False)
top_decided = SessionAgendaItem.objects.create(
    meeting=meeting_pub, number="2", name="BESCHLOSSENER-TOP-A", is_public=True, vote_result="approved"
)
app_a = SessionApplication.objects.create(
    tenant=tenant_a,
    title="ANTRAG-A",
    justification="x",
    resolution_proposal="y",
    submitter_name="N",
    submitter_email="n@example.org",
)
consultation_a = SessionConsultation.objects.create(paper=paper_pub, organization=org_a, order=1)
protocol_a = SessionProtocol.objects.create(meeting=meeting_pub, content="Protokoll A")
file_pub = SessionFile.objects.create(
    tenant=tenant_a,
    name="oeffentliche-anlage-a.txt",
    file=SimpleUploadedFile("oeffentliche-anlage-a.txt", b"public content A"),
    is_public=True,
    paper=paper_pub,
)
file_np = SessionFile.objects.create(
    tenant=tenant_a,
    name="geheime-anlage-a.txt",
    file=SimpleUploadedFile("geheime-anlage-a.txt", b"secret content A"),
    is_public=False,
    paper=paper_pub,
)

# Tenant B: Fremddaten mit Markern
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="FREMDGREMIUM-XYZ")
person_b = SessionPerson.objects.create(tenant=tenant_b, given_name="Fritz", family_name="FREMDPERSON-XYZ")
meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="FREMD-SITZUNG-XYZ", organization=org_b, start=timezone.now(), is_public=True
)
paper_b = SessionPaper.objects.create(
    tenant=tenant_b, reference="V/2026/9001", name="FREMD-VORLAGE-XYZ", is_public=True
)
top_b = SessionAgendaItem.objects.create(meeting=meeting_b, number="1", name="FREMD-TOP-XYZ", is_public=True)
app_b = SessionApplication.objects.create(
    tenant=tenant_b,
    title="FREMD-ANTRAG-XYZ",
    justification="x",
    resolution_proposal="y",
    submitter_name="N",
    submitter_email="n@example.org",
)
file_b = SessionFile.objects.create(
    tenant=tenant_b,
    name="fremd-anlage-xyz.txt",
    file=SimpleUploadedFile("fremd-anlage-xyz.txt", b"foreign content B"),
    is_public=True,
    paper=paper_b,
)
membership_b = SessionOrganizationMembership.objects.create(organization=org_b, person=person_b)
consultation_b = SessionConsultation.objects.create(paper=paper_b, organization=org_b, order=1)

# =============================================================================
# Nutzer: Admin, ohne Rechte, je Berechtigung genau ein Nutzer
# =============================================================================

# Alle Permission-Flags des Role-Models (can_* Booleans)
ALL_PERM_FLAGS = [
    field.name[4:] for field in SessionRole._meta.get_fields() if field.name.startswith("can_") and field.concrete
]

# Berechtigungen, die in der GET-Matrix vorkommen
MATRIX_PERMS = [
    "view_dashboard",
    "view_meetings",
    "create_meetings",
    "edit_meetings",
    "view_non_public_meetings",
    "view_papers",
    "create_papers",
    "edit_papers",
    "approve_papers",
    "view_non_public_papers",
    "view_applications",
    "process_applications",
    "view_protocols",
    "edit_protocols",
    "manage_organizations",
    "manage_users",
    "manage_settings",
    "view_audit_log",
]


def make_user(name, perms):
    """Nutzer in Tenant A mit exakt den angegebenen Berechtigungen."""
    flags = {f"can_{p}": False for p in ALL_PERM_FLAGS}
    for p in perms:
        flags[f"can_{p}"] = True
    role = SessionRole.objects.create(tenant=tenant_a, name=f"rolle_{name}", **flags)
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-Test-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant_a)
    su.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


admin_role = SessionRole.objects.create(tenant=tenant_a, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-a@example.org", password="pw-Smoke-Test-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant_a)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

no_perm = make_user("nichts", [])
single = {perm: make_user(f"nur_{perm.replace('_', '-')}", [perm]) for perm in MATRIX_PERMS}
combo_convert = make_user("konverter", ["process_applications", "create_papers"])

b_admin_role = SessionRole.objects.create(tenant=tenant_b, name="Admin B", is_admin=True)
b_user = User.objects.create_user(email="admin-b@example.org", password="pw-Smoke-Test-1!")
su_b = SessionUser.objects.create(user=b_user, tenant=tenant_b)
su_b.roles.add(b_admin_role)
b_admin = Client()
b_admin.force_login(b_user)

base = f"/session/{tenant_a.slug}"

# =============================================================================
# Phase A: Permission-Matrix (GET-Views x Einzelberechtigungs-Nutzer)
# =============================================================================
print("=== Phase A: Permission-Matrix ===")

# (URL, benötigte Berechtigungen)
GET_MATRIX = [
    (f"{base}/dashboard/", {"view_dashboard"}),
    (f"{base}/meetings/", {"view_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/", {"view_meetings"}),
    (f"{base}/meetings/create/", {"create_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/edit/", {"edit_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/invitation/", {"edit_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/agenda.pdf", {"view_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/sitzung.ics", {"view_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/protocol/", {"view_protocols"}),
    (f"{base}/meetings/{meeting_pub.id}/protocol/edit/", {"edit_protocols"}),
    (f"{base}/meetings/{meeting_pub.id}/niederschrift.pdf", {"view_protocols"}),
    (f"{base}/agenda/{top_pub.id}/edit/", {"edit_meetings"}),
    (f"{base}/resolutions/", {"view_meetings"}),
    (f"{base}/agenda/{top_decided.id}/beschlussauszug.pdf", {"view_meetings"}),
    (f"{base}/meetings/{meeting_pub.id}/beschlussauszuege.pdf", {"view_meetings"}),
    (f"{base}/papers/", {"view_papers"}),
    (f"{base}/papers/{paper_pub.id}/", {"view_papers"}),
    (f"{base}/papers/create/", {"create_papers"}),
    (f"{base}/papers/{paper_pub.id}/edit/", {"edit_papers"}),
    (f"{base}/papers/review/", {"approve_papers"}),
    (f"{base}/applications/", {"view_applications"}),
    (f"{base}/applications/{app_a.id}/", {"view_applications"}),
    (f"{base}/applications/{app_a.id}/process/", {"process_applications"}),
    (f"{base}/applications/{app_a.id}/convert/", {"process_applications", "create_papers"}),
    (f"{base}/organizations/", {"view_meetings"}),
    (f"{base}/organizations/{org_a.id}/", {"view_meetings"}),
    (f"{base}/organizations/create/", {"manage_organizations"}),
    (f"{base}/organizations/{org_a.id}/edit/", {"manage_organizations"}),
    (f"{base}/persons/", {"view_meetings"}),
    (f"{base}/persons/{person_a.id}/", {"view_meetings"}),
    (f"{base}/persons/create/", {"manage_organizations"}),
    (f"{base}/persons/{person_a.id}/edit/", {"manage_organizations"}),
    (f"{base}/settings/", {"manage_settings"}),
    (f"{base}/settings/users/", {"manage_users"}),
    (f"{base}/settings/users/invite/", {"manage_users"}),
    (f"{base}/audit/", {"view_audit_log"}),
    (f"{base}/files/{file_pub.id}/download/", {"view_papers"}),
]


def run_matrix(label, client, user_perms):
    mismatches = []
    for url, required in GET_MATRIX:
        expected = 200 if required <= user_perms else 403
        status = client.get(url).status_code
        if status != expected:
            mismatches.append(f"{url}: erwartet {expected}, erhalten {status}")
    check(f"Matrix: {label} ({len(GET_MATRIX)} URLs)", not mismatches, "; ".join(mismatches[:5]))


run_matrix(
    "Admin hat überall Zugriff", admin, set(MATRIX_PERMS) | {"view_non_public_meetings", "view_non_public_papers"}
)
run_matrix("Nutzer ohne Rechte überall 403", no_perm, set())
for perm, client in single.items():
    run_matrix(f"nur {perm}", client, {perm})
run_matrix("Kombination process+create_papers", combo_convert, {"process_applications", "create_papers"})

# =============================================================================
# Phase B: Mutations-Endpunkte ohne Berechtigung
# =============================================================================
print()
print("=== Phase B: Mutationen ohne Berechtigung ===")

mutations = [
    (f"{base}/meetings/create/", {"name": "M", "organization": str(org_a.id), "start": "2026-08-01T10:00"}),
    (f"{base}/meetings/{meeting_pub.id}/invitation/", {"dispatch_type": "invitation"}),
    (f"{base}/papers/create/", {"reference": "V/X", "name": "P", "paper_type": "proposal"}),
    (f"{base}/papers/{paper_pub.id}/workflow/submit/", {}),
    (f"{base}/papers/{paper_pub.id}/workflow/approve/", {}),
    (f"{base}/meetings/{meeting_pub.id}/agenda/add/", {"name": "T"}),
    (f"{base}/meetings/{meeting_pub.id}/attendance/generate/", {}),
    (f"{base}/meetings/{meeting_pub.id}/attendance/add/", {"person": str(person_a.id)}),
    (f"{base}/meetings/{meeting_pub.id}/protocol/create/", {}),
    (f"{base}/meetings/{meeting_pub.id}/protocol/submit/", {}),
    (f"{base}/meetings/{meeting_pub.id}/protocol/approve/", {}),
    (f"{base}/meetings/{meeting_pub.id}/resolutions/generate/", {}),
    (f"{base}/agenda/{top_decided.id}/forwarding/add/", {"recipient": "Bauamt"}),
    (f"{base}/meetings/{meeting_pub.id}/agenda/reorder/", {"order": ""}),
    (f"{base}/agenda/{top_pub.id}/withdraw/", {"reason": "x"}),
    (f"{base}/agenda/{top_pub.id}/delete/", {}),
    (f"{base}/files/upload/", {"target_type": "paper", "target_id": str(paper_pub.id)}),
    (f"{base}/files/{file_pub.id}/delete/", {}),
    (f"{base}/files/{file_pub.id}/update/", {"is_public": "on"}),
    (f"{base}/organizations/create/", {"name": "O", "organization_type": "committee"}),
    (f"{base}/organizations/{org_a.id}/deactivate/", {}),
    (f"{base}/organizations/{org_a.id}/memberships/add/", {"person": str(person_a.id)}),
    (f"{base}/persons/create/", {"given_name": "X", "family_name": "Y"}),
    (f"{base}/persons/{person_a.id}/deactivate/", {}),
    (f"{base}/settings/users/invite/", {"email": "x@example.org"}),
    (f"{base}/applications/{app_a.id}/process/", {"status": "received"}),
    # Beratungsfolge (Issue #34)
    (f"{base}/papers/{paper_pub.id}/consultations/add/", {"organization": str(org_a.id)}),
    (f"{base}/consultations/{consultation_a.id}/update/", {"role": "hearing"}),
    (f"{base}/consultations/{consultation_a.id}/delete/", {}),
    (f"{base}/consultations/{consultation_a.id}/move/", {"direction": "up"}),
    (f"{base}/consultations/{consultation_a.id}/schedule/", {"meeting": str(meeting_pub.id)}),
    (f"{base}/consultations/{consultation_a.id}/forward/", {}),
]

before_counts = (
    SessionMeeting.objects.count(),
    SessionPaper.objects.count(),
    SessionAgendaItem.objects.count(),
    SessionFile.objects.count(),
    SessionOrganization.objects.count(),
    SessionPerson.objects.count(),
    SessionOrganizationMembership.objects.count(),
    SessionInvitationDispatch.objects.count(),
    SessionAttendance.objects.count(),
    SessionConsultation.objects.count(),
)

bad = [url for url, data in mutations if no_perm.post(url, data).status_code != 403]
check("Alle Mutations-Endpunkte ohne Rechte -> 403", not bad, "; ".join(bad[:5]))

after_counts = (
    SessionMeeting.objects.count(),
    SessionPaper.objects.count(),
    SessionAgendaItem.objects.count(),
    SessionFile.objects.count(),
    SessionOrganization.objects.count(),
    SessionPerson.objects.count(),
    SessionOrganizationMembership.objects.count(),
    SessionInvitationDispatch.objects.count(),
    SessionAttendance.objects.count(),
    SessionConsultation.objects.count(),
)
check(
    "Keine Mutation ohne Berechtigung ausgeführt", before_counts == after_counts, f"{before_counts} -> {after_counts}"
)
check("TOP nicht abgesetzt", not SessionAgendaItem.objects.get(pk=top_pub.pk).is_withdrawn)
check("Antrag-Status unverändert", SessionApplication.objects.get(pk=app_a.pk).status == "submitted")

# =============================================================================
# Phase C: Tenant-Isolation
# =============================================================================
print()
print("=== Phase C: Tenant-Isolation ===")

list_urls = [
    f"{base}/dashboard/",
    f"{base}/meetings/",
    f"{base}/papers/",
    f"{base}/applications/",
    f"{base}/organizations/",
    f"{base}/persons/",
    f"{base}/settings/users/",
    f"{base}/audit/",
    f"{base}/api/session/meetings/",
    f"{base}/api/session/papers/",
    f"{base}/api/session/applications/",
    f"{base}/api/oparl/meetings/",
    f"{base}/api/oparl/papers/",
    f"{base}/api/oparl/organizations/",
    f"{base}/api/oparl/people/",
    f"{base}/api/oparl/agendaitems/",
    f"{base}/api/oparl/consultations/",
    f"{base}/api/oparl/files/",
    f"{base}/api/oparl/memberships/",
]
leaks = []
for url in list_urls:
    resp = admin.get(url)
    if resp.status_code != 200:
        leaks.append(f"{url}: status {resp.status_code}")
    elif b"FREMD" in resp.content or b"fremd-anlage" in resp.content:
        leaks.append(f"{url}: Fremddaten sichtbar")
check("Keine Fremddaten in Listen-/API-Views", not leaks, "; ".join(leaks[:5]))

foreign_detail_urls = [
    f"{base}/meetings/{meeting_b.id}/",
    f"{base}/meetings/{meeting_b.id}/edit/",
    f"{base}/meetings/{meeting_b.id}/invitation/",
    f"{base}/meetings/{meeting_b.id}/agenda.pdf",
    f"{base}/meetings/{meeting_b.id}/sitzung.ics",
    f"{base}/meetings/{meeting_b.id}/protocol/",
    f"{base}/meetings/{meeting_b.id}/niederschrift.pdf",
    f"{base}/papers/{paper_b.id}/",
    f"{base}/papers/{paper_b.id}/edit/",
    f"{base}/applications/{app_b.id}/",
    f"{base}/organizations/{org_b.id}/",
    f"{base}/organizations/{org_b.id}/edit/",
    f"{base}/persons/{person_b.id}/",
    f"{base}/persons/{person_b.id}/edit/",
    f"{base}/agenda/{top_b.id}/edit/",
    f"{base}/files/{file_b.id}/download/",
]
wrong = []
for url in foreign_detail_urls:
    status = admin.get(url).status_code
    if status != 404:
        wrong.append(f"{url}: {status}")
check("Fremde Objekt-IDs unter eigenem Slug -> 404", not wrong, "; ".join(wrong[:5]))

# Fremde Mutations-Endpunkte
wrong = []
for url, data in [
    (f"{base}/agenda/{top_b.id}/delete/", {}),
    (f"{base}/files/{file_b.id}/delete/", {}),
    (f"{base}/memberships/{membership_b.id}/end/", {}),
    (f"{base}/organizations/{org_b.id}/deactivate/", {}),
    (f"{base}/meetings/{meeting_b.id}/attendance/generate/", {}),
    (f"{base}/meetings/{meeting_b.id}/resolutions/generate/", {}),
    (f"{base}/agenda/{top_b.id}/forwarding/add/", {"recipient": "Bauamt"}),
    (f"{base}/papers/{paper_b.id}/workflow/submit/", {}),
    (f"{base}/papers/{paper_b.id}/consultations/add/", {"organization": str(org_b.id)}),
    (f"{base}/consultations/{consultation_b.id}/update/", {"role": "hearing"}),
    (f"{base}/consultations/{consultation_b.id}/delete/", {}),
]:
    status = admin.post(url, data).status_code
    if status != 404:
        wrong.append(f"{url}: {status}")
check("Fremde Objekt-Mutationen -> 404", not wrong, "; ".join(wrong[:5]))
check("Fremder TOP existiert weiterhin", SessionAgendaItem.objects.filter(pk=top_b.pk).exists())

# Zugriff auf fremden Tenant-Slug
resp = admin.get(f"/session/{tenant_b.slug}/meetings/")
check("Fremder Tenant-Slug -> 403", resp.status_code == 403, f"got {resp.status_code}")

# B-Admin sieht in B keine A-Daten
resp = b_admin.get(f"/session/{tenant_b.slug}/meetings/")
check(
    "B-Admin sieht keine A-Daten",
    resp.status_code == 200 and b"SITZUNG-A" not in resp.content,
    f"status={resp.status_code}",
)

# =============================================================================
# Phase D: Ö/NÖ-Sichtbarkeit (UI + Session-API)
# =============================================================================
print()
print("=== Phase D: Ö/NÖ-Sichtbarkeit ===")

viewer = single["view_meetings"]  # hat view_meetings, aber NICHT view_non_public_meetings
paper_viewer = single["view_papers"]

resp = viewer.get(f"{base}/meetings/")
check("Sitzungsliste: NÖ-Sitzung unsichtbar", b"GEHEIME-SITZUNG-A" not in resp.content, f"status={resp.status_code}")
check("Sitzungsliste: Ö-Sitzung sichtbar", b"OEFFENTLICHE-SITZUNG-A" in resp.content)

resp = viewer.get(f"{base}/meetings/{meeting_np.id}/")
check("NÖ-Sitzungsdetail ohne NÖ-Recht -> 404", resp.status_code == 404, f"got {resp.status_code}")

resp = viewer.get(f"{base}/meetings/{meeting_pub.id}/")
check("Ö-Sitzungsdetail: NÖ-TOP unsichtbar", b"GEHEIMER-TOP-A" not in resp.content)
check("Ö-Sitzungsdetail: Ö-TOP sichtbar", b"OEFFENTLICHER-TOP-A" in resp.content)

resp = admin.get(f"{base}/meetings/{meeting_pub.id}/")
check("Admin sieht NÖ-TOP", b"GEHEIMER-TOP-A" in resp.content)

# NÖ-Sitzung nicht editierbar ohne NÖ-Recht
editor = single["edit_meetings"]
resp = editor.get(f"{base}/meetings/{meeting_np.id}/edit/")
check("NÖ-Sitzung ohne NÖ-Recht nicht editierbar -> 404", resp.status_code == 404, f"got {resp.status_code}")

resp = paper_viewer.get(f"{base}/papers/")
check("Vorlagenliste: NÖ-Vorlage unsichtbar", b"GEHEIME-VORLAGE-A" not in resp.content)
check("Vorlagenliste: Ö-Vorlage sichtbar", b"OEFFENTLICHE-VORLAGE-A" in resp.content)

resp = paper_viewer.get(f"{base}/papers/{paper_np.id}/")
check("NÖ-Vorlagendetail ohne NÖ-Recht -> 404", resp.status_code == 404, f"got {resp.status_code}")

resp = paper_viewer.get(f"{base}/papers/{paper_pub.id}/")
check("Vorlagendetail: NÖ-Anlage unsichtbar", b"geheime-anlage-a.txt" not in resp.content)
check("Vorlagendetail: Ö-Anlage sichtbar", b"oeffentliche-anlage-a.txt" in resp.content)

resp = paper_viewer.get(f"{base}/files/{file_np.id}/download/")
check("NÖ-Anlagen-Download ohne NÖ-Recht -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Session-API
resp = viewer.get(f"{base}/api/session/meetings/")
check("Session-API: NÖ-Sitzung für Viewer unsichtbar", b"GEHEIME-SITZUNG-A" not in resp.content)
resp = admin.get(f"{base}/api/session/meetings/")
check("Session-API: NÖ-Sitzung für Admin sichtbar", b"GEHEIME-SITZUNG-A" in resp.content)

resp = paper_viewer.get(f"{base}/api/session/papers/")
check("Session-API: NÖ-Vorlage für Viewer unsichtbar", b"GEHEIME-VORLAGE-A" not in resp.content)

resp = no_perm.get(f"{base}/api/session/applications/")
check("Session-API: Anträge ohne Recht -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase E: OParl-API liefert nur is_public-Daten (anonym)
# =============================================================================
print()
print("=== Phase E: OParl-API ===")

anon = Client()
resp = anon.get(f"{base}/api/oparl/meetings/")
check("OParl-Meetings -> 200 (anonym)", resp.status_code == 200, f"got {resp.status_code}")
check("OParl-Meetings: nur öffentliche", b"GEHEIME-SITZUNG-A" not in resp.content)
check("OParl-Meetings: öffentliche enthalten", b"OEFFENTLICHE-SITZUNG-A" in resp.content)
check("OParl-Meetings: kein NÖ-TOP eingebettet", b"GEHEIMER-TOP-A" not in resp.content)

resp = anon.get(f"{base}/api/oparl/papers/")
check("OParl-Papers -> 200 (anonym)", resp.status_code == 200, f"got {resp.status_code}")
check("OParl-Papers: nur öffentliche", b"GEHEIME-VORLAGE-A" not in resp.content)
check("OParl-Papers: öffentliche enthalten", b"OEFFENTLICHE-VORLAGE-A" in resp.content)
check("OParl-Papers: keine NÖ-Anlage", b"geheime-anlage-a" not in resp.content)

resp = anon.get(f"{base}/api/oparl/organizations/")
check("OParl-Organizations: keine Fremddaten", b"FREMDGREMIUM" not in resp.content)

# Alle OParl-Listen anonym erreichbar und frei von NÖ-/Fremd-Markern
markers = (b"GEHEIM", b"geheime-anlage", b"FREMD", b"fremd-anlage")
leaks = []
for segment in ("meetings", "papers", "organizations", "people", "agendaitems", "consultations", "files", "memberships", "legislativeterms"):
    resp = anon.get(f"{base}/api/oparl/{segment}/")
    if resp.status_code != 200:
        leaks.append(f"{segment}: status {resp.status_code}")
    elif any(m in resp.content for m in markers):
        leaks.append(f"{segment}: NÖ-/Fremddaten sichtbar")
check("Alle OParl-Listen: 200 + keine NÖ-/Fremddaten", not leaks, "; ".join(leaks))

# NÖ-Objekt-Endpunkte anonym -> 404 (nie veröffentlicht, kein Tombstone)
wrong = []
for kind, pk in [("meeting", meeting_np.id), ("paper", paper_np.id), ("agendaitem", top_np.id), ("file", file_np.id)]:
    status = anon.get(f"{base}/api/oparl/{kind}/{pk}/").status_code
    if status != 404:
        wrong.append(f"{kind}: {status}")
check("OParl-Objekt-Endpunkte: NÖ-Objekte -> 404", not wrong, "; ".join(wrong))

# NÖ-Datei-Download anonym -> 404
resp = anon.get(f"{base}/api/oparl/file/{file_np.id}/download/")
check("OParl-Datei-Download: NÖ-Anlage -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Anonymer Zugriff auf geschützte Views -> Redirect zum Login (kein Inhalt)
resp = anon.get(f"{base}/meetings/")
check("Anonym: Sitzungsliste -> Login-Redirect", resp.status_code == 302, f"got {resp.status_code}")
resp = anon.get(f"{base}/files/{file_np.id}/download/")
check("Anonym: NÖ-Anlage nicht erreichbar", resp.status_code in (302, 403), f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
