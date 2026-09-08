# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Benachrichtigungen für Gäste und Beteiligte (Issue #75).

- Gast-Einladung mit Freigaben: eine Mail mit Freigabeliste + Passwort-Link,
  In-App-Hinweise ohne zusätzliche Mails
- Teilen-Dialog: Freigabe/Stufenwechsel benachrichtigt (In-App + E-Mail)
- Ordner-Freigabe benachrichtigt
- Kommentare: Autor:in + bisherige Kommentator:innen (Gäste per E-Mail)
- „Zugang erneut senden“ für Gäste (Rechte, neuer Passwort-Link)
- E-Mail-Präferenz wird respektiert
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'smoke.sqlite3').as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["SITE_URL"] = "https://work.example"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionShare  # noqa: E402
from apps.work.notifications.models import Notification, NotificationPreference  # noqa: E402
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


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def mails_to(address):
    return [m for m in mail.outbox if address in m.to]


# =============================================================================
print("=== Setup ===")
source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Testhausen")
org = Organization.objects.create(name="Fraktion A", slug="fraktion-a", body=body)
admin_role = Role.objects.filter(organization=org, is_admin=True).first()

user_admin = User.objects.create_user(
    email="admin@example.org", password="test1234!", first_name="Alma", last_name="Admin"
)
m_admin = Membership.objects.create(user=user_admin, organization=org)
m_admin.roles.add(admin_role)
user_member = User.objects.create_user(
    email="member@example.org", password="test1234!", first_name="Max", last_name="Mitglied"
)
m_member = Membership.objects.create(user=user_member, organization=org)

c_admin = client_for(user_admin)
c_member = client_for(user_member)
BASE = f"/work/{org.slug}"

doc = Motion.objects.create(organization=org, author=m_admin, title="Haushaltsentwurf 2027", visibility="private")
doc2 = Motion.objects.create(organization=org, author=m_admin, title="Radwegekonzept", visibility="private")
folder = DocumentFolder.objects.create(organization=org, name="Projekte", created_by=m_admin)

# =============================================================================
print()
print("=== 1. Gast-Einladung mit Freigaben ===")
mail.outbox.clear()
resp = c_admin.post(
    f"{BASE}/organization/members/invite-guest/",
    {
        "email": "gast@example.org",
        "message": "Willkommen im Projekt",
        "share_level": "comment",
        "documents": [str(doc.id)],
        "folders": [str(folder.id)],
    },
)
check("Einladung -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
user_guest = User.objects.get(email="gast@example.org")
m_guest = Membership.objects.get(user=user_guest, organization=org)
check("Gast-Membership angelegt", m_guest.is_guest and not user_guest.has_usable_password())
guest_mails = mails_to("gast@example.org")
check(
    "Genau eine Einladungs-Mail (keine Mail-Flut)",
    len(guest_mails) == 1 and len(mail.outbox) == 1,
    str(len(mail.outbox)),
)
body_text = guest_mails[0].body
check(
    "Mail nennt Freigaben, Stufe, Nachricht und Passwort-Link",
    "Haushaltsentwurf 2027" in body_text
    and "Projekte" in body_text
    and "Kommentieren" in body_text
    and "Willkommen im Projekt" in body_text
    and "/accounts/" in body_text,
    body_text[:300],
)
guest_notes = Notification.objects.filter(recipient=m_guest, notification_type="motion_shared")
check("Zwei In-App-Hinweise (Dokument + Ordner) für den Gast", guest_notes.count() == 2, str(guest_notes.count()))
check("In-App-Hinweis ohne separate E-Mail", not any(n.email_sent for n in guest_notes))

# =============================================================================
print()
print("=== 2. Teilen-Dialog benachrichtigt ===")
mail.outbox.clear()
share_url = f"{BASE}/documents/{doc2.id}/share/update/"
resp = c_admin.post(
    share_url,
    {"visibility": "shared", "add_user_email": "gast@example.org", "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Freigabe -> 204", resp.status_code == 204, f"got {resp.status_code}")
n = (
    Notification.objects.filter(recipient=m_guest, metadata__contains={"motion_id": str(doc2.id)})
    if False
    else Notification.objects.filter(recipient=m_guest, message__contains="Radwegekonzept")
)
check(
    "Gast erhält In-App-Benachrichtigung zur Freigabe",
    n.count() == 1 and "Lesen" in n.first().message,
    str([x.message for x in n]),
)
check(
    "Gast erhält E-Mail zur Freigabe",
    len(mails_to("gast@example.org")) == 1 and "Radwegekonzept" in mails_to("gast@example.org")[0].body,
)

mail.outbox.clear()
resp = c_admin.post(
    share_url,
    {"visibility": "shared", "add_user_email": "gast@example.org", "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check(
    "Gleiche Stufe erneut: keine neue Benachrichtigung",
    Notification.objects.filter(recipient=m_guest, message__contains="Radwegekonzept").count() == 1
    and len(mail.outbox) == 0,
)
resp = c_admin.post(
    share_url,
    {"visibility": "shared", "add_user_email": "gast@example.org", "level": "edit"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
latest = (
    Notification.objects.filter(recipient=m_guest, message__contains="Radwegekonzept").order_by("-created_at").first()
)
check(
    "Stufenwechsel benachrichtigt erneut",
    Notification.objects.filter(recipient=m_guest, message__contains="Radwegekonzept").count() == 2
    and "Bearbeiten" in latest.message,
)

mail.outbox.clear()
resp = c_admin.post(
    share_url,
    {"visibility": "shared", "add_user_email": "member@example.org", "level": "comment"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check(
    "Mitglied wird ebenfalls benachrichtigt",
    Notification.objects.filter(recipient=m_member, message__contains="Radwegekonzept").exists()
    and len(mails_to("member@example.org")) == 1,
)

# =============================================================================
print()
print("=== 3. Ordner-Freigabe benachrichtigt ===")
folder2 = DocumentFolder.objects.create(organization=org, name="Sitzungsunterlagen", created_by=m_admin)
mail.outbox.clear()
resp = c_admin.post(
    f"{BASE}/documents/folders/{folder2.id}/share/",
    {"email": "gast@example.org", "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Ordner-Freigabe -> 200", resp.status_code == 200, f"got {resp.status_code}")
fn = Notification.objects.filter(recipient=m_guest, message__contains="Sitzungsunterlagen").first()
check("Ordner-Benachrichtigung mit Gast-Link", fn is not None and fn.link.endswith("/freigaben/"), str(fn and fn.link))
check(
    "Ordner-Mail an Gast",
    len(mails_to("gast@example.org")) == 1 and "Sitzungsunterlagen" in mails_to("gast@example.org")[0].body,
)
check("Ordner-Freigabe gespeichert", FolderGuestShare.objects.filter(folder=folder2, user=user_guest).exists())

# =============================================================================
print()
print("=== 4. Kommentare benachrichtigen Beteiligte ===")
user_guest.set_password("gast1234!")
user_guest.save()
c_guest = client_for(user_guest)
comment_url = reverse("work:document_comment", kwargs={"org_slug": org.slug, "motion_id": doc2.id})
mail.outbox.clear()
resp = c_guest.post(
    comment_url, {"content": "Bitte den Abschnitt 3 nochmal prüfen."}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
)
check("Gast kommentiert (Stufe edit) -> 200", resp.status_code == 200, f"got {resp.status_code} {resp.content[:120]}")
an = Notification.objects.filter(recipient=m_admin, notification_type="motion_comment").first()
check(
    "Autorin wird benachrichtigt (mit Auszug)",
    an is not None and "Abschnitt 3" in an.message and "Radwegekonzept" in an.message,
    str(an and an.message),
)
check("Autorin erhält E-Mail", len(mails_to("admin@example.org")) == 1)
check(
    "Gast erhält keine Benachrichtigung zum eigenen Kommentar",
    not Notification.objects.filter(recipient=m_guest, notification_type="motion_comment").exists(),
)

mail.outbox.clear()
resp = c_admin.post(comment_url, {"content": "Danke, ist eingearbeitet."}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
gn = Notification.objects.filter(recipient=m_guest, notification_type="motion_comment").first()
check("Antwort der Autorin benachrichtigt den Gast", gn is not None and "eingearbeitet" in gn.message)
check(
    "Gast erhält Antwort per E-Mail",
    len(mails_to("gast@example.org")) == 1 and "eingearbeitet" in mails_to("gast@example.org")[0].body,
)
check(
    "Autorin bekommt keine Benachrichtigung zum eigenen Kommentar",
    Notification.objects.filter(recipient=m_admin, notification_type="motion_comment").count() == 1,
)
check(
    "Mitglied ohne Beteiligung wird nicht benachrichtigt",
    not Notification.objects.filter(recipient=m_member, notification_type="motion_comment").exists(),
)

# =============================================================================
print()
print("=== 5. Zugang erneut senden ===")
user_guest.set_unusable_password()
user_guest.save()
resend_url = f"{BASE}/organization/members/{m_guest.id}/resend-access/"
mail.outbox.clear()
resp = c_admin.post(resend_url)
check(
    "Resend -> Redirect auf Detailseite",
    resp.status_code == 302 and str(m_guest.id) in resp["Location"],
    f"got {resp.status_code}",
)
rm = mails_to("gast@example.org")
check(
    "Neue Zugangs-Mail mit Passwort-Link + Freigabeliste",
    len(rm) == 1
    and "/accounts/" in rm[0].body
    and "Radwegekonzept" in rm[0].body
    and "Sitzungsunterlagen" in rm[0].body,
    rm[0].body[:300] if rm else "keine Mail",
)
check("Ohne guests.invite -> 403", c_member.post(resend_url).status_code == 403)
check(
    "Für Nicht-Gast -> 404",
    c_admin.post(f"{BASE}/organization/members/{m_member.id}/resend-access/").status_code == 404,
)
page = c_admin.get(f"{BASE}/organization/members/{m_guest.id}/").content.decode()
check("Button auf der Detailseite", "Zugang erneut senden" in page and "resend-access" in page)
check(
    "Button ohne Recht ausgeblendet",
    "Zugang erneut senden" not in c_member.get(f"{BASE}/organization/members/{m_guest.id}/").content.decode(),
)

# =============================================================================
print()
print("=== 6. E-Mail-Präferenz wird respektiert ===")
prefs, _ = NotificationPreference.objects.get_or_create(membership=m_guest)
prefs.email_enabled = False
prefs.save()
doc3 = Motion.objects.create(organization=org, author=m_admin, title="Kulturförderung", visibility="private")
mail.outbox.clear()
c_admin.post(
    f"{BASE}/documents/{doc3.id}/share/update/",
    {"visibility": "shared", "add_user_email": "gast@example.org", "level": "view"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check(
    "In-App ja, E-Mail nein bei deaktivierter Präferenz",
    Notification.objects.filter(recipient=m_guest, message__contains="Kulturförderung").exists()
    and len(mails_to("gast@example.org")) == 0,
)
check("Freigabe dennoch wirksam", MotionShare.objects.filter(motion=doc3, user=user_guest).exists())

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
