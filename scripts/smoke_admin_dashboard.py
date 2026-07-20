# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Admin-Dashboard rendert ohne verwaiste App-Referenzen (Issue #19).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_admin_dashboard.py

Hintergrund: Nach dem Auszug der Marketing-Inhalte (insight_content →
separate Website) blieb im Unfold-DASHBOARD_CALLBACK zeitweise ein
unbedingter Import von insight_content.models zurück. Da die App nicht
mehr in INSTALLED_APPS stand, führte jeder Aufruf von /admin/ nach dem
Login zu einem 500er (RuntimeError: Model class ... isn't in an
application in INSTALLED_APPS).

Prüft:
- /admin/ (Index inkl. DASHBOARD_CALLBACK) rendert für einen Superuser
  mit HTTP 200 — der Original-Fehlerpfad aus dem Issue
- /admin/login/ rendert anonym mit HTTP 200
- Die App insight_content existiert weder als Python-Modul noch als
  Referenz in INSTALLED_APPS
- Kein Python-Quelltext unter mandari/ referenziert insight_content mehr
  (statische Prüfung gegen Wiedereinführung verwaister Importe)
- Alle Admin-Links in der Unfold-SIDEBAR-Navigation lassen sich auflösen
  (reverse_lazy) und zeigen nur auf registrierte Admin-Views
"""

import base64
import importlib.util
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
REPO_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_DIR / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_admin_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

# Sync-Watchdog (insight_sync.apps.ready) nicht starten (SQLite-Lock)
sys.argv = ["manage.py", "smoke_admin_dashboard"]

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import NoReverseMatch  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# =============================================================================
print("\n=== 1. Verwaiste App: insight_content existiert nicht mehr ===")
# =============================================================================

_spec = importlib.util.find_spec("insight_content")
_spec_origin = Path(_spec.origin).resolve() if _spec and _spec.origin else None
check(
    "insight_content existiert nicht im Projektverzeichnis",
    not (PROJECT_DIR / "insight_content").exists()
    and (_spec_origin is None or not _spec_origin.is_relative_to(PROJECT_DIR)),
)
check(
    "insight_content steht nicht in INSTALLED_APPS",
    "insight_content" not in settings.INSTALLED_APPS,
)

# Statische Prüfung: kein Python-Quelltext referenziert die entfernte App
offenders = []
for py_file in PROJECT_DIR.rglob("*.py"):
    rel = py_file.relative_to(PROJECT_DIR)
    if "node_modules" in rel.parts or "__pycache__" in rel.parts:
        continue
    try:
        text = py_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    if "insight_content" in text:
        offenders.append(str(rel))

check(
    "Kein Python-Quelltext unter mandari/ referenziert insight_content",
    not offenders,
    f"Gefunden in: {offenders}",
)

# =============================================================================
print("\n=== 2. Unfold-Sidebar: alle Admin-Links auflösbar ===")
# =============================================================================


def iter_sidebar_links():
    for group in settings.UNFOLD.get("SIDEBAR", {}).get("navigation", []):
        for item in group.get("items", []):
            yield item.get("title"), item.get("link")


sidebar_errors = []
sidebar_count = 0
for title, link in iter_sidebar_links():
    if link is None:
        continue
    sidebar_count += 1
    try:
        str(link)  # erzwingt Auswertung von reverse_lazy
    except NoReverseMatch as exc:
        sidebar_errors.append(f"{title}: {exc}")

check(
    f"Alle {sidebar_count} Sidebar-Links auflösbar (keine verwaisten Admin-Views)",
    sidebar_count > 0 and not sidebar_errors,
    f"Fehler: {sidebar_errors}",
)

# =============================================================================
print("\n=== 3. /admin/ rendert (Repro des Original-Fehlerpfads) ===")
# =============================================================================

User = get_user_model()
admin_user = User.objects.create_user(email="admin@example.org", password="smoke-test-1234!")
admin_user.is_staff = True
admin_user.is_superuser = True
admin_user.save()

client = Client()

resp = client.get("/admin/login/")
check("/admin/login/ rendert anonym mit HTTP 200", resp.status_code == 200, f"Status: {resp.status_code}")

client.force_login(admin_user)

# Der Original-Bug: 500er auf dem Admin-Index nach Login, weil der
# Unfold-DASHBOARD_CALLBACK insight_content.models importierte.
resp = client.get("/admin/")
check(
    "/admin/ (Index inkl. DASHBOARD_CALLBACK) rendert mit HTTP 200",
    resp.status_code == 200,
    f"Status: {resp.status_code}",
)
if resp.status_code == 200:
    content = resp.content.decode("utf-8", errors="replace")
    check("Dashboard enthält keinen insight_content-Verweis", "insight_content" not in content)

# App-Liste des Admins rendert ebenfalls (alle registrierten ModelAdmins)
resp = client.get("/admin/", follow=True)
check("/admin/ mit follow=True stabil", resp.status_code == 200, f"Status: {resp.status_code}")

# =============================================================================
print("\n" + "=" * 60)
print(f"Ergebnis: {PASS} OK, {FAIL} FAIL")
print("=" * 60)
sys.exit(1 if FAIL else 0)
