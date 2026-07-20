# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Status-Sperre friert auch den Live-Kollaborationsmodus ein.

Läuft gegen eine frische SQLite-Instanz (Channels: InMemory-Layer):
    python scripts/smoke_motions_status_lock.py

Beweist:
- Motion.apply_status_lock/get_collab_access_level: gesperrte Status
  (z. B. submitted/approved) stufen Schreibrechte im WebSocket-Consumer auf
  'comment' herab — Ausnahme: motions.edit_all behält 'edit'.
- yjs_save persistiert in gesperrten Status NICHT (weder yjs_document noch
  content_encrypted), mit edit_all weiterhin schon.
- Normale Status (draft/internal_review): unverändert 'edit' + Persistenz.
- HTTP-Editor: Zugriffsstufen wie bisher (Autor 'admin' im Entwurf,
  'comment' im gesperrten Status, Speichern → 403); edit_all darf weiterhin
  speichern.
- Statuswechsel über die Sperrgrenze broadcastet das bestehende
  doc.reload-Event an verbundene Kollab-Clients.
"""

import asyncio
import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

# Windows-Konsole (cp1252) verträgt keine Pfeile/Umlaute in den Checknamen
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_statuslock_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
# Channels: InMemory-Layer statt Redis (REDIS_URL="" → InMemoryChannelLayer)
os.environ["REDIS_URL"] = ""

# Sync-Watchdog (insight_sync.apps) nicht starten
sys.argv = ["manage.py", "smoke_motions_status_lock"]

import django  # noqa: E402

django.setup()

from asgiref.sync import async_to_sync  # noqa: E402
from channels.db import database_sync_to_async  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.motions.consumers import DocumentCollaborationConsumer  # noqa: E402
from apps.work.motions.models import Motion, MotionShare  # noqa: E402

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


print("=== Setup ===")
org = Organization.objects.create(name="Fraktion SL", slug="fraktion-sl")
TenantEncryption(org).key


def perm(code, name, cat):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": name, "category": cat})
    return obj


p_view = perm("motions.view", "Anträge sehen", "motions")
p_edit = perm("motions.edit", "Anträge bearbeiten", "motions")
p_comment = perm("motions.comment", "Anträge kommentieren", "motions")
p_edit_all = perm("motions.edit_all", "Alle Anträge bearbeiten", "motions")

edit_role = Role.objects.create(organization=org, name="Bearbeiter", is_admin=False)
edit_role.permissions.add(p_view, p_edit, p_comment)

editall_role = Role.objects.create(organization=org, name="Vorstand", is_admin=False)
editall_role.permissions.add(p_view, p_edit, p_comment, p_edit_all)

author_user = User.objects.create_user(email="autor-sl@example.org", password="test1234!")
author_ms = Membership.objects.create(user=author_user, organization=org)
author_ms.roles.add(edit_role)

member_user = User.objects.create_user(email="mitglied-sl@example.org", password="test1234!")
member_ms = Membership.objects.create(user=member_user, organization=org)
member_ms.roles.add(edit_role)

editall_user = User.objects.create_user(email="vorstand-sl@example.org", password="test1234!")
editall_ms = Membership.objects.create(user=editall_user, organization=org)
editall_ms.roles.add(editall_role)

guest_user = User.objects.create_user(email="gast-sl@example.org", password="test1234!")
guest_ms = Membership.objects.create(user=guest_user, organization=org, is_guest=True)

motion = Motion.objects.create(
    organization=org,
    author=author_ms,
    title="Antrag mit Status-Sperre",
    visibility="organization",
    status="draft",
)
motion.set_content_encrypted("<p>Originalinhalt</p>")
motion.save()

# Gast: persönliche Freigabe mit Level edit
MotionShare.objects.create(motion=motion, scope="user", user=guest_user, level="edit", created_by=author_user)

DOCS = f"/work/{org.slug}/documents"


def set_status(status):
    Motion.objects.filter(id=motion.id).update(status=status)
    motion.refresh_from_db()


def ws_access(user):
    consumer = DocumentCollaborationConsumer()
    consumer.document_id = str(motion.id)
    consumer.user = user
    access, _ = async_to_sync(consumer._check_access)()
    return access


# =============================================================================
# 1. Zugriffsstufen: normaler Status vs. gesperrter Status
# =============================================================================
print("=== 1. WS-Zugriffsstufen (Status-Sperre) ===")

check("Modell: draft nicht gesperrt", motion.is_status_locked is False)
check("(c) draft: Mitglied (motions.edit) -> edit", ws_access(member_user) == "edit", ws_access(member_user))
check("draft: Autor -> edit", ws_access(author_user) == "edit", ws_access(author_user))
check("draft: edit_all -> edit", ws_access(editall_user) == "edit", ws_access(editall_user))
check("draft: Gast mit edit-Freigabe -> edit", ws_access(guest_user) == "edit", ws_access(guest_user))

set_status("submitted")
check("Modell: submitted gesperrt", motion.is_status_locked is True)
check(
    "(a) submitted: Mitglied (motions.edit) NICHT mehr edit",
    ws_access(member_user) == "comment",
    ws_access(member_user),
)
check("submitted: Autor -> comment", ws_access(author_user) == "comment", ws_access(author_user))
check("(b) submitted: edit_all -> weiterhin edit", ws_access(editall_user) == "edit", ws_access(editall_user))
check("submitted: Gast mit edit-Freigabe -> comment", ws_access(guest_user) == "comment", ws_access(guest_user))


# =============================================================================
# 2. yjs_save: Persistenz nur mit (verbliebenem) Schreibrecht
# =============================================================================
print("=== 2. yjs_save-Persistenz (Consumer end-to-end) ===")


def _make_communicator(as_user):
    communicator = WebsocketCommunicator(
        DocumentCollaborationConsumer.as_asgi(),
        f"/ws/documents/{motion.id}/",
    )
    communicator.scope["user"] = as_user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    return communicator


@database_sync_to_async
def _get_motion_state():
    m = Motion.objects.get(id=motion.id)
    return {
        "content": m.get_content_decrypted(),
        "yjs": bytes(m.yjs_document) if m.yjs_document else None,
    }


async def _save_via_ws(as_user, yjs_bytes, html):
    """Verbinden, Access-Level auslesen, yjs_save senden, sauber trennen.

    disconnect() wartet, bis der Consumer alle Nachrichten verarbeitet hat —
    der DB-Zustand danach ist also deterministisch.
    """
    comm = _make_communicator(as_user)
    connected, _ = await comm.connect(timeout=5)
    if not connected:
        return None
    msg = await comm.receive_json_from(timeout=5)
    access = (msg.get("user") or {}).get("access_level")
    await comm.receive_json_from(timeout=5)  # yjs_state
    await comm.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(yjs_bytes).decode(),
            "html": html,
        }
    )
    await comm.disconnect()
    return access


async def run_persistence_tests():
    results = []

    # (a) Gesperrter Status: Mitglied mit motions.edit -> comment, keine Persistenz
    access = await _save_via_ws(member_user, b"member-yjs", "<p>Manipuliert im gesperrten Status</p>")
    results.append(("(a) submitted: Verbindung ok, Level comment", access == "comment", str(access)))
    state = await _get_motion_state()
    results.append(("(a) submitted: yjs_document NICHT persistiert", state["yjs"] is None, str(state["yjs"])[:60]))
    results.append(
        ("(a) submitted: content unverändert", state["content"] == "<p>Originalinhalt</p>", state["content"][:80])
    )

    # (b) Gesperrter Status: edit_all -> edit, Persistenz weiterhin möglich
    access = await _save_via_ws(editall_user, b"editall-yjs", "<p>Korrektur durch edit_all</p>")
    results.append(("(b) submitted: edit_all Level edit", access == "edit", str(access)))
    state = await _get_motion_state()
    results.append(("(b) submitted: yjs_document persistiert", state["yjs"] == b"editall-yjs", str(state["yjs"])[:60]))
    results.append(
        (
            "(b) submitted: content aktualisiert",
            state["content"] == "<p>Korrektur durch edit_all</p>",
            state["content"][:80],
        )
    )

    return results


for name, cond, detail in asyncio.run(run_persistence_tests()):
    check(name, cond, detail)

# (c) Normaler Status: Mitglied persistiert unverändert
set_status("draft")
Motion.objects.filter(id=motion.id).update(yjs_document=None)
motion.refresh_from_db()


async def run_normal_status_test():
    results = []
    access = await _save_via_ws(member_user, b"member-yjs-draft", "<p>Normale Bearbeitung</p>")
    results.append(("(c) draft: Mitglied Level edit", access == "edit", str(access)))
    state = await _get_motion_state()
    results.append(("(c) draft: yjs_document persistiert", state["yjs"] == b"member-yjs-draft", str(state["yjs"])[:60]))
    results.append(
        ("(c) draft: content aktualisiert", state["content"] == "<p>Normale Bearbeitung</p>", state["content"][:80])
    )
    return results


for name, cond, detail in asyncio.run(run_normal_status_test()):
    check(name, cond, detail)


# =============================================================================
# 3. HTTP-Editor: Zugriffsstufen und Speichern wie bisher
# =============================================================================
print("=== 3. HTTP-Editor (Verhalten unverändert) ===")

author_client = Client()
author_client.force_login(author_user)
member_client = Client()
member_client.force_login(member_user)
editall_client = Client()
editall_client.force_login(editall_user)

set_status("draft")
resp = author_client.get(f"{DOCS}/{motion.id}/")
check("draft: Autor Editor 200", resp.status_code == 200, f"got {resp.status_code}")
check("draft: Autor access_level admin", resp.context["access_level"] == "admin", str(resp.context["access_level"]))
resp = member_client.get(f"{DOCS}/{motion.id}/")
check(
    "draft: Mitglied access_level comment (HTTP wie bisher)",
    resp.context["access_level"] == "comment",
    str(resp.context["access_level"]),
)
resp = editall_client.get(f"{DOCS}/{motion.id}/")
check("draft: edit_all access_level edit", resp.context["access_level"] == "edit", str(resp.context["access_level"]))

set_status("submitted")
resp = author_client.get(f"{DOCS}/{motion.id}/")
check(
    "submitted: Autor access_level comment (Sperre wie bisher)",
    resp.context["access_level"] == "comment",
    str(resp.context["access_level"]),
)
resp = editall_client.get(f"{DOCS}/{motion.id}/")
check(
    "submitted: edit_all access_level edit (Sonderrecht)",
    resp.context["access_level"] == "edit",
    str(resp.context["access_level"]),
)

resp = author_client.post(
    f"{DOCS}/{motion.id}/",
    {"action": "save", "title": "Antrag mit Status-Sperre", "content": "<p>Autor-Hack</p>"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("submitted: Autor speichern -> 403", resp.status_code == 403, f"got {resp.status_code}")
motion.refresh_from_db()
check(
    "submitted: Inhalt nicht überschrieben",
    motion.get_content_decrypted() == "<p>Normale Bearbeitung</p>",
    motion.get_content_decrypted()[:80],
)

resp = editall_client.post(
    f"{DOCS}/{motion.id}/",
    {"action": "save", "title": "Antrag mit Status-Sperre", "content": "<p>edit_all speichert</p>"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("submitted: edit_all speichern -> 200", resp.status_code == 200, f"got {resp.status_code}")
motion.refresh_from_db()
check(
    "submitted: edit_all Inhalt gespeichert",
    motion.get_content_decrypted() == "<p>edit_all speichert</p>",
    motion.get_content_decrypted()[:80],
)


# =============================================================================
# 4. Statuswechsel broadcastet reload an verbundene Kollab-Clients
# =============================================================================
print("=== 4. Statuswechsel -> doc.reload-Broadcast ===")

set_status("internal_review")  # bearbeitbar; Übergang zu approved (gesperrt) erlaubt


@database_sync_to_async
def _post_status(status):
    response = author_client.post(
        f"{DOCS}/{motion.id}/status/",
        {"status": status},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    return response.status_code, response.json()


async def run_broadcast_tests():
    results = []

    comm = _make_communicator(member_user)
    connected, _ = await comm.connect(timeout=5)
    results.append(("Kollab-Client verbunden (internal_review)", connected is True, str(connected)))
    msg = await comm.receive_json_from(timeout=5)
    results.append(
        (
            "internal_review: Mitglied Level edit",
            (msg.get("user") or {}).get("access_level") == "edit",
            str(msg)[:120],
        )
    )
    await comm.receive_json_from(timeout=5)  # yjs_state

    # (e) Wechsel in gesperrten Status -> reload-Event an verbundene Clients
    status, data = await _post_status("approved")
    results.append(("Statuswechsel approved: 200", status == 200 and data.get("success") is True, f"{status} {data}"))
    try:
        reload_msg = await comm.receive_json_from(timeout=5)
    except Exception as e:  # noqa: BLE001
        reload_msg = {"error": str(e)}
    results.append(("(e) reload-Broadcast empfangen", reload_msg.get("type") == "reload", str(reload_msg)))

    # Nach dem Reload verbindet der Client neu -> herabgestufte Stufe
    comm2 = _make_communicator(member_user)
    connected, _ = await comm2.connect(timeout=5)
    msg = await comm2.receive_json_from(timeout=5)
    results.append(
        (
            "(e) Neuverbindung nach Sperre: Level comment",
            (msg.get("user") or {}).get("access_level") == "comment",
            str(msg)[:120],
        )
    )
    await comm2.receive_json_from(timeout=5)  # yjs_state

    # Entsperren (approved -> draft): ebenfalls reload (Stufe wiedererlangt)
    status, data = await _post_status("draft")
    results.append(("Statuswechsel draft: 200", status == 200 and data.get("success") is True, f"{status} {data}"))
    try:
        reload_msg = await comm2.receive_json_from(timeout=5)
    except Exception as e:  # noqa: BLE001
        reload_msg = {"error": str(e)}
    results.append(("Entsperren: reload-Broadcast empfangen", reload_msg.get("type") == "reload", str(reload_msg)))

    # Wechsel ohne Sperrgrenze (draft -> internal_review): KEIN reload
    status, data = await _post_status("internal_review")
    results.append(
        ("Statuswechsel internal_review: 200", status == 200 and data.get("success") is True, f"{status} {data}")
    )
    nothing = await comm2.receive_nothing(timeout=1)
    results.append(("Kein reload ohne Sperrgrenzen-Wechsel", nothing is True, str(nothing)))

    await comm.disconnect()
    await comm2.disconnect()
    return results


for name, cond, detail in asyncio.run(run_broadcast_tests()):
    check(name, cond, detail)


# =============================================================================
# 5. Stale-Verbindung: Sperre greift auch ohne Client-Reload
# =============================================================================
print("=== 5. Sperre greift für bereits verbundene Clients (serverseitig) ===")


async def run_stale_connection_tests():
    results = []

    # Mitglied verbindet sich im bearbeitbaren Status (Level edit) ...
    comm = _make_communicator(member_user)
    connected, _ = await comm.connect(timeout=5)
    msg = await comm.receive_json_from(timeout=5)
    results.append(
        (
            "Stale: verbunden mit Level edit (internal_review)",
            connected is True and (msg.get("user") or {}).get("access_level") == "edit",
            str(msg)[:120],
        )
    )
    await comm.receive_json_from(timeout=5)  # yjs_state

    # ... dann wird das Dokument gesperrt, OHNE dass der Client neu lädt
    await database_sync_to_async(set_status)("submitted")
    await comm.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(b"stale-yjs").decode(),
            "html": "<p>Stale-Client schreibt weiter</p>",
        }
    )
    await comm.disconnect()
    state = await _get_motion_state()
    results.append(("Stale: yjs_document NICHT überschrieben", state["yjs"] != b"stale-yjs", str(state["yjs"])[:60]))
    results.append(
        (
            "Stale: content NICHT überschrieben",
            state["content"] == "<p>edit_all speichert</p>",
            state["content"][:80],
        )
    )

    # edit_all-Client mit stehender Verbindung darf dagegen weiter speichern
    comm2 = _make_communicator(editall_user)
    connected, _ = await comm2.connect(timeout=5)
    await comm2.receive_json_from(timeout=5)  # connected
    await comm2.receive_json_from(timeout=5)  # yjs_state
    await comm2.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(b"editall-stale-yjs").decode(),
            "html": "<p>edit_all trotz Sperre</p>",
        }
    )
    await comm2.disconnect()
    state = await _get_motion_state()
    results.append(
        ("Stale: edit_all persistiert weiterhin", state["yjs"] == b"editall-stale-yjs", str(state["yjs"])[:60])
    )
    results.append(
        (
            "Stale: edit_all content aktualisiert",
            state["content"] == "<p>edit_all trotz Sperre</p>",
            state["content"][:80],
        )
    )

    return results


for name, cond, detail in asyncio.run(run_stale_connection_tests()):
    check(name, cond, detail)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
