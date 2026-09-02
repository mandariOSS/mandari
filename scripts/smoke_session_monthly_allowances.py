# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Monatliche Pauschalen nach EntschVO NRW.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_monthly_allowances.py

Prüft:
- Pauschalen-Katalog (anlegen, Betrag-Parsing, Löschen inkl. Schutz nach Abrechnung)
- Zuordnungen mit Zeitraum (aktiv/abgelaufen)
- Monatslauf (idempotent, Betrag-Snapshot), Genehmigung
- CSV-Export mit Bankdaten, SEPA-Export (markiert als ausgezahlt)
- Jahresbericht enthält Pauschalen-Spalte
- Berechtigungen und Tenant-Isolation
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import date
from decimal import Decimal
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
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionPerson,
    SessionPersonMonthlyRate,
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
# Setup
# =============================================================================
today = timezone.localdate()
year, month = today.year, today.month
period = date(year, month, 1)

tenant = SessionTenant.objects.create(name="Pauschalenstadt", slug="pauschalenstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-mp")

p1 = SessionPerson.objects.create(tenant=tenant, given_name="Frida", family_name="FRAKTIONSVORSITZ")
p1.set_bank_iban_encrypted("DE02120300000000202051")
p1.set_bank_account_holder_encrypted("Frida Fraktionsvorsitz")
p1.save()
p2 = SessionPerson.objects.create(tenant=tenant, given_name="Emil", family_name="EHEMALIG")
p_b = SessionPerson.objects.create(tenant=tenant_b, given_name="Fritz", family_name="FREMD")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-mp@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True)
viewer_user = User.objects.create_user(email="leser-mp@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Katalog + Zuordnungen ===")
resp = admin.get(f"{base}/allowances/monthly/")
html = resp.content.decode("utf-8")
check("Seite -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("NRW-Hinweis vorhanden", "EntschVO NRW" in html)

resp = admin.post(
    f"{base}/allowances/monthly/rate/",
    {"name": "Aufwandsentschädigung (Teilpauschale)", "amount": "322,50", "legal_basis": "§ 2 Abs. 1 EntschVO NRW"},
)
resp = admin.post(
    f"{base}/allowances/monthly/rate/",
    {"name": "Zulage Fraktionsvorsitz", "amount": "874.00", "legal_basis": "§ 5 Abs. 6 EntschVO NRW"},
)
check("2 Pauschalen angelegt", SessionMonthlyRate.objects.filter(tenant=tenant).count() == 2)
teilpauschale = SessionMonthlyRate.objects.get(name__contains="Teilpauschale")
zulage = SessionMonthlyRate.objects.get(name__contains="Fraktionsvorsitz")
check("Komma-Betrag geparst", teilpauschale.amount == Decimal("322.50"), str(teilpauschale.amount))

resp = admin.post(f"{base}/allowances/monthly/rate/", {"name": "Kaputt", "amount": "abc"})
check("Ungültiger Betrag abgelehnt", SessionMonthlyRate.objects.filter(tenant=tenant).count() == 2)

# Zuordnungen: p1 beide, p2 nur Teilpauschale aber bereits ausgeschieden
resp = admin.post(f"{base}/allowances/monthly/assignment/", {"person": str(p1.id), "rate": str(teilpauschale.id)})
resp = admin.post(f"{base}/allowances/monthly/assignment/", {"person": str(p1.id), "rate": str(zulage.id)})
resp = admin.post(
    f"{base}/allowances/monthly/assignment/",
    {"person": str(p2.id), "rate": str(teilpauschale.id), "end_date": "2020-12-31"},
)
check("3 Zuordnungen", SessionPersonMonthlyRate.objects.filter(person__tenant=tenant).count() == 3)

resp = viewer.get(f"{base}/allowances/monthly/")
check("Ohne manage_allowances -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase B: Monatslauf + Genehmigung ===")
resp = admin.post(f"{base}/allowances/monthly/generate/", {"year": year, "month": month})
allowances = SessionMonthlyAllowance.objects.filter(tenant=tenant, period=period)
check("Monatslauf: 2 Posten (nur aktive Zuordnungen)", allowances.count() == 2, f"got {allowances.count()}")
check("Abgelaufene Zuordnung ausgelassen", not allowances.filter(person=p2).exists())
check("Betrag-Snapshot", allowances.filter(rate=zulage, amount=Decimal("874.00")).exists())

# Idempotenz + Satzänderung ändert bestehende Posten nicht
zulage.amount = Decimal("900.00")
zulage.save()
resp = admin.post(f"{base}/allowances/monthly/generate/", {"year": year, "month": month})
allowances = SessionMonthlyAllowance.objects.filter(tenant=tenant, period=period)
check("Idempotent (weiterhin 2 Posten)", allowances.count() == 2)
check("Bestehender Posten behält alten Betrag", allowances.get(rate=zulage).amount == Decimal("874.00"))

resp = admin.post(f"{base}/allowances/monthly/approve/", {"year": year, "month": month})
check("Alle genehmigt", allowances.filter(status="approved").count() == 2)

# =============================================================================
print()
print("=== Phase C: Exporte ===")
resp = admin.post(f"{base}/allowances/monthly/export/csv/", {"year": year, "month": month})
csv_text = resp.content.decode("utf-8")
check("CSV -> 200", resp.status_code == 200 and "text/csv" in resp["Content-Type"])
check("CSV: Pauschale + IBAN", "Fraktionsvorsitz" in csv_text and "DE02120300000000202051" in csv_text)
check("CSV: Rechtsgrundlage", "EntschVO NRW" in csv_text)

# SEPA ohne Auftraggeberkonto -> Fehler
resp = admin.post(f"{base}/allowances/monthly/export/sepa/", {"year": year, "month": month})
check("SEPA ohne Debitorkonto blockiert", resp.status_code == 302)

tenant.settings = {"allowances": {"debtor_name": "Stadt Pauschalenstadt", "debtor_iban": "DE02100100100006820101"}}
tenant.save(update_fields=["settings"])
resp = admin.post(f"{base}/allowances/monthly/export/sepa/", {"year": year, "month": month})
check("SEPA -> 200 XML", resp.status_code == 200 and "xml" in resp["Content-Type"], f"got {resp.status_code}")
xml = resp.content.decode("utf-8")
check("SEPA: Summe beider Pauschalen", "1196.50" in xml, "")
allowances = SessionMonthlyAllowance.objects.filter(tenant=tenant, period=period)
check("Posten als ausgezahlt markiert", allowances.filter(status="paid").count() == 2)
check("Export-Referenz gesetzt", all(a.export_reference for a in allowances))

# =============================================================================
print()
print("=== Phase D: Jahresbericht + Schutz ===")
resp = admin.get(f"{base}/reports/?year={year}")
html = resp.content.decode("utf-8")
check("Bericht: Pauschalen-Spalte", "Pauschalen" in html)
check("Bericht: Summe enthält Pauschalen", "1196.50" in html or "1196,50" in html, "")

resp = admin.post(f"{base}/allowances/monthly/rate/delete/", {"rate_id": str(zulage.id)})
check("Löschen nach Abrechnung blockiert", SessionMonthlyRate.objects.filter(pk=zulage.id).exists())

unused = SessionMonthlyRate.objects.create(tenant=tenant, name="Ungenutzt", amount=Decimal("10.00"))
resp = admin.post(f"{base}/allowances/monthly/rate/delete/", {"rate_id": str(unused.id)})
check("Ungenutzte Pauschale löschbar", not SessionMonthlyRate.objects.filter(pk=unused.id).exists())

# Tenant-Isolation
rate_b = SessionMonthlyRate.objects.create(tenant=tenant_b, name="Fremdpauschale", amount=Decimal("99.00"))
resp = admin.post(f"{base}/allowances/monthly/assignment/", {"person": str(p_b.id), "rate": str(rate_b.id)})
check("Fremde Person/Pauschale nicht zuordenbar", not SessionPersonMonthlyRate.objects.filter(person=p_b).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
