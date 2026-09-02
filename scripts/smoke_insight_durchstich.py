# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Insight-Durchstich — Session-Kommune im Bürgerportal (Issue #36).

Läuft gegen eine frische SQLite-Instanz mit echtem HTTP-Roundtrip
(LiveServerThread):
    python scripts/smoke_insight_durchstich.py

Ende-zu-Ende:
1. Session-Daten anlegen (Ö + NÖ, inkl. Beratungsfolge und Anlagen)
2. Veröffentlichungs-Schalter im Settings-UI aktivieren
   -> OParl-Quelle wird automatisch registriert (Provisioning-Hook)
3. Lokale Sync-Pipeline (manage.py sync_session_insight) konsumiert die
   Session-OParl-API (Issue #35) über HTTP als normale OParl-Quelle
4. Insight-Modelle enthalten die öffentlichen Daten; das Bürgerportal
   (Vorgangs-/Termin-Detailseiten) zeigt sie an
5. Inkrementeller Sync (modified_since) übernimmt Änderungen
6. Tombstones: Ö->NÖ-Wechsel in Session markiert die Insight-Spiegel
   als gelöscht (Portal blendet aus)
7. NÖ-BEWEIS: NÖ-Objekte und verschlüsselte Personendaten werden NIEMALS
   synchronisiert (kompletter Insight-Datenbestand wird geprüft)
"""

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_durchstich_"))
_db_path = _tmp_dir / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["OPARL_API_RATE_LIMIT"] = "100000"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
_dj_settings.MEDIA_ROOT = str(_tmp_dir / "media")

from datetime import timedelta  # noqa: E402

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.testcases import LiveServerThread, _StaticFilesHandler  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionAuditLog,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
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
# Phase 1: Session-Daten anlegen (Ö + NÖ)
# =============================================================================
print("=== Phase 1: Session-Daten anlegen ===")

tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt", short_name="Musterstadt")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin@musterstadt.example", password="pw-Smoke-Test-1!")
su = SessionUser.objects.create(user=admin_user, tenant=tenant)
su.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

council = SessionOrganization.objects.create(
    tenant=tenant, name="Rat der Stadt Musterstadt", organization_type="council"
)
committee = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")

person = SessionPerson.objects.create(
    tenant=tenant, given_name="Erika", family_name="Musterfrau", email="erika@musterstadt.example"
)
person.set_phone_encrypted("GEHEIM-TELEFON-999")
person.set_bank_iban_encrypted("DE99GEHEIMIBAN1111")
person.save()
SessionOrganizationMembership.objects.create(organization=council, person=person, role="chair")

now = timezone.now()
meeting_pub = SessionMeeting.objects.create(
    tenant=tenant, name="DURCHSTICH-RATSSITZUNG", organization=council, start=now + timedelta(days=10)
)
meeting_np = SessionMeeting.objects.create(
    tenant=tenant,
    name="GEHEIME-DURCHSTICH-SITZUNG",
    organization=council,
    start=now + timedelta(days=11),
    is_public=False,
)

paper = SessionPaper.objects.create(
    tenant=tenant, reference="V/2026/0500", name="DURCHSTICH-VORLAGE-RADWEG", status="approved", is_public=True
)
paper_np = SessionPaper.objects.create(
    tenant=tenant, reference="V/2026/0501", name="GEHEIME-DURCHSTICH-VORLAGE", is_public=False
)

top_pub = SessionAgendaItem.objects.create(
    meeting=meeting_pub,
    number="1",
    order=1,
    name="DURCHSTICH-TOP-RADWEG",
    is_public=True,
    paper=paper,
    resolution_text="Der Radweg wird gebaut.",
    vote_result="approved",
)
top_np = SessionAgendaItem.objects.create(
    meeting=meeting_pub, number="N1", order=2, name="GEHEIMER-DURCHSTICH-TOP", is_public=False
)

SessionFile.objects.create(
    tenant=tenant,
    name="radweg-plan.txt",
    file=SimpleUploadedFile("radweg-plan.txt", b"oeffentlicher radweg plan"),
    is_public=True,
    paper=paper,
    mime_type="text/plain",
)
SessionFile.objects.create(
    tenant=tenant,
    name="GEHEIME-DURCHSTICH-ANLAGE.txt",
    file=SimpleUploadedFile("geheim.txt", b"geheim"),
    is_public=False,
    paper=paper,
)

SessionConsultation.objects.create(paper=paper, organization=committee, role="preliminary", order=1)
SessionConsultation.objects.create(
    paper=paper,
    organization=council,
    role="decision",
    authoritative=True,
    order=2,
    meeting=meeting_pub,
    agenda_item=top_pub,
)

check("Session-Daten angelegt", SessionPaper.objects.count() == 2 and SessionMeeting.objects.count() == 2)

# =============================================================================
# Phase 2: Veröffentlichungs-Schalter (Auto-Registrierung der Quelle)
# =============================================================================
print()
print("=== Phase 2: Veröffentlichungs-Schalter ===")

base = f"/session/{tenant.slug}"
check("Vor Veröffentlichung: keine Quelle registriert", OParlSource.objects.count() == 0)

resp = admin.post(f"{base}/settings/insight-publish/", {"publish": "1"})
tenant.refresh_from_db()
check("Schalter aktiviert -> Redirect", resp.status_code == 302 and tenant.insight_publish)

source = OParlSource.objects.first()
check("OParl-Quelle automatisch registriert", source is not None and source.is_active)
check(
    "Quelle: sync_config verweist auf den Mandanten",
    source is not None
    and source.sync_config.get("session_tenant") == "musterstadt"
    and source.sync_config.get("source_type") == "oparl",
)
check(
    "Quelle: URL zeigt auf die Mandanten-OParl-API",
    source is not None and source.url.endswith("/session/musterstadt/api/oparl/"),
    source.url if source else "",
)
check(
    "Audit: Veröffentlichung protokolliert",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionTenant", action="publish").exists(),
)

# Schalter aus -> Quelle inaktiv; wieder an -> aktiv
admin.post(f"{base}/settings/insight-publish/", {"publish": "0"})
source.refresh_from_db()
check("Schalter aus -> Quelle deaktiviert", not source.is_active)
admin.post(f"{base}/settings/insight-publish/", {"publish": "1"})
source.refresh_from_db()
check("Schalter an -> Quelle wieder aktiv", source.is_active)

# Ohne manage_settings kein Zugriff auf den Schalter
viewer_role = SessionRole.objects.create(tenant=tenant, name="nur-lesen")
viewer_user = User.objects.create_user(email="viewer@musterstadt.example", password="pw-Smoke-Test-1!")
su_v = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_v.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)
resp = viewer.post(f"{base}/settings/insight-publish/", {"publish": "0"})
tenant.refresh_from_db()
check("Schalter ohne manage_settings -> 403, unverändert", resp.status_code == 403 and tenant.insight_publish)

# =============================================================================
# Phase 3: HTTP-Server starten + Quelle auf lokale URL registrieren
# =============================================================================
print()
print("=== Phase 3: Sync über HTTP (lokale Pipeline) ===")

server = LiveServerThread("127.0.0.1", _StaticFilesHandler, port=0)
server.daemon = True
server.start()
server.is_ready.wait(timeout=30)
if server.error:
    raise server.error
base_url = f"http://127.0.0.1:{server.port}"

# Quelle auf die lokale Server-URL umregistrieren (Command aus Issue #36)
call_command("session_insight_source", tenant="musterstadt", base_url=base_url)
local_source = OParlSource.objects.filter(url=f"{base_url}/session/musterstadt/api/oparl/").first()
OParlSource.objects.exclude(pk=local_source.pk).update(is_active=False)
check("Quelle für lokale Basis-URL registriert (Command)", local_source is not None and local_source.is_active)

# Voll-Sync über die lokale Pipeline
call_command("sync_session_insight", source_url=local_source.url, full=True)

body = OParlBody.objects.filter(source=local_source).first()
check("Insight: Body angelegt", body is not None and body.name == "Stadt Musterstadt")
check("Insight: Gremien gespiegelt", OParlOrganization.objects.filter(body=body).count() == 2)
check("Insight: Person gespiegelt", OParlPerson.objects.filter(body=body, family_name="Musterfrau").exists())
check("Insight: Membership gespiegelt", OParlMembership.objects.count() == 1)
check(
    "Insight: nur die Ö-Sitzung gespiegelt",
    OParlMeeting.objects.filter(body=body).count() == 1
    and OParlMeeting.objects.filter(name="DURCHSTICH-RATSSITZUNG").exists(),
)
check(
    "Insight: nur der Ö-TOP gespiegelt",
    OParlAgendaItem.objects.count() == 1 and OParlAgendaItem.objects.first().name == "DURCHSTICH-TOP-RADWEG",
)
check(
    "Insight: nur die Ö-Vorlage gespiegelt",
    OParlPaper.objects.filter(body=body).count() == 1 and OParlPaper.objects.filter(reference="V/2026/0500").exists(),
)
check(
    "Insight: nur die Ö-Anlage gespiegelt",
    OParlFile.objects.count() == 1 and OParlFile.objects.first().name == "radweg-plan.txt",
)
consultations = OParlConsultation.objects.order_by("oparl_created")
check("Insight: Beratungsfolge gespiegelt (2 Stationen)", consultations.count() == 2)
check(
    "Insight: authoritative-Station mit TOP-Referenz",
    consultations.filter(authoritative=True, agenda_item_external_id__isnull=False).exists(),
)
check("Insight: Quelle last_sync gesetzt", OParlSource.objects.get(pk=local_source.pk).last_sync is not None)

# =============================================================================
# Phase 4: NÖ-BEWEIS — nichts Nicht-Öffentliches im Insight-Bestand
# =============================================================================
print()
print("=== Phase 4: NÖ-Beweis im Insight-Datenbestand ===")

NON_PUBLIC_MARKERS = [
    "GEHEIME-DURCHSTICH-SITZUNG",
    "GEHEIMER-DURCHSTICH-TOP",
    "GEHEIME-DURCHSTICH-VORLAGE",
    "GEHEIME-DURCHSTICH-ANLAGE",
    "GEHEIM-TELEFON",
    "DE99GEHEIMIBAN",
]

dump_parts = []
for model in (
    OParlBody,
    OParlOrganization,
    OParlPerson,
    OParlMembership,
    OParlMeeting,
    OParlAgendaItem,
    OParlPaper,
    OParlFile,
    OParlConsultation,
    OParlLegislativeTerm,
):
    for obj in model.objects.all():
        raw = obj.raw_json if isinstance(obj.raw_json, dict) else {}
        dump_parts.append(json.dumps(raw, ensure_ascii=False, default=str))
        dump_parts.append(str(getattr(obj, "name", "")))
insight_dump = "\n".join(dump_parts)
leaks = [marker for marker in NON_PUBLIC_MARKERS if marker in insight_dump]
check("NÖ-BEWEIS: keine NÖ-/verschlüsselten Inhalte im Insight-Bestand", not leaks, ", ".join(leaks))
check("Gegenprobe: öffentliche Inhalte im Insight-Bestand", "DURCHSTICH-VORLAGE-RADWEG" in insight_dump)

# =============================================================================
# Phase 5: Bürgerportal zeigt die Daten
# =============================================================================
print()
print("=== Phase 5: Bürgerportal ===")

anon = Client()
insight_paper = OParlPaper.objects.get(reference="V/2026/0500")
resp = anon.get(reverse("insight_core:insight:paper_detail", kwargs={"pk": insight_paper.pk}))
check(
    "Portal: Vorgangs-Detailseite zeigt die Session-Vorlage",
    resp.status_code == 200 and b"DURCHSTICH-VORLAGE-RADWEG" in resp.content,
    f"status={resp.status_code}",
)

insight_meeting = OParlMeeting.objects.get(name="DURCHSTICH-RATSSITZUNG")
resp = anon.get(reverse("insight_core:insight:meeting_detail", kwargs={"pk": insight_meeting.pk}))
check(
    "Portal: Termin-Detailseite zeigt die Session-Sitzung (inkl. TOP)",
    resp.status_code == 200 and b"DURCHSTICH-TOP-RADWEG" in resp.content,
    f"status={resp.status_code}",
)

resp = anon.get(reverse("insight_core:insight:paper_list"))
check("Portal: Vorgangsliste erreichbar", resp.status_code == 200, f"status={resp.status_code}")

# =============================================================================
# Phase 6: Inkrementeller Sync (modified_since) + Tombstones
# =============================================================================
print()
print("=== Phase 6: Inkrementeller Sync + Tombstones ===")

# Änderung in Session: Vorlagen-Name + neue Vorlage
paper.name = "DURCHSTICH-VORLAGE-RADWEG-GEAENDERT"
paper.save()
paper_new = SessionPaper.objects.create(
    tenant=tenant, reference="V/2026/0502", name="NEUE-DURCHSTICH-VORLAGE", is_public=True
)

call_command("sync_session_insight", source_url=local_source.url)

insight_paper.refresh_from_db()
check("Inkrementell: Namensänderung übernommen", insight_paper.name == "DURCHSTICH-VORLAGE-RADWEG-GEAENDERT")
check("Inkrementell: neue Vorlage übernommen", OParlPaper.objects.filter(reference="V/2026/0502").exists())

# Ö->NÖ in Session -> Tombstone -> Insight-Spiegel als gelöscht markiert
meeting_pub.is_public = False
meeting_pub.save()
call_command("sync_session_insight", source_url=local_source.url)

insight_meeting.refresh_from_db()
check("Tombstone: Sitzung im Insight-Spiegel als gelöscht markiert", insight_meeting.deleted)
insight_top = OParlAgendaItem.objects.first()
check("Tombstone: TOP im Insight-Spiegel als gelöscht markiert", insight_top is not None and insight_top.deleted)

# Portal blendet als gelöscht markierte Sitzungen aus Listen aus
resp = anon.get(reverse("insight_core:insight:meeting_list"))
check(
    "Portal: entöffentlichte Sitzung nicht mehr in der Terminliste",
    resp.status_code == 200 and b"DURCHSTICH-RATSSITZUNG" not in resp.content,
    f"status={resp.status_code}",
)

# Wieder veröffentlichen -> Sitzung kehrt zurück
meeting_pub.is_public = True
meeting_pub.save()
call_command("sync_session_insight", source_url=local_source.url)
insight_meeting.refresh_from_db()
check("Re-Publikation: Sitzung im Insight-Spiegel wieder aktiv", not insight_meeting.deleted)

# NÖ-Daten auch nach allen Syncs nicht im Bestand
final_dump = "\n".join(
    json.dumps(obj.raw_json, ensure_ascii=False, default=str)
    for model in (OParlMeeting, OParlPaper, OParlAgendaItem, OParlFile, OParlPerson)
    for obj in model.objects.all()
)
leaks = [marker for marker in NON_PUBLIC_MARKERS if marker in final_dump]
check("NÖ-BEWEIS nach allen Sync-Zyklen weiterhin bestanden", not leaks, ", ".join(leaks))

server.terminate()

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
