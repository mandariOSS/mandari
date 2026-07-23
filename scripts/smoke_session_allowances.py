# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Sitzungsgeld-Abrechnung im Session RIS (Issue #38).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_allowances.py

Prüft:
- Entschädigungssätze je Gremium/Funktion (Fallback: Gremiums-Standardsatz)
- Abrechnungslauf aus Anwesenheiten (present/joined_late/left_early,
  keine Gäste, keine abgesagten Sitzungen), idempotent
- Genehmigung mit Vier-Augen-Prinzip (Ersteller darf nicht selbst genehmigen)
- CSV-Export mit Bankdaten (verschlüsselte Accessoren), SEPA-pain.001-XML
  inkl. Export-Referenz, Status "Ausgezahlt" und Überspringen ohne IBAN
- Abrechnungsmitteilung als PDF, Jahresübersicht (Seite + CSV)
- Permission-Checks (manage_allowances) und Tenant-Isolation
- Audit-Einträge für Lauf und Exporte
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

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from xml.etree.ElementTree import fromstring  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAllowance,
    SessionAllowanceRate,
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from django.utils import timezone  # noqa: E402

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
other_tenant = SessionTenant.objects.create(name="Stadt Anderswo", slug="anderswo")

kaemmerei_user = User.objects.create_user(email="kaemmerei@example.org", password="pw-Smoke-Test-1!")
pruefer_user = User.objects.create_user(email="pruefer@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_kaemmerei = SessionUser.objects.create(user=kaemmerei_user, tenant=tenant)
su_kaemmerei.roles.add(roles["admin"])
su_pruefer = SessionUser.objects.create(user=pruefer_user, tenant=tenant)
su_pruefer.roles.add(roles["admin"])
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(roles["viewer"])

kaemmerei = Client()
kaemmerei.force_login(kaemmerei_user)
pruefer = Client()
pruefer.force_login(pruefer_user)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

org = SessionOrganization.objects.create(
    tenant=tenant, name="Hauptausschuss", organization_type="committee", allowance_amount=Decimal("20.00")
)
foreign_org = SessionOrganization.objects.create(tenant=other_tenant, name="Fremder Rat")

person_chair = SessionPerson.objects.create(tenant=tenant, given_name="Vera", family_name="Vorsitz")
person_chair.set_bank_account_holder_encrypted("Vera Vorsitz")
person_chair.set_bank_iban_encrypted("DE02 1203 0000 0000 2020 51")
person_chair.set_bank_bic_encrypted("BYLADEM1001")
person_chair.save()

person_member = SessionPerson.objects.create(tenant=tenant, given_name="Max", family_name="Mitglied")
person_member.set_bank_iban_encrypted("DE02120300000000202052")
person_member.save()

person_no_iban = SessionPerson.objects.create(tenant=tenant, given_name="Nora", family_name="Ohnekonto")

start = timezone.now() - timedelta(days=10)
meeting = SessionMeeting.objects.create(tenant=tenant, name="Sitzung Juli", organization=org, start=start)
cancelled_meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Abgesagte Sitzung", organization=org, start=start + timedelta(days=1), cancelled=True
)

SessionAttendance.objects.create(meeting=meeting, person=person_chair, status="present", role="chair")
SessionAttendance.objects.create(meeting=meeting, person=person_member, status="joined_late", role="member")
SessionAttendance.objects.create(meeting=meeting, person=person_no_iban, status="present", role="member")
# Nicht anrechenbar: Gast + abwesend + abgesagte Sitzung
guest = SessionPerson.objects.create(tenant=tenant, given_name="Gast", family_name="Gast")
SessionAttendance.objects.create(meeting=meeting, person=guest, status="present", role="guest")
absent = SessionPerson.objects.create(tenant=tenant, given_name="Abel", family_name="Abwesend")
SessionAttendance.objects.create(meeting=meeting, person=absent, status="absent", role="member")
SessionAttendance.objects.create(meeting=cancelled_meeting, person=person_member, status="present", role="member")

period = {"from": (start - timedelta(days=5)).date().isoformat(), "to": timezone.localdate().isoformat()}

# =============================================================================
# Phase A: Zugriff + Sätze
# =============================================================================
print("=== Phase A: Zugriff und Entschädigungssätze ===")

resp = kaemmerei.get(f"{base}/allowances/")
check("Sitzungsgeld-Seite (manage_allowances) -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = viewer.get(f"{base}/allowances/")
check("Sitzungsgeld-Seite (Viewer) -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = kaemmerei.post(
    f"{base}/allowances/rates/save/",
    {"organization": str(org.id), "role": "chair", "amount": "30,00"},
)
rate = SessionAllowanceRate.objects.filter(organization=org, role="chair").first()
check("Satz je Funktion gespeichert (30 € Vorsitz)", rate is not None and rate.amount == Decimal("30.00"))

resp = kaemmerei.post(
    f"{base}/allowances/rates/save/",
    {"organization": str(foreign_org.id), "role": "member", "amount": "99"},
)
check(
    "Satz für fremdes Gremium -> 404",
    resp.status_code == 404 and not SessionAllowanceRate.objects.filter(organization=foreign_org).exists(),
)

resp = viewer.post(
    f"{base}/allowances/rates/save/",
    {"organization": str(org.id), "role": "member", "amount": "5"},
)
check("Satz ohne Recht -> 403", resp.status_code == 403)

# =============================================================================
# Phase B: Abrechnungslauf
# =============================================================================
print("=== Phase B: Abrechnungslauf ===")

resp = kaemmerei.post(f"{base}/allowances/generate/", period)
allowances = SessionAllowance.objects.filter(attendance__meeting__tenant=tenant)
check("Lauf erzeugt 3 Positionen (Vorsitz, verspätet, ohne IBAN)", allowances.count() == 3, f"{allowances.count()}")

chair_allowance = allowances.filter(attendance__person=person_chair).first()
member_allowance = allowances.filter(attendance__person=person_member).first()
check("Vorsitz-Satz angewendet (30 €)", chair_allowance is not None and chair_allowance.amount == Decimal("30.00"))
check(
    "Fallback Gremiums-Satz (20 €) + Verspätet zählt",
    member_allowance is not None and member_allowance.amount == Decimal("20.00"),
)
check(
    "Gast/Abwesend/abgesagte Sitzung ohne Position",
    not allowances.filter(attendance__person__in=[guest, absent]).exists()
    and not allowances.filter(attendance__meeting=cancelled_meeting).exists(),
)

resp = kaemmerei.post(f"{base}/allowances/generate/", period)
check("Lauf ist idempotent", SessionAllowance.objects.filter(attendance__meeting__tenant=tenant).count() == 3)

check(
    "Abrechnungslauf im Audit-Log",
    SessionAuditLog.objects.filter(tenant=tenant, changes__has_key="abrechnungslauf").exists(),
)

# =============================================================================
# Phase C: Genehmigung (Vier-Augen)
# =============================================================================
print("=== Phase C: Genehmigung (Vier-Augen-Prinzip) ===")

resp = kaemmerei.post(f"{base}/allowances/approve/", period)
check(
    "Ersteller kann nicht selbst genehmigen",
    SessionAllowance.objects.filter(attendance__meeting__tenant=tenant, status="approved").count() == 0,
)

resp = pruefer.post(f"{base}/allowances/approve/", period)
approved = SessionAllowance.objects.filter(attendance__meeting__tenant=tenant, status="approved")
check("Zweite Person genehmigt alle Positionen", approved.count() == 3, f"{approved.count()}")
chair_allowance.refresh_from_db()
check("Genehmigt-Metadaten gesetzt", chair_allowance.approved_by_id == su_pruefer.pk and chair_allowance.approved_at)

# =============================================================================
# Phase D: Exporte (CSV + SEPA)
# =============================================================================
print("=== Phase D: CSV- und SEPA-Export ===")

resp = kaemmerei.get(f"{base}/allowances/export.csv", period)
csv_text = resp.content.decode("utf-8")
check("CSV-Export -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("CSV enthält IBAN (Accessor)", "DE02120300000000202051" in csv_text.replace(" ", ""))
check("CSV enthält Beträge", "30,00" in csv_text and "20,00" in csv_text)

resp = viewer.get(f"{base}/allowances/export.csv", period)
check("CSV ohne Recht -> 403", resp.status_code == 403)

# SEPA ohne Auftraggeberkonto -> Fehler-Redirect
resp = kaemmerei.post(f"{base}/allowances/export/sepa/", period)
check("SEPA ohne Auftraggeberkonto abgelehnt", resp.status_code == 302)

resp = kaemmerei.post(
    f"{base}/allowances/debtor/save/",
    {"debtor_name": "Stadt Musterstadt", "debtor_iban": "DE89 3704 0044 0532 0130 00", "debtor_bic": "COBADEFFXXX"},
)
tenant.refresh_from_db()
check(
    "Auftraggeberkonto gespeichert",
    tenant.settings.get("allowances", {}).get("debtor_iban") == "DE89370400440532013000",
)

resp = kaemmerei.post(f"{base}/allowances/export/sepa/", period)
check("SEPA-Export -> 200 (XML)", resp.status_code == 200 and b"pain.001.001.03" in resp.content)

root = fromstring(resp.content)
ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"}
nb_of_txs = root.findtext(".//p:GrpHdr/p:NbOfTxs", namespaces=ns)
ctrl_sum = root.findtext(".//p:GrpHdr/p:CtrlSum", namespaces=ns)
check("SEPA: 2 Transaktionen (Person ohne IBAN übersprungen)", nb_of_txs == "2", f"NbOfTxs={nb_of_txs}")
check("SEPA: Kontrollsumme 50.00", ctrl_sum == "50.00", f"CtrlSum={ctrl_sum}")
ibans = [e.text for e in root.findall(".//p:CdtTrfTxInf/p:CdtrAcct/p:Id/p:IBAN", namespaces=ns)]
check("SEPA: Empfänger-IBANs enthalten", sorted(ibans) == ["DE02120300000000202051", "DE02120300000000202052"])

chair_allowance.refresh_from_db()
member_allowance.refresh_from_db()
no_iban_allowance = SessionAllowance.objects.get(attendance__person=person_no_iban)
check(
    "Exportierte Positionen ausgezahlt + Referenz",
    chair_allowance.status == "paid"
    and chair_allowance.export_reference.startswith("SG-")
    and member_allowance.status == "paid",
)
check("Position ohne IBAN bleibt genehmigt", no_iban_allowance.status == "approved" and not no_iban_allowance.export_reference)

check(
    "SEPA-Export im Audit-Log",
    SessionAuditLog.objects.filter(tenant=tenant, action="download", changes__has_key="sitzungsgeld_export").exists(),
)

# =============================================================================
# Phase E: Mitteilung + Jahresübersicht
# =============================================================================
print("=== Phase E: Abrechnungsmitteilung und Jahresübersicht ===")

resp = kaemmerei.get(f"{base}/allowances/notice/{person_chair.id}.pdf", period)
check(
    "Abrechnungsmitteilung PDF -> 200",
    resp.status_code == 200 and resp.content.startswith(b"%PDF"),
    f"got {resp.status_code}",
)

year = timezone.localdate().year
resp = kaemmerei.get(f"{base}/allowances/year/", {"year": year})
check("Jahresübersicht -> 200", resp.status_code == 200, f"got {resp.status_code}")
rows = {row["person"].pk: row for row in resp.context["rows"]}
check(
    "Jahresübersicht: Summen je Person",
    rows[person_chair.pk]["paid"] == Decimal("30.00") and rows[person_no_iban.pk]["approved"] == Decimal("20.00"),
)

resp = kaemmerei.get(f"{base}/allowances/year/", {"year": year, "format": "csv"})
check("Jahresübersicht CSV -> 200", resp.status_code == 200 and "Vorsitz" in resp.content.decode("utf-8"))

resp = viewer.get(f"{base}/allowances/year/", {"year": year})
check("Jahresübersicht ohne Recht -> 403", resp.status_code == 403)

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
