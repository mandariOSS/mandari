# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Echtzeit-Kollaboration (Yjs/Channels) + KI-Chat mit Admin-Modellen und Limits.

Läuft gegen eine frische SQLite-Instanz (Channels: InMemory-Layer):
    python scripts/smoke_collab_ai.py

Prüft:
- DocumentCollaborationConsumer: Verbinden mit Zugriff ok / ohne Zugriff abgelehnt (4403)
- yjs_save mit HTML → Motion.content_encrypted aktualisiert + Revision (gedrosselt, 10 Min)
- Disconnect des letzten Teilnehmers → Snapshot-Revision (inhaltsgleiche werden übersprungen)
- Revision-Restore → yjs_document geleert + reload-Broadcast an verbundene Clients
- AISettings (Admin-Singleton): verschlüsselter API-Key, Provider-Auflösung,
  OpenAI-kompatibler und Anthropic-HTTP-Pfad (Mock via unittest.mock auf httpx)
- Verbrauchsbuchung (OrganizationAITokenUsage) + Monatslimit-Durchsetzung
  (Org-Override, 0 = deaktiviert, leer = Default aus AISettings)
- Chat sendet Dokumentkontext mit (Payload-Assert)
- KI-Panel: Kontingentanzeige im HTML, "nicht konfiguriert"-Pfad ohne Key
- Editor-Seite 200, Collab-Init im Template statt Solo-Schalter, Bundle-Funktionen
"""

import asyncio
import base64
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Windows-Konsole (cp1252) verträgt keine Pfeile/Umlaute in den Checknamen
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_collab_ai_"))
_db_path = _tmp_dir / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
# Channels: InMemory-Layer statt Redis (REDIS_URL="" → InMemoryChannelLayer)
os.environ["REDIS_URL"] = ""
# Kein globaler Nebius-Fallback-Key aus der Umgebung
os.environ.pop("NEBIUS_API_KEY", None)

# Sync-Watchdog (insight_sync.apps) nicht starten (erkennt Management-Commands
# an sys.argv — entsprechend tarnen).
sys.argv = ["manage.py", "smoke_collab_ai"]

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.MEDIA_ROOT = str(_tmp_dir / "media")

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.models import AISettings  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.motions.consumers import DocumentCollaborationConsumer  # noqa: E402
from apps.work.motions.models import Motion, MotionRevision, OrganizationAITokenUsage  # noqa: E402
from apps.work.motions.services import MotionAIService  # noqa: E402
from channels.db import database_sync_to_async  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402

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
org = Organization.objects.create(name="Fraktion A", slug="fraktion-a")
role = Role.objects.filter(organization=org, is_admin=True).first()
if role is None:
    role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
user = User.objects.create_user(email="autor@example.org", password="test1234!")
membership = Membership.objects.create(user=user, organization=org)
membership.roles.add(role)

# Nutzer OHNE Mitgliedschaft (Zugriff muss abgelehnt werden)
outsider = User.objects.create_user(email="fremd@example.org", password="test1234!")

client = Client()
client.force_login(user)

DOCS = f"/work/{org.slug}/documents"

motion = Motion.objects.create(
    organization=org,
    author=membership,
    title="Kollaborations-Dokument",
    responsible=membership,
)
# Enthält einen manuellen Seitenumbruch (Etappe-3-Node) — Kollab-Init damit testen
INITIAL_CONTENT = '<p>Erster Absatz</p><div data-page-break="true" class="page-break"></div><p>Zweite Seite</p>'
motion.set_content_encrypted(INITIAL_CONTENT)
motion.save()


# =============================================================================
# 1. Editor-Seite: Kollaboration aktiv statt Solo-Schalter
# =============================================================================
print("=== 1. Editor: Collab-Init statt Solo-Schalter ===")
resp = client.get(f"{DOCS}/{motion.id}/")
check("Editor-Seite 200", resp.status_code == 200, f"got {resp.status_code}")
html = resp.content.decode()
check("Collab-Init im Template (_initCollabEditor)", "_initCollabEditor(" in html)
check("Kein Solo-Zwangsschalter mehr", "force solo mode" not in html)
check("createCollaborativeEditor wird genutzt", "createCollaborativeEditor" in html)
check("Solo-Fallback vorhanden (_switchToSoloMode)", "_switchToSoloMode(" in html)
check("Presence-Anzeige (collabUsers)", "collabUsers" in html)
check("WebSocket-URL /ws/documents/", "/ws/documents/" in html)
check("Seitenumbruch im Initial-Content", "data-page-break" in html)

bundle = (PROJECT_DIR / "static" / "js" / "editor.bundle.js").read_text(encoding="utf-8", errors="ignore")
check("Bundle: createCollaborativeEditor exportiert", "createCollaborativeEditor" in bundle)
check("Bundle: yjs_save sendet HTML mit (getHtml)", "getHtml" in bundle)
check("Bundle: reload-Handling (onReloadRequired)", "onReloadRequired" in bundle)
check("Bundle: PageBreak-Node registriert (pageBreak)", "pageBreak" in bundle)

# KI ist noch nicht konfiguriert → "nicht eingerichtet"-Pfad
check("KI-Panel: 'nicht eingerichtet' ohne Key", "noch nicht eingerichtet" in html)


# =============================================================================
# 2. Channels-Consumer: Zugriff, yjs_save, Revisionen, Restore-Broadcast
# =============================================================================
print("=== 2. Kollaborations-Consumer ===")


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
        "revisions": list(
            MotionRevision.objects.filter(motion=m).order_by("version").values_list("version", "change_summary")
        ),
    }


@database_sync_to_async
def _restore_revision(revision_version):
    rev = MotionRevision.objects.get(motion_id=motion.id, version=revision_version)
    response = client.post(f"{DOCS}/{motion.id}/revisions/{rev.id}/restore/")
    return response.status_code, response.json()


async def _wait_for(predicate_coro, timeout=5.0):
    """DB-Zustand pollen, bis Bedingung erfüllt oder Timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _get_motion_state()
        if predicate_coro(state):
            return state
    return await _get_motion_state()


async def run_collab_tests():
    results = []

    # 2a. Ohne Mitgliedschaft → abgelehnt
    comm_out = _make_communicator(outsider)
    connected, _ = await comm_out.connect(timeout=5)
    results.append(("Verbindung ohne Zugriff abgelehnt", connected is False, f"connected={connected}"))
    await comm_out.disconnect()

    # 2b. Autor verbindet sich → connected + yjs_state(null)
    comm = _make_communicator(user)
    connected, _ = await comm.connect(timeout=5)
    results.append(("Verbindung mit Zugriff ok", connected is True, f"connected={connected}"))

    msg1 = await comm.receive_json_from(timeout=5)
    results.append(
        ("connected-Message mit User-Info", msg1.get("type") == "connected" and "user" in msg1, str(msg1)[:120])
    )
    results.append(
        ("Access-Level edit für Autor", (msg1.get("user") or {}).get("access_level") == "edit", str(msg1)[:120])
    )
    msg2 = await comm.receive_json_from(timeout=5)
    results.append(
        (
            "yjs_state initial null (frisches Dokument)",
            msg2.get("type") == "yjs_state" and msg2.get("data") is None,
            str(msg2)[:120],
        )
    )

    # 2c. yjs_save mit HTML → Content aktualisiert + Revision angelegt
    yjs_bytes_1 = b"fake-yjs-state-v2"
    await comm.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(yjs_bytes_1).decode(),
            "html": "<p>Kollab Version 2</p>",
        }
    )
    state = await _wait_for(lambda s: s["content"] == "<p>Kollab Version 2</p>")
    results.append(
        (
            "yjs_save: content_encrypted aktualisiert",
            state["content"] == "<p>Kollab Version 2</p>",
            state["content"][:80],
        )
    )
    results.append(("yjs_save: yjs_document persistiert", state["yjs"] == yjs_bytes_1, str(state["yjs"])[:60]))
    results.append(
        (
            "yjs_save: Kollab-Revision angelegt",
            len(state["revisions"]) == 1 and state["revisions"][0][1] == "Automatische Sicherung (Kollaboration)",
            str(state["revisions"]),
        )
    )

    # 2d. Zweites yjs_save kurz danach → gedrosselt (keine neue Revision)
    await comm.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(b"fake-yjs-state-v3").decode(),
            "html": "<p>Kollab Version 3</p>",
        }
    )
    state = await _wait_for(lambda s: s["content"] == "<p>Kollab Version 3</p>")
    results.append(
        ("yjs_save 2: content aktualisiert", state["content"] == "<p>Kollab Version 3</p>", state["content"][:80])
    )
    results.append(("yjs_save 2: Revision gedrosselt (10 Min)", len(state["revisions"]) == 1, str(state["revisions"])))

    # 2e. Restore → yjs_document geleert + reload-Broadcast an Clients
    status, data = await _restore_revision(1)
    results.append(("Restore-Endpoint success", status == 200 and data.get("success") is True, f"{status} {data}"))

    reload_msg = None
    try:
        for _ in range(3):
            m = await comm.receive_json_from(timeout=5)
            if m.get("type") == "reload":
                reload_msg = m
                break
    except Exception as e:  # noqa: BLE001
        reload_msg = None
        results.append(("(Debug) reload-Empfang", False, str(e)))
    results.append(("Restore: reload-Broadcast empfangen", reload_msg is not None, str(reload_msg)))

    state = await _get_motion_state()
    results.append(("Restore: yjs_document geleert", state["yjs"] is None, str(state["yjs"])[:60]))
    results.append(
        ("Restore: Inhalt wiederhergestellt", state["content"] == "<p>Kollab Version 2</p>", state["content"][:80])
    )
    # Safety- + Restore-Revision zusätzlich zur Kollab-Revision
    results.append(("Restore: Safety+Restore-Revisionen", len(state["revisions"]) == 3, str(state["revisions"])))

    # 2f. Weitere Änderung, dann Disconnect des letzten Teilnehmers → Snapshot-Revision
    await comm.send_json_to(
        {
            "type": "yjs_save",
            "data": base64.b64encode(b"fake-yjs-state-v4").decode(),
            "html": "<p>Kollab Version 4</p>",
        }
    )
    await _wait_for(lambda s: s["content"] == "<p>Kollab Version 4</p>")
    await comm.disconnect()
    state = await _wait_for(lambda s: len(s["revisions"]) == 4)
    results.append(
        (
            "Disconnect: Snapshot-Revision vom letzten Teilnehmer",
            len(state["revisions"]) == 4 and state["revisions"][-1][1] == "Automatische Sicherung (Kollaboration)",
            str(state["revisions"]),
        )
    )

    return results


for name, cond, detail in asyncio.run(run_collab_tests()):
    check(name, cond, detail)


# =============================================================================
# 3. AISettings: Verschlüsselung, Provider-Auflösung, Limits, Verbrauch
# =============================================================================
print("=== 3. AISettings + KI-Chat ===")

service = MotionAIService(organization=org, user_id=user.id)
check("KI ohne Konfiguration nicht verfügbar", service.is_available() is False)

ai_settings = AISettings.get_settings()
ai_settings.enabled = True
ai_settings.provider = AISettings.PROVIDER_OPENAI
ai_settings.model_name = "gpt-4o-mini"
ai_settings.max_output_tokens = 512
ai_settings.default_org_monthly_token_limit = 100000
ai_settings.set_api_key("test-key-123")
ai_settings.save()

check("API-Key verschlüsselt gespeichert", ai_settings.api_key_encrypted is not None)
check("API-Key nicht im Klartext", b"test-key-123" not in bytes(ai_settings.api_key_encrypted))
check("API-Key entschlüsselbar", ai_settings.get_api_key() == "test-key-123")
check("Base-URL-Default OpenAI", ai_settings.get_effective_base_url() == "https://api.openai.com/v1/")

service = MotionAIService(organization=org, user_id=user.id)
check("KI mit AISettings verfügbar", service.is_available() is True)
quota = service.get_quota_status()
check("Quota: Default-Monatslimit aus AISettings", quota["limit"] == 100000 and quota["used"] == 0, str(quota))


def _mock_httpx_client(response_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json
    mock_resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.post.return_value = mock_resp
    return mock_client


# 3a. Chat über den View — sendet Dokumentkontext mit, bucht Tokens
openai_response = {
    "choices": [{"message": {"content": "Zusammenfassung: Es geht um Spielplätze."}}],
    "usage": {"total_tokens": 123},
}
mock_client = _mock_httpx_client(openai_response)
with patch("apps.work.motions.services.httpx.Client", return_value=mock_client):
    resp = client.post(
        f"/work/{org.slug}/documents/ai/",
        {
            "action": "chat",
            "text": "<p>Antrag über neue Spielplätze im Stadtteil</p>",
            "instruction": "Fasse den Antrag zusammen",
            "motion_type": "motion",
            "selected_text": "",
            "history": "[]",
        },
    )
check("Chat-Endpoint 200", resp.status_code == 200, f"got {resp.status_code}")
data = resp.json()
check(
    "Chat success + Inhalt", data.get("success") is True and "Spielplätze" in data.get("content", ""), str(data)[:200]
)
check("Chat: Tokens in Response", data.get("tokens_used") == 123, str(data.get("tokens_used")))
check(
    "Chat: Quota in Response (Restkontingent-Update)",
    (data.get("quota") or {}).get("used") == 123 and (data.get("quota") or {}).get("limit") == 100000,
    str(data.get("quota")),
)

call_args = mock_client.post.call_args
called_url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
payload = call_args.kwargs.get("json") or {}
headers = call_args.kwargs.get("headers") or {}
messages = payload.get("messages") or []
system_texts = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
check("OpenAI-Pfad: URL /chat/completions", called_url.endswith("/chat/completions"), called_url)
check("OpenAI-Pfad: Bearer-Auth", headers.get("Authorization") == "Bearer test-key-123", str(headers))
check("OpenAI-Pfad: Modell aus AISettings", payload.get("model") == "gpt-4o-mini", str(payload.get("model")))
check(
    "Chat sendet Dokumentkontext mit",
    "Dokumentkontext" in system_texts and "Spielplätze" in system_texts,
    system_texts[:200],
)
check(
    "max_output_tokens aus AISettings greift (Cap 512)",
    payload.get("max_tokens") == 512,
    str(payload.get("max_tokens")),
)

used = OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_MONTH)
check("Verbrauch gebucht (123 Tokens, Monat)", used == 123, str(used))
used_day = OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_DAY)
check("Verbrauch gebucht (Tag)", used_day == 123, str(used_day))

# 3b. Monatslimit (Org-Override) überschritten → klare Fehlermeldung
org.ai_token_limit_monthly = 10
org.save(update_fields=["ai_token_limit_monthly"])
mock_client = _mock_httpx_client(openai_response)
with patch("apps.work.motions.services.httpx.Client", return_value=mock_client):
    resp = client.post(
        f"/work/{org.slug}/documents/ai/",
        {"action": "chat", "text": "<p>x</p>", "instruction": "Hallo", "motion_type": "motion", "history": "[]"},
    )
check("Limit überschritten → Fehlerstatus", resp.status_code == 500, f"got {resp.status_code}")
err = resp.json().get("error", "")
check("Fehlermeldung 'KI-Kontingent aufgebraucht'", "KI-Kontingent aufgebraucht" in err, err)
check("Limit: kein HTTP-Call an Provider", mock_client.post.call_count == 0, str(mock_client.post.call_count))

# 3c. Limit 0 → KI deaktiviert (503 / nicht verfügbar)
org.ai_token_limit_monthly = 0
org.save(update_fields=["ai_token_limit_monthly"])
service = MotionAIService(organization=org, user_id=user.id)
check("Limit 0 → is_available False", service.is_available() is False)
resp = client.post(
    f"/work/{org.slug}/documents/ai/",
    {"action": "chat", "text": "<p>x</p>", "instruction": "Hallo", "motion_type": "motion", "history": "[]"},
)
check("Limit 0 → Endpoint 503", resp.status_code == 503, f"got {resp.status_code}")

# zurück auf Default (leer = AISettings-Standard)
org.ai_token_limit_monthly = None
org.save(update_fields=["ai_token_limit_monthly"])

# 3d. Anthropic-Pfad (Messages API)
ai_settings = AISettings.get_settings()
ai_settings.provider = AISettings.PROVIDER_ANTHROPIC
ai_settings.model_name = "claude-sonnet-4-5"
ai_settings.save()

anthropic_response = {
    "content": [{"type": "text", "text": "Antwort von Claude."}],
    "usage": {"input_tokens": 40, "output_tokens": 20},
}
mock_client = _mock_httpx_client(anthropic_response)
with patch("apps.work.motions.services.httpx.Client", return_value=mock_client):
    resp = client.post(
        f"/work/{org.slug}/documents/ai/",
        {
            "action": "chat",
            "text": "<p>Antrag über Radwege</p>",
            "instruction": "Prüfe den Antrag",
            "motion_type": "motion",
            "history": "[]",
        },
    )
check("Anthropic: Endpoint 200", resp.status_code == 200, f"got {resp.status_code}")
data = resp.json()
check("Anthropic: Antwort geparst", "Claude" in data.get("content", ""), str(data)[:160])
check("Anthropic: Tokens input+output", data.get("tokens_used") == 60, str(data.get("tokens_used")))

call_args = mock_client.post.call_args
called_url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
payload = call_args.kwargs.get("json") or {}
headers = call_args.kwargs.get("headers") or {}
check("Anthropic: URL /messages", called_url.endswith("/messages"), called_url)
check("Anthropic: x-api-key Header", headers.get("x-api-key") == "test-key-123", str(headers))
check("Anthropic: anthropic-version Header", headers.get("anthropic-version") == "2023-06-01", str(headers))
check(
    "Anthropic: system als Top-Level-Parameter",
    "Radwege" in str(payload.get("system", "")),
    str(payload.get("system"))[:160],
)
check(
    "Anthropic: keine system-Rollen in messages",
    all(m.get("role") != "system" for m in payload.get("messages", [])),
    str(payload.get("messages"))[:160],
)

used_after = OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_MONTH)
check("Verbrauch kumuliert (123+60)", used_after == 183, str(used_after))


# =============================================================================
# 4. KI-Panel im Editor: Kontingentanzeige
# =============================================================================
print("=== 4. KI-Panel: Kontingentanzeige ===")
resp = client.get(f"{DOCS}/{motion.id}/")
check("Editor-Seite 200 (KI konfiguriert)", resp.status_code == 200, f"got {resp.status_code}")
html = resp.content.decode()
check("KI-Panel aktiv (kein 'nicht eingerichtet')", "noch nicht eingerichtet" not in html)
check("Kontingentanzeige im Panel", "KI-Kontingent" in html)
check("aiQuotaLimit initialisiert (100000)", "aiQuotaLimit: 100000" in html)
check("aiQuotaUsed initialisiert (183)", "aiQuotaUsed: 183" in html)
check("Quota-Update aus AI-Response verdrahtet", "data.quota" in html)
check("Übernehmen-Button (Einfügen)", "applyAiContent(" in html)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
