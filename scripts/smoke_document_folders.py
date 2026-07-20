# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Ordner-Ablage für Dokumente (DocumentFolder).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_document_folders.py

Prüft:
- Baum anlegen (Tiefe 4 ok, Tiefe 5 -> ValidationError, Zyklen verboten)
- unique-Name je Ebene (Wurzel + Unterordner)
- Dokumente verschieben (einzeln, Mehrfachauswahl, Editor-Sidebar-Feld)
- Ordner löschen -> Inhalte (Dokumente + Unterordner) wandern zum Parent
- Ordner-Filter kombiniert mit Status/Suche (?ordner&status&q)
- Zähler sichtbarkeitsabhängig (privates Dokument eines anderen zählt nicht)
- Gäste sehen keine Ordner-UI (Liste -> Redirect, Sidebar ohne Ordner-Block)
- Org-Grenzen dicht (fremder Ordner -> 404, fremde Ordner-Verwaltung -> 404)
- Erstellen im gewählten Ordner
- Verwaltungsrechte (eigene Ordner vs. Organisationsverwaltung)
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
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.core.exceptions import ValidationError  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.motions.models import DocumentFolder, Motion, MotionShare  # noqa: E402

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


def make_org(name, slug):
    org = Organization.objects.create(name=name, slug=slug)
    role = Role.objects.filter(organization=org, is_admin=True).first()
    if role is None:
        role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
    return org, role


def make_member(org, role, email):
    user = User.objects.create_user(email=email, password="test1234!")
    membership = Membership.objects.create(user=user, organization=org)
    membership.roles.add(role)
    return user, membership


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


def ajax_post(client, url, data):
    return client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def folder_counts(resp):
    """Ordner-Zähler aus dem Listen-Kontext: {folder_name: count}."""
    return {e["folder"].name: e["count"] for e in resp.context["folder_entries"]}


print("=== Setup ===")
org_a, role_a = make_org("Fraktion A", "fraktion-a")
org_b, role_b = make_org("Fraktion B", "fraktion-b")

user_admin, m_admin = make_member(org_a, role_a, "admin@example.org")
user_member, m_member = make_member(org_a, role_a, "mitglied@example.org")
user_foreign, m_foreign = make_member(org_b, role_b, "fremd@example.org")

guest_user = User.objects.create_user(email="gast@example.org", password="test1234!")
m_guest = Membership.objects.create(user=guest_user, organization=org_a, is_guest=True)

c_admin = client_for(user_admin)
c_member = client_for(user_member)
c_foreign = client_for(user_foreign)
c_guest = client_for(guest_user)

BASE = f"/work/{org_a.slug}/documents"

# --- 1. Baum anlegen: Tiefe 4 ok, Tiefe 5 -> ValidationError ------------------
print("=== 1. Baum & Tiefenlimit ===")
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 1", "color": "blue"})
check("Ordner anlegen (Redirect)", resp.status_code == 302, f"got {resp.status_code}")
f1 = DocumentFolder.objects.get(organization=org_a, name="Ebene 1")
check("Ordner auf Wurzelebene (parent=None)", f1.parent is None and f1.color == "blue")
check("created_by = Membership", f1.created_by_id == m_admin.id)

resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 2", "parent": str(f1.id)})
f2 = DocumentFolder.objects.get(organization=org_a, name="Ebene 2")
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 3", "parent": str(f2.id)})
f3 = DocumentFolder.objects.get(organization=org_a, name="Ebene 3")
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 4", "parent": str(f3.id)})
f4 = DocumentFolder.objects.get(organization=org_a, name="Ebene 4")
check("Tiefe 4 erlaubt", f4.get_depth() == 4)

# Tiefe 5 über die View: Fehlermeldung, kein Ordner
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 5", "parent": str(f4.id)}, follow=True)
check(
    "Tiefe 5 über View abgelehnt",
    not DocumentFolder.objects.filter(organization=org_a, name="Ebene 5").exists()
    and "Verschachtelungstiefe" in resp.content.decode(),
)

# Tiefe 5 direkt am Modell: ValidationError
try:
    deep = DocumentFolder(organization=org_a, name="Zu tief", parent=f4)
    deep.full_clean()
    check("Tiefe 5 -> ValidationError", False, "full_clean() ohne Fehler")
except ValidationError:
    check("Tiefe 5 -> ValidationError", True)

# Zyklus: Ordner in eigenen Unterordner verschieben
resp = c_admin.post(f"{BASE}/folders/{f1.id}/update/", {"name": "Ebene 1", "parent": str(f3.id)})
f1.refresh_from_db()
check("Zyklus (Ordner in eigenen Unterordner) abgelehnt", f1.parent is None)

# Verschieben in eigenen Unterordner (Zyklus, unabhängig vom Tiefenlimit)
resp = c_admin.post(f"{BASE}/folders/{f2.id}/update/", {"name": "Ebene 2", "parent": str(f4.id)})
f2.refresh_from_db()
check("Verschieben in eigenen Unterordner abgelehnt", f2.parent_id == f1.id)

# Teilbaum-Verschiebung, die das Tiefenlimit sprengen würde (Höhe 2 unter Tiefe 3)
z1 = DocumentFolder.objects.create(organization=org_a, name="Zweig", created_by=m_admin)
DocumentFolder.objects.create(organization=org_a, name="Zweig-Kind", parent=z1, created_by=m_admin)
resp = c_admin.post(f"{BASE}/folders/{z1.id}/update/", {"name": "Zweig", "parent": str(f3.id)})
z1.refresh_from_db()
check("Teilbaum-Verschiebung über Tiefenlimit abgelehnt", z1.parent is None)
resp = c_admin.post(f"{BASE}/folders/{z1.id}/update/", {"name": "Zweig", "parent": str(f2.id)})
z1.refresh_from_db()
check("Teilbaum-Verschiebung innerhalb des Limits erlaubt", z1.parent_id == f2.id)
DocumentFolder.objects.filter(organization=org_a, name="Zweig-Kind").delete()
z1.delete()

# --- 2. unique-Name je Ebene ---------------------------------------------------
print("=== 2. Eindeutige Namen je Ebene ===")
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 1"}, follow=True)
check(
    "Doppelter Name auf Wurzelebene abgelehnt",
    DocumentFolder.objects.filter(organization=org_a, name="Ebene 1").count() == 1,
)
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 2", "parent": str(f1.id)}, follow=True)
check(
    "Doppelter Name im selben Ordner abgelehnt",
    DocumentFolder.objects.filter(organization=org_a, name="Ebene 2").count() == 1,
)
# Gleicher Name in anderem Ordner ist ok
resp = c_admin.post(f"{BASE}/folders/create/", {"name": "Ebene 2", "parent": str(f2.id)})
check(
    "Gleicher Name auf anderer Ebene erlaubt",
    DocumentFolder.objects.filter(organization=org_a, name="Ebene 2").count() == 2,
)
DocumentFolder.objects.filter(organization=org_a, name="Ebene 2", parent=f2).delete()

# Gleicher Name in anderer Organisation ist ok
DocumentFolder.objects.create(organization=org_b, name="Ebene 1", created_by=m_foreign)
check("Gleicher Name in anderer Org erlaubt", DocumentFolder.objects.filter(name="Ebene 1").count() == 2)

# --- 3. Erstellen im gewählten Ordner ------------------------------------------
print("=== 3. Erstellen im Ordner ===")
resp = c_admin.get(f"{BASE}/create/?ordner={f2.id}")
check("Create-Formular zeigt Ziel-Ordner", f"{f2.id}" in resp.content.decode())
resp = c_admin.post(f"{BASE}/create/", {"title": "Radweg Hauptstraße", "folder": str(f2.id)})
doc_a = Motion.objects.get(organization=org_a, title="Radweg Hauptstraße")
check("Neues Dokument landet im Ordner", doc_a.folder_id == f2.id)

doc_root = Motion.objects.create(organization=org_a, author=m_admin, title="Wurzel-Dokument", visibility="organization")
doc_b = Motion.objects.create(organization=org_a, author=m_admin, title="Spielplatz Nord", visibility="organization")
doc_c = Motion.objects.create(organization=org_a, author=m_admin, title="Spielplatz Süd", visibility="organization")

# --- 4. Dokumente verschieben (einzeln + Mehrfach + Sidebar) --------------------
print("=== 4. Dokumente verschieben ===")
resp = ajax_post(c_admin, f"{BASE}/move-to-folder/", {"motion_ids": [str(doc_b.id)], "folder": str(f1.id)})
doc_b.refresh_from_db()
check("Einzeln verschieben", resp.status_code == 200 and doc_b.folder_id == f1.id)

resp = ajax_post(
    c_admin,
    f"{BASE}/move-to-folder/",
    {"motion_ids": [str(doc_b.id), str(doc_c.id)], "folder": str(f3.id)},
)
doc_b.refresh_from_db()
doc_c.refresh_from_db()
check(
    "Mehrfachauswahl verschieben",
    resp.json()["moved"] == 2 and doc_b.folder_id == f3.id and doc_c.folder_id == f3.id,
)

# Zurück zur Wurzel (folder leer)
resp = ajax_post(c_admin, f"{BASE}/move-to-folder/", {"motion_ids": [str(doc_c.id)], "folder": ""})
doc_c.refresh_from_db()
check("Zurück zur Wurzel verschieben", doc_c.folder_id is None)

# Editor-Sidebar: Ordner-Feld sichtbar + set_folder über Meta-Endpoint
resp = c_admin.get(f"{BASE}/{doc_c.id}/")
html = resp.content.decode()
check("Editor-Sidebar: Ordner-Feld vorhanden", 'name="folder"' in html and "set_folder" in html)
resp = ajax_post(c_admin, f"{BASE}/{doc_c.id}/meta/", {"action": "set_folder", "folder": str(f4.id)})
doc_c.refresh_from_db()
check("Sidebar set_folder verschiebt Dokument", resp.status_code == 200 and doc_c.folder_id == f4.id)
resp = ajax_post(c_admin, f"{BASE}/{doc_c.id}/meta/", {"action": "set_folder", "folder": ""})
doc_c.refresh_from_db()
check("Sidebar set_folder zurück zur Wurzel", doc_c.folder_id is None)

# --- 5. Filter kombiniert (?ordner&status&q) ------------------------------------
print("=== 5. Kombinierte Filter ===")
doc_a.status = "submitted"
doc_a.save(update_fields=["status"])
doc_review = Motion.objects.create(
    organization=org_a, author=m_admin, title="Radweg Nebenstraße", folder=f2, visibility="organization"
)

resp = c_admin.get(f"{BASE}/?ordner={f1.id}")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check(
    "Ordner-Filter inkl. Unterordner",
    "Radweg Hauptstraße" in titles and "Radweg Nebenstraße" in titles and "Wurzel-Dokument" not in titles,
)
check("Breadcrumb/aktueller Ordner im Kontext", resp.context["current_folder"].id == f1.id)

resp = c_admin.get(f"{BASE}/?ordner={f1.id}&status=submitted&q=Radweg")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check(
    "Ordner + Status + Suche kombiniert",
    titles == ["Radweg Hauptstraße"],
    f"got {titles}",
)
resp = c_admin.get(f"{BASE}/?ordner={f1.id}&q=Wurzel")
check("Suche bleibt auf Ordner begrenzt", len(resp.context["motion_rows"]) == 0)

# Breadcrumb der Unterordner
resp = c_admin.get(f"{BASE}/?ordner={f3.id}")
crumbs = [f.name for f in resp.context["folder_breadcrumbs"]]
check("Breadcrumb Wurzel->Ordner->Unterordner", crumbs == ["Ebene 1", "Ebene 2"], f"got {crumbs}")

# --- 6. Zähler sichtbarkeitsabhängig --------------------------------------------
print("=== 6. Sichtbarkeitsabhängige Zähler ===")
# Privates Dokument eines anderen im selben Ordner
Motion.objects.create(organization=org_a, author=m_admin, title="Privates Dokument", folder=f4, visibility="private")
resp = c_admin.get(f"{BASE}/")
counts_admin = folder_counts(resp)
resp = c_member.get(f"{BASE}/")
counts_member = folder_counts(resp)
check(
    "Autor sieht eigenes privates Dokument im Zähler",
    counts_admin["Ebene 4"] == 1 and counts_admin["Ebene 1"] == 4,
    f"got {counts_admin}",
)
check(
    "Privates Dokument eines anderen zählt nicht",
    counts_member["Ebene 4"] == 0 and counts_member["Ebene 1"] == 2,
    f"got {counts_member}",
)
check(
    "Wurzel-Zähler sichtbarkeitsabhängig",
    resp.context["root_document_count"] == Motion.visible_to(m_member).count(),
)

# Persönlich geteiltes Dokument zählt beim Empfänger
doc_shared = Motion.objects.create(
    organization=org_a, author=m_admin, title="Geteiltes Dokument", folder=f4, visibility="shared"
)
MotionShare.objects.create(motion=doc_shared, scope="user", level="view", user=user_member, created_by=user_admin)
resp = c_member.get(f"{BASE}/")
check("Geteiltes Dokument zählt beim Empfänger", folder_counts(resp)["Ebene 4"] == 1)

# --- 7. Ordner löschen -> Inhalte zum Parent -------------------------------------
print("=== 7. Ordner löschen ===")
# f3 enthält doc_b und Unterordner f4 (mit 2 Dokumenten)
resp = c_admin.post(f"{BASE}/folders/{f3.id}/delete/")
doc_b.refresh_from_db()
f4.refresh_from_db()
check("Ordner gelöscht", not DocumentFolder.objects.filter(id=f3.id).exists())
check("Dokumente wandern zum Parent", doc_b.folder_id == f2.id)
check("Unterordner wandern zum Parent", f4.parent_id == f2.id)
check(
    "Dokumente im Unterordner bleiben erhalten",
    Motion.objects.filter(folder=f4).count() == 2,
)

# Wurzel-Ordner löschen -> Inhalte zur Wurzel (folder=None)
f_tmp = DocumentFolder.objects.create(organization=org_a, name="Temporär", created_by=m_admin)
doc_tmp = Motion.objects.create(organization=org_a, author=m_admin, title="Tmp-Dokument", folder=f_tmp)
c_admin.post(f"{BASE}/folders/{f_tmp.id}/delete/")
doc_tmp.refresh_from_db()
check("Wurzel-Ordner löschen -> Dokumente ohne Ordner", doc_tmp.folder_id is None)

# --- 8. Verwaltungsrechte --------------------------------------------------------
print("=== 8. Verwaltungsrechte ===")
# Mitglied (Admin-Rolle) darf eigene Ordner verwalten
resp = c_member.post(f"{BASE}/folders/create/", {"name": "Mitglied-Ordner"})
f_member = DocumentFolder.objects.get(organization=org_a, name="Mitglied-Ordner")
resp = c_member.post(f"{BASE}/folders/{f_member.id}/update/", {"name": "Mitglied-Ordner neu", "color": "green"})
f_member.refresh_from_db()
check("Eigenen Ordner umbenennen + Farbe", f_member.name == "Mitglied-Ordner neu" and f_member.color == "green")

# Verwaltung fremder Ordner braucht organization.edit — mit eingeschränkter Rolle prüfen
limited_role = Role.objects.create(organization=org_a, name="Nur Dokumente")
limited_role.permissions.set(Permission.objects.filter(codename__in=["motions.view", "motions.create", "motions.edit"]))
user_limited, m_limited = make_member(org_a, limited_role, "limitiert@example.org")
c_limited = client_for(user_limited)

resp = c_limited.post(f"{BASE}/folders/{f_member.id}/update/", {"name": "Gekapert"})
f_member.refresh_from_db()
check(
    "Fremden Ordner ohne organization.edit nicht verwaltbar",
    resp.status_code == 403 and f_member.name == "Mitglied-Ordner neu",
)
resp = c_limited.post(f"{BASE}/folders/create/", {"name": "Limitiert-Ordner"})
check(
    "Erstellrecht reicht zum Anlegen",
    DocumentFolder.objects.filter(organization=org_a, name="Limitiert-Ordner").exists(),
)
resp = c_admin.post(f"{BASE}/folders/{f_member.id}/update/", {"name": "Admin-Umbenannt"})
f_member.refresh_from_db()
check("Organisationsverwaltung darf fremde Ordner verwalten", f_member.name == "Admin-Umbenannt")

# --- 9. Gäste sehen keine Ordner -------------------------------------------------
print("=== 9. Gäste ===")
MotionShare.objects.create(motion=doc_shared, scope="user", level="edit", user=guest_user, created_by=user_admin)
resp = c_guest.get(f"{BASE}/")
check("Gast: Dokumentliste -> Redirect auf Freigaben", resp.status_code == 302 and "/freigaben/" in resp["Location"])
resp = c_guest.get(f"/work/{org_a.slug}/freigaben/")
html = resp.content.decode()
check("Gast-Übersicht bleibt flach (keine Ordner-UI)", resp.status_code == 200 and "Ebene" not in html)
resp = c_guest.get(f"{BASE}/{doc_shared.id}/")
html = resp.content.decode()
check("Gast-Editor: kein Ordner-Feld in der Sidebar", resp.status_code == 200 and "set_folder" not in html)
resp = c_guest.post(f"{BASE}/folders/create/", {"name": "Gast-Ordner"})
check(
    "Gast kann keine Ordner anlegen",
    resp.status_code in (302, 403) and not DocumentFolder.objects.filter(name="Gast-Ordner").exists(),
)

# --- 10. Org-Grenzen --------------------------------------------------------------
print("=== 10. Organisations-Grenzen ===")
resp = c_foreign.get(f"/work/{org_b.slug}/documents/?ordner={f1.id}")
check("Fremder Ordner im Filter -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_foreign.post(f"/work/{org_b.slug}/documents/folders/{f1.id}/update/", {"name": "Gekapert"})
f1.refresh_from_db()
check("Fremder Ordner nicht verwaltbar (404)", resp.status_code == 404 and f1.name == "Ebene 1")
resp = c_foreign.post(f"/work/{org_b.slug}/documents/folders/{f1.id}/delete/")
check(
    "Fremder Ordner nicht löschbar (404)", resp.status_code == 404 and DocumentFolder.objects.filter(id=f1.id).exists()
)
resp = ajax_post(
    c_foreign,
    f"/work/{org_b.slug}/documents/move-to-folder/",
    {"motion_ids": [str(doc_a.id)], "folder": ""},
)
doc_a.refresh_from_db()
check("Fremde Dokumente nicht verschiebbar", doc_a.folder_id == f2.id)
resp = c_admin.get(f"{BASE}/?ordner=kein-uuid")
check("Ungültige Ordner-ID -> 404", resp.status_code == 404)

# --- Ergebnis ---------------------------------------------------------------------
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
