# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Gast-Isolation (Matrix), Ordner-Freigaben, Multi-Org.

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_guest_isolation.py

Prüft:
- Isolations-Matrix: ALLE work-URL-Namen werden enumeriert und als
  eingeloggter Gast per GET und POST aufgerufen. Alles außerhalb der
  expliziten Gast-Whitelist muss dicht sein (Redirect auf die
  Gast-Übersicht /freigaben/ bzw. 403/404) — insbesondere
  Fraktionssitzungen, Sitzungsvorbereitung, Aufgaben, Mitgliederliste,
  Dashboard, RIS, Einstellungen, Benachrichtigungen, Suche, HTMX/JSON.
- WebSocket-Consumer: DocumentCollaborationConsumer (share-basiert)
  und PreparationConsumer (Gäste ausgeschlossen).
- Ordner-Freigaben (FolderGuestShare): gelten rekursiv für Unterordner
  und enthaltene Dokumente, auch künftig hinzukommende; Level-Vererbung
  (höchstes Level gewinnt); Gast-Übersicht mit navigierbarem Baum;
  Verwaltungs-Endpunkte (freigeben/entziehen) inkl. Org-Grenzen.
- Multi-Org: derselbe User ist Gast in Org A und Voll-Mitglied in Org B —
  in A nur Freigaben, in B alles Normale; Org-Switcher zeigt beide;
  bestehende User (Gast anderswo) können in weitere Orgs eingeladen
  werden (Gast-Einladung + reguläre Einladung).
"""

import base64
import os
import secrets
import sys
import tempfile
import uuid
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

import django  # noqa: E402

django.setup()

from asgiref.sync import async_to_sync  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

import apps.work.urls as work_urls  # noqa: E402
from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role, UserInvitation  # noqa: E402
from apps.work.meetings.consumers import PreparationConsumer  # noqa: E402
from apps.work.motions.consumers import DocumentCollaborationConsumer  # noqa: E402
from apps.work.motions.models import (  # noqa: E402
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionShare,
)
from insight_core.models import OParlBody, OParlPaper, OParlSource  # noqa: E402

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

org_a = Organization.objects.create(name="Fraktion A", slug="fraktion-a", body=body)
org_b = Organization.objects.create(name="Fraktion B", slug="fraktion-b", body=body)
org_c = Organization.objects.create(name="Fraktion C", slug="fraktion-c", body=body)

admin_role_a = Role.objects.filter(organization=org_a, is_admin=True).first()
admin_role_b = Role.objects.filter(organization=org_b, is_admin=True).first()
admin_role_c = Role.objects.filter(organization=org_c, is_admin=True).first()

# Admin in Org A
user_admin = User.objects.create_user(email="admin-a@example.org", password="test1234!")
m_admin = Membership.objects.create(user=user_admin, organization=org_a)
m_admin.roles.add(admin_role_a)

# Multi-Org-User: Gast in Org A + Voll-Mitglied (Admin) in Org B
user_multi = User.objects.create_user(email="multi@example.org", password="test1234!")
m_guest_a = Membership.objects.create(user=user_multi, organization=org_a, is_guest=True)
m_member_b = Membership.objects.create(user=user_multi, organization=org_b)
m_member_b.roles.add(admin_role_b)

# Admin in Org C (lädt den Multi-User später als Gast ein)
user_admin_c = User.objects.create_user(email="admin-c@example.org", password="test1234!")
m_admin_c = Membership.objects.create(user=user_admin_c, organization=org_c)
m_admin_c.roles.add(admin_role_c)

# Mitglied ohne Ordner-Verwaltungsrechte in Org A (nur motions.share individuell)
user_plain = User.objects.create_user(email="plain-a@example.org", password="test1234!")
m_plain = Membership.objects.create(user=user_plain, organization=org_a)

c_admin = client_for(user_admin)
c_guest = client_for(user_multi)
c_admin_c = client_for(user_admin_c)

BASE_A = f"/work/{org_a.slug}"
BASE_B = f"/work/{org_b.slug}"

# Ordnerbaum in Org A: Projekte > 2026 > Q1  |  Intern (nicht freigegeben)
folder_root = DocumentFolder.objects.create(organization=org_a, name="Projekte", created_by=m_admin)
folder_sub = DocumentFolder.objects.create(organization=org_a, name="2026", parent=folder_root, created_by=m_admin)
folder_subsub = DocumentFolder.objects.create(organization=org_a, name="Q1", parent=folder_sub, created_by=m_admin)
folder_secret = DocumentFolder.objects.create(organization=org_a, name="Intern", created_by=m_admin)

doc_root = Motion.objects.create(
    organization=org_a, author=m_admin, title="Projektplan", visibility="private", folder=folder_root
)
doc_sub = Motion.objects.create(
    organization=org_a, author=m_admin, title="Jahresplanung", visibility="organization", folder=folder_sub
)
doc_secret = Motion.objects.create(
    organization=org_a, author=m_admin, title="Internes Papier", visibility="organization", folder=folder_secret
)
doc_no_folder = Motion.objects.create(organization=org_a, author=m_admin, title="Ohne Ordner", visibility="private")
doc_direct_share = Motion.objects.create(
    organization=org_a, author=m_admin, title="Direkt geteilt", visibility="shared"
)
MotionShare.objects.create(motion=doc_direct_share, scope="user", user=user_multi, level="view", created_by=user_admin)

# =============================================================================
print("=== A. Ordner-Freigabe: rekursive Semantik ===")

share_root = FolderGuestShare.objects.create(folder=folder_root, user=user_multi, level="view", created_by=user_admin)

check("Gast: Dokument im freigegebenen Ordner -> can_access", doc_root.can_access(m_guest_a))
check("Gast: Dokument im Unterordner (rekursiv) -> can_access", doc_sub.can_access(m_guest_a))
check("Gast: Dokument in fremdem Ordner -> kein Zugriff", not doc_secret.can_access(m_guest_a))
check("Gast: Dokument ohne Ordner/Freigabe -> kein Zugriff", not doc_no_folder.can_access(m_guest_a))
check("Level view: kein Edit", not doc_root.can_edit(m_guest_a))
check("Level view: kein Kommentar", not doc_root.can_comment(m_guest_a))

# Künftig hinzukommendes Dokument im tiefsten Unterordner
doc_future = Motion.objects.create(
    organization=org_a, author=m_admin, title="Später hinzugefügt", visibility="private", folder=folder_subsub
)
check("Künftiges Dokument im Teilbaum -> can_access", doc_future.can_access(m_guest_a))

resp = c_guest.get(f"{BASE_A}/documents/{doc_sub.id}/")
check("Gast: Editor für Ordner-Dokument -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_A}/documents/{doc_secret.id}/")
check("Gast: Editor für nicht freigegebenes Dokument -> 403", resp.status_code == 403, f"got {resp.status_code}")

# visible_to enthält Ordner-Dokumente + Direkt-Freigaben
visible_titles = set(Motion.visible_to(m_guest_a).values_list("title", flat=True))
check(
    "visible_to: Ordner-Teilbaum + Direkt-Freigabe, nichts anderes",
    visible_titles == {"Projektplan", "Jahresplanung", "Später hinzugefügt", "Direkt geteilt"},
    str(visible_titles),
)

print("=== A. Level-Vererbung (höchstes Level gewinnt) ===")
FolderGuestShare.objects.create(folder=folder_sub, user=user_multi, level="edit", created_by=user_admin)
levels = FolderGuestShare.shared_folder_levels(user_multi, org_a)
check("Wurzel-Ordner: view", levels.get(folder_root.id) == "view", str(levels))
check("Unterordner: edit (direkte Freigabe schlägt geerbte)", levels.get(folder_sub.id) == "edit")
check("Tiefster Ordner erbt edit", levels.get(folder_subsub.id) == "edit")
check("Nicht freigegebener Ordner fehlt", folder_secret.id not in levels)
check("Dokument im edit-Teilbaum: can_edit", doc_sub.can_edit(m_guest_a))
check("Dokument im edit-Teilbaum: can_comment", doc_sub.can_comment(m_guest_a))
check("Dokument im view-Teil: weiterhin kein Edit", not doc_root.can_edit(m_guest_a))

print("=== A. Gast-Übersicht: navigierbarer Baum ===")
resp = c_guest.get(f"{BASE_A}/freigaben/")
content = resp.content.decode("utf-8", errors="ignore")
check("Übersicht -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Übersicht listet freigegebenen Wurzel-Ordner", "Projekte" in content)
check("Übersicht listet Direkt-Freigabe", "Direkt geteilt" in content)
check("Übersicht listet fremden Ordner NICHT", "Intern" not in content)

resp = c_guest.get(f"{BASE_A}/freigaben/?ordner={folder_root.id}")
content = resp.content.decode("utf-8", errors="ignore")
check("Ordner-Ansicht -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Ordner-Ansicht zeigt Dokument", "Projektplan" in content)
check("Ordner-Ansicht zeigt Unterordner", "2026" in content)

resp = c_guest.get(f"{BASE_A}/freigaben/?ordner={folder_subsub.id}")
check("Unter-Unterordner navigierbar -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Künftiges Dokument sichtbar", "Später hinzugefügt" in resp.content.decode("utf-8", errors="ignore"))

resp = c_guest.get(f"{BASE_A}/freigaben/?ordner={folder_secret.id}")
check("Nicht freigegebener Ordner -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_A}/freigaben/?ordner={uuid.uuid4()}")
check("Unbekannter Ordner -> 404", resp.status_code == 404, f"got {resp.status_code}")

print("=== A. Verwaltungs-Endpunkte (freigeben/entziehen) ===")
resp = c_admin.post(
    f"{BASE_A}/documents/folders/{folder_secret.id}/share/",
    {"email": user_multi.email, "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Admin: Ordner freigeben -> 200", resp.status_code == 200, f"got {resp.status_code}")
new_share = FolderGuestShare.objects.filter(folder=folder_secret, user=user_multi).first()
check("Freigabe angelegt (level=view)", new_share is not None and new_share.level == "view")
check("Gast sieht Dokument im neu freigegebenen Ordner", doc_secret.can_access(m_guest_a))

resp = c_admin.post(
    f"{BASE_A}/documents/folders/{folder_secret.id}/share/",
    {"email": "unbekannt@example.org", "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Unbekannte E-Mail -> 400", resp.status_code == 400, f"got {resp.status_code}")

resp = c_admin.post(
    f"{BASE_A}/documents/folders/{folder_secret.id}/share/",
    {"email": user_admin_c.email, "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Nutzer ohne Org-Zugang -> 400", resp.status_code == 400, f"got {resp.status_code}")

# Mitglied ohne guests.manage/motions.share kommt nicht an den Endpoint
c_plain = client_for(user_plain)
resp = c_plain.post(
    f"{BASE_A}/documents/folders/{folder_secret.id}/share/",
    {"email": user_multi.email, "level": "view"},
)
check("Mitglied ohne Recht: Freigeben -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Fremde Org kommt nicht an Ordner/Freigaben heran
resp = c_admin_c.post(
    f"/work/{org_c.slug}/documents/folders/{folder_secret.id}/share/",
    {"email": user_admin_c.email, "level": "view"},
)
check("Fremde Org: Ordner-Freigabe -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_admin_c.post(f"/work/{org_c.slug}/documents/folders/shares/{new_share.id}/remove/")
check("Fremde Org: Freigabe entziehen -> 404", resp.status_code == 404, f"got {resp.status_code}")

resp = c_admin.post(
    f"{BASE_A}/documents/folders/shares/{new_share.id}/remove/",
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Admin: Freigabe entziehen -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Zugriff nach Entzug wieder dicht", not doc_secret.can_access(m_guest_a))

print("=== A. Gast-Einladung mit Ordner-Freigabe ===")
resp = c_admin.get(f"{BASE_A}/organization/members/invite-guest/")
content = resp.content.decode("utf-8", errors="ignore")
check("Einladungsseite -> 200 mit Ordner-Auswahl", resp.status_code == 200 and "Ordner freigeben" in content)
check("Einladungsseite listet Ordner", "Projekte" in content and "Intern" in content)

resp = c_admin.post(
    f"{BASE_A}/organization/members/invite-guest/",
    {"email": "ordnergast@example.org", "share_level": "comment", "folders": [str(folder_root.id)]},
)
check("Einladung: Redirect", resp.status_code == 302, f"got {resp.status_code}")
folder_guest = User.objects.filter(email="ordnergast@example.org").first()
check("Gast-User angelegt", folder_guest is not None)
inv_share = FolderGuestShare.objects.filter(folder=folder_root, user=folder_guest).first()
check("Ordner-Freigabe aus Einladung (level=comment)", inv_share is not None and inv_share.level == "comment")
m_folder_guest = Membership.objects.get(user=folder_guest, organization=org_a)
check("Eingeladener Gast: Dokument im Teilbaum -> can_comment", doc_sub.can_comment(m_folder_guest))
check("Gast-Limit zählt pro Gast (2 Gäste, nicht pro Freigabe)", org_a.get_active_guest_count() == 2)

# =============================================================================
print("=== B. Isolations-Matrix: alle work-URLs als Gast ===")

# Whitelist: Views mit guest_allowed=True (Zugriff dort share-basiert geprüft)
GUEST_ALLOWED = {
    "guest_documents",
    "document_editor",
    "document_export",
    "document_comment",
    "document_comment_resolve",
    "document_revisions",
    "document_revision_detail",
    "profile",
    "security",
}
# Kein Org-Kontext (Redirect-Helfer bzw. öffentliche Einladungsannahme)
SKIP = {"root", "accept_invitation"}

FIXED_KWARGS = {
    "org_slug": org_a.slug,
    "token": "dummy-token",
    "anchor_type": "file",
    "category_slug": "kategorie",
    "article_slug": "artikel",
}

tested = 0
blocked = 0
leaks = []

for pattern in work_urls.urlpatterns:
    name = pattern.name
    if not name or name in SKIP or name in GUEST_ALLOWED:
        continue
    kwargs = {}
    for param in pattern.pattern.converters:
        kwargs[param] = FIXED_KWARGS.get(param, uuid.uuid4())
    url = reverse(f"work:{name}", kwargs=kwargs)

    for method in ("get", "post"):
        resp = getattr(c_guest, method)(url)
        tested += 1
        location = resp.headers.get("Location", "")
        is_blocked = (resp.status_code in (301, 302) and location.endswith("/freigaben/")) or resp.status_code in (
            403,
            404,
            405,
        )
        if not is_blocked and resp.status_code in (301, 302):
            # Legacy-Redirects (/motions/ -> /documents/): Kette folgen —
            # das Ziel muss selbst dicht sein (Gast-Übersicht, 403 oder 404).
            final = getattr(c_guest, method)(url, follow=True)
            final_path = final.request.get("PATH_INFO", "")
            is_blocked = final.status_code in (403, 404, 405) or (
                final.status_code == 200 and final_path.endswith("/freigaben/")
            )
        if is_blocked:
            blocked += 1
        else:
            leaks.append(f"{method.upper()} {name} -> {resp.status_code} {location}")

check(
    f"Matrix: {tested} Aufrufe ({tested // 2} URL-Namen x GET/POST) alle dicht",
    not leaks,
    "; ".join(leaks[:10]),
)
print(f"       geprüft: {tested} Aufrufe, dicht: {blocked}, Whitelist: {len(GUEST_ALLOWED)}, übersprungen: {len(SKIP)}")

# Whitelist-Endpunkte: erreichbar, aber share-basiert geprüft
resp = c_guest.get(f"{BASE_A}/freigaben/")
check("Whitelist: Gast-Übersicht -> 200", resp.status_code == 200)
resp = c_guest.get(f"{BASE_A}/profile/")
check("Whitelist: Profil -> 200", resp.status_code == 200)
resp = c_guest.get(f"{BASE_A}/documents/{doc_no_folder.id}/revisions/")
check("Whitelist: Revisions ohne Freigabe -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_A}/documents/{doc_no_folder.id}/export/?format=pdf")
check("Whitelist: Export ohne Freigabe -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = c_guest.post(f"{BASE_A}/documents/{doc_no_folder.id}/comment/", {"content": "Hack"})
check("Whitelist: Kommentar ohne Freigabe -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_A}/documents/{doc_sub.id}/revisions/")
check("Whitelist: Revisions mit Ordner-Freigabe -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Gast hat in fremder Org (ohne Membership) gar nichts verloren
resp = c_guest.get(f"/work/{org_c.slug}/freigaben/")
check("Fremde Org ohne Membership -> 403", resp.status_code == 403, f"got {resp.status_code}")

print("=== B. WebSocket-Consumer ===")


def doc_ws_access(user, motion):
    consumer = DocumentCollaborationConsumer()
    consumer.document_id = str(motion.id)
    consumer.user = user
    return async_to_sync(consumer._check_access)()


access, _ = doc_ws_access(user_multi, doc_no_folder)
check("Doc-WS: Gast ohne Freigabe abgewiesen", access is None, str(access))
access, ms_id = doc_ws_access(user_multi, doc_sub)
check("Doc-WS: Ordner-Freigabe edit -> edit", access == "edit" and ms_id == m_guest_a.id, str(access))
access, _ = doc_ws_access(user_multi, doc_root)
check("Doc-WS: Ordner-Freigabe view -> view", access == "view", str(access))
access, _ = doc_ws_access(user_admin_c, doc_sub)
check("Doc-WS: Fremder ohne Membership abgewiesen", access is None, str(access))

paper = OParlPaper.objects.create(external_id="https://ris.example.org/paper/1", body=body, name="Vorlage")


def prep_ws_access(user):
    consumer = PreparationConsumer()
    consumer.org_slug = org_a.slug
    consumer.scope_type = "paper"
    consumer.object_id = str(paper.id)
    return async_to_sync(consumer._check_access)(user)


check("Prep-WS: Gast abgewiesen", prep_ws_access(user_multi) is None)
check("Prep-WS: Admin zugelassen", prep_ws_access(user_admin) == org_a.id)

# =============================================================================
print("=== C. Multi-Org: Gast in A + Voll-Mitglied in B ===")

resp = c_guest.get(f"{BASE_A}/")
check(
    "In A: Dashboard -> Redirect auf Gast-Übersicht",
    resp.status_code == 302 and resp.headers.get("Location", "").endswith("/freigaben/"),
    f"got {resp.status_code} -> {resp.headers.get('Location')}",
)
for label, url in [
    ("Fraktionssitzungen", f"{BASE_A}/faction/"),
    ("Aufgaben", f"{BASE_A}/tasks/"),
    ("Mitglieder", f"{BASE_A}/organization/members/"),
]:
    resp = c_guest.get(url)
    check(
        f"In A: {label} dicht",
        resp.status_code == 302 and resp.headers.get("Location", "").endswith("/freigaben/"),
        f"got {resp.status_code}",
    )

resp = c_guest.get(f"{BASE_B}/")
check("In B: Dashboard -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_B}/documents/")
check("In B: Dokumente -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_B}/faction/")
check("In B: Fraktionssitzungen -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_guest.get(f"{BASE_B}/organization/members/")
check("In B: Mitgliederliste -> 200", resp.status_code == 200, f"got {resp.status_code}")

print("=== C. Org-Switcher ===")
resp = c_guest.get(f"{BASE_B}/")
content = resp.content.decode("utf-8", errors="ignore")
check("Switcher in B zeigt Org A", f"/work/{org_a.slug}/" in content)
check("Switcher in B markiert Gast-Zugang", "(Gast)" in content)
resp = c_guest.get(f"{BASE_A}/freigaben/")
content = resp.content.decode("utf-8", errors="ignore")
check("Switcher in A (Gast-Ansicht) zeigt Org B", f"/work/{org_b.slug}/" in content)

print("=== C. Bestehender User in weitere Org einladbar ===")
# Gast-Einladung: existierender User (Gast in A, Mitglied in B) wird Gast in C
resp = c_admin_c.post(
    f"/work/{org_c.slug}/organization/members/invite-guest/",
    {"email": user_multi.email, "share_level": "view"},
)
m_guest_c = Membership.objects.filter(user=user_multi, organization=org_c).first()
check("Gast-Einladung für bestehenden User -> Membership(is_guest)", m_guest_c is not None and m_guest_c.is_guest)
check(
    "User hat jetzt 3 Mitgliedschaften (A: Gast, B: Mitglied, C: Gast)",
    user_multi.memberships.filter(is_active=True).count() == 3,
)
resp = c_guest.get(f"/work/{org_c.slug}/")
check(
    "In C: als Gast nur Freigaben",
    resp.status_code == 302 and resp.headers.get("Location", "").endswith("/freigaben/"),
    f"got {resp.status_code}",
)

# Reguläre Einladung: existierender User (Gast in C) wird Voll-Mitglied in Org D
org_d = Organization.objects.create(name="Fraktion D", slug="fraktion-d", body=body)
member_role_d = Role.objects.filter(organization=org_d, name="Fraktionsmitglied").first()
invitation = UserInvitation.create_for_organization(
    organization=org_d,
    email=user_multi.email,
    invited_by=user_admin_c,
    roles=Role.objects.filter(id=member_role_d.id),
    valid_days=7,
)
resp = c_guest.post(f"/work/invitation/{invitation.token}/")
m_member_d = Membership.objects.filter(user=user_multi, organization=org_d).first()
check("Reguläre Einladung angenommen -> Voll-Mitgliedschaft", m_member_d is not None and not m_member_d.is_guest)
invitation.refresh_from_db()
check("Einladung als angenommen markiert", invitation.accepted_at is not None)
resp = c_guest.get(f"/work/{org_d.slug}/")
check("In D: Dashboard als Voll-Mitglied -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Gast-Status ist pro Membership, nicht pro User
check(
    "is_guest pro Membership: A=Gast, B=Mitglied, C=Gast, D=Mitglied",
    m_guest_a.is_guest and not m_member_b.is_guest and m_guest_c.is_guest and not m_member_d.is_guest,
)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
