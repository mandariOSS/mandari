# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Antragsdatenbank mit Tracking (Etappe 1).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_motions_tracking.py

Prüft:
- Status-Pipeline (erlaubte/verbotene Übergänge, zentrale Matrix)
- Zuständigkeiten (responsible/contributors, Filter, Benachrichtigung)
- Themenkatalog inkl. Datenmigration aus Motion.tags
- Kompetenz-Kachel (Fachgebiete x Dokument-Themen)
- Fristen (überfällig-Badge, Erinnerungs-Command)
- Checkliste (Default aus Typ, Haken, Fortschritt)
- Freigaben-Rundlauf (anfragen, entscheiden, Benachrichtigungen)
- Listen-Rendering (Tracker, Chips, Progress, eingerückte Änderungsanträge)
- Organisations-Grenzen
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import timedelta
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
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role, Topic  # noqa: E402
from apps.work.motions.models import Motion, MotionApproval, MotionType  # noqa: E402
from apps.work.notifications.models import Notification, NotificationType  # noqa: E402
from apps.work.tasks.models import Task  # noqa: E402

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
    # Signal legt Standard-Rollen an; Admin-Rolle wiederverwenden
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


print("=== Setup ===")
org_a, role_a = make_org("Fraktion A", "fraktion-a")
org_b, role_b = make_org("Fraktion B", "fraktion-b")

user_author, m_author = make_member(org_a, role_a, "autor@example.org")
user_expert, m_expert = make_member(org_a, role_a, "experte@example.org")
user_approver, m_approver = make_member(org_a, role_a, "freigabe@example.org")
user_foreign, m_foreign = make_member(org_b, role_b, "fremd@example.org")

c_author = client_for(user_author)
c_expert = client_for(user_expert)
c_approver = client_for(user_approver)
c_foreign = client_for(user_foreign)

BASE = f"/work/{org_a.slug}/documents"

doc_type = MotionType.objects.create(
    organization=org_a,
    name="Antrag",
    slug="antrag",
    default_checklist=["Recherche abgeschlossen", "Mit Fraktion abgestimmt"],
)

# --- 1. Dokument anlegen: Defaults (responsible=Autor, Checkliste aus Typ) ---
print("=== 1. Anlegen: Defaults ===")
resp = c_author.post(
    f"{BASE}/create/",
    {"title": "Radweg Hauptstraße", "summary": "Neuer Radweg", "document_type": str(doc_type.id)},
)
check("Create redirect", resp.status_code == 302, f"got {resp.status_code}")
motion = Motion.objects.get(organization=org_a, title="Radweg Hauptstraße")
check("responsible = Autor", motion.responsible_id == m_author.id)
check(
    "Default-Checkliste aus Typ angelegt",
    list(motion.checklist_items.values_list("title", flat=True))
    == ["Recherche abgeschlossen", "Mit Fraktion abgestimmt"],
)
check("checklist_progress 0/2", motion.checklist_progress == {"done": 0, "total": 2, "percent": 0})

# --- 2. Status-Pipeline -------------------------------------------------------
print("=== 2. Status-Pipeline ===")
status_url = f"{BASE}/{motion.id}/status/"

resp = ajax_post(c_author, status_url, {"status": "approved"})
check("draft -> approved verboten", resp.status_code == 400, f"got {resp.status_code}")

resp = ajax_post(c_author, status_url, {"status": "quatsch"})
check("Unbekannter Status abgelehnt", resp.status_code == 400)

pipeline = ["internal_review", "external_review", "approved", "submitted", "at_admin", "on_agenda", "completed"]
ok = True
for target in pipeline:
    resp = ajax_post(c_author, status_url, {"status": target})
    if resp.status_code != 200:
        ok = False
        print(f"       Übergang -> {target}: {resp.status_code} {resp.content[:120]}")
        break
check("Volle Pipeline durchlaufen (inkl. external_review)", ok)
motion.refresh_from_db()
check("Status = completed", motion.status == "completed")

resp = ajax_post(c_author, status_url, {"status": "on_agenda"})
check("Rücksprung completed -> on_agenda erlaubt", resp.status_code == 200)
resp = ajax_post(c_author, status_url, {"status": "draft"})
check("on_agenda -> draft verboten", resp.status_code == 400)

motion.refresh_from_db()
check(
    "allowed_next_statuses zentral aus Matrix",
    [v for v, _ in motion.allowed_next_statuses()] == Motion.VALID_TRANSITIONS["on_agenda"],
)

# Skip-Variante: internal_review -> approved (external optional)
motion2 = Motion.objects.create(organization=org_a, author=m_author, title="Zweiter Antrag", responsible=m_author)
url2 = f"{BASE}/{motion2.id}/status/"
ajax_post(c_author, url2, {"status": "internal_review"})
resp = ajax_post(c_author, url2, {"status": "approved"})
check("external_review überspringbar", resp.status_code == 200)

# --- 3. Zuständigkeiten -------------------------------------------------------
print("=== 3. Zuständigkeiten ===")
meta_url = f"{BASE}/{motion.id}/meta/"

resp = ajax_post(c_author, meta_url, {"action": "set_responsible", "responsible": str(m_expert.id)})
motion.refresh_from_db()
check("Federführung gesetzt", resp.status_code == 200 and motion.responsible_id == m_expert.id)
check(
    "Benachrichtigung an neue Federführung",
    Notification.objects.filter(
        recipient=m_expert, notification_type=NotificationType.MOTION_ASSIGNED, metadata__motion_id=str(motion.id)
    ).exists(),
)

resp = ajax_post(c_author, meta_url, {"action": "set_contributors", "contributors": [str(m_approver.id)]})
check("Mitarbeit gesetzt", resp.status_code == 200 and list(motion.contributors.all()) == [m_approver])

# Cross-Org: fremde Membership als Federführung -> 404
resp = ajax_post(c_author, meta_url, {"action": "set_responsible", "responsible": str(m_foreign.id)})
check("Fremde Membership als responsible abgelehnt", resp.status_code == 404, f"got {resp.status_code}")

# Filter verantwortlich=me
resp = c_expert.get(f"/work/{org_a.slug}/documents/?verantwortlich=me")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check("Filter verantwortlich=me (Treffer)", "Radweg Hauptstraße" in titles)
resp = c_author.get(f"/work/{org_a.slug}/documents/?verantwortlich=me")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check("Filter verantwortlich=me (kein Treffer für Autor)", "Radweg Hauptstraße" not in titles)
resp = c_author.get(f"/work/{org_a.slug}/documents/?mine=1")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check("Bestehender mine-Filter funktioniert weiter", "Radweg Hauptstraße" in titles)

# --- 4. Themen: Datenmigration aus tags --------------------------------------
print("=== 4. Themen / Datenmigration ===")
from importlib import import_module  # noqa: E402

from django.apps import apps as global_apps  # noqa: E402

mig = import_module("apps.work.migrations.0033_migrate_tags_to_topics")

tagged_a = Motion.objects.create(
    organization=org_a, author=m_author, title="Alt mit Tags", tags=["Verkehr", "Klima", "  ", "Verkehr"]
)
tagged_b = Motion.objects.create(organization=org_b, author=m_foreign, title="Fremd mit Tags", tags=["Verkehr"])

mig.migrate_tags_to_topics(global_apps, None)

topics_a = {t.name: t for t in Topic.objects.filter(organization=org_a)}
topics_b = {t.name: t for t in Topic.objects.filter(organization=org_b)}
check("Topics je Org erzeugt", set(topics_a) == {"Verkehr", "Klima"} and set(topics_b) == {"Verkehr"})
check("Topics org-getrennt", topics_a["Verkehr"].id != topics_b["Verkehr"].id)
check(
    "M2M-Zuordnung aus Tags",
    set(tagged_a.topics.values_list("name", flat=True)) == {"Verkehr", "Klima"},
)
check("Farben rotieren aus Palette", topics_a["Verkehr"].color != topics_a["Klima"].color)
tagged_a.refresh_from_db()
check("JSON-Feld tags bleibt erhalten", tagged_a.tags == ["Verkehr", "Klima", "  ", "Verkehr"])
check(
    "Migration idempotent (get_or_create)",
    mig.migrate_tags_to_topics(global_apps, None) is None and Topic.objects.filter(organization=org_a).count() == 2,
)

# Themen am Dokument über Endpoint setzen + Org-Grenze
topic_verkehr = topics_a["Verkehr"]
resp = ajax_post(c_author, meta_url, {"action": "set_topics", "topics": [str(topic_verkehr.id)]})
check("Thema zugeordnet", resp.status_code == 200 and list(motion.topics.all()) == [topic_verkehr])
resp = ajax_post(c_author, meta_url, {"action": "set_topics", "topics": [str(topics_b["Verkehr"].id)]})
check("Fremdes Topic wird ignoriert", resp.status_code == 200 and motion.topics.count() == 0)
ajax_post(c_author, meta_url, {"action": "set_topics", "topics": [str(topic_verkehr.id)]})

# --- 5. Kompetenz-Kachel ------------------------------------------------------
print("=== 5. Kompetenz im Thema ===")
m_expert.expertise_topics.add(topic_verkehr)
resp = c_author.get(f"{BASE}/{motion.id}/")
competent = list(resp.context["competent_members"])
check("Experte erscheint in Kompetenz-Kachel", m_expert in competent)
check("Nicht-Experte erscheint nicht", m_approver not in competent)
check("Kachel im HTML", "Kompetenz im Thema" in resp.content.decode())

# --- 6. Fristen ---------------------------------------------------------------
print("=== 6. Fristen ===")
yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
resp = ajax_post(c_author, meta_url, {"action": "set_due_date", "due_date": yesterday})
motion.refresh_from_db()
check("Frist gesetzt", resp.status_code == 200 and str(motion.due_date) == yesterday)
# Status on_agenda (nicht abgeschlossen) -> überfällig
check("is_overdue bei vergangener Frist", motion.is_overdue is True)
resp = c_author.get(f"/work/{org_a.slug}/documents/")
check("Überfällig-Badge in Liste", "überfällig" in resp.content.decode())
check("Stats-Kachel überfällig", resp.context["stats"]["overdue"] >= 1)
resp = ajax_post(c_author, meta_url, {"action": "set_due_date", "due_date": "kein-datum"})
check("Ungültiges Datum abgelehnt", resp.status_code == 400)

# Erinnerungs-Command: Dokument heute fällig
motion_due = Motion.objects.create(
    organization=org_a,
    author=m_author,
    title="Heute fällig",
    responsible=m_expert,
    due_date=timezone.localdate(),
)
call_command("send_task_due_reminders")
reminder_qs = Notification.objects.filter(
    recipient=m_expert, notification_type=NotificationType.MOTION_DUE_SOON, metadata__motion_id=str(motion_due.id)
)
check("Frist-Erinnerung an Federführung", reminder_qs.count() == 1)
call_command("send_task_due_reminders")
check("Erinnerung dedupliziert (1x pro Tag)", reminder_qs.count() == 1)

# --- 7. Checkliste ------------------------------------------------------------
print("=== 7. Checkliste ===")
checklist_url = f"{BASE}/{motion.id}/checklist/"
item1 = motion.checklist_items.first()
resp = ajax_post(c_author, checklist_url, {"action": "toggle", "item_id": str(item1.id)})
item1.refresh_from_db()
check("Haken setzen", resp.status_code == 200 and item1.is_completed and item1.completed_by_id == m_author.id)
check("Progress 1/2 = 50%", motion.checklist_progress == {"done": 1, "total": 2, "percent": 50})
check("Panel-HTML zurückgegeben", "checklist" in resp.json()["html"] or "Fortschritt" in resp.json()["html"])

resp = ajax_post(c_author, checklist_url, {"action": "add", "title": "Pressemitteilung"})
check("Punkt hinzufügen", resp.status_code == 200 and motion.checklist_items.count() == 3)
new_item = motion.checklist_items.order_by("position").last()
resp = ajax_post(c_author, checklist_url, {"action": "delete", "item_id": str(new_item.id)})
check("Punkt löschen", resp.status_code == 200 and motion.checklist_items.count() == 2)
resp = ajax_post(c_author, checklist_url, {"action": "add", "title": "   "})
check("Leerer Titel abgelehnt", resp.status_code == 400)

# Task-Verknüpfung: Quick-Link Prefill
resp = c_author.get(f"/work/{org_a.slug}/tasks/create/?related_motion={motion.id}")
check("Task-Formular Prefill zeigt Dokument", "Radweg Hauptstraße" in resp.content.decode())
resp = c_author.post(
    f"/work/{org_a.slug}/tasks/create/",
    {
        "title": "Rederecht klären",
        "description": "",
        "visibility": "organization",
        "priority": "medium",
        "status": "todo",
        "tags": "[]",
        "related_motion": str(motion.id),
    },
)
task = Task.objects.filter(organization=org_a, title="Rederecht klären").first()
check("Task mit related_motion erstellt", task is not None and task.related_motion_id == motion.id)
resp = c_author.get(f"{BASE}/{motion.id}/")
check("Verknüpfte Aufgabe im Editor-Kontext", task in list(resp.context["linked_tasks"]))

# --- 8. Freigaben (MotionApproval) --------------------------------------------
print("=== 8. Freigaben ===")
motion3 = Motion.objects.create(organization=org_a, author=m_author, title="Freigabe-Antrag", responsible=m_author)
url3 = f"{BASE}/{motion3.id}/status/"
ajax_post(c_author, url3, {"status": "internal_review"})

approval_req_url = f"{BASE}/{motion3.id}/approvals/request/"
resp = ajax_post(c_author, approval_req_url, {"approver": str(m_approver.id), "approval_type": "chair"})
check("Freigabe angefragt", resp.status_code == 200 and resp.json()["created"] is True)
approval = MotionApproval.objects.get(motion=motion3, approver=m_approver, approval_type="chair")
check(
    "Benachrichtigung an angefragte Person",
    Notification.objects.filter(
        recipient=m_approver,
        notification_type=NotificationType.MOTION_APPROVAL_REQUESTED,
        metadata__approval_id=str(approval.id),
    ).exists(),
)
resp = ajax_post(c_author, approval_req_url, {"approver": str(m_approver.id), "approval_type": "unbekannt"})
check("Ungültiger Genehmigungstyp abgelehnt", resp.status_code == 400)

# Angefragte Person sieht Entscheiden-Block
resp = c_approver.get(f"{BASE}/{motion3.id}/")
check("Entscheiden-Block sichtbar", "Deine Freigabe wurde angefragt" in resp.content.decode())

decide_url = f"{BASE}/{motion3.id}/approvals/{approval.id}/decide/"
resp = ajax_post(c_expert, decide_url, {"decision": "approve"})
check("Fremde Person darf nicht entscheiden", resp.status_code == 403)

resp = ajax_post(c_approver, decide_url, {"decision": "approve", "comment": "Passt so"})
approval.refresh_from_db()
check("Freigabe erteilt", resp.status_code == 200 and approval.approved is True and approval.comment == "Passt so")
check("decided_at gesetzt", approval.decided_at is not None)
check(
    "Autor über Entscheidung benachrichtigt",
    Notification.objects.filter(
        recipient=m_author,
        notification_type=NotificationType.MOTION_APPROVAL_DECIDED,
        metadata__approval_id=str(approval.id),
    ).exists(),
)
resp = ajax_post(c_approver, decide_url, {"decision": "reject"})
check("Doppelte Entscheidung abgelehnt", resp.status_code == 400)
check("approval_summary 1/1", motion3.approval_summary == {"approved": 1, "rejected": 0, "pending": 0, "total": 1})

# Kein Zwang: Status weiterhin manuell wechselbar
resp = ajax_post(c_author, url3, {"status": "draft"})
check("Status trotz Freigaben manuell wechselbar", resp.status_code == 200)

# --- 9. Liste: Tracker, Chips, Progress, Änderungsanträge ---------------------
print("=== 9. Tracking-Liste ===")
amendment = Motion.objects.create(
    organization=org_a,
    author=m_author,
    title="Änderung: Radweg beidseitig",
    parent_motion=motion,
    responsible=m_author,
)
resp = c_author.get(f"/work/{org_a.slug}/documents/")
html = resp.content.decode()
rows = resp.context["motion_rows"]
row_titles = [(row["motion"].title, row["is_amendment"]) for row in rows]
parent_idx = row_titles.index(("Radweg Hauptstraße", False))
check(
    "Änderungsantrag direkt unter Hauptantrag (eingerückt)",
    row_titles[parent_idx + 1] == ("Änderung: Radweg beidseitig", True),
)
check("Einrückungs-Icon im HTML", "corner-down-right" in html)
check("Tracker im HTML (Pipeline-Punkt mit Tooltip)", 'title="Interne Absprache"' in html)
check("Themen-Chip im HTML", ">Verkehr</span>" in html)
check("Progress-Spalte im HTML", "1/2" in html)
check("Stats in Beratung", resp.context["stats"]["in_consultation"] >= 1)
check(
    "Änderungsantrag nicht separat paginiert",
    "Änderung: Radweg beidseitig" not in [m.title for m in resp.context["motions"]],
)

# Rejected-Tracker rot
motion_rejected = Motion.objects.create(
    organization=org_a, author=m_author, title="Abgelehnter Antrag", status="rejected"
)
resp = c_author.get(f"/work/{org_a.slug}/documents/")
check("Abgelehnt-Badge (rot) im HTML", "Abgelehnt" in resp.content.decode())

# Themen-Filter
resp = c_author.get(f"/work/{org_a.slug}/documents/?thema={topic_verkehr.id}")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check("Themen-Filter greift", "Radweg Hauptstraße" in titles and "Freigabe-Antrag" not in titles)

# --- 10. Org-Grenzen -----------------------------------------------------------
print("=== 10. Organisations-Grenzen ===")
resp = c_foreign.get(f"{BASE}/{motion.id}/")
check("Fremder Nutzer: Editor verweigert", resp.status_code in (403, 404), f"got {resp.status_code}")
resp = ajax_post(c_foreign, meta_url, {"action": "set_due_date", "due_date": yesterday})
check("Fremder Nutzer: Meta-Endpoint verweigert", resp.status_code in (403, 404))
resp = ajax_post(c_foreign, f"{BASE}/{motion.id}/checklist/", {"action": "add", "title": "Hack"})
check("Fremder Nutzer: Checklist-Endpoint verweigert", resp.status_code in (403, 404))
resp = c_foreign.get(f"/work/{org_b.slug}/documents/{motion.id}/")
check("Dokument nicht über fremde Org erreichbar", resp.status_code == 404)
resp = c_foreign.get(f"/work/{org_b.slug}/documents/")
titles = [row["motion"].title for row in resp.context["motion_rows"]]
check("Fremde Liste zeigt keine Org-A-Dokumente", "Radweg Hauptstraße" not in titles)

# Profil: Fachgebiete speichern
resp = c_expert.post(
    f"/work/{org_a.slug}/profile/committees/",
    {"action": "save_expertise", "expertise_topics": [str(topics_a["Klima"].id), str(topics_b["Verkehr"].id)]},
)
check(
    "Profil: Fachgebiete gespeichert, fremde Topics ignoriert",
    resp.status_code == 302 and set(m_expert.expertise_topics.values_list("name", flat=True)) == {"Klima"},
)

# Admin: Fachgebiete in Mitgliederverwaltung setzen
resp = c_author.post(
    f"/work/{org_a.slug}/organization/members/{m_approver.id}/",
    {"action": "update_expertise", "expertise_topics": [str(topic_verkehr.id)]},
)
check(
    "Mitgliederverwaltung: Fachgebiete gesetzt",
    set(m_approver.expertise_topics.values_list("name", flat=True)) == {"Verkehr"},
)

# Topics-Verwaltung (Dokument-Einstellungen)
resp = c_author.post(f"/work/{org_a.slug}/organization/documents/topics/", {"name": "Bildung", "color": "green"})
check("Topic-CRUD: anlegen", Topic.objects.filter(organization=org_a, name="Bildung").exists())
new_topic = Topic.objects.get(organization=org_a, name="Bildung")
c_author.post(
    f"/work/{org_a.slug}/organization/documents/topics/{new_topic.id}/update/",
    {"name": "Bildung & Schule", "color": "purple", "sort_order": "5"},
)
new_topic.refresh_from_db()
check("Topic-CRUD: ändern", new_topic.name == "Bildung & Schule" and new_topic.color == "purple")
c_author.post(f"/work/{org_a.slug}/organization/documents/topics/{new_topic.id}/delete/")
check("Topic-CRUD: löschen", not Topic.objects.filter(id=new_topic.id).exists())
resp = c_foreign.post(
    f"/work/{org_b.slug}/organization/documents/topics/{topic_verkehr.id}/delete/",
)
check("Topic-CRUD: fremde Org kommt nicht an Topic", Topic.objects.filter(id=topic_verkehr.id).exists())

# Typ-Formular: default_checklist
resp = c_author.post(
    f"/work/{org_a.slug}/organization/documents/types/{doc_type.id}/",
    {
        "name": "Antrag",
        "slug": "antrag",
        "description": "",
        "icon": "file-text",
        "color": "blue",
        "requires_approval": "on",
        "is_submittable": "on",
        "default_checklist": "Punkt 1\n\nPunkt 2\n  Punkt 3  ",
    },
)
doc_type.refresh_from_db()
check("Typ: default_checklist aus Textarea", doc_type.default_checklist == ["Punkt 1", "Punkt 2", "Punkt 3"])

# --- Ergebnis ------------------------------------------------------------------
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
