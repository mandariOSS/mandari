# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Gäste im Editor (Issue #76) und Freigaben-Übersicht (Issue #77).

- Gast mit Stufe „Bearbeiten“ sieht keine Verwaltungsaktionen (Status,
  Dokumenttyp, KI, Zuständigkeit, Themen, Frist, Freigabe anfordern, Papierkorb),
  wohl aber Speichern, Kommentare und Anhänge (Download über geschützte View)
- Mitglied mit Bearbeiten sieht diese Aktionen weiterhin
- Mitglieds-Detailseite eines Gastes: Dokument- und Ordner-Freigaben mit Stufe,
  Quelle, Datum, Zähler; Entzug führt zurück und entfernt die Freigabe
- Gästeliste zeigt Zähler
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'smoke.sqlite3').as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
_dj_settings.MEDIA_ROOT = str(_tmp / "media")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionDocument, MotionShare  # noqa: E402
from insight_core.models import OParlBody, OParlSource  # noqa: E402

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


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def html(resp):
    return resp.content.decode("utf-8", errors="replace")


# =============================================================================
print("=== Setup ===")
source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Testhausen")
org = Organization.objects.create(name="Fraktion A", slug="fraktion-a", body=body)
admin_role = Role.objects.filter(organization=org, is_admin=True).first()

user_admin = User.objects.create_user(
    email="admin@example.org", password="test1234!", first_name="Alma", last_name="Admin"
)
m_admin = Membership.objects.create(user=user_admin, organization=org)
m_admin.roles.add(admin_role)
user_member = User.objects.create_user(
    email="member@example.org", password="test1234!", first_name="Max", last_name="Mitglied"
)
m_member = Membership.objects.create(user=user_member, organization=org)
user_guest = User.objects.create_user(
    email="gast@example.org", password="test1234!", first_name="Gerda", last_name="Gast"
)
m_guest = Membership.objects.create(user=user_guest, organization=org, is_guest=True)

c_admin = client_for(user_admin)
c_member = client_for(user_member)
c_guest = client_for(user_guest)
BASE = f"/work/{org.slug}"

folder = DocumentFolder.objects.create(organization=org, name="Projekte", created_by=m_admin)
subfolder = DocumentFolder.objects.create(organization=org, name="2027", parent=folder, created_by=m_admin)
doc = Motion.objects.create(
    organization=org, author=m_admin, title="Haushaltsentwurf", visibility="private", status="internal_review"
)
doc_in_folder = Motion.objects.create(
    organization=org, author=m_admin, title="Projektplan", visibility="private", folder=subfolder
)
Motion.objects.create(organization=org, author=m_admin, title="Ordnerdoku", visibility="private", folder=folder)
attachment = MotionDocument.objects.create(
    motion=doc,
    file=SimpleUploadedFile("anlage.pdf", b"%PDF-1.4 anlage", content_type="application/pdf"),
    filename="anlage.pdf",
    mime_type="application/pdf",
    file_size=15,
    uploaded_by=m_admin,
)
MotionShare.objects.create(motion=doc, scope="user", user=user_guest, level="edit", created_by=user_admin)
folder_share = FolderGuestShare.objects.create(folder=folder, user=user_guest, level="view", created_by=user_admin)

# =============================================================================
print()
print("=== 1. Editor: Gast mit Stufe Bearbeiten ===")
resp = c_guest.get(f"{BASE}/documents/{doc.id}/")
page = html(resp)
check("Gast öffnet Editor -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Speichern-Button vorhanden (Bearbeiten erlaubt)", '@click="save()"' in page)
check("Kein Status-Dropdown", 'title="Status ändern"' not in page)
check("Kein Papierkorb", "In den Papierkorb" not in page)
check("Kein Dokumenttyp-Dropdown", 'x-text="documentTypeName"' not in page)
check("Kein KI-Tab", "sidebarTab = 'ai'" not in page)
check(
    "Keine Zuständigkeits-/Frist-/Themen-Formulare",
    'name="responsible"' not in page and 'name="due_date"' not in page and 'name="topics"' not in page,
)
check("Keine Freigabe-Anforderung", 'name="approver"' not in page and "Interne Absprache“ angefragt" not in page)
check(
    "Anhang gelistet mit geschütztem Download",
    "anlage.pdf" in page and f"/documents/{doc.id}/files/{attachment.id}/download/" in page,
)
resp = c_guest.get(f"{BASE}/documents/{doc.id}/files/{attachment.id}/download/")
check("Gast kann Anhang herunterladen", resp.status_code == 200, f"got {resp.status_code}")
resp.close()

resp = c_admin.get(f"{BASE}/documents/{doc.id}/")
page = html(resp)
check(
    "Autorin sieht Status, Papierkorb, KI, Freigabe anfordern",
    'title="Status ändern"' in page
    and "In den Papierkorb" in page
    and "sidebarTab = 'ai'" in page
    and 'name="approver"' in page,
)

MotionShare.objects.filter(motion=doc, user=user_guest).update(level="view")
page = html(c_guest.get(f"{BASE}/documents/{doc.id}/"))
check("Gast mit Lesen: kein Speichern", '@click="save()"' not in page and "anlage.pdf" in page)
MotionShare.objects.filter(motion=doc, user=user_guest).update(level="edit")

# =============================================================================
print()
print("=== 2. Mitglieds-Detail: Was sieht dieser Gast? ===")
detail_url = f"{BASE}/organization/members/{m_guest.id}/"
resp = c_admin.get(detail_url)
page = html(resp)
check("Detailseite -> 200 mit Übersicht", resp.status_code == 200 and "Was sieht dieser Gast?" in page)
check(
    "Dokument-Freigabe mit Stufe",
    "Haushaltsentwurf" in page and "Dokument-Freigabe" in page and "Stufe Bearbeiten" in page,
)
check(
    "Ordner-Freigabe rekursiv mit Zählern",
    "Projekte" in page and "inkl. 1 Unterordner" in page and "aktuell 2 Dokumente" in page,
)
check("Zähler im Kopf", "1 Dokument" in page and "1 Ordner" in page)
check("Entziehen-Buttons für Admin", page.count("Entziehen") >= 2)
members_view, _ = Permission.objects.get_or_create(
    codename="members.view", defaults={"name": "members.view", "category": "members"}
)
viewer_role = Role.objects.create(organization=org, name="Nur Einsicht", is_admin=False)
viewer_role.permissions.add(members_view)
m_member.roles.add(viewer_role)
member_page = html(c_member.get(detail_url))
check(
    "Ohne Gast-Verwaltungsrecht: Übersicht sichtbar, kein Entzug",
    "Was sieht dieser Gast?" in member_page and 'name="next"' not in member_page,
)

resp = c_admin.get(f"{BASE}/organization/members/")
page = html(resp)
check("Gästeliste zeigt Zähler", "1 Dokument" in page and "1 Ordner freigegeben" in page)

# Entzug über bestehende Views mit Rücksprung
share = MotionShare.objects.get(motion=doc, user=user_guest)
resp = c_admin.post(f"{BASE}/documents/share/{share.id}/remove/", {"next": f"{detail_url}#guest-shares"})
check(
    "Dokument-Entzug -> Redirect zurück",
    resp.status_code == 302 and detail_url in resp["Location"],
    f"got {resp.status_code}",
)
check("Dokument-Freigabe entfernt", not MotionShare.objects.filter(id=share.id).exists())
resp = c_admin.post(
    f"{BASE}/documents/folders/shares/{folder_share.id}/remove/", {"next": f"{detail_url}#guest-shares"}
)
check(
    "Ordner-Entzug -> Redirect zurück",
    resp.status_code == 302 and detail_url in resp["Location"],
    f"got {resp.status_code}",
)
check("Ordner-Freigabe entfernt", not FolderGuestShare.objects.filter(id=folder_share.id).exists())
check("Gast sieht Dokument nicht mehr", c_guest.get(f"{BASE}/documents/{doc.id}/").status_code in (403, 404))
page = html(c_admin.get(detail_url))
check("Leerer Zustand nach Entzug", "keine Freigaben" in page)

# Offener Redirect wird ignoriert
share2 = MotionShare.objects.create(motion=doc, scope="user", user=user_guest, level="view", created_by=user_admin)
resp = c_admin.post(f"{BASE}/documents/share/{share2.id}/remove/", {"next": "https://evil.example/"})
check("Fremdes next-Ziel wird ignoriert", resp.status_code == 204)

# Gast-Verwalter:in ohne motions.edit_all darf Gast-Freigaben entziehen
guests_manage, _ = Permission.objects.get_or_create(
    codename="guests.manage", defaults={"name": "guests.manage", "category": "guests"}
)
motions_share, _ = Permission.objects.get_or_create(
    codename="motions.share", defaults={"name": "motions.share", "category": "motions"}
)
manager_role = Role.objects.create(organization=org, name="Gastbetreuung", is_admin=False)
manager_role.permissions.add(guests_manage, motions_share, members_view)
user_mgr = User.objects.create_user(email="mgr@example.org", password="test1234!")
m_mgr = Membership.objects.create(user=user_mgr, organization=org)
m_mgr.roles.add(manager_role)
c_mgr = client_for(user_mgr)
share3 = MotionShare.objects.create(motion=doc, scope="user", user=user_guest, level="view", created_by=user_admin)
check("Gastbetreuung sieht Entziehen", "Entziehen" in html(c_mgr.get(detail_url)))
resp = c_mgr.post(f"{BASE}/documents/share/{share3.id}/remove/", {"next": detail_url})
check(
    "Gastbetreuung darf Gast-Freigabe entziehen",
    resp.status_code == 302 and not MotionShare.objects.filter(id=share3.id).exists(),
    f"got {resp.status_code}",
)
share_member = MotionShare.objects.create(
    motion=doc, scope="user", user=user_member, level="view", created_by=user_admin
)
resp = c_mgr.post(f"{BASE}/documents/share/{share_member.id}/remove/", {"next": detail_url})
check(
    "…aber keine Mitglieds-Freigabe",
    resp.status_code == 403 and MotionShare.objects.filter(id=share_member.id).exists(),
    f"got {resp.status_code}",
)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
