# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Fehlerseiten-Design + „Problem melden"-Formular.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_feedback.py

Prüft:
- Fehlerseiten (404/403/500) im mandari-Design mit Fehler-ID und
  „Problem melden"-Link
- Auth-Seiten (Login) im neuen Design
- Meldeformular: Anlage mit Ticket-Nummer, Validierung, Rate-Limit,
  Konto-Verknüpfung bei angemeldeten Nutzern
- Admin-Aktion „Als gelöst markieren + Rückmeldung senden" (E-Mail)
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

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.template.loader import render_to_string  # noqa: E402
from django.test import Client, RequestFactory  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.models import ProblemReport  # noqa: E402

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


anon = Client()
factory = RequestFactory()

# =============================================================================
print("=== Phase A: Fehlerseiten-Design ===")
request = factory.get("/kaputt/")
html = render_to_string("404.html", {"request": request})
check("404: Titel", "Seite nicht gefunden" in html)
check("404: mandari-Wortmarke", "mandari<span" in html)
check("404: Work-Hintergrund", "faf9f7" in html)
check("404: Problem-melden-Link", "/feedback/" in html)
check("404: kein alter Verlauf", "error-gradient" not in html)

html = render_to_string("500.html", {"request_id": "abc12345", "request": request})
check("500: Fehler-ID sichtbar", "abc12345" in html)
check("500: Problem-melden mit Fehler-ID", "/feedback/?error_id=abc12345" in html)
check("500: mandari-Wortmarke", "mandari<span" in html)

html = render_to_string("403.html", {"request": request})
check("403: Anmelden-Aktion", "Anmelden" in html and "Zugriff verweigert" in html)

resp = anon.get("/accounts/login/")
html = resp.content.decode("utf-8")
check("Login -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Login: neues Design (Wortmarke + heller Hintergrund)", "mandari<span" in html and "faf9f7" in html)
check("Login: kein alter Gradient", "from-indigo-500 via-purple-500" not in html)

# =============================================================================
print()
print("=== Phase B: Meldeformular ===")
resp = anon.get("/feedback/?error_id=abc12345&url=https://mandari.de/session/x/papers/")
html = resp.content.decode("utf-8")
check("Formular -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Fehler-ID vorbefüllt", "abc12345" in html)
check("Anmelde-Hinweis für Gäste", "mit deinem Konto anmelden" in html)
check("Technische Angaben-Block", "Technische Angaben" in html)

resp = anon.post(
    "/feedback/",
    {
        "message": "Beim Öffnen der Vorlagenliste erscheint ein Serverfehler.",
        "error_id": "abc12345",
        "url": "https://mandari.de/session/x/papers/",
        "browser_info": "Browser: Testclient",
        "email": "melder@example.org",
    },
)
report = ProblemReport.objects.first()
check("Meldung angelegt -> Redirect", resp.status_code == 302 and report is not None)
check("Ticket-Nummer vergeben", report.reference.startswith("PM-"), report.reference)
check("Felder gespeichert", report.error_id == "abc12345" and report.email == "melder@example.org" and "Testclient" in report.browser_info)
check("IP erfasst", report.ip_address is not None)

resp = anon.get(resp.headers["Location"])
html = resp.content.decode("utf-8")
check("Danke-Seite mit Ticket-Nummer", report.reference in html)
check("Danke-Seite: Rückmeldungs-Hinweis", "Rückmeldung per E-Mail" in html)

resp = anon.post("/feedback/", {"message": "zu kurz"})
check("Zu kurze Beschreibung -> 400", resp.status_code == 400, f"got {resp.status_code}")
check("Keine zweite Meldung angelegt", ProblemReport.objects.count() == 1)

# Rate-Limit: 5/Stunde je IP
for i in range(4):
    anon.post("/feedback/", {"message": f"Ratelimit-Testmeldung Nummer {i} mit genug Zeichen."})
check("5 Meldungen erlaubt", ProblemReport.objects.count() == 5)
resp = anon.post("/feedback/", {"message": "Die sechste Meldung sollte geblockt werden."})
check("6. Meldung -> 429", resp.status_code == 429, f"got {resp.status_code}")
check("Rate-Limit greift", ProblemReport.objects.count() == 5)

# Angemeldeter Nutzer: Konto wird verknüpft
user = User.objects.create_user(email="konto@example.org", password="pw-Smoke-1!")
logged_in = Client(REMOTE_ADDR="10.9.8.7")
logged_in.force_login(user)
resp = logged_in.get("/feedback/")
html = resp.content.decode("utf-8")
check("Angemeldet: Konto-Hinweis statt E-Mail-Feld", "konto@example.org" in html and "mit deinem Konto anmelden" not in html)
resp = logged_in.post("/feedback/", {"message": "Meldung eines angemeldeten Kontos mit genug Text."})
report_user = ProblemReport.objects.order_by("-created_at").first()
check("Konto verknüpft", report_user.user_id == user.id)
check("Rückmeldeadresse = Konto-Adresse", report_user.reporter_email == "konto@example.org")

# =============================================================================
print()
print("=== Phase C: Admin-Ticket + Rückmeldung ===")
from apps.common.admin import ProblemReportAdmin  # noqa: E402
from django.contrib import admin as dj_admin  # noqa: E402

admin_instance = ProblemReportAdmin(ProblemReport, dj_admin.site)
report.admin_note = "Ursache behoben, Verbindungslimit erhöht."
report.save()

mail.outbox = []
admin_request = factory.post("/admin/")
admin_request.user = user


class _Messages:
    def add(self, *args, **kwargs):
        pass


from unittest.mock import patch  # noqa: E402

with patch.object(ProblemReportAdmin, "message_user", lambda *a, **k: None):
    admin_instance.mark_resolved_and_notify(admin_request, ProblemReport.objects.filter(pk=report.pk))

report.refresh_from_db()
check("Status auf gelöst", report.status == "resolved" and report.resolved_at is not None)
check("Rückmeldung versandt", len(mail.outbox) == 1 and report.notified_at is not None)
check("Mail an meldende Person", mail.outbox and mail.outbox[0].to == ["melder@example.org"])
check("Mail enthält Ticket-Nr. + Anmerkung",
      mail.outbox and report.reference in mail.outbox[0].body and "Verbindungslimit" in mail.outbox[0].body)

# Anonyme Meldung ohne Kontakt: keine Mail
anon_report = ProblemReport.objects.filter(email="", user__isnull=True).first()
mail.outbox = []
with patch.object(ProblemReportAdmin, "message_user", lambda *a, **k: None):
    admin_instance.mark_resolved_and_notify(admin_request, ProblemReport.objects.filter(pk=anon_report.pk))
anon_report.refresh_from_db()
check("Ohne Kontakt: gelöst ohne Mail", anon_report.status == "resolved" and len(mail.outbox) == 0)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
