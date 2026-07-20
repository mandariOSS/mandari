# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Session-Anlagenverwaltung mit Ö/NÖ-Kennzeichnung (Issue #25).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_files.py

Prüft:
- Mehrfach-Upload an Vorlage und Sitzung inkl. Ö/NÖ-Kennzeichnung
- Dateityp-/Größen-Validierung, Virenscan-Hook wird aufgerufen
- Text-Extraktion befüllt text_content
- Ö/NÖ nachträglich änderbar (mit Audit-Eintrag), Ersetzen (Versionierung),
  Löschen
- Zugriffskontrolle: NÖ-Anlagen sind für Nicht-Berechtigte weder in der UI
  noch über die Download-URL erreichbar; /media/-Direktzugriff ist blockiert
- Tenant-Isolation der Download-View
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

# Media-Root in temporäres Verzeichnis umlenken
from django.conf import settings as django_settings  # noqa: E402

django_settings.MEDIA_ROOT = str(_tmp_dir / "media")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
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
tenant2 = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

clerk_user = User.objects.create_user(email="clerk@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")
foreign_user = User.objects.create_user(email="foreign@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_clerk = SessionUser.objects.create(user=clerk_user, tenant=tenant)
su_clerk.roles.add(roles["clerk"])
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(roles["viewer"])

roles2 = SessionRole.create_default_roles(tenant2)
su_foreign = SessionUser.objects.create(user=foreign_user, tenant=tenant2)
su_foreign.roles.add(roles2["admin"])

from django.utils import timezone  # noqa: E402

org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0001", name="Vorlage mit Anlagen")
meeting = SessionMeeting.objects.create(tenant=tenant, name="Sitzung", organization=org, start=timezone.now())

clerk = Client()
clerk.force_login(clerk_user)
viewer = Client()
viewer.force_login(viewer_user)
foreign = Client()
foreign.force_login(foreign_user)

# =============================================================================
# Phase A: Upload (Mehrfach, Ö/NÖ, Validierung, Text-Extraktion)
# =============================================================================
print("=== Phase A: Upload ===")

resp = clerk.post(
    f"/session/{tenant.slug}/files/upload/",
    {
        "target_type": "paper",
        "target_id": str(paper.id),
        "is_public": "on",
        "files": [
            SimpleUploadedFile("gutachten.txt", b"Inhalt des Gutachtens Springbrunnen", content_type="text/plain"),
            SimpleUploadedFile("kalkulation.csv", b"posten;betrag\nbau;1000", content_type="text/csv"),
        ],
    },
)
check("Mehrfach-Upload an Vorlage -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Zwei Anlagen angelegt", paper.files.count() == 2, f"count={paper.files.count()}")

public_file = paper.files.get(name="gutachten.txt")
check("Anlage öffentlich gekennzeichnet", public_file.is_public)
check("Größe erfasst", public_file.size > 0)
check("MIME-Typ erfasst", public_file.mime_type.startswith("text/"), public_file.mime_type)
check(
    "Text-Extraktion befüllt text_content",
    "Springbrunnen" in public_file.text_content,
    f"text_content={public_file.text_content[:60]!r}",
)
check(
    "Audit: create-Eintrag für Upload",
    SessionAuditLog.objects.filter(object_id=public_file.id, action="create").exists(),
)

# NÖ-Anlage an Sitzung
resp = clerk.post(
    f"/session/{tenant.slug}/files/upload/",
    {
        "target_type": "meeting",
        "target_id": str(meeting.id),
        "files": [
            SimpleUploadedFile("personalie.txt", b"Streng vertrauliche Personalie XYZZY", content_type="text/plain")
        ],
    },
)
check("NÖ-Upload an Sitzung -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
np_file = meeting.files.get(name="personalie.txt")
check("Anlage nichtöffentlich gekennzeichnet", not np_file.is_public)

# Validierung: verbotener Typ
resp = clerk.post(
    f"/session/{tenant.slug}/files/upload/",
    {
        "target_type": "paper",
        "target_id": str(paper.id),
        "is_public": "on",
        "files": [SimpleUploadedFile("malware.exe", b"MZ...", content_type="application/octet-stream")],
    },
)
check("Verbotener Dateityp wird abgelehnt", not paper.files.filter(name="malware.exe").exists())

# Validierung: zu groß (Limit temporär senken)
from apps.session.services import file_service  # noqa: E402

_orig_max = file_service.MAX_FILE_SIZE_MB
file_service.MAX_FILE_SIZE_MB = 0
resp = clerk.post(
    f"/session/{tenant.slug}/files/upload/",
    {
        "target_type": "paper",
        "target_id": str(paper.id),
        "is_public": "on",
        "files": [SimpleUploadedFile("riesig.txt", b"x" * 1024, content_type="text/plain")],
    },
)
file_service.MAX_FILE_SIZE_MB = _orig_max
check("Zu große Datei wird abgelehnt", not paper.files.filter(name="riesig.txt").exists())

# Virenscan-Hook wird aufgerufen
scan_calls = []


def _test_scan_hook(uploaded_file):
    scan_calls.append(uploaded_file.name)


import apps.session.services.file_service as fs_module  # noqa: E402

_orig_scan = fs_module.scan_upload
fs_module.scan_upload = _test_scan_hook
try:
    clerk.post(
        f"/session/{tenant.slug}/files/upload/",
        {
            "target_type": "paper",
            "target_id": str(paper.id),
            "is_public": "on",
            "files": [SimpleUploadedFile("scanme.txt", b"scan mich", content_type="text/plain")],
        },
    )
finally:
    fs_module.scan_upload = _orig_scan
check("Virenscan-Hook wird beim Upload aufgerufen", "scanme.txt" in scan_calls, f"calls={scan_calls}")

# Ohne Edit-Berechtigung: kein Upload
resp = viewer.post(
    f"/session/{tenant.slug}/files/upload/",
    {
        "target_type": "paper",
        "target_id": str(paper.id),
        "is_public": "on",
        "files": [SimpleUploadedFile("verboten.txt", b"nope", content_type="text/plain")],
    },
)
check("Upload ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Upload ohne Berechtigung: keine Anlage angelegt", not paper.files.filter(name="verboten.txt").exists())

# =============================================================================
# Phase B: Zugriffskontrolle Download + Media-Leak
# =============================================================================
print()
print("=== Phase B: Zugriffskontrolle ===")

resp = clerk.get(f"/session/{tenant.slug}/files/{public_file.id}/download/")
check("Download öffentliche Anlage (Berechtigter) -> 200", resp.status_code == 200, f"got {resp.status_code}")
content = b"".join(resp.streaming_content)
check("Download liefert Dateiinhalt", b"Springbrunnen" in content)
check(
    "Audit: download-Eintrag",
    SessionAuditLog.objects.filter(object_id=public_file.id, action="download").exists(),
)

resp = clerk.get(f"/session/{tenant.slug}/files/{np_file.id}/download/")
check("Download NÖ-Anlage mit NÖ-Berechtigung -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = viewer.get(f"/session/{tenant.slug}/files/{np_file.id}/download/")
check("Download NÖ-Anlage ohne NÖ-Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = viewer.get(f"/session/{tenant.slug}/files/{public_file.id}/download/")
check("Download öffentliche Anlage als Viewer -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Tenant-Isolation: Fremder Tenant-Nutzer kommt nicht ran
resp = foreign.get(f"/session/{tenant.slug}/files/{np_file.id}/download/")
check("Download als Fremd-Tenant-Nutzer -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = foreign.get(f"/session/{tenant2.slug}/files/{np_file.id}/download/")
check("Download über fremden Tenant-Slug -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Media-URL-Leak: direkter /media/-Zugriff blockiert (auch anonym)
anon = Client()
media_path = np_file.file.name  # z. B. session/files/2026/07/personalie.txt
resp = anon.get(f"/media/{media_path}")
check("Direkter /media/-Zugriff auf Session-Anlage -> 404", resp.status_code == 404, f"got {resp.status_code}")

# UI: NÖ-Anlage taucht für Viewer nicht in der Sitzungs-Detailseite auf
resp = clerk.get(f"/session/{tenant.slug}/meetings/{meeting.id}/")
check("Detailseite zeigt NÖ-Anlage für Berechtigte", b"personalie.txt" in resp.content)
resp = viewer.get(f"/session/{tenant.slug}/meetings/{meeting.id}/")
check("Detailseite verbirgt NÖ-Anlage vor Viewer", b"personalie.txt" not in resp.content, f"status={resp.status_code}")

# =============================================================================
# Phase C: Ö/NÖ ändern, Ersetzen (Version), Löschen
# =============================================================================
print()
print("=== Phase C: Verwaltung ===")

resp = clerk.post(f"/session/{tenant.slug}/files/{public_file.id}/update/", {})
public_file.refresh_from_db()
check("Ö -> NÖ umschaltbar", not public_file.is_public)
check(
    "Audit: Eintrag für Ö/NÖ-Wechsel",
    SessionAuditLog.objects.filter(object_id=public_file.id, action="update", changes__has_key="is_public").exists(),
)

resp = clerk.post(f"/session/{tenant.slug}/files/{public_file.id}/update/", {"is_public": "on"})
public_file.refresh_from_db()
check("NÖ -> Ö umschaltbar", public_file.is_public)

# Ersetzen
resp = clerk.post(
    f"/session/{tenant.slug}/files/{public_file.id}/replace/",
    {"file": SimpleUploadedFile("gutachten_v2.txt", b"Neues Gutachten Wasserspiel", content_type="text/plain")},
)
public_file.refresh_from_db()
check("Ersetzen -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Version hochgezählt", public_file.version == 2, f"version={public_file.version}")
check("Neuer Name übernommen", public_file.name == "gutachten_v2.txt")
check("Text-Extraktion nach Ersetzen aktualisiert", "Wasserspiel" in public_file.text_content)
check(
    "Audit: replace-Eintrag mit alter/neuer Datei",
    SessionAuditLog.objects.filter(object_id=public_file.id, action="replace").exists(),
)

# Ersetzen ohne Berechtigung
resp = viewer.post(
    f"/session/{tenant.slug}/files/{public_file.id}/replace/",
    {"file": SimpleUploadedFile("boese.txt", b"x", content_type="text/plain")},
)
check("Ersetzen ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Löschen
file_id = public_file.id
resp = clerk.post(f"/session/{tenant.slug}/files/{file_id}/delete/")
check("Löschen -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Anlage gelöscht", not SessionFile.objects.filter(id=file_id).exists())
check("Audit: delete-Eintrag", SessionAuditLog.objects.filter(object_id=file_id, action="delete").exists())

# Löschen ohne Berechtigung
resp = viewer.post(f"/session/{tenant.slug}/files/{np_file.id}/delete/")
check("Löschen ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("NÖ-Anlage existiert weiterhin", SessionFile.objects.filter(id=np_file.id).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
