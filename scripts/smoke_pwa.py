# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: PWA (Manifest, Service Worker, Offline-Seite, Meta-Tags, Icons).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_pwa.py

Prüfungen:
  1. /manifest.webmanifest erreichbar, valides JSON, Pflichtfelder + Icons
     (192/512, any + maskable), korrekter Content-Type.
  2. /sw.js erreichbar mit JS-Content-Type; konservative Strategie ist
     ablesbar (nur GET, network-first für Navigationen, kein HTML-Caching,
     cache-first nur für /static/), Cache-Version eingerendert.
  3. /offline/ rendert die Offline-Fallback-Seite.
  4. PWA-Meta-Tags in base_work (Dashboard) und Login (base_auth):
     Manifest-Link, apple-mobile-web-app-*, apple-touch-icon,
     viewport-fit=cover, SW-Registrierung, iOS-Install-Hinweis (nur Work),
     Install-Menüpunkt (beforeinstallprompt, nur Work).
  5. Icon-Dateien existieren als valide PNGs in den erwarteten Größen und
     werden über die Manifest-URLs ausgeliefert.
  6. start_url-Flow: /work/ leitet anonym zum Login, eingeloggt zur Org.
"""

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_pwa_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_pwa"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from django.contrib.staticfiles import finders  # noqa: E402
from PIL import Image  # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# =============================================================================
# Testdaten
# =============================================================================
org = Organization.objects.create(name="Fraktion PWA", slug="fraktion-pwa")
TenantEncryption(org).key
admin_role = Role.objects.filter(organization=org, is_admin=True).first()
if admin_role is None:
    admin_role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
user = User.objects.create_user(email="pwa@example.org", password="test1234!")
ms = Membership.objects.create(user=user, organization=org)
ms.roles.add(admin_role)

anon = Client()
client = Client()
client.force_login(user)

# =============================================================================
# 1. Manifest
# =============================================================================
print("=== 1. Web App Manifest ===")
resp = anon.get("/manifest.webmanifest")
check("GET /manifest.webmanifest -> 200", resp.status_code == 200, f"status={resp.status_code}")
check(
    "Content-Type application/manifest+json",
    resp.headers.get("Content-Type", "").startswith("application/manifest+json"),
    resp.headers.get("Content-Type"),
)
manifest = json.loads(resp.content.decode("utf-8"))
for field, expected in [
    ("name", "mandari"),
    ("short_name", "mandari"),
    ("start_url", "/work/"),
    ("display", "standalone"),
    ("theme_color", "#4F46E5"),
]:
    check(f"manifest.{field} == {expected!r}", manifest.get(field) == expected, f"got={manifest.get(field)!r}")
check("manifest.background_color gesetzt", bool(manifest.get("background_color")))
check("manifest.scope == '/'", manifest.get("scope") == "/")

icons = manifest.get("icons", [])
sizes_by_purpose = {}
for icon in icons:
    sizes_by_purpose.setdefault(icon.get("purpose", "any"), set()).add(icon.get("sizes"))
check("Icons: 192+512 purpose=any", {"192x192", "512x512"} <= sizes_by_purpose.get("any", set()), str(icons))
check("Icons: 192+512 purpose=maskable", {"192x192", "512x512"} <= sizes_by_purpose.get("maskable", set()))

# =============================================================================
# 2. Service Worker
# =============================================================================
print("=== 2. Service Worker ===")
resp = anon.get("/sw.js")
check("GET /sw.js -> 200", resp.status_code == 200, f"status={resp.status_code}")
check(
    "Content-Type text/javascript",
    resp.headers.get("Content-Type", "").startswith("text/javascript"),
    resp.headers.get("Content-Type"),
)
sw = resp.content.decode("utf-8")
check("SW: Cache-Version eingerendert", "const CACHE_VERSION = '" in sw and "const CACHE_VERSION = ''" not in sw)
check("SW: nur GET-Requests", "request.method !== 'GET'" in sw)
check("SW: network-first für Navigationen", "request.mode === 'navigate'" in sw)
check("SW: cache-first nur für /static/", "url.pathname.startsWith('/static/')" in sw)
check("SW: Offline-Fallback registriert", "/offline/" in sw)
check("SW: skipWaiting + clients.claim", "skipWaiting" in sw and "clients.claim" in sw)
# Kein HTML-Caching: im navigate-Zweig darf kein cache.put vorkommen
navigate_branch = sw.split("request.mode === 'navigate'")[1].split("startsWith('/static/')")[0]
check("SW: kein cache.put im Navigations-Zweig (kein HTML-Caching)", "cache.put" not in navigate_branch)
check("SW: Cache-Control no-cache", resp.headers.get("Cache-Control") == "no-cache")

# =============================================================================
# 3. Offline-Seite
# =============================================================================
print("=== 3. Offline-Seite ===")
resp = anon.get("/offline/")
check("GET /offline/ -> 200", resp.status_code == 200, f"status={resp.status_code}")
offline_html = resp.content.decode("utf-8")
check("Offline: 'Du bist offline' vorhanden", "Du bist offline" in offline_html)
check("Offline: self-contained (kein /static/-Verweis)", "/static/" not in offline_html)

# =============================================================================
# 4. Meta-Tags in base_work + Login
# =============================================================================
print("=== 4. PWA-Meta-Tags ===")
resp = client.get(f"/work/{org.slug}/")
check("GET Dashboard -> 200", resp.status_code == 200, f"status={resp.status_code}")
dash = resp.content.decode("utf-8")
resp = anon.get("/accounts/login/")
check("GET Login -> 200", resp.status_code == 200, f"status={resp.status_code}")
login = resp.content.decode("utf-8")

for label, html in [("base_work", dash), ("login", login)]:
    check(f"{label}: Manifest-Link", 'rel="manifest"' in html and "/manifest.webmanifest" in html)
    check(f"{label}: apple-mobile-web-app-capable", 'name="apple-mobile-web-app-capable" content="yes"' in html)
    check(f"{label}: apple-mobile-web-app-status-bar-style", "apple-mobile-web-app-status-bar-style" in html)
    check(f"{label}: apple-mobile-web-app-title", 'name="apple-mobile-web-app-title" content="mandari"' in html)
    check(f"{label}: apple-touch-icon 180px", 'rel="apple-touch-icon" sizes="180x180"' in html)
    check(f"{label}: viewport-fit=cover", "viewport-fit=cover" in html)
    check(f"{label}: theme-color", 'name="theme-color"' in html)
    check(f"{label}: SW-Registrierung", "serviceWorker" in html and "/sw.js" in html)

check("base_work: iOS-Install-Hinweis", "iosInstallHint" in dash and "Zum Home-Bildschirm" in dash)
check(
    "base_work: Install-Menüpunkt (beforeinstallprompt)", "beforeinstallprompt" in dash and "App installieren" in dash
)
check("base_work: Safe-Area-Insets", "safe-area-inset-bottom" in dash)
check("login: kein iOS-Hinweis (nur Work-Kontext)", "iosInstallHint" not in login)

# =============================================================================
# 5. Icons existieren und werden ausgeliefert
# =============================================================================
print("=== 5. Icons ===")
EXPECTED_ICONS = {
    "brand/icon-192.png": 192,
    "brand/icon-512.png": 512,
    "brand/icon-maskable-192.png": 192,
    "brand/icon-maskable-512.png": 512,
    "brand/apple-touch-icon.png": 180,
}
for rel_path, size in EXPECTED_ICONS.items():
    found = finders.find(rel_path)
    check(f"{rel_path} existiert", bool(found))
    if found:
        with Image.open(found) as im:
            check(f"{rel_path} ist {size}x{size} PNG", im.format == "PNG" and im.size == (size, size), str(im.size))

for icon in icons:
    resp = anon.get(icon["src"])
    body = b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content
    check(f"GET {icon['src']} -> 200 PNG", resp.status_code == 200 and body[:8] == b"\x89PNG\r\n\x1a\n")

# Maskable: Safe-Zone-Ecken müssen deckend (Hintergrundfarbe) sein
maskable = finders.find("brand/icon-maskable-512.png")
if maskable:
    with Image.open(maskable) as im:
        rgba = im.convert("RGBA")
        corners = [rgba.getpixel(p) for p in [(2, 2), (509, 2), (2, 509), (509, 509)]]
        check("maskable-512: Ecken deckend (full-bleed)", all(px[3] == 255 for px in corners), str(corners))

# =============================================================================
# 6. start_url-Flow
# =============================================================================
print("=== 6. start_url /work/ ===")
resp = anon.get("/work/")
check(
    "anonym: /work/ -> Login-Redirect",
    resp.status_code == 302 and "/accounts/login/" in resp.headers.get("Location", ""),
)
resp = client.get("/work/")
check(
    "eingeloggt: /work/ -> Org-Dashboard",
    resp.status_code == 302 and f"/work/{org.slug}/" in resp.headers.get("Location", ""),
    f"status={resp.status_code} loc={resp.headers.get('Location')}",
)

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
