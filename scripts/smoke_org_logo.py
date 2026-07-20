# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Organisations-Logo — Upload, Anzeige und Media-Auslieferung.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_org_logo.py

Läuft bewusst mit DEBUG=false (wie in Produktion), um den gefixten Bug zu
beweisen: `static(settings.MEDIA_URL, ...)` in mandari/urls.py ist bei
DEBUG=False ein No-Op — /media/* lieferte in Produktion für jedes
hochgeladene Logo 404. Der Fix registriert eine explizite media-Route
unabhängig von DEBUG.

Geprüft wird der komplette Flow:
  1. Org-Admin lädt PNG über die Organisationseinstellungen hoch
  2. Organization.logo ist gesetzt, Datei liegt in MEDIA_ROOT
  3. Einstellungsseite und Work-Sidebar rendern <img src="/media/...">
  4. GET auf die Media-URL liefert 200 (+ Cache-Control)
  5. Party-Fallback: Org ohne eigenes Logo erbt PartyGroup-Logo
     (effective_logo) in der Sidebar
  6. Ungültiger Dateityp wird abgelehnt, remove_logo entfernt das Logo
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_logo_"))
_db_path = _tmp_dir / "smoke.sqlite3"
_media_root = _tmp_dir / "media"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
# DEBUG=false: Produktionsverhalten — genau dort war /media/* kaputt.
os.environ["DEBUG"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_org_logo"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

# Bei DEBUG=false nutzt settings.py den WhiteNoise-Manifest-Storage, der ohne
# collectstatic jedes {% static %} scheitern lässt — für den Test auf den
# einfachen Storage umstellen. MEDIA_ROOT auf ein Temp-Verzeichnis umbiegen.
_overrides = override_settings(
    MEDIA_ROOT=str(_media_root),
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
_overrides.enable()

call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, PartyGroup, Role  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

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


# Kleinstes gültiges 1x1-PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

print("=== Setup ===")
org = Organization.objects.create(name="Fraktion Logo", slug="fraktion-logo")
TenantEncryption(org).key

admin_role = Role.objects.filter(organization=org, is_admin=True).first()
if admin_role is None:
    admin_role = Role.objects.create(organization=org, name="Administrator", is_admin=True)

admin = User.objects.create_user(email="admin@example.org", password="test1234!")
admin_ms = Membership.objects.create(user=admin, organization=org)
admin_ms.roles.add(admin_role)

client = Client()
client.force_login(admin)

SETTINGS_URL = f"/work/{org.slug}/organization/"

print("=== 1. Einstellungsseite erreichbar (DEBUG=false) ===")
resp = client.get(SETTINGS_URL)
check("GET Organisationseinstellungen -> 200", resp.status_code == 200, f"status={resp.status_code}")
check(
    "Formular hat multipart/form-data",
    b'enctype="multipart/form-data"' in resp.content,
)

print("=== 2. Logo-Upload als Org-Admin ===")
resp = client.post(
    SETTINGS_URL,
    {
        "action": "update_general",
        "name": org.name,
        "description": "",
        "primary_color": "#6366f1",
        "logo": SimpleUploadedFile("logo.png", PNG_BYTES, content_type="image/png"),
    },
)
check("POST Upload -> Redirect", resp.status_code == 302, f"status={resp.status_code}")
org.refresh_from_db()
check("Organization.logo ist gesetzt", bool(org.logo), f"logo={org.logo!r}")
check(
    "Datei existiert in MEDIA_ROOT",
    bool(org.logo) and Path(org.logo.path).is_file(),
)
logo_url = org.logo.url if org.logo else ""
check("Logo-URL beginnt mit /media/", logo_url.startswith("/media/"), f"url={logo_url}")

print("=== 3. Anzeige: Einstellungsseite + Work-Sidebar ===")
resp = client.get(SETTINGS_URL)
content = resp.content.decode("utf-8")
check("Einstellungsseite rendert <img src=/media/...>", f'src="{logo_url}"' in content)
# Die Sidebar (work-org-logo in base_work.html) rendert effective_logo
check(
    "Work-Sidebar rendert Logo",
    f'<img src="{logo_url}" alt="{org.name}">' in content,
)

print("=== 4. Media-Auslieferung bei DEBUG=false (der Prod-Bug) ===")
resp = client.get(logo_url)
check("GET auf Media-URL -> 200", resp.status_code == 200, f"status={resp.status_code}")
if resp.status_code == 200:
    body = b"".join(resp.streaming_content) if resp.streaming else resp.content
    check("Antwort enthält die PNG-Bytes", body == PNG_BYTES)
    check(
        "Cache-Control gesetzt",
        resp.headers.get("Cache-Control") == "public, max-age=3600",
        f"cache={resp.headers.get('Cache-Control')!r}",
    )
check("Unbekannte Media-Datei -> 404", client.get("/media/gibt/es/nicht.png").status_code == 404)

print("=== 5. Party-Fallback (effective_logo) ===")
party = PartyGroup.objects.create(name="Testpartei Logo")
party.logo.save("party.png", ContentFile(PNG_BYTES), save=True)
org2 = Organization.objects.create(name="Fraktion Erbe", slug="fraktion-erbe", party_group=party)
TenantEncryption(org2).key
admin_role2 = Role.objects.filter(organization=org2, is_admin=True).first()
if admin_role2 is None:
    admin_role2 = Role.objects.create(organization=org2, name="Administrator", is_admin=True)
admin2 = User.objects.create_user(email="admin2@example.org", password="test1234!")
admin2_ms = Membership.objects.create(user=admin2, organization=org2)
admin2_ms.roles.add(admin_role2)

check("effective_logo fällt auf Party-Logo zurück", org2.effective_logo == party.logo)
client2 = Client()
client2.force_login(admin2)
resp = client2.get(f"/work/{org2.slug}/organization/")
check(
    "Sidebar rendert geerbtes Party-Logo",
    resp.status_code == 200 and f'<img src="{party.logo.url}" alt="{org2.name}">' in resp.content.decode("utf-8"),
    f"status={resp.status_code}",
)
check("GET auf Party-Logo-URL -> 200", client2.get(party.logo.url).status_code == 200)

print("=== 6. Validierung + Entfernen ===")
resp = client.post(
    SETTINGS_URL,
    {
        "action": "update_general",
        "name": org.name,
        "logo": SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml"),
    },
)
org.refresh_from_db()
check("SVG-Upload wird abgelehnt, Logo unverändert", bool(org.logo) and org.logo.url == logo_url)

resp = client.post(
    SETTINGS_URL,
    {"action": "update_general", "name": org.name, "remove_logo": "1"},
)
org.refresh_from_db()
check("remove_logo entfernt das Logo", not org.logo)

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
