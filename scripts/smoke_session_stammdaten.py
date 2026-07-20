# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Stammdaten-Verwaltung im Session-UI (Issue #27).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_stammdaten.py

Prüft:
- Gremien-CRUD inkl. Sitzungsturnus, Ladungsfrist, Mitgliederzahl
- Personen-CRUD: Kontaktdaten verschlüsselt über Accessoren; Bankdaten
  (IBAN) nur für Berechtigte sichtbar/editierbar
- Besetzungs-CRUD: Rolle (inkl. sachkundige/r Bürger/in), Stimmrecht,
  Vertreterregelung, Zeitraum; Nachrücker-Flow in einem Schritt
- Benutzerverwaltung: Einladung per E-Mail (bestehendes Konto + neues
  Konto via Token), Rollen zuweisen/entziehen, Deaktivieren mit
  Letzter-Admin-Schutz
- Audit-Einträge und Permission-Checks
- Kompletter Lebenszyklus ohne Django-Admin
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
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

# SQLite-Robustheit unter Windows: laengere Busy-Timeouts gegen
# transiente "database is locked"-Fehler (Virenscanner/Indexer).
from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionInvitation,
    SessionOrganization,
    SessionOrganizationMembership,
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

admin_user = User.objects.create_user(email="admin@example.org", password="pw-Smoke-Test-1!")
clerk_user = User.objects.create_user(email="clerk@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(roles["admin"])
su_clerk = SessionUser.objects.create(user=clerk_user, tenant=tenant)
su_clerk.roles.add(roles["clerk"])
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(roles["viewer"])

admin = Client()
admin.force_login(admin_user)
clerk = Client()
clerk.force_login(clerk_user)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Gremien-CRUD
# =============================================================================
print("=== Phase A: Gremien-CRUD ===")

resp = admin.get(f"{base}/organizations/create/")
check("Gremium-Formular -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = admin.post(
    f"{base}/organizations/create/",
    {
        "name": "Hauptausschuss",
        "short_name": "HA",
        "organization_type": "committee",
        "meeting_frequency": "monatlich",
        "invitation_period_days": "10",
        "target_member_count": "15",
        "allowance_amount": "30.00",
        "is_active": "on",
    },
)
check("Gremium angelegt -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
org = SessionOrganization.objects.get(tenant=tenant, name="Hauptausschuss")
check("Sitzungsturnus gespeichert", org.meeting_frequency == "monatlich")
check("Ladungsfrist gespeichert", org.invitation_period_days == 10)
check("Soll-Mitgliederzahl gespeichert", org.target_member_count == 15)
check("Audit: create-Eintrag Gremium", SessionAuditLog.objects.filter(object_id=org.id, action="create").exists())

resp = admin.post(
    f"{base}/organizations/{org.id}/edit/",
    {
        "name": "Haupt- und Finanzausschuss",
        "organization_type": "committee",
        "meeting_frequency": "monatlich",
        "invitation_period_days": "7",
        "allowance_amount": "30.00",
        "is_active": "on",
    },
)
org.refresh_from_db()
check("Gremium bearbeitet", org.name == "Haupt- und Finanzausschuss")

# Ohne Berechtigung
resp = clerk.post(
    f"{base}/organizations/create/",
    {
        "name": "Schattenausschuss",
        "organization_type": "committee",
        "invitation_period_days": "7",
        "allowance_amount": "0",
    },
)
check("Gremium-Anlage ohne manage_organizations -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Kein Gremium angelegt", not SessionOrganization.objects.filter(name="Schattenausschuss").exists())

# =============================================================================
# Phase B: Personen-CRUD (verschlüsselt, Bankdaten nur für Berechtigte)
# =============================================================================
print()
print("=== Phase B: Personen-CRUD ===")

IBAN = "DE89370400440532013000"
resp = admin.post(
    f"{base}/persons/create/",
    {
        "given_name": "Erika",
        "family_name": "Mustermann",
        "email": "erika@example.org",
        "phone": "0251 4920",
        "address": "Prinzipalmarkt 10",
        "bank_account_holder": "Erika Mustermann",
        "bank_iban": IBAN,
        "bank_bic": "COBADEFFXXX",
        "is_active": "on",
    },
)
check("Person angelegt -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
person = SessionPerson.objects.get(tenant=tenant, family_name="Mustermann")
check("Telefon über Accessor entschlüsselbar", person.get_phone_decrypted() == "0251 4920")
check("IBAN über Accessor entschlüsselbar", person.get_bank_iban_decrypted() == IBAN)

# DB-Rohwert: kein Klartext
with connection.cursor() as cur:
    cur.execute("SELECT bank_iban_encrypted, phone_encrypted FROM session_persons WHERE id = %s", [person.id.hex])
    raw_iban, raw_phone = cur.fetchone()
check("IBAN in DB nicht im Klartext", raw_iban and IBAN.encode() not in bytes(raw_iban))
check("Telefon in DB nicht im Klartext", raw_phone and b"0251 4920" not in bytes(raw_phone))

# Bankdaten-Sichtbarkeit: Admin sieht sie, Clerk (ohne manage_allowances)... clerk hat manage_attendance
resp = admin.get(f"{base}/persons/{person.id}/")
check("Admin sieht Bankdaten auf Detailseite", IBAN.encode() in resp.content)

resp = viewer.get(f"{base}/persons/{person.id}/")
check("Viewer sieht KEINE Bankdaten", IBAN.encode() not in resp.content, f"status={resp.status_code}")

resp = clerk.get(f"{base}/persons/{person.id}/")
check("Clerk (ohne manage_allowances) sieht KEINE Bankdaten", IBAN.encode() not in resp.content)

# Bearbeiten ohne Bank-Berechtigung darf Bankdaten nicht löschen — Clerk hat
# kein manage_organizations, daher 403; wir testen stattdessen: Admin-Edit
# ohne Bankfelder-Aenderung erhaelt Telefon
resp = admin.post(
    f"{base}/persons/{person.id}/edit/",
    {
        "given_name": "Erika",
        "family_name": "Mustermann-Schmidt",
        "email": "erika@example.org",
        "phone": "0251 4920",
        "address": "Prinzipalmarkt 10",
        "bank_account_holder": "Erika Mustermann",
        "bank_iban": IBAN,
        "bank_bic": "COBADEFFXXX",
        "is_active": "on",
    },
)
person.refresh_from_db()
check("Person bearbeitet", person.family_name == "Mustermann-Schmidt")
check("Audit: update-Eintrag Person", SessionAuditLog.objects.filter(object_id=person.id, action="update").exists())
audit_text = " ".join(str(e.changes) for e in SessionAuditLog.objects.filter(object_id=person.id))
check("Audit enthält keine Klartext-IBAN", IBAN not in audit_text)

resp = viewer.post(f"{base}/persons/create/", {"given_name": "Hack", "family_name": "Er"})
check("Personen-Anlage ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Zweite Person für Besetzung/Nachrücker
resp = admin.post(
    f"{base}/persons/create/",
    {"given_name": "Max", "family_name": "Beispiel", "email": "max@example.org", "is_active": "on"},
)
person2 = SessionPerson.objects.get(tenant=tenant, family_name="Beispiel")

# =============================================================================
# Phase C: Besetzungs-CRUD + Nachrücker
# =============================================================================
print()
print("=== Phase C: Besetzung + Nachrücker ===")

resp = admin.post(
    f"{base}/organizations/{org.id}/memberships/add/",
    {"person": str(person.id), "role": "chair", "has_voting_rights": "on"},
)
check("Besetzung angelegt -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
membership = SessionOrganizationMembership.objects.get(organization=org, person=person)
check("Funktion Vorsitz gespeichert", membership.role == "chair")
check("Stimmrecht gespeichert", membership.has_voting_rights)
check(
    "Audit: create-Eintrag Besetzung",
    SessionAuditLog.objects.filter(object_id=membership.id, action="create").exists(),
)

# Doppelte aktive Mitgliedschaft verhindert
resp = admin.post(
    f"{base}/organizations/{org.id}/memberships/add/",
    {"person": str(person.id), "role": "member"},
)
check(
    "Doppelte aktive Mitgliedschaft verhindert",
    SessionOrganizationMembership.objects.filter(organization=org, person=person).count() == 1,
)

# Update: Rolle sachkundige/r Bürger/in + Vertretung
resp = admin.post(
    f"{base}/memberships/{membership.id}/update/",
    {"role": "expert_citizen", "substitute_for": str(person2.id)},
)
membership.refresh_from_db()
check("Rolle sachkundige/r Bürger/in setzbar", membership.role == "expert_citizen")
check("Vertreterregelung gespeichert", membership.substitute_for_id == person2.id)
check("Stimmrecht entziehbar", not membership.has_voting_rights)

# Nachrücker-Flow: person scheidet aus, person2 rückt nach
resp = admin.post(
    f"{base}/memberships/{membership.id}/succession/",
    {"successor": str(person2.id), "change_date": "2026-07-01"},
)
check("Nachrücker-Flow -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
membership.refresh_from_db()
check("Alte Mitgliedschaft beendet", str(membership.end_date) == "2026-07-01")
new_membership = SessionOrganizationMembership.objects.get(organization=org, person=person2, end_date__isnull=True)
check("Nachfolger übernimmt Funktion", new_membership.role == "expert_citizen")
check("Nachfolger-Start = Wechseldatum", str(new_membership.start_date) == "2026-07-01")

# Beenden
resp = admin.post(f"{base}/memberships/{new_membership.id}/end/", {"end_date": "2026-12-31"})
new_membership.refresh_from_db()
check("Mitgliedschaft beendbar", str(new_membership.end_date) == "2026-12-31")

# Ohne Berechtigung
resp = viewer.post(
    f"{base}/organizations/{org.id}/memberships/add/",
    {"person": str(person.id), "role": "member"},
)
check("Besetzungs-Anlage ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase D: Benutzerverwaltung
# =============================================================================
print()
print("=== Phase D: Benutzerverwaltung ===")

resp = admin.get(f"{base}/settings/users/")
check("Benutzerliste -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Einladung: bestehendes Konto wird direkt Mitglied
existing = User.objects.create_user(email="bestand@example.org", password="pw-Smoke-Test-1!")
resp = admin.post(
    f"{base}/settings/users/invite/",
    {"email": "bestand@example.org", "roles": [str(roles["viewer"].id)]},
)
check("Bestehendes Konto direkt hinzugefügt", SessionUser.objects.filter(user=existing, tenant=tenant).exists())
su_existing = SessionUser.objects.get(user=existing, tenant=tenant)
check("Rollen vorbelegt", list(su_existing.roles.all()) == [roles["viewer"]])

# Einladung: neues Konto via Token
mail.outbox = []
resp = admin.post(
    f"{base}/settings/users/invite/",
    {"email": "neu@example.org", "roles": [str(roles["recorder"].id)]},
)
invitation = SessionInvitation.objects.get(tenant=tenant, email="neu@example.org")
check("Einladung angelegt", invitation.is_valid)
check("Einladungs-E-Mail versendet", len(mail.outbox) == 1 and "neu@example.org" in mail.outbox[0].to)
check("E-Mail enthält Token-Link", invitation.token in mail.outbox[0].body)

# Annahme durch neuen Nutzer (Registrierung über Einladung)
anon = Client()
resp = anon.get(f"/session/invite/{invitation.token}/")
check("Accept-Seite -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = anon.post(
    f"/session/invite/{invitation.token}/",
    {
        "first_name": "Nina",
        "last_name": "Neu",
        "password": "pw-Smoke-Test-neu-77!",
        "password_confirm": "pw-Smoke-Test-neu-77!",
    },
)
check("Annahme -> Redirect ins Dashboard", resp.status_code == 302, f"got {resp.status_code}")
new_user = User.objects.filter(email="neu@example.org").first()
check("Konto angelegt", new_user is not None)
su_new = SessionUser.objects.filter(user=new_user, tenant=tenant).first()
check("SessionUser angelegt", su_new is not None and su_new.is_active)
check("Eingeladene Rollen zugewiesen", su_new is not None and list(su_new.roles.all()) == [roles["recorder"]])
invitation.refresh_from_db()
check("Einladung als angenommen markiert", invitation.accepted_at is not None)

# Abgelaufener/verbrauchter Token
resp = anon.get(f"/session/invite/{invitation.token}/")
check("Verbrauchter Token -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Rollen zuweisen/entziehen
resp = admin.post(
    f"{base}/settings/users/{su_new.id}/roles/",
    {"roles": [str(roles["clerk"].id), str(roles["viewer"].id)]},
)
check("Rollen aktualisierbar", set(su_new.roles.all()) == {roles["clerk"], roles["viewer"]})

# Deaktivieren
resp = admin.post(f"{base}/settings/users/{su_new.id}/deactivate/")
su_new.refresh_from_db()
check("Benutzer deaktivierbar", not su_new.is_active)

# Selbst-Deaktivierung verhindert
resp = admin.post(f"{base}/settings/users/{su_admin.id}/deactivate/")
su_admin.refresh_from_db()
check("Selbst-Deaktivierung verhindert", su_admin.is_active)

# Letzter Admin: Rollenentzug verhindert
resp = admin.post(f"{base}/settings/users/{su_admin.id}/roles/", {"roles": [str(roles["viewer"].id)]})
check("Letzter Admin behält Admin-Rolle", su_admin.is_admin())

# Ohne Berechtigung
resp = viewer.get(f"{base}/settings/users/")
check("Benutzerliste ohne manage_users -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.post(f"{base}/settings/users/invite/", {"email": "boese@example.org"})
check("Einladen ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Keine Einladung angelegt", not SessionInvitation.objects.filter(email="boese@example.org").exists())

# =============================================================================
# Phase E: Lebenszyklus komplett ohne Django-Admin (Akzeptanzkriterium)
# =============================================================================
print()
print("=== Phase E: Lebenszyklus ===")

# Neues Ratsmitglied -> Ausschusszuordnung -> Ausscheiden + Nachrücker: alles oben per UI erledigt
lifecycle_ok = (
    SessionOrganization.objects.filter(tenant=tenant).exists()
    and SessionPerson.objects.filter(tenant=tenant).count() >= 2
    and SessionOrganizationMembership.objects.filter(organization__tenant=tenant).count() >= 2
    and SessionOrganizationMembership.objects.filter(organization__tenant=tenant, end_date__isnull=False).count() >= 2
)
check("Kompletter Lebenszyklus ohne Django-Admin abgebildet", lifecycle_ok)

anzahl_audit = SessionAuditLog.objects.filter(tenant=tenant).count()
check("Audit-Log dokumentiert Stammdatenpflege", anzahl_audit >= 10, f"count={anzahl_audit}")

# Fremd-Tenant-Zugriff
tenant2 = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")
foreign_org = SessionOrganization.objects.create(tenant=tenant2, name="Fremdgremium")
resp = admin.post(
    f"{base}/organizations/{foreign_org.id}/edit/",
    {"name": "Gekapert", "organization_type": "committee", "invitation_period_days": "7", "allowance_amount": "0"},
)
check("Fremdes Gremium nicht bearbeitbar -> 404", resp.status_code == 404, f"got {resp.status_code}")
foreign_org.refresh_from_db()
check("Fremdes Gremium unverändert", foreign_org.name == "Fremdgremium")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
