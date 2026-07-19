# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: OParl-Tombstones (Issue #17, Lösch-Markierung statt Löschung).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_tombstones.py

Prüft:
- Datenmodell: deleted/deleted_at auf allen OParl-Entitäten, Default False
- Sync-Pfad: mark_deleted markiert statt löscht (setzt deleted_at + bumpt
  oparl_modified auf den Löschzeitpunkt), idempotent; Ingestor-Quelltext
  enthält keinen physischen Delete-Pfad mehr (statische Prüfung)
- Öffentliche Portale: Listen/Kalender/Sitemap/Stats blenden markierte
  Objekte aus; Detailseiten liefern 200 mit Rückzugs-Hinweis statt 404
- OParl-API: Objekt-Endpunkt liefert Tombstone (HTTP 200, exakt die
  Pflichtfelder id/type/created/modified/deleted); normale Listen ohne
  Tombstones; modified_since-Listen mit Tombstones; eingebettete Referenzen
  lassen gelöschte Objekte aus
- purge_deleted: Dry-Run löscht nichts; --yes entfernt markierte Objekte
  endgültig inkl. lokaler Dateikopie; unmarkierte Objekte bleiben bestehen
"""

import base64
import io
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
REPO_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_DIR / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_tombstones_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["OPARL_API_RATE_LIMIT"] = "100000"

import django  # noqa: E402

# Sync-Watchdog (insight_sync.apps.ready) nicht starten (SQLite-Lock)
sys.argv = ["manage.py", "smoke_tombstones"]

django.setup()

from datetime import UTC, datetime, timedelta  # noqa: E402

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
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
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


BASE = settings.OPARL_BASE_URL
RIS = "https://ris.example.org/oparl"
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def ts(hours):
    return T0 + timedelta(hours=hours)


# =============================================================================
# Testdaten
# =============================================================================
print("== Testdaten anlegen ==")

source = OParlSource.objects.create(name="RIS Musterstadt", url=f"{RIS}/system")
body = OParlBody.objects.create(
    external_id=f"{RIS}/body/1",
    source=source,
    name="Stadt Musterstadt",
    slug="musterstadt",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)

org = OParlOrganization.objects.create(
    external_id=f"{RIS}/organization/1",
    body=body,
    name="Hauptausschuss",
    organization_type="gremium",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
person = OParlPerson.objects.create(
    external_id=f"{RIS}/person/1",
    body=body,
    family_name="Mustermann",
    given_name="Erika",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
membership = OParlMembership.objects.create(
    external_id=f"{RIS}/membership/1",
    person=person,
    organization=org,
    role="Mitglied",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
meeting_kept = OParlMeeting.objects.create(
    external_id=f"{RIS}/meeting/1",
    body=body,
    name="Sitzung bleibt",
    start=timezone.now() + timedelta(days=7),
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
meeting_kept.organizations.add(org)
meeting_gone = OParlMeeting.objects.create(
    external_id=f"{RIS}/meeting/2",
    body=body,
    name="Sitzung verschwindet",
    start=timezone.now() + timedelta(days=14),
    oparl_created=ts(0),
    oparl_modified=ts(2),
)
meeting_gone.organizations.add(org)
agenda_item = OParlAgendaItem.objects.create(
    external_id=f"{RIS}/agendaitem/1",
    meeting=meeting_kept,
    number="1",
    order=1,
    name="TOP 1",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
paper_kept = OParlPaper.objects.create(
    external_id=f"{RIS}/paper/1",
    body=body,
    name="Vorlage bleibt",
    reference="V/2024/001",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
paper_gone = OParlPaper.objects.create(
    external_id=f"{RIS}/paper/2",
    body=body,
    name="Vorlage verschwindet",
    reference="V/2024/002",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)
# Lokale Dateikopie für purge-Test
_local_file = Path(tempfile.mkdtemp(prefix="mandari_smoke_files_")) / "anlage.pdf"
_local_file.write_bytes(b"%PDF-1.4 smoke")
file_kept = OParlFile.objects.create(
    external_id=f"{RIS}/file/1",
    body=body,
    paper=paper_kept,
    name="Anlage bleibt",
    file_name="bleibt.pdf",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
file_gone = OParlFile.objects.create(
    external_id=f"{RIS}/file/2",
    body=body,
    paper=paper_kept,
    name="Anlage verschwindet",
    file_name="anlage.pdf",
    local_path=str(_local_file),
    oparl_created=ts(0),
    oparl_modified=ts(2),
)
consultation = OParlConsultation.objects.create(
    external_id=f"{RIS}/consultation/1",
    body=body,
    paper=paper_kept,
    paper_external_id=paper_kept.external_id,
    meeting_external_id=meeting_kept.external_id,
    agenda_item_external_id=agenda_item.external_id,
    role="Entscheidung",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
location = OParlLocation.objects.create(
    external_id=f"{RIS}/location/1",
    body=body,
    description="Rathaus",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
term = OParlLegislativeTerm.objects.create(
    external_id=f"{RIS}/term/1",
    body=body,
    name="WP 2020-2025",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)

client = Client()
session = client.session
session["active_body_id"] = str(body.id)
session.save()


def get_json(path, **kwargs):
    resp = client.get(path, kwargs or None)
    try:
        return resp, json.loads(resp.content)
    except json.JSONDecodeError:
        return resp, None


# =============================================================================
# 1. Datenmodell
# =============================================================================
print("== Datenmodell ==")
ENTITY_MODELS = [
    OParlBody,
    OParlOrganization,
    OParlPerson,
    OParlMembership,
    OParlMeeting,
    OParlAgendaItem,
    OParlPaper,
    OParlFile,
    OParlConsultation,
    OParlLocation,
    OParlLegislativeTerm,
]
for model in ENTITY_MODELS:
    fields = {f.name for f in model._meta.get_fields()}
    check(f"{model.__name__}: deleted + deleted_at vorhanden", {"deleted", "deleted_at"} <= fields)
check("Neue Objekte: deleted=False als Default", paper_gone.deleted is False and paper_gone.deleted_at is None)

# =============================================================================
# 2. Sync-Pfad: markieren statt löschen
# =============================================================================
print("== Sync-Pfad (Markierung statt Löschung) ==")
before_count = OParlPaper.objects.count()
paper_gone.mark_deleted()
paper_gone.refresh_from_db()
check("mark_deleted: Objekt existiert weiter (kein DELETE)", OParlPaper.objects.count() == before_count)
check("mark_deleted: deleted=True + deleted_at gesetzt", paper_gone.deleted and paper_gone.deleted_at is not None)
check(
    "mark_deleted: oparl_modified = Löschzeitpunkt (für modified_since)",
    paper_gone.oparl_modified == paper_gone.deleted_at,
)
_first_deleted_at = paper_gone.deleted_at
paper_gone.mark_deleted()
paper_gone.refresh_from_db()
check("mark_deleted: idempotent (Zeitstempel unverändert)", paper_gone.deleted_at == _first_deleted_at)

# Weitere Entitäten markieren (für Portal-/API-/purge-Checks)
meeting_gone.mark_deleted()
file_gone.mark_deleted()

# Statische Prüfung: Ingestor markiert statt zu löschen
orchestrator_src = (REPO_DIR / "ingestor" / "src" / "sync" / "orchestrator.py").read_text(encoding="utf-8")
database_src = (REPO_DIR / "ingestor" / "src" / "storage" / "database.py").read_text(encoding="utf-8")
check("Ingestor: orchestrator nutzt mark-Pfad für deleted:true", "_mark_deleted" in orchestrator_src)
check("Ingestor: kein delete_entity-Aufruf mehr", "delete_entity" not in orchestrator_src)
check(
    "Ingestor: storage markiert (mark_entity_deleted) statt DELETE",
    "mark_entity_deleted" in database_src and "delete(model)" not in database_src,
)
check(
    "Ingestor: Full-Sync prüft deleted-Flag vor Upsert",
    orchestrator_src.count('item.get("deleted") is True') >= 2,
)

# =============================================================================
# 3. Öffentliche Portale
# =============================================================================
print("== Öffentliches Portal ==")
resp = client.get("/insight/vorgaenge/")
content = resp.content.decode("utf-8")
check("Paper-Liste: 200", resp.status_code == 200)
check("Paper-Liste: aktives Paper sichtbar", "Vorlage bleibt" in content)
check("Paper-Liste: markiertes Paper ausgeblendet", "Vorlage verschwindet" not in content)

# Meeting-Listen zeigen den Gremiennamen an — daher über die Detail-URLs prüfen
resp = client.get("/insight/termine/", {"period": "all"})
content = resp.content.decode("utf-8")
check("Meeting-Liste: markiertes Meeting ausgeblendet", str(meeting_gone.id) not in content)
check("Meeting-Liste: aktives Meeting sichtbar", str(meeting_kept.id) in content)

resp, events = get_json("/insight/termine/partials/calendar-events/")
event_ids = {e["id"] for e in (events or [])}
check(
    "Kalender-Events: markiertes Meeting fehlt",
    str(meeting_kept.id) in event_ids and str(meeting_gone.id) not in event_ids,
)

resp = client.get("/sitemap-insight-musterstadt.xml")
sitemap = resp.content.decode("utf-8")
check(
    "Sitemap: markierte Objekte fehlen",
    str(paper_kept.id) in sitemap and str(paper_gone.id) not in sitemap and str(meeting_gone.id) not in sitemap,
)

resp, stats = get_json("/api/stats/")
check("Stats-API: Papers zählen ohne Tombstones", stats and stats.get("papers") == 1, str(stats))
check("Stats-API: Meetings zählen ohne Tombstones", stats and stats.get("meetings") == 1, str(stats))

resp = client.get(f"/insight/vorgaenge/{paper_gone.id}/")
content = resp.content.decode("utf-8")
check("Detailseite (markiert): 200 statt 404", resp.status_code == 200)
check("Detailseite (markiert): Rückzugs-Hinweis sichtbar", "von der Quelle zurückgezogen" in content)

resp = client.get(f"/insight/vorgaenge/{paper_kept.id}/")
content = resp.content.decode("utf-8")
check(
    "Detailseite (aktiv): kein Hinweis, gelöschte Datei ausgeblendet?",
    resp.status_code == 200 and "von der Quelle zurückgezogen" not in content,
)

resp = client.get(f"/insight/termine/{meeting_gone.id}/")
check(
    "Meeting-Detail (markiert): 200 + Hinweis",
    resp.status_code == 200 and "von der Quelle zurückgezogen" in resp.content.decode("utf-8"),
)

# =============================================================================
# 4. OParl-API: Tombstones
# =============================================================================
print("== OParl-API ==")
resp, tomb = get_json(f"/oparl/v1/paper/{paper_gone.id}")
check("Objekt-Endpunkt: Tombstone mit HTTP 200", resp.status_code == 200)
check("Tombstone: deleted=true", tomb and tomb.get("deleted") is True, str(tomb))
check(
    "Tombstone: exakt die Pflichtfelder id/type/created/modified/deleted",
    tomb is not None and set(tomb.keys()) == {"id", "type", "created", "modified", "deleted"},
    str(tomb),
)
check(
    "Tombstone: id/type korrekt",
    tomb.get("id") == f"{BASE}/v1/paper/{paper_gone.id}" and tomb.get("type") == "https://schema.oparl.org/1.1/Paper",
)
check(
    "Tombstone: modified = Löschzeitpunkt",
    tomb.get("modified") == paper_gone.deleted_at.isoformat(),
    f"{tomb.get('modified')} != {paper_gone.deleted_at.isoformat()}",
)

resp, papers_list = get_json(f"/oparl/v1/body/{body.id}/papers")
ids = [p.get("id") for p in papers_list["data"]]
check(
    "Normale Liste: Tombstone ausgeblendet",
    papers_list["pagination"]["totalElements"] == 1 and f"{BASE}/v1/paper/{paper_gone.id}" not in ids,
    str(papers_list["pagination"]),
)

since = (paper_gone.deleted_at - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
resp, incr = get_json(f"/oparl/v1/body/{body.id}/papers", modified_since=since)
tombs = [p for p in incr["data"] if p.get("deleted") is True]
check(
    "modified_since-Liste: Tombstone enthalten",
    len(tombs) == 1 and tombs[0]["id"] == f"{BASE}/v1/paper/{paper_gone.id}",
    str(incr["data"])[:300],
)
check(
    "modified_since-Liste: Tombstone gekürzt (keine Namen/Referenzen)",
    "name" not in tombs[0] and "reference" not in tombs[0],
)

old_since = "2000-01-01T00:00:00Z"
resp, incr_all = get_json(f"/oparl/v1/body/{body.id}/papers", modified_since=old_since)
check(
    "modified_since-Liste: aktive Objekte weiterhin vollständig",
    incr_all["pagination"]["totalElements"] == 2 and any(p.get("name") == "Vorlage bleibt" for p in incr_all["data"]),
)

resp, until_list = get_json(
    f"/oparl/v1/body/{body.id}/papers", modified_until=(timezone.now() + timedelta(days=1)).isoformat()
)
check(
    "Nur modified_until: Tombstones bleiben ausgeblendet (Spec)",
    all(not p.get("deleted") for p in until_list["data"]),
)

# Eingebettete Referenzen: gelöschte Datei taucht im Paper nicht mehr auf
resp, paper_json = get_json(f"/oparl/v1/paper/{paper_kept.id}")
embedded_files = [paper_json.get("mainFile", {}).get("id")] + [f.get("id") for f in paper_json.get("auxiliaryFile", [])]
check(
    "Eingebettet: gelöschte Datei ausgelassen, aktive enthalten",
    f"{BASE}/v1/file/{file_gone.id}" not in embedded_files and f"{BASE}/v1/file/{file_kept.id}" in embedded_files,
    str(embedded_files),
)

resp, meetings_list = get_json(f"/oparl/v1/body/{body.id}/meetings")
check(
    "Meetings-Liste: Tombstone ausgeblendet",
    meetings_list["pagination"]["totalElements"] == 1,
    str(meetings_list["pagination"]),
)

resp, meeting_tomb = get_json(f"/oparl/v1/meeting/{meeting_gone.id}")
check(
    "Meeting-Tombstone am Objekt-Endpunkt",
    resp.status_code == 200 and meeting_tomb.get("deleted") is True,
)

# =============================================================================
# 5. purge_deleted
# =============================================================================
print("== purge_deleted ==")
out = io.StringIO()
call_command("purge_deleted", "--body", "musterstadt", stdout=out)
dry_output = out.getvalue()
check("Dry-Run: markierte Objekte gelistet", str(paper_gone.id) in dry_output and str(meeting_gone.id) in dry_output)
check("Dry-Run: nichts gelöscht", OParlPaper.objects.filter(pk=paper_gone.pk).exists())
check("Dry-Run: Hinweis auf --yes", "--yes" in dry_output)
check("Dry-Run: lokale Datei unangetastet", _local_file.is_file())

# --ids: unmarkierte Objekte werden übersprungen
out = io.StringIO()
call_command("purge_deleted", "--ids", str(paper_kept.id), stdout=out)
check(
    "--ids mit unmarkiertem Objekt: übersprungen mit Hinweis",
    "übersprungen" in out.getvalue() and OParlPaper.objects.filter(pk=paper_kept.pk).exists(),
)

out = io.StringIO()
call_command("purge_deleted", "--body", "musterstadt", "--older-than", "9999", stdout=out)
check("--older-than 9999: nichts zu löschen", "Nichts zu löschen" in out.getvalue())

out = io.StringIO()
call_command("purge_deleted", "--body", "musterstadt", "--yes", stdout=out)
purge_output = out.getvalue()
check(
    "--yes: markierte Objekte endgültig entfernt",
    not OParlPaper.objects.filter(pk=paper_gone.pk).exists()
    and not OParlMeeting.objects.filter(pk=meeting_gone.pk).exists()
    and not OParlFile.objects.filter(pk=file_gone.pk).exists(),
)
check(
    "--yes: unmarkierte Objekte bleiben",
    OParlPaper.objects.filter(pk=paper_kept.pk).exists()
    and OParlMeeting.objects.filter(pk=meeting_kept.pk).exists()
    and OParlFile.objects.filter(pk=file_kept.pk).exists()
    and OParlPerson.objects.filter(pk=person.pk).exists(),
)
check("--yes: lokale Dateikopie entfernt", not _local_file.exists(), purge_output[-300:])

resp, _ = get_json(f"/oparl/v1/paper/{paper_gone.id}")
check("Nach Purge: Objekt-Endpunkt 404", resp.status_code == 404)

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n{PASS} bestanden, {FAIL} fehlgeschlagen")
sys.exit(1 if FAIL else 0)
