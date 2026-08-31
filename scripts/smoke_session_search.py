# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Übergreifende Suche im Session-Bereich (Issue #45).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_search.py

Prüft:
- Treffer über Vorlagen, Sitzungen, TOPs/Beschlüsse, Protokolle,
  Anlagen (inkl. extrahiertem Text) und Anträge
- Ö/NÖ- und Berechtigungsfilter: NÖ-Inhalte nie für Unberechtigte
- Filter: Trefferart, Gremium, Jahr
- Tenant-Isolation
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

_tmp = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'smoke.sqlite3').as_posix()}"
os.environ["MEDIA_ROOT"] = str(_tmp / "media")
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core.files.base import ContentFile  # noqa: E402
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
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
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
        print(f"  FAIL {name} {detail}")


# =============================================================================
# Setup: alles enthält das Suchwort ORTSDURCHFAHRT
# =============================================================================
now = timezone.now()

tenant = SessionTenant.objects.create(name="Suchstadt", slug="suchstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-such")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
org2 = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")

paper = SessionPaper.objects.create(
    tenant=tenant, reference="SV/1", name="PAPER-ORTSDURCHFAHRT Sanierung",
    main_text="Die Ortsdurchfahrt wird saniert.", is_public=True,
    main_organization=org,
)
paper_np = SessionPaper.objects.create(
    tenant=tenant, reference="SV/2", name="NP-PAPER-ORTSDURCHFAHRT",
    is_public=False,
)
meeting = SessionMeeting.objects.create(
    tenant=tenant, name="MEETING-ORTSDURCHFAHRT", organization=org,
    start=now - timedelta(days=30), is_public=True,
)
meeting_np = SessionMeeting.objects.create(
    tenant=tenant, name="NP-MEETING-ORTSDURCHFAHRT", organization=org2,
    start=now - timedelta(days=20), is_public=False,
)
item = SessionAgendaItem.objects.create(
    meeting=meeting, number="1", order=1, name="TOP-ORTSDURCHFAHRT",
    vote_result="approved", resolution_number="B/2026/0042",
    resolution_text="Die Ortsdurchfahrt wird beschlossen.",
)
SessionAgendaItem.objects.create(
    meeting=meeting_np, number="1", order=1, name="NP-TOP-ORTSDURCHFAHRT", is_public=False,
)
protocol = SessionProtocol.objects.create(
    meeting=meeting, content="Protokoll zur ORTSDURCHFAHRT ohne Aussprache.",
)
f = SessionFile(
    tenant=tenant, name="anlage-plan.pdf", is_public=True, paper=paper,
    text_content="Lageplan der ORTSDURCHFAHRT mit Details.",
)
f.file.save("anlage-plan.pdf", ContentFile(b"%PDF-1.4 dummy"), save=True)
f_np = SessionFile(
    tenant=tenant, name="np-anlage.pdf", is_public=False, meeting=meeting_np,
    text_content="Geheimer Plan ORTSDURCHFAHRT.",
)
f_np.file.save("np-anlage.pdf", ContentFile(b"%PDF-1.4 dummy"), save=True)
application = SessionApplication.objects.create(
    tenant=tenant, reference="A/1", title="ANTRAG-ORTSDURCHFAHRT Tempo 30",
    submitter_name="Vera Beispiel", status="submitted", submitted_at=now,
)

# Fremd-Mandant
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdrat")
SessionPaper.objects.create(tenant=tenant_b, reference="F/1", name="FREMD-ORTSDURCHFAHRT")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-such@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(
    tenant=tenant, name="Leser",
    can_view_meetings=True, can_view_papers=True, can_view_protocols=True,
    can_view_applications=False,
)
viewer_user = User.objects.create_user(email="leser-such@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Treffer über alle Kategorien (Admin) ===")
resp = admin.get(f"{base}/search/?q=ortsdurchfahrt")
html = resp.content.decode("utf-8")
check("Suche -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Vorlage gefunden", "PAPER-ORTSDURCHFAHRT" in html)
check("NÖ-Vorlage für Admin gefunden", "NP-PAPER-ORTSDURCHFAHRT" in html)
check("Sitzung gefunden", "MEETING-ORTSDURCHFAHRT" in html)
check("Beschluss gefunden (inkl. Nummer)", "TOP-ORTSDURCHFAHRT" in html and "B/2026/0042" in html)
check("Protokoll gefunden", "Niederschrift: MEETING-ORTSDURCHFAHRT" in html)
check("Anlage über Textinhalt gefunden", "anlage-plan.pdf" in html)
check("Antrag gefunden", "ANTRAG-ORTSDURCHFAHRT" in html)
check("Keine Fremddaten", "FREMD-ORTSDURCHFAHRT" not in html)

# Beschlussnummern-Suche
resp = admin.get(f"{base}/search/?q=B/2026/0042")
check("Suche nach Beschlussnummer", "TOP-ORTSDURCHFAHRT" in resp.content.decode("utf-8"))

# =============================================================================
print()
print("=== Phase B: Ö/NÖ und Berechtigungen ===")
resp = viewer.get(f"{base}/search/?q=ortsdurchfahrt")
html_v = resp.content.decode("utf-8")
check("Viewer -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Viewer: Ö-Vorlage gefunden", "PAPER-ORTSDURCHFAHRT" in html_v)
check("Viewer: KEINE NÖ-Vorlage", "NP-PAPER-ORTSDURCHFAHRT" not in html_v)
check("Viewer: KEINE NÖ-Sitzung", "NP-MEETING-ORTSDURCHFAHRT" not in html_v)
check("Viewer: KEIN NÖ-TOP", "NP-TOP-ORTSDURCHFAHRT" not in html_v)
check("Viewer: KEINE NÖ-Anlage", "np-anlage.pdf" not in html_v)
check("Viewer ohne view_applications: kein Antrag", "ANTRAG-ORTSDURCHFAHRT" not in html_v)

# =============================================================================
print()
print("=== Phase C: Filter ===")
resp = admin.get(f"{base}/search/?q=ortsdurchfahrt&kind=papers")
html = resp.content.decode("utf-8")
check("Filter Trefferart: nur Vorlagen", "PAPER-ORTSDURCHFAHRT" in html and "MEETING-ORTSDURCHFAHRT (" not in html and "Sitzungen (" not in html)

resp = admin.get(f"{base}/search/?q=ortsdurchfahrt&kind=meetings&organization={org.id}")
html = resp.content.decode("utf-8")
check("Filter Gremium: Rat-Sitzung", "MEETING-ORTSDURCHFAHRT" in html)
resp = admin.get(f"{base}/search/?q=ortsdurchfahrt&kind=meetings&organization={org2.id}")
html = resp.content.decode("utf-8")
check("Filter Gremium: NÖ-Sitzung des Bauausschusses", "NP-MEETING-ORTSDURCHFAHRT" in html and "MEETING-ORTSDURCHFAHRT (" not in html.replace("NP-MEETING-ORTSDURCHFAHRT", ""))

year = (now - timedelta(days=30)).year
resp = admin.get(f"{base}/search/?q=ortsdurchfahrt&kind=meetings&year={year - 3}")
check("Filter Jahr ohne Treffer", "MEETING-ORTSDURCHFAHRT" not in resp.content.decode("utf-8"))

resp = admin.get(f"{base}/search/?q=x")
check("Zu kurze Suchanfrage -> keine Ergebnisse", "Treffer für" not in resp.content.decode("utf-8"))

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
