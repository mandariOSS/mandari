# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Öffentliche Fraktions-API v1 — erweiterte Optionen + eigener Reiter.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_work_public_api.py

Prüft:
- Neuer Einstellungs-Reiter „API" (Optionen, Statistik, Einbindungs-Snippet)
- Speichern aller Optionen inkl. Origin-Bereinigung; Token-Erneuerung
- API-Verhalten: Zeitfenster (past/future), Inhaltsumfang (Ort/Tagesordnung),
  CORS je erlaubtem Origin, konfigurierbare Cache-Dauer
- Nutzungsstatistik (Zähler + letzter Abruf)
- Alter Fraktions-Reiter verweist auf den neuen API-Reiter
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
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.faction.models import (  # noqa: E402
    FactionMeeting,
    FactionPublicApiAccess,
)
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


# =============================================================================
# Setup
# =============================================================================
now = timezone.now()

source = OParlSource.objects.create(name="Test-RIS", url="https://ris.api.example/system")
body = OParlBody.objects.create(
    source=source, external_id="https://ris.api.example/body/1", name="API-Stadt", slug="api-stadt"
)
org = Organization.objects.create(name="Fraktion API-Test", slug="fraktion-api-test", body=body)
TenantEncryption(org).key

perm, _ = Permission.objects.get_or_create(
    codename="faction.manage", defaults={"name": "faction.manage", "category": "faction"}
)
manager_user = User.objects.create_user(email="api-manager@example.org", password="pw-Smoke-1!")
ms = Membership.objects.create(user=manager_user, organization=org)
role = Role.objects.create(organization=org, name="Verwalter", is_admin=False)
role.permissions.add(perm)
ms.roles.add(role)
manager = Client()
manager.force_login(manager_user)

anon = Client()
base = f"/work/{org.slug}"

# Sitzungen: kommend (10 Tage), fern (500 Tage), vergangen (30 Tage), alt (200 Tage)
m_soon = FactionMeeting.objects.create(
    organization=org, title="API-SITZUNG-BALD", status="planned",
    start=now + timedelta(days=10), location="Rathaus, Raum 1",
)
m_far = FactionMeeting.objects.create(
    organization=org, title="API-SITZUNG-FERN", status="planned", start=now + timedelta(days=500),
)
m_past = FactionMeeting.objects.create(
    organization=org, title="API-SITZUNG-VERGANGEN", status="completed", start=now - timedelta(days=30),
)
m_old = FactionMeeting.objects.create(
    organization=org, title="API-SITZUNG-ALT", status="completed", start=now - timedelta(days=200),
)

# =============================================================================
print("=== Phase A: Neuer Reiter ===")
resp = manager.get(f"{base}/organization/api/")
html = resp.content.decode("utf-8")
check("API-Reiter -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Optionen sichtbar", "Zukunft (Tage)" in html and "Erlaubte Origins" in html and "Cache (Sekunden)" in html)
check("Statistik sichtbar", "Abrufe gesamt" in html)
check("Einbindungs-Snippet vorhanden", "fraktions-termine" in html and "fetch(" in html)
check("Tab in Navigation", '>API<' in html.replace("\n", "").replace(" ", "").replace(">API<", ">API<") or "API" in html)

resp = manager.get(f"{base}/organization/faction-settings/")
html = resp.content.decode("utf-8")
check("Alter Reiter verweist auf neuen", "umgezogen" in html and "organization/api/" in html)

# Speichern mit allen Optionen (inkl. Origin-Bereinigung)
resp = manager.post(
    f"{base}/organization/api/",
    {
        "section": "api_save",
        "api_enabled": "on",
        "api_past_days": "60",
        "api_future_days": "100",
        "api_cache_seconds": "120",
        "api_show_location": "on",
        "api_show_agenda": "on",
        "api_allowed_origins": "https://fraktion.example/, javascript:alert(1), http://lokal.test, kein-schema.de",
    },
)
access = FactionPublicApiAccess.objects.get(organization=org)
check("Optionen gespeichert", access.is_enabled and access.past_days == 60 and access.future_days == 100 and access.cache_seconds == 120)
check(
    "Origins bereinigt (nur http/https, ohne Slash)",
    access.origin_list() == ["https://fraktion.example", "http://lokal.test"],
    str(access.origin_list()),
)

# =============================================================================
print()
print("=== Phase B: Zeitfenster + Inhaltsumfang ===")
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
data = resp.json()
titles = [m["title"] for m in data["meetings"]]
check("Terminliste -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Kommende Sitzung enthalten", "API-SITZUNG-BALD" in titles)
check("Zukunfts-Fenster greift (500 Tage raus)", "API-SITZUNG-FERN" not in titles)
check("Vergangenheit 60 Tage: 30 Tage drin, 200 raus", "API-SITZUNG-VERGANGEN" in titles and "API-SITZUNG-ALT" not in titles)
check("Ort ausgeliefert", any(m["location"] == "Rathaus, Raum 1" for m in data["meetings"]))
check("Cache-Dauer konfiguriert", resp["Cache-Control"] == "public, max-age=120", resp["Cache-Control"])

# Ort abschalten
access.show_location = False
access.save(update_fields=["show_location"])
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Ort abgeschaltet", all(m["location"] == "" for m in resp.json()["meetings"]))

# Tagesordnung abschalten
from apps.work.faction.models import FactionAgendaItem  # noqa: E402

FactionAgendaItem.objects.create(
    meeting=m_soon, number="1", order=1, title="OEFFENTLICHER-TOP", visibility="public", proposal_status="active"
)
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/{m_soon.id}/")
check("Tagesordnung enthalten", "OEFFENTLICHER-TOP" in resp.content.decode("utf-8"))
access.show_agenda = False
access.save(update_fields=["show_agenda"])
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/{m_soon.id}/")
check("Tagesordnung abgeschaltet", "OEFFENTLICHER-TOP" not in resp.content.decode("utf-8"))

# =============================================================================
print()
print("=== Phase C: CORS je Origin ===")
resp = anon.get(
    f"/api/public/v1/fraktionen/{access.token}/sitzungen/", HTTP_ORIGIN="https://fraktion.example"
)
check("Erlaubter Origin gespiegelt", resp.headers.get("Access-Control-Allow-Origin") == "https://fraktion.example")
resp = anon.get(
    f"/api/public/v1/fraktionen/{access.token}/sitzungen/", HTTP_ORIGIN="https://boese-seite.example"
)
check("Fremder Origin ohne CORS-Freigabe", "Access-Control-Allow-Origin" not in resp.headers)

# Ohne Einschränkung: *
access.allowed_origins = ""
access.save(update_fields=["allowed_origins"])
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Ohne Einschränkung: *", resp.headers.get("Access-Control-Allow-Origin") == "*")

# =============================================================================
print()
print("=== Phase D: Statistik + Token ===")
access.refresh_from_db()
check("Abrufe gezählt", access.request_count >= 5, str(access.request_count))
check("Letzter Abruf gesetzt", access.last_request_at is not None)

html = manager.get(f"{base}/organization/api/").content.decode("utf-8")
check("Statistik im Reiter sichtbar", str(access.request_count) in html)

old_token = access.token
resp = manager.post(f"{base}/organization/api/", {"section": "api_regenerate"})
access.refresh_from_db()
check("Token erneuert", access.token != old_token)
resp = anon.get(f"/api/public/v1/fraktionen/{old_token}/sitzungen/")
check("Altes Token -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = anon.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Neues Token funktioniert", resp.status_code == 200, f"got {resp.status_code}")

# Rechte: ohne faction.manage kein Zugriff auf den Reiter
plain_user = User.objects.create_user(email="ohne-recht@example.org", password="pw-Smoke-1!")
Membership.objects.create(user=plain_user, organization=org)
plain = Client()
plain.force_login(plain_user)
resp = plain.get(f"{base}/organization/api/")
check("Ohne faction.manage -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
