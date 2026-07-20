# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Aufgaben Import/Export (Issue #7).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_tasks_import_export.py

Prüft:
- Export als CSV/JSON/XML (Download-Header, Inhalte, Format)
- Sichtbarkeit: fremde private Aufgaben werden NICHT exportiert
- Import CSV mit deutschem Spalten-Mapping, Status-/Prioritäts-Aliassen,
  Datum in TT.MM.JJJJ, Zuweisung per E-Mail, Label-Anlage
- Dry-Run (Vorschau) schreibt nichts
- Idempotenz: erneuter Import derselben Datei erzeugt keine Duplikate
  (ID -> Update, Titel -> Skip), auch formatübergreifend (JSON -> XML)
- Fehlerbericht: fehlerhafte Zeilen werden gemeldet, gültige importiert
- Trello-JSON-Import (cards/lists/labels/checklists, archivierte Karten
  werden übersprungen)
- Berechtigungen: Export erfordert tasks.view, Import tasks.create
"""

import base64
import json
import os
import secrets
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_taskio_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_tasks_import_export"]
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.tasks.models import Task, TaskLabel  # noqa: E402

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


def perm(code, name):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": name, "category": "tasks"})
    return obj


def make_member(org, email, perm_codes):
    user = User.objects.create_user(email=email, password="test1234!")
    ms = Membership.objects.create(user=user, organization=org)
    if perm_codes:
        role = Role.objects.create(organization=org, name=f"R-{email}", is_admin=False)
        role.permissions.add(*[perm(code, code) for code in perm_codes])
        ms.roles.add(role)
    c = Client()
    c.force_login(user)
    return user, ms, c


def upload(client, org, content, filename, dry_run=False, content_type="text/plain"):
    if isinstance(content, str):
        content = content.encode("utf-8")
    data = {"file": SimpleUploadedFile(filename, content, content_type=content_type)}
    if dry_run:
        data["dry_run"] = "1"
    return client.post(f"/work/{org.slug}/tasks/import-file/", data)


print("=== Setup ===")
org = Organization.objects.create(name="Org X", slug="org-x")
org2 = Organization.objects.create(name="Org Y", slug="org-y")
TenantEncryption(org).key
TenantEncryption(org2).key

creator_user, creator_ms, creator_c = make_member(
    org, "creator@example.org", ["tasks.view", "tasks.create", "tasks.edit"]
)
other_user, other_ms, other_c = make_member(org, "other@example.org", ["tasks.view"])
viewer_user, viewer_ms, viewer_c = make_member(org, "viewer@example.org", ["tasks.view"])
noperm_user, noperm_ms, noperm_c = make_member(org, "noperm@example.org", [])
org2_user, org2_ms, org2_c = make_member(org2, "boss@example.org", ["tasks.view", "tasks.create", "tasks.manage"])

label = TaskLabel.objects.create(organization=org, name="Wichtig", color="red")
t1 = Task.objects.create(
    organization=org,
    title="Antrag vorbereiten",
    description="Antrag für die nächste Sitzung",
    status="in_progress",
    priority="high",
    visibility="organization",
    created_by=creator_ms,
    assigned_to=other_ms,
)
t1.labels.add(label)
t1.checklist_items.create(title="Entwurf schreiben", is_completed=True, position=0)
t1.checklist_items.create(title="Korrektur lesen", is_completed=False, position=1)
t2 = Task.objects.create(
    organization=org,
    title="Geheime private Aufgabe",
    visibility="private",
    created_by=other_ms,
    assigned_to=other_ms,
)
t3 = Task.objects.create(
    organization=org,
    title="Eigene private Aufgabe",
    visibility="private",
    created_by=creator_ms,
    assigned_to=creator_ms,
)

# =============================================================================
print("\n=== 1. Export CSV ===")
# =============================================================================

resp = creator_c.get(f"/work/{org.slug}/tasks/export/?format=csv")
check("CSV-Export liefert HTTP 200", resp.status_code == 200, f"Status: {resp.status_code}")
check("CSV-Export als Download deklariert", "attachment" in resp.get("Content-Disposition", ""))
csv_text = resp.content.decode("utf-8-sig")
check("CSV enthält Kopfzeile mit Titel-Spalte", "Titel" in csv_text.splitlines()[0])
check("CSV nutzt Semikolon-Trennung", ";" in csv_text.splitlines()[0])
check("CSV enthält Org-Aufgabe", "Antrag vorbereiten" in csv_text)
check("CSV enthält eigene private Aufgabe", "Eigene private Aufgabe" in csv_text)
check("CSV enthält KEINE fremde private Aufgabe", "Geheime private Aufgabe" not in csv_text)
check("CSV enthält Zuweisungs-E-Mail", "other@example.org" in csv_text)

resp = creator_c.get(f"/work/{org.slug}/tasks/export/?format=docx")
check("Unbekanntes Format wird abgelehnt (400)", resp.status_code == 400)

# =============================================================================
print("\n=== 2. Export JSON + XML ===")
# =============================================================================

resp = creator_c.get(f"/work/{org.slug}/tasks/export/?format=json")
check("JSON-Export liefert HTTP 200", resp.status_code == 200)
export_data = json.loads(resp.content.decode("utf-8"))
check("JSON-Format-Kennung vorhanden", export_data.get("format") == "mandari-tasks")
titles = [t["title"] for t in export_data["tasks"]]
check("JSON enthält sichtbare Aufgaben", "Antrag vorbereiten" in titles and "Eigene private Aufgabe" in titles)
check("JSON enthält KEINE fremde private Aufgabe", "Geheime private Aufgabe" not in titles)
exported_t1 = next(t for t in export_data["tasks"] if t["title"] == "Antrag vorbereiten")
check("JSON: Labels verschachtelt", exported_t1["labels"] == [{"name": "Wichtig", "color": "red"}])
check("JSON: Checkliste verschachtelt", len(exported_t1["checklist"]) == 2)
check("JSON: Zuweisung als E-Mail", exported_t1["assigned_to"] == "other@example.org")

resp = creator_c.get(f"/work/{org.slug}/tasks/export/?format=xml")
check("XML-Export liefert HTTP 200", resp.status_code == 200)
xml_root = ET.fromstring(resp.content)
xml_titles = [el.findtext("title") for el in xml_root.findall("task")]
check("XML enthält sichtbare Aufgaben", "Antrag vorbereiten" in xml_titles)
check("XML enthält KEINE fremde private Aufgabe", "Geheime private Aufgabe" not in xml_titles)
xml_export_bytes = resp.content

# =============================================================================
print("\n=== 3. Import CSV (Spalten-Mapping, Dry-Run, Idempotenz) ===")
# =============================================================================

csv_import = (
    "Titel;Beschreibung;Status;Priorität;Fällig am;Zugewiesen an;Labels\r\n"
    "Plakate bestellen;Für den Wahlkampf;In Bearbeitung;Hoch;24.12.2026;other@example.org;Wahlkampf, Orga\r\n"
    "Flyer verteilen;;offen;niedrig;2026-08-01;;Wahlkampf\r\n"
)

count_before = Task.objects.filter(organization=org).count()
resp = upload(creator_c, org, csv_import, "aufgaben.csv", dry_run=True)
check("CSV-Dry-Run liefert HTTP 200", resp.status_code == 200, f"Status: {resp.status_code}")
report = resp.json()["report"]
check("Dry-Run meldet 2 neue Aufgaben", report["created"] == 2, str(report))
check("Dry-Run schreibt nichts", Task.objects.filter(organization=org).count() == count_before)

resp = upload(creator_c, org, csv_import, "aufgaben.csv")
report = resp.json()["report"]
check("CSV-Import legt 2 Aufgaben an", report["created"] == 2, str(report))

imported = Task.objects.filter(organization=org, title="Plakate bestellen").first()
check("Importierte Aufgabe existiert", imported is not None)
if imported:
    check("Status-Alias 'In Bearbeitung' gemappt", imported.status == "in_progress", imported.status)
    check("Prioritäts-Alias 'Hoch' gemappt", imported.priority == "high", imported.priority)
    check("Deutsches Datum geparst", str(imported.due_date) == "2026-12-24", str(imported.due_date))
    check("Zuweisung per E-Mail aufgelöst", imported.assigned_to == other_ms)
    check(
        "Labels angelegt und verknüpft",
        sorted(imported.labels.values_list("name", flat=True)) == ["Orga", "Wahlkampf"],
    )
    check("Aktivität 'created' geloggt", imported.activities.filter(activity_type="created").exists())

count_after_first = Task.objects.filter(organization=org).count()
resp = upload(creator_c, org, csv_import, "aufgaben.csv")
report = resp.json()["report"]
check("Erneuter Import: 0 neu, 2 übersprungen", report["created"] == 0 and report["skipped"] == 2, str(report))
check("Erneuter Import erzeugt keine Duplikate", Task.objects.filter(organization=org).count() == count_after_first)

# =============================================================================
print("\n=== 4. Fehlerbericht ===")
# =============================================================================

bad_csv = "Titel;Status\r\n;todo\r\nKaputter Status;quantensprung\r\nGültige Zeile;done\r\n"
resp = upload(creator_c, org, bad_csv, "kaputt.csv")
check("Import mit Fehlern liefert HTTP 200", resp.status_code == 200)
report = resp.json()["report"]
check("2 fehlerhafte Zeilen gemeldet", report["failed"] == 2, str(report))
check("Fehlermeldungen mit Zeilennummer", any("Zeile" in e for e in report["errors"]), str(report["errors"]))
check("Gültige Zeile trotzdem importiert", report["created"] == 1, str(report))
done_task = Task.objects.filter(organization=org, title="Gültige Zeile").first()
check("Status 'done' setzt is_completed", done_task is not None and done_task.is_completed)

resp = upload(creator_c, org, "hello world", "notizen.txt")
check("Datei ohne Titel-Spalte wird abgelehnt (400)", resp.status_code == 400)

# =============================================================================
print("\n=== 5. Idempotenter Re-Import per ID (JSON) und Roundtrip ===")
# =============================================================================

resp = creator_c.get(f"/work/{org.slug}/tasks/export/?format=json")
json_export = resp.content.decode("utf-8")
count_before = Task.objects.filter(organization=org).count()
resp = upload(creator_c, org, json_export, "aufgaben.json")
report = resp.json()["report"]
check(
    "JSON-Re-Import in dieselbe Org: nur Updates, nichts Neues",
    report["created"] == 0 and report["failed"] == 0 and report["updated"] > 0,
    str(report),
)
check("Re-Import erzeugt keine Duplikate", Task.objects.filter(organization=org).count() == count_before)

# Migration in andere Organisation: IDs unbekannt -> Neuanlage dort
exported_count = len(json.loads(json_export)["tasks"])
resp = upload(org2_c, org2, json_export, "aufgaben.json")
report = resp.json()["report"]
check(
    f"JSON-Import in Org Y legt {exported_count} Aufgaben an",
    report["created"] == exported_count,
    str(report),
)
migrated = Task.objects.filter(organization=org2, title="Antrag vorbereiten").first()
check("Org-Y-Aufgabe mit Checkliste übernommen", migrated is not None and migrated.checklist_items.count() == 2)
check(
    "Unbekannte Zuweisungs-E-Mail bleibt beim Importeur",
    migrated is not None and migrated.assigned_to == org2_ms,
)

# Formatübergreifende Idempotenz: gleicher Datenstand als XML -> alles übersprungen
resp = upload(org2_c, org2, xml_export_bytes, "aufgaben.xml")
report = resp.json()["report"]
check(
    "XML-Import desselben Stands in Org Y: 0 neu",
    report["created"] == 0,
    str(report),
)

# =============================================================================
print("\n=== 6. Trello-JSON-Import ===")
# =============================================================================

trello = {
    "name": "Testboard",
    "lists": [
        {"id": "l1", "name": "To Do"},
        {"id": "l2", "name": "Done"},
    ],
    "labels": [{"id": "lb1", "name": "Presse"}],
    "cards": [
        {
            "id": "c1",
            "name": "Pressemitteilung schreiben",
            "desc": "Zur Haushaltsdebatte",
            "idList": "l1",
            "idLabels": ["lb1"],
            "due": "2026-09-15T12:00:00.000Z",
            "closed": False,
            "checklists": [
                {
                    "checkItems": [
                        {"name": "Zitat einholen", "state": "complete"},
                        {"name": "Versand", "state": "incomplete"},
                    ]
                }
            ],
        },
        {"id": "c2", "name": "Alte Karte", "idList": "l2", "closed": True},
        {"id": "c3", "name": "Archiv sichten", "idList": "l2", "closed": False},
    ],
}
resp = upload(creator_c, org, json.dumps(trello), "trello.json")
check("Trello-Import liefert HTTP 200", resp.status_code == 200, f"Status: {resp.status_code}")
report = resp.json()["report"]
check("Trello: 2 Karten importiert (archivierte übersprungen)", report["created"] == 2, str(report))
trello_task = Task.objects.filter(organization=org, title="Pressemitteilung schreiben").first()
check("Trello: Karte übernommen", trello_task is not None)
if trello_task:
    check("Trello: Liste 'To Do' -> Status todo", trello_task.status == "todo")
    check("Trello: Fälligkeit übernommen", str(trello_task.due_date) == "2026-09-15", str(trello_task.due_date))
    check("Trello: Label übernommen", list(trello_task.labels.values_list("name", flat=True)) == ["Presse"])
    check("Trello: Checkliste übernommen", trello_task.checklist_items.count() == 2)
done_trello = Task.objects.filter(organization=org, title="Archiv sichten").first()
check("Trello: Liste 'Done' -> erledigt", done_trello is not None and done_trello.status == "done")

# =============================================================================
print("\n=== 7. Berechtigungen ===")
# =============================================================================

resp = noperm_c.get(f"/work/{org.slug}/tasks/export/?format=csv")
check("Export ohne tasks.view verweigert", resp.status_code in (302, 403), f"Status: {resp.status_code}")

count_before = Task.objects.filter(organization=org).count()
resp = upload(viewer_c, org, csv_import, "aufgaben.csv")
check("Import ohne tasks.create verweigert", resp.status_code in (302, 403), f"Status: {resp.status_code}")
check("Import ohne Berechtigung schreibt nichts", Task.objects.filter(organization=org).count() == count_before)

resp = viewer_c.get(f"/work/{org.slug}/tasks/export/?format=json")
check("Export mit tasks.view erlaubt", resp.status_code == 200, f"Status: {resp.status_code}")
viewer_titles = [t["title"] for t in json.loads(resp.content.decode("utf-8"))["tasks"]]
check("Viewer sieht keine fremden privaten Aufgaben", "Eigene private Aufgabe" not in viewer_titles)

# Aufgabenboard rendert weiterhin (inkl. neuem Export-/Import-UI)
resp = creator_c.get(f"/work/{org.slug}/tasks/")
check("Aufgabenboard rendert mit HTTP 200", resp.status_code == 200, f"Status: {resp.status_code}")
board_html = resp.content.decode("utf-8")
check("Export-Menü im Board vorhanden", "tasks/export/?format=csv" in board_html)
check("Datei-Import-Dialog im Board vorhanden", "file-import-modal" in board_html)

# =============================================================================
print("\n" + "=" * 60)
print(f"Ergebnis: {PASS} OK, {FAIL} FAIL")
print("=" * 60)
sys.exit(1 if FAIL else 0)
