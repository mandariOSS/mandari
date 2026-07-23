# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Wahlperioden im Session RIS (Issue #39).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_terms.py

Prüft:
- Perioden-Verwaltung ohne Django-Admin (anlegen, bearbeiten, löschen)
  inkl. Permission-Checks und Tenant-Isolation
- Automatische Perioden-Zuordnung bei neuen Sitzungen und Besetzungen
- Perioden-Filter in Sitzungs-, Vorlagen- und Gremienliste sowie
  Besetzung je Periode auf der Gremien-Detailseite
- Periodenwechsel-Assistent: Übernahme der Besetzungen bzw. Neubesetzen;
  Alt-Daten bleiben unter der alten Periode auffindbar
- Archiv-Ansicht mit Kennzahlen je Periode
- Audit-Einträge für Perioden-Mutationen
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

from datetime import date  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
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
other_tenant = SessionTenant.objects.create(name="Stadt Anderswo", slug="anderswo")

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(roles["admin"])
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(roles["viewer"])

admin = Client()
admin.force_login(admin_user)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Perioden-Verwaltung (UI, Permissions, Tenant-Isolation)
# =============================================================================
print("=== Phase A: Perioden-Verwaltung ===")

resp = admin.get(f"{base}/settings/terms/")
check("Perioden-Seite (Admin) -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = viewer.get(f"{base}/settings/terms/")
check("Perioden-Seite (Viewer) -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = admin.post(
    f"{base}/settings/terms/save/",
    {"name": "Wahlperiode 2020-2025", "start_date": "2020-11-01", "end_date": "2025-10-31"},
)
term_old = SessionLegislativeTerm.objects.filter(tenant=tenant, name="Wahlperiode 2020-2025").first()
check("Periode angelegt", resp.status_code == 302 and term_old is not None)

resp = viewer.post(f"{base}/settings/terms/save/", {"name": "Hack", "start_date": "2020-01-01"})
check(
    "Periode anlegen ohne Recht -> 403",
    resp.status_code == 403 and not SessionLegislativeTerm.objects.filter(name="Hack").exists(),
)

# Bearbeiten
resp = admin.post(
    f"{base}/settings/terms/save/",
    {"term_id": str(term_old.id), "name": "Wahlperiode 2020–2025", "start_date": "2020-11-01", "end_date": ""},
)
term_old.refresh_from_db()
check("Periode bearbeitet (Name, Ende offen)", term_old.name == "Wahlperiode 2020–2025" and term_old.end_date is None)

# Plausibilität: Beginn nach Ende
resp = admin.post(
    f"{base}/settings/terms/save/",
    {"name": "Kaputt", "start_date": "2030-01-01", "end_date": "2020-01-01"},
)
check("Beginn nach Ende wird abgelehnt", not SessionLegislativeTerm.objects.filter(name="Kaputt").exists())

# Tenant-Isolation: fremde Periode nicht bearbeitbar
foreign_term = SessionLegislativeTerm.objects.create(tenant=other_tenant, name="Fremde Periode")
resp = admin.post(f"{base}/settings/terms/save/", {"term_id": str(foreign_term.id), "name": "Gekapert"})
foreign_term.refresh_from_db()
check("Fremde Periode -> 404", resp.status_code == 404 and foreign_term.name == "Fremde Periode")

# Audit
check(
    "Audit-Eintrag für Perioden-Anlage",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionLegislativeTerm", action="create").exists(),
)

# =============================================================================
# Phase B: Automatische Zuordnung (Sitzung + Besetzung)
# =============================================================================
print("=== Phase B: Automatische Perioden-Zuordnung ===")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
person_a = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Alt")
person_b = SessionPerson.objects.create(tenant=tenant, given_name="Bernd", family_name="Beispiel")

resp = admin.post(
    f"{base}/organizations/{org.id}/memberships/add/",
    {"person": str(person_a.id), "role": "chair", "has_voting_rights": "on", "start_date": "2021-01-15"},
)
membership_a = SessionOrganizationMembership.objects.filter(organization=org, person=person_a).first()
check(
    "Besetzung erhält Wahlperiode automatisch",
    membership_a is not None and membership_a.legislative_term_id == term_old.id,
)

resp = admin.post(
    f"{base}/meetings/create/",
    {
        "name": "Ratssitzung Juni",
        "organization": str(org.id),
        "start": "2024-06-12 17:00",
        "end": "2024-06-12 19:00",
        "is_public": "on",
    },
)
meeting_old = SessionMeeting.objects.filter(tenant=tenant, name="Ratssitzung Juni").first()
check(
    "Sitzung erhält Wahlperiode automatisch",
    meeting_old is not None and meeting_old.legislative_term_id == term_old.id,
    f"status={resp.status_code}",
)

paper_old = SessionPaper.objects.create(
    tenant=tenant, name="Alte Vorlage", reference="V/2024/0001", date=date(2024, 6, 1), is_public=True
)

# Löschen einer benutzten Periode ist gesperrt
resp = admin.post(f"{base}/settings/terms/{term_old.id}/delete/")
check("Benutzte Periode nicht löschbar", SessionLegislativeTerm.objects.filter(pk=term_old.pk).exists())

# =============================================================================
# Phase C: Periodenwechsel-Assistent (Übernahme)
# =============================================================================
print("=== Phase C: Periodenwechsel ===")

resp = admin.post(
    f"{base}/settings/terms/change/",
    {"name": "Wahlperiode 2025-2030", "start_date": "2025-11-01", "end_date": "2030-10-31", "mode": "carry"},
)
term_new = SessionLegislativeTerm.objects.filter(tenant=tenant, name="Wahlperiode 2025-2030").first()
check("Neue Periode angelegt", resp.status_code == 302 and term_new is not None)

term_old.refresh_from_db()
check("Alte Periode automatisch abgeschlossen", term_old.end_date == date(2025, 10, 31))

membership_a.refresh_from_db()
check(
    "Alt-Besetzung zum Stichtag beendet",
    membership_a.end_date == date(2025, 10, 31) and membership_a.legislative_term_id == term_old.id,
)

carried = SessionOrganizationMembership.objects.filter(
    organization=org, person=person_a, legislative_term=term_new, end_date__isnull=True
).first()
check(
    "Besetzung in neue Periode übernommen (Funktion/Stimmrecht)",
    carried is not None and carried.role == "chair" and carried.has_voting_rights and carried.start_date == date(2025, 11, 1),
)

meeting_old.refresh_from_db()
check("Alte Sitzung bleibt in alter Periode", meeting_old.legislative_term_id == term_old.id)

check(
    "Periodenwechsel im Audit-Log",
    SessionAuditLog.objects.filter(
        tenant=tenant, model_name="SessionLegislativeTerm", changes__has_key="periodenwechsel"
    ).exists()
    or SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionLegislativeTerm")
    .exclude(changes={})
    .exists(),
)

# Neue Sitzung landet in der neuen Periode
resp = admin.post(
    f"{base}/meetings/create/",
    {
        "name": "Konstituierende Sitzung",
        "organization": str(org.id),
        "start": "2025-11-20 17:00",
        "is_public": "on",
    },
)
meeting_new = SessionMeeting.objects.filter(tenant=tenant, name="Konstituierende Sitzung").first()
check(
    "Neue Sitzung in neuer Periode",
    meeting_new is not None and meeting_new.legislative_term_id == term_new.id,
    f"status={resp.status_code}",
)

# Nachrücker in der neuen Periode
resp = admin.post(
    f"{base}/memberships/{carried.id}/succession/",
    {"successor": str(person_b.id), "change_date": "2026-02-01"},
)
successor = SessionOrganizationMembership.objects.filter(
    organization=org, person=person_b, end_date__isnull=True
).first()
check(
    "Nachrücker erhält neue Periode",
    successor is not None and successor.legislative_term_id == term_new.id,
)

# =============================================================================
# Phase D: Filter + Archiv
# =============================================================================
print("=== Phase D: Perioden-Filter und Archiv ===")

resp = admin.get(f"{base}/meetings/", {"term": str(term_old.id)})
names = [m.name for m in resp.context["meetings"]]
check("Sitzungsliste gefiltert (alte Periode)", names == ["Ratssitzung Juni"], str(names))

resp = admin.get(f"{base}/meetings/", {"term": str(term_new.id)})
names = [m.name for m in resp.context["meetings"]]
check("Sitzungsliste gefiltert (neue Periode)", names == ["Konstituierende Sitzung"], str(names))

resp = admin.get(f"{base}/papers/", {"term": str(term_old.id)})
names = [p.name for p in resp.context["papers"]]
check("Vorlagenliste gefiltert (Zeitraum)", names == ["Alte Vorlage"], str(names))

resp = admin.get(f"{base}/papers/", {"term": str(term_new.id)})
check("Vorlagenliste: alte Vorlage nicht in neuer Periode", "Alte Vorlage" not in [p.name for p in resp.context["papers"]])

resp = admin.get(f"{base}/organizations/", {"term": str(term_old.id)})
check("Gremienliste gefiltert (Besetzungen der Periode)", org in list(resp.context["organizations"]))

resp = admin.get(f"{base}/organizations/{org.id}/", {"term": str(term_old.id)})
rows = list(resp.context["memberships"])
check(
    "Gremien-Detail: Besetzung je Periode (alt)",
    len(rows) == 1 and rows[0].person_id == person_a.id,
    f"{len(rows)} Zeilen",
)

resp = admin.get(f"{base}/organizations/{org.id}/")
rows = list(resp.context["memberships"])
check(
    "Gremien-Detail: aktive Besetzung (Nachrücker)",
    len(rows) == 1 and rows[0].person_id == person_b.id,
    f"{len(rows)} Zeilen",
)

resp = admin.get(f"{base}/archive/")
check("Archiv-Ansicht -> 200", resp.status_code == 200, f"got {resp.status_code}")
archive_rows = {row["term"].pk: row for row in resp.context["rows"]}
check(
    "Archiv: Kennzahlen alte Periode",
    archive_rows[term_old.pk]["meeting_count"] == 1
    and archive_rows[term_old.pk]["paper_count"] == 1
    and archive_rows[term_old.pk]["membership_count"] == 1,
)
check("Archiv: neue Periode als aktuell markiert", archive_rows[term_new.pk]["is_current"])

resp = viewer.get(f"{base}/archive/")
check("Archiv für Viewer (view_meetings) -> 200", resp.status_code == 200, f"got {resp.status_code}")

# OParl: legislativeTerm am Body (Bestandsfunktion aus Issue #35)
resp = admin.get(f"{base}/api/oparl/body/")
body_json = resp.json()
check(
    "OParl-Body liefert legislativeTerm",
    resp.status_code == 200 and len(body_json.get("legislativeTerm", [])) >= 2,
)

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
