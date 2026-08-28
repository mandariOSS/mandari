# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Dokument-Freigaben und geschützte Anhänge (Work-Portal).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_document_sharing.py

Prüft:
- Anhänge (MotionDocument) sind NICHT mehr per /media/-URL abrufbar
  (auch nicht für den Autor) – nur über die zugriffsgeprüfte Download-View:
  Autor 200, Gast mit Freigabe 200, Gast ohne Freigabe 403, Fremd-Org 404,
  anonym -> Login-Redirect
- Übrige Uploads (z. B. Aufgaben-Anhänge) unter /media/ nur angemeldet;
  Logos bleiben öffentlich
- Teilen-Dialog: Stufenwahl (view/comment/edit) wird übernommen, "admin"
  wird auf "view" normalisiert, erneutes Hinzufügen aktualisiert die Stufe
- Teilen-Dialog: nur aktive Mitglieder/Gäste DIESER Organisation
  (org-fremde und unbekannte Adressen -> 400, keine Freigabe angelegt)
- Ordner-Freigaben nur für Gastzugänge (Mitglied -> 400)
- Gast-Limit gilt auch bei Reaktivierung
- Entfernen einer Mitgliedschaft löscht Dokument- und Ordner-Freigaben
  der Organisation (andere Organisationen unberührt)
- Revisionshistorie für Gäste erst ab Beginn ihrer Freigabe (Liste und
  Detail; ältere Versionen -> 404), Mitglieder sehen alles
"""

import base64
import os
import secrets
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "mandari"))

DB_PATH = BASE_DIR / "smoke_document_sharing.sqlite3"
if DB_PATH.exists():
    DB_PATH.unlink()
MEDIA_DIR = BASE_DIR / "smoke_document_sharing_media"

os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"  # LocMem-Cache + DB-Sessions statt Redis
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ.setdefault("SECRET_KEY", "smoke-document-sharing")
# Verschlüsselte Inhalte (Revisionen) brauchen einen Master-Key – im CI nicht gesetzt
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.MEDIA_ROOT = str(MEDIA_DIR)

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.motions.models import (  # noqa: E402
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionDocument,
    MotionShare,
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
        print(f"  FAIL {name} {detail}")


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


# =============================================================================
print("=== Setup ===")
source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Testhausen")
org_a = Organization.objects.create(name="Fraktion A", slug="fraktion-a", body=body, guest_limit=1)
org_b = Organization.objects.create(name="Fraktion B", slug="fraktion-b", body=body)
admin_role_a = Role.objects.filter(organization=org_a, is_admin=True).first()
admin_role_b = Role.objects.filter(organization=org_b, is_admin=True).first()

user_admin = User.objects.create_user(email="admin-a@example.org", password="test1234!")
m_admin = Membership.objects.create(user=user_admin, organization=org_a)
m_admin.roles.add(admin_role_a)

user_member = User.objects.create_user(email="member-a@example.org", password="test1234!")
m_member = Membership.objects.create(user=user_member, organization=org_a)

user_guest = User.objects.create_user(email="guest@example.org", password="test1234!")
m_guest = Membership.objects.create(user=user_guest, organization=org_a, is_guest=True)

user_b = User.objects.create_user(email="admin-b@example.org", password="test1234!")
m_b = Membership.objects.create(user=user_b, organization=org_b)
m_b.roles.add(admin_role_b)

c_admin = client_for(user_admin)
c_member = client_for(user_member)
c_guest = client_for(user_guest)
c_b = client_for(user_b)
c_anon = Client()

BASE_A = f"/work/{org_a.slug}"

doc = Motion.objects.create(organization=org_a, author=m_admin, title="Vertraulich", visibility="private")
attachment = MotionDocument.objects.create(
    motion=doc,
    file=SimpleUploadedFile("geheim.pdf", b"%PDF-1.4 geheimer inhalt", content_type="application/pdf"),
    filename="geheim.pdf",
    mime_type="application/pdf",
    file_size=24,
    uploaded_by=m_admin,
)
media_url = f"/media/{attachment.file.name}"
download_url = f"{BASE_A}/documents/{doc.id}/files/{attachment.id}/download/"
check(
    "Anhang liegt unter motions/documents/", attachment.file.name.startswith("motions/documents/"), attachment.file.name
)

# =============================================================================
print("=== 1. Anhänge: kein direkter Media-Zugriff ===")
resp = c_admin.get(media_url)
check("Autor: /media/-URL -> 404 (nur Download-View)", resp.status_code == 404, f"got {resp.status_code}")
resp = c_anon.get(media_url)
check("Anonym: /media/-URL -> 404", resp.status_code == 404, f"got {resp.status_code}")

resp = c_admin.get(download_url)
check("Autor: Download-View -> 200", resp.status_code == 200, f"got {resp.status_code}")
check(
    "Download als Attachment mit Dateiname",
    'attachment; filename="geheim.pdf"' in resp.get("Content-Disposition", ""),
    resp.get("Content-Disposition"),
)
if resp.status_code == 200:
    content = b"".join(resp.streaming_content) if resp.streaming else resp.content
    check("Download liefert Dateiinhalt", content == b"%PDF-1.4 geheimer inhalt")
check("nosniff gesetzt", resp.get("X-Content-Type-Options") == "nosniff")

resp = c_member.get(download_url)
check("Mitglied ohne Zugriff (privates Dokument) -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = c_guest.get(download_url)
check("Gast ohne Freigabe -> 403", resp.status_code == 403, f"got {resp.status_code}")

MotionShare.objects.create(motion=doc, scope="user", user=user_guest, level="view", created_by=user_admin)
resp = c_guest.get(download_url)
check("Gast mit Freigabe (view) -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_guest.get(media_url)
check("Gast mit Freigabe: /media/ trotzdem 404", resp.status_code == 404, f"got {resp.status_code}")

resp = c_b.get(download_url)
check("Fremd-Org -> kein Zugriff (403/404)", resp.status_code in (403, 404), f"got {resp.status_code}")
resp = c_anon.get(download_url)
check("Anonym: Download-View -> Login-Redirect", resp.status_code == 302, f"got {resp.status_code}")

# Falsche Dokument-/Motion-Kombination
doc2 = Motion.objects.create(organization=org_a, author=m_admin, title="Anderes", visibility="organization")
resp = c_admin.get(f"{BASE_A}/documents/{doc2.id}/files/{attachment.id}/download/")
check("Anhang unter fremder Motion-ID -> 404", resp.status_code == 404, f"got {resp.status_code}")

print("=== 1b. Übrige Medien: Login-Pflicht, Logos öffentlich ===")
(MEDIA_DIR / "tasks" / "attachments").mkdir(parents=True, exist_ok=True)
(MEDIA_DIR / "tasks" / "attachments" / "notiz.txt").write_text("intern", encoding="utf-8")
(MEDIA_DIR / "organizations" / "logos").mkdir(parents=True, exist_ok=True)
(MEDIA_DIR / "organizations" / "logos" / "logo.svg").write_text("<svg/>", encoding="utf-8")
resp = c_anon.get("/media/tasks/attachments/notiz.txt")
check("Anonym: Aufgaben-Anhang -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_member.get("/media/tasks/attachments/notiz.txt")
check("Angemeldet: Aufgaben-Anhang -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Angemeldet: kein öffentliches Caching", "no-store" in resp.get("Cache-Control", ""), resp.get("Cache-Control"))
resp = c_anon.get("/media/organizations/logos/logo.svg")
check("Anonym: Logo -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = c_anon.get("/media/session/files/2026/01/x.pdf")
check("Session-Anlagen weiterhin 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
print("=== 2. Teilen-Dialog: Stufenwahl ===")
share_url = f"{BASE_A}/documents/{doc.id}/share/update/"


def share_post(client, email, level=None, **extra):
    data = {"visibility": "shared", "add_user_email": email}
    if level is not None:
        data["level"] = level
    return client.post(share_url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest", **extra)


resp = share_post(c_admin, "member-a@example.org", "comment")
check("Freigabe mit Stufe comment -> 204", resp.status_code == 204, f"got {resp.status_code}")
share = MotionShare.objects.filter(motion=doc, scope="user", user=user_member).first()
check("Stufe comment gespeichert", share is not None and share.level == "comment", str(share and share.level))

resp = share_post(c_admin, "member-a@example.org", "edit")
share.refresh_from_db()
check("Erneutes Hinzufügen aktualisiert Stufe (edit)", share.level == "edit", share.level)
check("Keine doppelte Freigabe", MotionShare.objects.filter(motion=doc, scope="user", user=user_member).count() == 1)

resp = share_post(c_admin, "member-a@example.org", "admin")
share.refresh_from_db()
check("Stufe admin wird auf view normalisiert", share.level == "view", share.level)

resp = share_post(c_admin, "guest@example.org")
guest_share = MotionShare.objects.get(motion=doc, scope="user", user=user_guest)
check("Ohne Stufe: Standard view", guest_share.level == "view", guest_share.level)
check("Gast nach Freigabe: can_access, kein Edit", doc.can_access(m_guest) and not doc.can_edit(m_guest))

resp = share_post(c_admin, "guest@example.org", "edit")
check("Gast auf edit -> can_edit", Motion.objects.get(id=doc.id).can_edit(m_guest))

print("=== 2b. Teilen-Dialog: nur Mitglieder/Gäste dieser Organisation ===")
resp = share_post(c_admin, "admin-b@example.org", "view")
check("Org-fremder Nutzer -> 400", resp.status_code == 400, f"got {resp.status_code}")
check("Keine Freigabe für Fremd-Org angelegt", not MotionShare.objects.filter(motion=doc, user=user_b).exists())
resp = share_post(c_admin, "niemand@example.org", "view")
check("Unbekannte Adresse -> 400", resp.status_code == 400, f"got {resp.status_code}")
resp_known = share_post(c_admin, "admin-b@example.org", "view")
check(
    "Gleiche Fehlermeldung für unbekannt und org-fremd (keine Konto-Enumeration)",
    resp.json().get("error", "").replace("niemand@example.org", "X")
    == resp_known.json().get("error", "").replace("admin-b@example.org", "X"),
    f"{resp.json()} vs {resp_known.json()}",
)
m_member.is_active = False
m_member.save()
resp = share_post(c_admin, "member-a@example.org", "view")
check("Deaktiviertes Mitglied -> 400", resp.status_code == 400, f"got {resp.status_code}")
m_member.is_active = True
m_member.save()

resp = share_post(c_member, "guest@example.org", "edit")
check("Nicht-Autor ohne edit_all darf nicht freigeben -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print("=== 3. Ordner-Freigaben nur für Gäste ===")
folder = DocumentFolder.objects.create(organization=org_a, name="Projekte", created_by=m_admin)
folder_share_url = f"{BASE_A}/documents/folders/{folder.id}/share/"
resp = c_admin.post(
    folder_share_url, {"email": "member-a@example.org", "level": "view"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
)
check("Ordner-Freigabe an Mitglied -> 400", resp.status_code == 400, f"got {resp.status_code}")
check(
    "Keine Ordner-Freigabe für Mitglied", not FolderGuestShare.objects.filter(folder=folder, user=user_member).exists()
)
resp = c_admin.post(
    folder_share_url, {"email": "guest@example.org", "level": "comment"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
)
check("Ordner-Freigabe an Gast -> 200", resp.status_code == 200, f"got {resp.status_code}")
check(
    "Ordner-Freigabe angelegt",
    FolderGuestShare.objects.filter(folder=folder, user=user_guest, level="comment").exists(),
)

# =============================================================================
print("=== 4. Gast-Limit bei Reaktivierung ===")
m_guest.is_active = False
m_guest.save()
user_guest2 = User.objects.create_user(email="guest2@example.org", password="test1234!")
m_guest2 = Membership.objects.create(user=user_guest2, organization=org_a, is_guest=True)
check("Limit 1 belegt durch Gast 2", not org_a.has_free_guest_slot())
resp = c_admin.post(f"{BASE_A}/organization/members/{m_guest.id}/", {"action": "reactivate"})
m_guest.refresh_from_db()
check("Reaktivierung über Limit blockiert", m_guest.is_active is False, f"is_active={m_guest.is_active}")
m_guest2.is_active = False
m_guest2.save()
resp = c_admin.post(f"{BASE_A}/organization/members/{m_guest.id}/", {"action": "reactivate"})
m_guest.refresh_from_db()
check("Reaktivierung mit freiem Platz möglich", m_guest.is_active is True)
resp = c_admin.post(f"{BASE_A}/organization/members/{m_member.id}/", {"action": "deactivate"})
resp = c_admin.post(f"{BASE_A}/organization/members/{m_member.id}/", {"action": "reactivate"})
m_member.refresh_from_db()
check("Mitglieder-Reaktivierung unabhängig vom Gast-Limit", m_member.is_active is True)

# =============================================================================
print("=== 5. Entfernen löscht Freigaben (nur diese Organisation) ===")
# Gast ist zusätzlich Mitglied in Org B mit eigener Freigabe dort
m_guest_b = Membership.objects.create(user=user_guest, organization=org_b)
doc_b = Motion.objects.create(organization=org_b, author=m_b, title="B-Dokument", visibility="shared")
MotionShare.objects.create(motion=doc_b, scope="user", user=user_guest, level="view", created_by=user_b)
check(
    "Vorher: Dokument-Freigabe in A", MotionShare.objects.filter(motion__organization=org_a, user=user_guest).exists()
)
check(
    "Vorher: Ordner-Freigabe in A",
    FolderGuestShare.objects.filter(folder__organization=org_a, user=user_guest).exists(),
)
resp = c_admin.post(f"{BASE_A}/organization/members/{m_guest.id}/", {"action": "remove"})
check("Gast entfernt", not Membership.objects.filter(id=m_guest.id).exists())
check(
    "Dokument-Freigaben in A gelöscht",
    not MotionShare.objects.filter(motion__organization=org_a, user=user_guest).exists(),
)
check(
    "Ordner-Freigaben in A gelöscht",
    not FolderGuestShare.objects.filter(folder__organization=org_a, user=user_guest).exists(),
)
check("Freigabe in Org B bleibt", MotionShare.objects.filter(motion=doc_b, user=user_guest).exists())
resp = c_guest.get(download_url)
check("Entfernter Gast: Download -> kein Zugriff", resp.status_code in (403, 404), f"got {resp.status_code}")

# Erneute Aufnahme als Gast: keine Altlasten
m_guest_again = Membership.objects.create(user=user_guest, organization=org_a, is_guest=True)
check("Nach erneuter Aufnahme: keine alte Freigabe wirksam", not doc.can_access(m_guest_again))

# =============================================================================
print("=== 6. Revisionshistorie für Gäste erst ab Freigabe ===")
from datetime import timedelta  # noqa: E402

from apps.work.motions.models import MotionRevision  # noqa: E402
from django.utils import timezone  # noqa: E402

doc_rev = Motion.objects.create(organization=org_a, author=m_admin, title="Mit Historie", visibility="private")
alt = []
for v in (1, 2):
    rev = MotionRevision(motion=doc_rev, version=v, changed_by=m_admin, change_summary=f"Entwurf {v}")
    rev.set_content_encrypted(f"<p>Interner Entwurf {v}</p>")
    rev.save()
    MotionRevision.objects.filter(id=rev.id).update(created_at=timezone.now() - timedelta(days=3 - v))
    alt.append(rev)

user_guest3 = User.objects.create_user(email="guest3@example.org", password="test1234!")
m_guest3 = Membership.objects.create(user=user_guest3, organization=org_a, is_guest=True)
c_guest3 = client_for(user_guest3)
rev_url = f"{BASE_A}/documents/{doc_rev.id}/revisions/"

resp = c_guest3.get(rev_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
check("Gast ohne Freigabe: Historie -> 403", resp.status_code == 403, f"got {resp.status_code}")

share3 = MotionShare.objects.create(motion=doc_rev, scope="user", user=user_guest3, level="view", created_by=user_admin)
neu = MotionRevision(motion=doc_rev, version=3, changed_by=m_admin, change_summary="Nach Freigabe")
neu.set_content_encrypted("<p>Version nach Freigabe</p>")
neu.save()

resp = c_guest3.get(rev_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
versions = [r["version"] for r in resp.json().get("revisions", [])]
check("Gast: nur Versionen ab Freigabe", versions == [3], str(versions))
check("Gast: restricted_since gesetzt", resp.json().get("restricted_since") is not None)
resp = c_guest3.get(f"{rev_url}{alt[0].id}/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
check("Gast: alte Version im Detail -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = c_guest3.get(f"{rev_url}{neu.id}/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
check("Gast: neue Version im Detail -> 200", resp.status_code == 200, f"got {resp.status_code}")
check(
    "Gast: Detail enthält keinen alten Inhalt",
    resp.status_code == 200 and "Interner Entwurf" not in resp.content.decode("utf-8", errors="ignore"),
)

resp = c_admin.get(rev_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
versions = [r["version"] for r in resp.json().get("revisions", [])]
check("Autor: alle Versionen", sorted(versions) == [1, 2, 3], str(versions))
check("Autor: restricted_since leer", resp.json().get("restricted_since") is None)

# Ordner-Freigabe: Zeitpunkt der Ordner-Freigabe zählt
folder_rev = DocumentFolder.objects.create(organization=org_a, name="Historie", created_by=m_admin)
doc_rev.folder = folder_rev
doc_rev.save(update_fields=["folder"])
share3.delete()
FolderGuestShare.objects.create(folder=folder_rev, user=user_guest3, level="view", created_by=user_admin)
neu2 = MotionRevision(motion=doc_rev, version=4, changed_by=m_admin, change_summary="Nach Ordner-Freigabe")
neu2.set_content_encrypted("<p>v4</p>")
neu2.save()
resp = c_guest3.get(rev_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
versions = [r["version"] for r in resp.json().get("revisions", [])]
check("Ordner-Freigabe: nur Versionen ab deren Beginn", versions == [4], str(versions))

# =============================================================================
import shutil  # noqa: E402

shutil.rmtree(MEDIA_DIR, ignore_errors=True)
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
if FAIL:
    sys.exit(1)
print("SMOKE_DOCUMENT_SHARING_OK")
