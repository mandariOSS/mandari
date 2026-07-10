# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Organisations-Struktur, Gastrolle, modulares Rechtesystem.

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_org_guests.py

Prüft:
- Organisation: clean()-Pflichtvalidierung (body/party_group bei aktiven Orgs),
  Data-Migration 0018 (party_group aus parties) idempotent
- Gäste: Einladen erzeugt User + Membership(is_guest, keine Rollen) +
  Passwort-Setz-Mail, Gast-Limit durchgesetzt (26. Gast -> Fehlermeldung),
  Gast sieht NUR freigegebene Dokumente (Editor 200/403, Dashboard/Aufgaben/
  RIS -> Redirect auf Gast-Übersicht), Gast-Übersicht listet Freigaben,
  Freigabe-Level (view/edit) auf Modellebene, Mitgliederliste mit Gäste-Sektion
- Provisioning: guest_limit via PATCH änderbar, GET liefert guest_limit +
  guest_count, member_count ohne Gäste
- Rechte: individuelle Berechtigung greift (403 -> 200), verweigerte schlägt
  Rolle (200 -> 403), Herkunftsanzeige im Mitglieder-Detail-HTML,
  update_permissions-Aktion, Rollen-Reset + Standard-Rollen wiederherstellen
- Organisations-Grenzen (Fremd-Org-Zugriffe dicht)
"""

import base64
import importlib
import json
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
os.environ["DEBUG"] = "true"  # LocMem-Cache + DB-Sessions statt Redis
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["PROVISIONING_API_KEY"] = "smoke-provisioning-key"

import django  # noqa: E402

django.setup()

from django.apps import apps as global_apps  # noqa: E402
from django.core import mail  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.permissions import DEFAULT_ROLES  # noqa: E402
from apps.tenants.models import Membership, Organization, PartyGroup, Permission, Role  # noqa: E402
from apps.work.motions.models import Motion, MotionShare  # noqa: E402
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
        print(f"  FAIL {name} {detail}")


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


# =============================================================================
print("=== Setup ===")
source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Testhausen")
party = PartyGroup.objects.create(name="Testpartei")
party2 = PartyGroup.objects.create(name="Zweitpartei")

org_a = Organization.objects.create(name="Fraktion A", slug="fraktion-a", body=body, party_group=party)
org_b = Organization.objects.create(name="Fraktion B", slug="fraktion-b", body=body, party_group=party)

admin_role_a = Role.objects.filter(organization=org_a, is_admin=True).first()
admin_role_b = Role.objects.filter(organization=org_b, is_admin=True).first()

user_admin = User.objects.create_user(email="admin@example.org", password="test1234!")
m_admin = Membership.objects.create(user=user_admin, organization=org_a)
m_admin.roles.add(admin_role_a)

user_foreign = User.objects.create_user(email="fremd@example.org", password="test1234!")
m_foreign = Membership.objects.create(user=user_foreign, organization=org_b)
m_foreign.roles.add(admin_role_b)

c_admin = client_for(user_admin)
c_foreign = client_for(user_foreign)

check("Standard-Rollen per Signal angelegt", Role.objects.filter(organization=org_a).count() == len(DEFAULT_ROLES))

# =============================================================================
print("=== A. Organisation: Pflicht-Zuordnungen (clean) ===")
bad_org = Organization(name="Ohne Zuordnung", slug="ohne-zuordnung", is_active=True)
try:
    bad_org.full_clean()
    check("clean(): aktive Org ohne body/party_group abgelehnt", False, "keine ValidationError")
except ValidationError as e:
    check(
        "clean(): aktive Org ohne body/party_group abgelehnt",
        "body" in e.message_dict and "party_group" in e.message_dict,
        str(e.message_dict),
    )

inactive_org = Organization(name="Inaktiv", slug="inaktiv-org", is_active=False)
try:
    inactive_org.full_clean(exclude=["owner", "registration_default_role"])
    check("clean(): inaktive Org ohne Zuordnung erlaubt", True)
except ValidationError as e:
    check("clean(): inaktive Org ohne Zuordnung erlaubt", False, str(e))

try:
    org_a.full_clean(exclude=["owner", "registration_default_role"])
    check("clean(): Org mit body+party_group gültig", True)
except ValidationError as e:
    check("clean(): Org mit body+party_group gültig", False, str(e))

print("=== A. Data-Migration 0018 (party_group-Backfill, idempotent) ===")
org_legacy = Organization.objects.create(name="Legacy", slug="legacy-org", body=body, is_active=False)
org_legacy.parties.add(party2)
check("Ausgangslage: party_group leer", org_legacy.party_group_id is None)

mig = importlib.import_module("apps.tenants.migrations.0018_backfill_party_group")
mig.backfill_party_group(global_apps, None)
org_legacy.refresh_from_db()
check("Backfill setzt party_group aus parties.first()", org_legacy.party_group_id == party2.id)

# Idempotenz: zweiter Lauf ändert nichts (auch nicht bei Orgs mit gesetztem Wert)
mig.backfill_party_group(global_apps, None)
org_legacy.refresh_from_db()
org_a.refresh_from_db()
check("Backfill idempotent", org_legacy.party_group_id == party2.id and org_a.party_group_id == party.id)

# =============================================================================
print("=== B. Gast einladen ===")
BASE_A = f"/work/{org_a.slug}"

doc_shared = Motion.objects.create(
    organization=org_a, author=m_admin, title="Geteiltes Dokument", visibility="organization"
)
doc_secret = Motion.objects.create(
    organization=org_a, author=m_admin, title="Internes Dokument", visibility="organization"
)

mail.outbox = []
resp = c_admin.post(
    f"{BASE_A}/organization/members/invite-guest/",
    {"email": "gast@example.org", "message": "Willkommen!", "share_level": "view", "documents": [str(doc_shared.id)]},
)
check("Gast-Einladung: Redirect", resp.status_code == 302, f"got {resp.status_code}")

guest_user = User.objects.filter(email="gast@example.org").first()
check("Gast-Einladung erzeugt User", guest_user is not None)
guest_membership = Membership.objects.filter(user=guest_user, organization=org_a).first()
check("Gast-Einladung erzeugt Membership(is_guest=True)", guest_membership is not None and guest_membership.is_guest)
check("Gast hat KEINE Rollen", guest_membership is not None and guest_membership.roles.count() == 0)
check("Gast-User ohne nutzbares Passwort", guest_user is not None and not guest_user.has_usable_password())
check(
    "Passwort-Setz-Mail versendet (Reset-Link)",
    len(mail.outbox) == 1 and "/accounts/password-reset/" in mail.outbox[0].body,
    f"outbox={len(mail.outbox)}",
)
share = MotionShare.objects.filter(motion=doc_shared, scope="user", user=guest_user).first()
check("Freigabe (MotionShare scope=user) angelegt", share is not None and share.level == "view")
check("Gast zählt nicht als reguläres Mitglied", org_a.get_active_member_count() == 1)
check("Gast-Zähler = 1", org_a.get_active_guest_count() == 1)

print("=== B. Gast-Limit (25 frei, 26. abgelehnt) ===")
check("Default guest_limit = 25", org_a.guest_limit == 25)
for i in range(24):
    filler = User.objects.create_user(email=f"gast{i}@example.org", password=None)
    Membership.objects.create(user=filler, organization=org_a, is_guest=True)
check("25 Gastplätze belegt", org_a.get_active_guest_count() == 25)

resp = c_admin.post(
    f"{BASE_A}/organization/members/invite-guest/",
    {"email": "gast26@example.org", "share_level": "view"},
    follow=True,
)
check("26. Gast: kein User angelegt", not User.objects.filter(email="gast26@example.org").exists())
content = resp.content.decode("utf-8", errors="ignore")
# Hinweis: Messages werden als JS-Toasts mit |escapejs gerendert — Bindestriche
# erscheinen dort als -, daher bindestrichfreie Fragmente prüfen.
check(
    "26. Gast: Meldung 'Gast-Limit erreicht (25)' + Addon-Hinweis",
    "Limit erreicht (25)" in content and "Addon im Kundenportal" in content,
)

print("=== B. Gast-Zugriffsmodell ===")
guest_user.set_password("gast1234!")
guest_user.save()
c_guest = client_for(guest_user)

resp = c_guest.get(f"{BASE_A}/documents/{doc_shared.id}/")
check("Gast: geteiltes Dokument -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = c_guest.get(f"{BASE_A}/documents/{doc_secret.id}/")
check("Gast: ungeteiltes Dokument -> 403", resp.status_code == 403, f"got {resp.status_code}")

for label, url in [
    ("Dashboard", f"{BASE_A}/"),
    ("Aufgaben", f"{BASE_A}/tasks/"),
    ("RIS", f"{BASE_A}/ris/"),
    ("Dokumentliste", f"{BASE_A}/documents/"),
    ("Mitglieder", f"{BASE_A}/organization/members/"),
]:
    resp = c_guest.get(url)
    check(
        f"Gast: {label} -> Redirect auf Gast-Übersicht",
        resp.status_code == 302 and resp.headers.get("Location", "").endswith("/freigaben/"),
        f"got {resp.status_code} -> {resp.headers.get('Location')}",
    )

resp = c_guest.get(f"{BASE_A}/freigaben/")
content = resp.content.decode("utf-8", errors="ignore")
check("Gast-Übersicht -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Gast-Übersicht listet geteiltes Dokument", "Geteiltes Dokument" in content)
check("Gast-Übersicht listet ungeteiltes NICHT", "Internes Dokument" not in content)
check("Gast-Navigation reduziert (keine Fraktionssitzungen)", "Fraktionssitzungen" not in content)

resp = c_guest.get(f"{BASE_A}/profile/")
check("Gast: Konto/Profil erreichbar -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Export/Levels
resp = c_guest.get(f"{BASE_A}/documents/{doc_secret.id}/export/?format=pdf")
check("Gast: Export ungeteiltes Dokument -> 403", resp.status_code == 403, f"got {resp.status_code}")

check("Level view: kein Edit (Modell)", not doc_shared.can_edit(guest_membership))
check("Level view: kein Kommentar (Modell)", not doc_shared.can_comment(guest_membership))
share.level = "edit"
share.save()
check("Level edit: Edit erlaubt (Modell)", doc_shared.can_edit(guest_membership))
check("Level edit: Kommentar erlaubt (Modell)", doc_shared.can_comment(guest_membership))
check("PermissionChecker: Gast hat keine der Berechtigungen", not guest_membership.has_permission("dashboard.view"))

# Org-Grenzen: Gast aus Org A hat in Org B nichts verloren
resp = c_guest.get(f"/work/{org_b.slug}/freigaben/")
check("Gast: fremde Org -> 403", resp.status_code == 403, f"got {resp.status_code}")

print("=== B. Mitgliederliste mit Gäste-Sektion ===")
resp = c_admin.get(f"{BASE_A}/organization/members/")
content = resp.content.decode("utf-8", errors="ignore")
check("Mitgliederliste -> 200", resp.status_code == 200)
check("Gäste-Sektion mit Zähler", "25 von 25 Gastplätzen belegt" in content)
check("Gast-Badge in Liste", "gast@example.org" in content)

# =============================================================================
print("=== B. Provisioning: guest_limit + Zähler ===")
c_api = Client()
AUTH = {"HTTP_AUTHORIZATION": "Bearer smoke-provisioning-key"}

resp = c_api.get(f"/api/provisioning/organizations/{org_a.slug}/", **AUTH)
data = resp.json()
check("GET liefert guest_limit", data.get("guest_limit") == 25, str(data.get("guest_limit")))
check("GET liefert guest_count", data.get("guest_count") == 25, str(data.get("guest_count")))
check("member_count ohne Gäste", data.get("member_count") == 1, str(data.get("member_count")))

resp = c_api.patch(
    f"/api/provisioning/organizations/{org_a.slug}/",
    data=json.dumps({"guest_limit": 50}),
    content_type="application/json",
    **AUTH,
)
check("PATCH guest_limit -> 200", resp.status_code == 200, f"got {resp.status_code}")
org_a.refresh_from_db()
check("PATCH setzt guest_limit=50", org_a.guest_limit == 50)
check("PATCH-Antwort enthält guest_limit=50", resp.json().get("guest_limit") == 50)

resp = c_api.patch(
    f"/api/provisioning/organizations/{org_a.slug}/",
    data=json.dumps({"guest_limit": "viele"}),
    content_type="application/json",
    **AUTH,
)
check("PATCH ungültiger guest_limit -> 400", resp.status_code == 400, f"got {resp.status_code}")

# Nach Erhöhung: 26. Gast jetzt möglich
resp = c_admin.post(
    f"{BASE_A}/organization/members/invite-guest/",
    {"email": "gast26@example.org", "share_level": "view"},
)
check("Nach Addon-Erhöhung: 26. Gast möglich", User.objects.filter(email="gast26@example.org").exists())

# =============================================================================
print("=== C. Individuelle Berechtigungen ===")
user_plain = User.objects.create_user(email="basis@example.org", password="test1234!")
m_plain = Membership.objects.create(user=user_plain, organization=org_a)
c_plain = client_for(user_plain)

resp = c_plain.get(f"{BASE_A}/organization/members/")
check("Ohne Rolle/Recht: Mitgliederliste -> 403", resp.status_code == 403, f"got {resp.status_code}")

m_plain.individual_permissions.add(Permission.objects.get(codename="members.view"))
resp = c_plain.get(f"{BASE_A}/organization/members/")
check("Individuelle Berechtigung greift -> 200", resp.status_code == 200, f"got {resp.status_code}")

print("=== C. Verweigerte Berechtigung schlägt Rolle ===")
member_role = Role.objects.get(organization=org_a, name="Fraktionsmitglied")
user_roled = User.objects.create_user(email="rolle@example.org", password="test1234!")
m_roled = Membership.objects.create(user=user_roled, organization=org_a)
m_roled.roles.add(member_role)
c_roled = client_for(user_roled)

resp = c_roled.get(f"{BASE_A}/organization/members/")
check("Rolle gewährt members.view -> 200", resp.status_code == 200, f"got {resp.status_code}")

m_roled.denied_permissions.add(Permission.objects.get(codename="members.view"))
resp = c_roled.get(f"{BASE_A}/organization/members/")
check("Verweigert schlägt Rolle -> 403", resp.status_code == 403, f"got {resp.status_code}")

print("=== C. Mitglieder-Detail: Matrix + Herkunft + update_permissions ===")
resp = c_admin.get(f"{BASE_A}/organization/members/{m_roled.id}/")
content = resp.content.decode("utf-8", errors="ignore")
check("Detail -> 200", resp.status_code == 200)
check("Matrix vorhanden ('Effektive Berechtigungen')", "Effektive Berechtigungen" in content)
check("Herkunftsanzeige ('aus Rolle: Fraktionsmitglied')", "aus Rolle: Fraktionsmitglied" in content)
check("Drei-Zustände-Legende", "Individuell" in content and "Verweigert" in content)

resp = c_admin.post(
    f"{BASE_A}/organization/members/{m_roled.id}/",
    {
        "action": "update_permissions",
        "individual_permissions": ["organization.view"],
        "denied_permissions": ["motions.create"],
    },
)
check("update_permissions: Redirect", resp.status_code == 302, f"got {resp.status_code}")
check(
    "individual_permissions gesetzt",
    set(m_roled.individual_permissions.values_list("codename", flat=True)) == {"organization.view"},
)
check(
    "denied_permissions gesetzt",
    set(m_roled.denied_permissions.values_list("codename", flat=True)) == {"motions.create"},
)
check("Effektiv: motions.create verweigert", not m_roled.has_permission("motions.create"))
check("Effektiv: organization.view individuell", m_roled.has_permission("organization.view"))

# Gast-Membership: Rollen/Berechtigungen bleiben tabu
resp = c_admin.post(
    f"{BASE_A}/organization/members/{guest_membership.id}/",
    {"action": "update_roles", "roles": [str(member_role.id)]},
)
check("Gast: update_roles abgelehnt", guest_membership.roles.count() == 0)

print("=== C. Rollen-Reset + Standard-Rollen wiederherstellen ===")
default_perm_count = len(DEFAULT_ROLES["faction_member"]["permissions"])
member_role.permissions.clear()
member_role.description = "verbogen"
member_role.save()

resp = c_admin.post(f"{BASE_A}/organization/roles/{member_role.id}/reset/")
member_role.refresh_from_db()
check("Rollen-Reset: Redirect", resp.status_code == 302, f"got {resp.status_code}")
check(
    "Rollen-Reset stellt Standard-Berechtigungen her",
    member_role.permissions.count() == default_perm_count,
    f"{member_role.permissions.count()} != {default_perm_count}",
)
check("Rollen-Reset stellt Beschreibung her", member_role.description != "verbogen")

custom_role = Role.objects.create(organization=org_a, name="Sonderrolle")
resp = c_admin.post(f"{BASE_A}/organization/roles/{custom_role.id}/reset/", follow=True)
check(
    "Reset eigener Rolle ohne Standard-Definition: Fehlermeldung",
    "existiert keine Standard" in resp.content.decode("utf-8", errors="ignore"),
)

# Fehlende Standard-Rolle wiederherstellen (bestehende bleiben unverändert)
ag_role = Role.objects.get(organization=org_a, name="AG-Sprecher/in")
ag_role.delete()
chair_role = Role.objects.get(organization=org_a, name="Fraktionsvorsitz")
chair_role.permissions.clear()  # angepasste Rolle darf NICHT überschrieben werden

resp = c_admin.post(f"{BASE_A}/organization/roles/restore-defaults/")
check("Restore-Defaults: Redirect", resp.status_code == 302, f"got {resp.status_code}")
check("Fehlende Standard-Rolle wieder angelegt", Role.objects.filter(organization=org_a, name="AG-Sprecher/in").exists())
chair_role.refresh_from_db()
check("Bestehende (angepasste) Rolle unverändert", chair_role.permissions.count() == 0)

print("=== C. Rollen-Seite: Doku-Text + Buttons ===")
resp = c_admin.get(f"{BASE_A}/organization/roles/")
content = resp.content.decode("utf-8", errors="ignore")
check("Doku-Text 'frei kombinierbar'", "frei kombinierbar" in content)
check("Button 'Standard-Rollen wiederherstellen'", "Standard-Rollen wiederherstellen" in content)
check("Button 'Auf Standard zurücksetzen'", "Auf Standard zurücksetzen" in content)

print("=== Organisationseinstellungen: Heimat-Kommune + Partei ===")
resp = c_admin.get(f"{BASE_A}/organization/")
content = resp.content.decode("utf-8", errors="ignore")
check("Settings -> 200", resp.status_code == 200)
check("Heimat-Kommune prominent", "Stadt/Kommune" in content and "Stadt Testhausen" in content)
check("Partei prominent", "Testpartei" in content)

print("=== Organisations-Grenzen ===")
resp = c_foreign.get(f"{BASE_A}/organization/members/{m_roled.id}/")
check("Fremde Org: Mitglieder-Detail dicht", resp.status_code in (403, 404), f"got {resp.status_code}")
resp = c_foreign.post(f"{BASE_A}/organization/roles/{member_role.id}/reset/")
check("Fremde Org: Rollen-Reset dicht", resp.status_code in (403, 404), f"got {resp.status_code}")
resp = c_foreign.post(
    f"/work/{org_b.slug}/organization/roles/{member_role.id}/reset/",
)
check("Rolle aus Org A via Org B: 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_foreign.get(f"{BASE_A}/documents/{doc_shared.id}/")
check("Fremde Org: Dokument dicht", resp.status_code in (403, 404), f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
