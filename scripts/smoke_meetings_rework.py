# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Sitzungsvorbereitungs-Umbau Etappe 1 (work.meetings).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_meetings_rework.py

Prüft:
- Begriffe (Verweisen / Kenntnisnahme / Mit Änderungsantrag im Rendering)
- forms.py entfernt + keine Importe mehr
- Private-Notiz-Rundlauf über den korrekten Endpoint (PrivateNoteAPIView)
- Position mit reasoning + outcome (partielle, idempotente Saves)
- Übergreifende Positions-Anzeige (2 Gremien, selbe Vorlage)
- Migrations-Integrität AgendaItemNote -> PaperComment (Zähler, Sichtbarkeit,
  Flags, Autor, Datum, Decrypt-Gleichheit, Idempotenz, keine Doppelanzeige)
- Einheitlicher Thread: POST an TOP mit Vorlage erzeugt PaperComment
- Channels-Consumer (Zugriff + Broadcast bei Kommentar-POST)
- Abgeleitetes is_prepared (Auto-Save statt Button)
- Redebeitrag: HTML-Whitelist im Teleprompter + linked_document (403 ohne Zugriff)
- Vorlagen-Anhang mit share_across_committees über Gremien hinweg / Org-Grenze
- Summary zeigt alle Positionsarten
"""

import asyncio
import base64
import importlib
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
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
# Kein Redis im Smoke: InMemoryChannelLayer (Consumer-Test)
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

# Sync-Watchdog (insight_sync.apps.ready) nicht starten: er greift parallel
# auf die SQLite zu und sperrt sie während der Migration ("database is locked").
sys.argv = ["manage.py", "smoke_meetings_rework"]

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from channels.routing import URLRouter  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402
from django.apps import apps as django_apps  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.meetings.models import (  # noqa: E402
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    MeetingPreparation,
    PaperComment,
)
from apps.work.meetings.routing import websocket_urlpatterns  # noqa: E402
from apps.work.meetings.sanitize import sanitize_speech_html  # noqa: E402
from apps.work.motions.models import Motion  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
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
        print(f"  FAIL {name} {detail}")


def make_org(name, slug, body):
    org = Organization.objects.create(name=name, slug=slug, body=body)
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


print("=== Setup ===")
source = OParlSource.objects.create(name="Smoke-Quelle", url="https://oparl.example.org/system")
body = OParlBody.objects.create(
    source=source, external_id="https://oparl.example.org/body/1", name="Musterstadt"
)

org_a, role_a = make_org("Fraktion A", "fraktion-a", body)
org_b, role_b = make_org("Fraktion B", "fraktion-b", body)

# Tenant-Keys sofort erzeugen: TenantEncryption regeneriert (und überschreibt!)
# den Key, wenn eine Instanz mit leerem encryption_key verschlüsselt. Da dieses
# Skript Modul-Instanzen von org_a/org_b für direkte Model-Verschlüsselung
# nutzt, muss der Key VOR dem ersten Request existieren.
from apps.common.encryption import TenantEncryption  # noqa: E402

TenantEncryption(org_a).key
TenantEncryption(org_b).key

user_admin, m_admin = make_member(org_a, role_a, "admin@example.org")
user_member, m_member = make_member(org_a, role_a, "mitglied@example.org")
user_foreign, m_foreign = make_member(org_b, role_b, "fremd@example.org")

c_admin = client_for(user_admin)
c_member = client_for(user_member)
c_foreign = client_for(user_foreign)

# Zwei Gremien, zwei Sitzungen, EINE Vorlage (Beratungsfolge)
committee_1 = OParlOrganization.objects.create(
    external_id="https://oparl.example.org/organization/1",
    body=body,
    name="Hauptausschuss",
    organization_type="committee",
)
committee_2 = OParlOrganization.objects.create(
    external_id="https://oparl.example.org/organization/2",
    body=body,
    name="Rat",
    organization_type="committee",
)

now = timezone.now()
meeting_1 = OParlMeeting.objects.create(
    external_id="https://oparl.example.org/meeting/1",
    body=body,
    name="Hauptausschuss-Sitzung",
    start=now + timezone.timedelta(days=3),
)
meeting_1.organizations.add(committee_1)
meeting_2 = OParlMeeting.objects.create(
    external_id="https://oparl.example.org/meeting/2",
    body=body,
    name="Ratssitzung",
    start=now + timezone.timedelta(days=10),
)
meeting_2.organizations.add(committee_2)

paper = OParlPaper.objects.create(
    external_id="https://oparl.example.org/paper/1",
    body=body,
    name="Neubau Spielplatz",
    reference="V/2026/001",
)

item_m1_paper = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/m1-1",
    meeting=meeting_1,
    number="1",
    name="Beratung Spielplatz (Vorberatung)",
)
item_m1_plain = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/m1-2",
    meeting=meeting_1,
    number="2",
    name="Verschiedenes",
)
item_m2_paper = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/m2-1",
    meeting=meeting_2,
    number="1",
    name="Beratung Spielplatz (Entscheidung)",
)

OParlConsultation.objects.create(
    external_id="https://oparl.example.org/consultation/1",
    body=body,
    paper=paper,
    agenda_item_external_id=item_m1_paper.external_id,
    meeting_external_id=meeting_1.external_id,
    role="Vorberatung",
)
OParlConsultation.objects.create(
    external_id="https://oparl.example.org/consultation/2",
    body=body,
    paper=paper,
    agenda_item_external_id=item_m2_paper.external_id,
    meeting_external_id=meeting_2.external_id,
    role="Entscheidung",
    authoritative=True,
)

BASE_A = f"/work/{org_a.slug}/meetings"
BASE_B = f"/work/{org_b.slug}/meetings"


def api_post(client, url, payload):
    import json as _json

    return client.post(url, _json.dumps(payload), content_type="application/json")


# --- 1. Begriffe im Rendering -------------------------------------------------
print("=== 1. Begriffe (Verweisen / Kenntnisnahme / Mit Änderungsantrag) ===")
resp = c_admin.get(f"{BASE_A}/{meeting_1.id}/prepare/")
html = resp.content.decode("utf-8")
check("Prepare-Seite lädt", resp.status_code == 200, f"status={resp.status_code}")
check("'Verweisen' im Rendering", "Verweisen" in html)
check("'Kenntnisnahme' im Rendering", "Kenntnisnahme" in html)
check("'Mit Änderungsantrag' im Rendering", "Mit Änderungsantrag" in html)
check("'Überweisen' entfernt", "Überweisen" not in html)
check("'Zur Kenntnis' entfernt", "Zur Kenntnis" not in html)

# --- 2. forms.py entfernt ------------------------------------------------------
print("=== 2. forms.py entfernt ===")
forms_path = PROJECT_DIR / "apps" / "work" / "meetings" / "forms.py"
check("forms.py existiert nicht mehr", not forms_path.exists())
import subprocess  # noqa: E402

grep = subprocess.run(
    [sys.executable, "-c", "import apps.work.meetings.forms"],
    capture_output=True,
    cwd=str(PROJECT_DIR),
)
check("meetings.forms nicht importierbar", grep.returncode != 0)

# --- 3. Private Notiz: Rundlauf über korrekten Endpoint ------------------------
print("=== 3. Private Notiz (korrekter Endpoint) ===")
url = f"{BASE_A}/{meeting_1.id}/private-note/{item_m1_paper.id}/"
resp = api_post(c_admin, url, {"content": "Meine private Notiz"})
check("Private Notiz speichern", resp.status_code == 200 and resp.json().get("success"))
resp = c_admin.get(url)
check("Private Notiz laden (Rundlauf)", resp.json().get("content") == "Meine private Notiz")
note = AgendaPrivateNote.objects.get(author=m_admin, agenda_item=item_m1_paper)
check("Private Notiz verschlüsselt gespeichert", note.get_content_decrypted() == "Meine private Notiz")

# --- 4. Position: reasoning + outcome, partielle Saves -------------------------
print("=== 4. Position mit Begründung + Beratungsergebnis ===")
pos_url = f"{BASE_A}/{meeting_1.id}/position/{item_m1_paper.id}/"
resp = api_post(c_admin, pos_url, {"position": "for"})
check("Position setzen", resp.status_code == 200 and resp.json()["position"] == "for")
resp = api_post(c_admin, pos_url, {"reasoning": "Gute Sache für Familien"})
check(
    "Begründung partiell speichern", resp.status_code == 200 and resp.json()["reasoning"] == "Gute Sache für Familien"
)
resp = api_post(c_admin, pos_url, {"outcome": "accepted"})
check("Outcome partiell speichern", resp.status_code == 200 and resp.json()["outcome"] == "accepted")
resp = c_admin.get(pos_url)
data = resp.json()["position"]
check(
    "Partielle Saves erhalten alle Felder",
    data["position"] == "for" and data["reasoning"] == "Gute Sache für Familien" and data["outcome"] == "accepted",
    str(data),
)
resp = api_post(c_admin, pos_url, {"outcome": "quatsch"})
check("Ungültiges Outcome -> 400", resp.status_code == 400)
resp = api_post(c_admin, pos_url, {"position": "quatsch"})
check("Ungültige Position -> 400", resp.status_code == 400)

# --- 5. Übergreifende Positions-Anzeige ----------------------------------------
print("=== 5. Positionen übergreifend (2 Gremien, selbe Vorlage) ===")
cross_url = f"{BASE_A}/{meeting_2.id}/position/{item_m2_paper.id}/"
resp = c_admin.get(cross_url)
cross = resp.json()["cross_positions"]
check("Position aus Gremium A erscheint in Gremium B", len(cross) == 1, str(cross))
if cross:
    entry = cross[0]
    check(
        "Cross-Position mit gremium/sitzung/datum/position/outcome/reasoning",
        entry["position"] == "for"
        and entry["outcome"] == "accepted"
        and entry["reasoning"] == "Gute Sache für Familien"
        and entry["gremium"] == "Hauptausschuss"
        and entry["datum"],
        str(entry),
    )
resp = c_foreign.get(f"{BASE_B}/{meeting_2.id}/position/{item_m2_paper.id}/")
check("Org-Grenze: fremde Org sieht keine Cross-Positionen", resp.json()["cross_positions"] == [])
resp = c_admin.get(f"{BASE_A}/{meeting_2.id}/prepare/")
check("Prepare-Kontext enthält crossPositions", "crossPositions" in resp.content.decode("utf-8"))

# --- 6. Datenmigration AgendaItemNote -> PaperComment ---------------------------
print("=== 6. Migrations-Integrität (AgendaItemNote -> PaperComment) ===")
# 3 Alt-Notizen: 2 am TOP mit Vorlage (1 consulting, 1 decision), 1 ohne Vorlage
note_consulting = AgendaItemNote(organization=org_a, agenda_item=item_m1_paper, author=m_admin, visibility="consulting")
note_consulting.set_content_encrypted("Alte Consulting-Notiz")
note_consulting.save()
note_decision = AgendaItemNote(
    organization=org_a, agenda_item=item_m1_paper, author=m_member, visibility="organization", is_decision=True
)
note_decision.set_content_encrypted("Alter Fraktionsbeschluss")
note_decision.save()
note_plain = AgendaItemNote(organization=org_a, agenda_item=item_m1_plain, author=m_admin, visibility="organization")
note_plain.set_content_encrypted("Notiz ohne Vorlage")
note_plain.save()
# created_at zurückdatieren, um die Übernahme zu prüfen
old_date = now - timezone.timedelta(days=30)
AgendaItemNote.objects.filter(pk=note_decision.pk).update(created_at=old_date)

before_notes = AgendaItemNote.objects.count()
before_comments = PaperComment.objects.count()

migration = importlib.import_module("apps.work.migrations.0037_notizen_zu_vorgang_kommentaren")
migration.migrate_notes_to_paper_comments(django_apps, None)

after_comments = PaperComment.objects.count()
check(
    "Zähler: 3 Notizen -> exakt 2 neue PaperComments",
    after_comments - before_comments == 2 and AgendaItemNote.objects.count() == before_notes,
    f"comments {before_comments}->{after_comments}",
)

note_consulting.refresh_from_db()
note_decision.refresh_from_db()
note_plain.refresh_from_db()
check(
    "Notizen mit Vorlage markiert, ohne Vorlage nicht",
    note_consulting.migrated_to_paper_comment_id is not None
    and note_decision.migrated_to_paper_comment_id is not None
    and note_plain.migrated_to_paper_comment_id is None,
)

mc = note_consulting.migrated_to_paper_comment
md = note_decision.migrated_to_paper_comment
check("Visibility consulting -> consulting", mc is not None and mc.visibility == "consulting")
check("Visibility organization -> organization", md is not None and md.visibility == "organization")
check("is_decision -> is_recommendation", md.is_recommendation is True and mc.is_recommendation is False)
check("Autor erhalten", mc.author_id == m_admin.id and md.author_id == m_member.id)
check("created_at erhalten", abs((md.created_at - old_date).total_seconds()) < 2, str(md.created_at))
check(
    "Inhalt-Decrypt-Gleichheit (Stichproben)",
    mc.get_content_decrypted() == "Alte Consulting-Notiz" and md.get_content_decrypted() == "Alter Fraktionsbeschluss",
)
check("Paper-Bezug korrekt", mc.paper_id == paper.id and md.paper_id == paper.id)

# Idempotenz: Doppellauf erzeugt nichts Neues
migration.migrate_notes_to_paper_comments(django_apps, None)
check("Idempotenz (Doppellauf)", PaperComment.objects.count() == after_comments)

# Keine Doppelanzeige: Notes-API liefert migrierten Inhalt nur einmal (als PaperComment)
resp = c_admin.get(f"{BASE_A}/{meeting_1.id}/notes/{item_m1_paper.id}/")
notes_payload = resp.json()["notes"]
contents = [n["content"] for n in notes_payload]
check(
    "Keine Doppelanzeige nach Migration",
    contents.count("Alte Consulting-Notiz") == 1 and contents.count("Alter Fraktionsbeschluss") == 1,
    str(contents),
)
check(
    "Migrierte Inhalte kommen als PaperComment",
    all(n["source"] == "paper_comment" for n in notes_payload if n["content"].startswith("Alte")),
)

# --- 7. Einheitlicher Thread ----------------------------------------------------
print("=== 7. Einheitlicher Diskussions-Thread ===")
resp = api_post(
    c_admin,
    f"{BASE_A}/{meeting_1.id}/notes/{item_m1_paper.id}/",
    {"content": "Neuer Thread-Beitrag", "visibility": "organization", "is_decision": True},
)
check(
    "POST an TOP mit Vorlage erzeugt PaperComment",
    resp.status_code == 200
    and resp.json()["note"]["source"] == "paper_comment"
    and PaperComment.objects.filter(paper=paper, is_recommendation=True).count() == 2,
)
resp = c_admin.get(f"{BASE_A}/{meeting_2.id}/notes/{item_m2_paper.id}/")
check(
    "Thread-Beitrag im ganzen Beratungsverlauf sichtbar (Gremium B)",
    any(n["content"] == "Neuer Thread-Beitrag" for n in resp.json()["notes"]),
)
resp = api_post(
    c_admin,
    f"{BASE_A}/{meeting_1.id}/notes/{item_m1_plain.id}/",
    {"content": "Lokale TOP-Notiz", "visibility": "organization"},
)
check(
    "POST an TOP ohne Vorlage erzeugt AgendaItemNote",
    resp.status_code == 200
    and resp.json()["note"]["source"] == "agenda_note"
    and AgendaItemNote.objects.filter(agenda_item=item_m1_plain, migrated_to_paper_comment__isnull=True).count() == 2,
)
# Org-Grenze: fremde Org sieht organization-Kommentare nicht
resp = c_foreign.get(f"{BASE_B}/{meeting_1.id}/notes/{item_m1_paper.id}/")
check(
    "Org-Grenze im Thread dicht",
    all(n["content"] not in ("Neuer Thread-Beitrag", "Alter Fraktionsbeschluss") for n in resp.json()["notes"]),
)

# --- 8. Abgeleitetes is_prepared -------------------------------------------------
print("=== 8. Abgeleitetes is_prepared ===")
prep = MeetingPreparation.objects.get(organization=org_a, meeting=meeting_1)
check(
    "is_prepared automatisch nach inhaltlichem Save",
    prep.is_prepared and prep.prepared_at is not None and prep.prepared_by_id is not None,
)
# Sitzung ohne inhaltliche Arbeit (nur Seitenaufruf) bleibt unvorbereitet
resp = c_foreign.get(f"{BASE_B}/{meeting_1.id}/prepare/")
prep_b = MeetingPreparation.objects.get(organization=org_b, meeting=meeting_1)
check("Reiner Seitenaufruf setzt is_prepared nicht", prep_b.is_prepared is False)

# --- 9. Redebeitrag: HTML + Whitelist + linked_document ---------------------------
print("=== 9. Redebeitrag (WYSIWYG + Dokument-Verknüpfung) ===")
speech_url = f"{BASE_A}/{meeting_1.id}/speech/{item_m1_paper.id}/"
evil_html = (
    '<h2>Rede</h2><p onclick="x()">Hallo <b>Welt</b></p><script>alert(1)</script><a href="https://evil">Link</a>'
)
resp = api_post(c_admin, speech_url, {"content": evil_html, "title": "Meine Rede"})
check("Redebeitrag speichern", resp.status_code == 200)
resp = c_admin.get(speech_url)
check("Lesen strippt nichts (Roh-HTML bleibt)", resp.json()["own"]["content"] == evil_html)
# Partieller Save: nur Titel ändern, Inhalt bleibt
resp = api_post(c_admin, speech_url, {"title": "Neuer Titel"})
resp = c_admin.get(speech_url)
check(
    "Partieller Save (nur title)",
    resp.json()["own"]["title"] == "Neuer Titel" and resp.json()["own"]["content"] == evil_html,
)
# Teleprompter rendert sanitized
resp = c_admin.get(f"{BASE_A}/{meeting_1.id}/teleprompter/{item_m1_paper.id}/")
tele = resp.content.decode("utf-8")
check("Teleprompter lädt", resp.status_code == 200)
check(
    "Whitelist: script/attribute raus, b/h2 bleiben",
    "alert(1)" not in tele and "onclick" not in tele and "<b>Welt</b>" in tele and "<h2>Rede</h2>" in tele,
)
check(
    "Sanitizer-Funktion direkt",
    sanitize_speech_html(evil_html) == "<h2>Rede</h2><p>Hallo <b>Welt</b></p>Link",
    sanitize_speech_html(evil_html),
)

# linked_document: fremdes/unzugängliches Dokument -> 403
doc_private = Motion.objects.create(organization=org_a, author=m_admin, title="Rede-Dokument", visibility="private")
doc_private.set_content_encrypted("<p>Dokument-Inhalt als Rede</p>")
doc_private.save()
resp = api_post(c_member, speech_url, {"linked_document": str(doc_private.id)})
check("Privates fremdes Dokument verknüpfen -> 403", resp.status_code == 403)
doc_foreign = Motion.objects.create(
    organization=org_b, author=m_foreign, title="Fremdes Dokument", visibility="organization"
)
resp = api_post(c_admin, speech_url, {"linked_document": str(doc_foreign.id)})
check("Dokument fremder Org verknüpfen -> 403", resp.status_code == 403)
resp = api_post(c_admin, speech_url, {"linked_document": str(doc_private.id)})
check("Eigenes Dokument verknüpfen", resp.status_code == 200)
resp = c_admin.get(speech_url)
own = resp.json()["own"]
check(
    "Verknüpftes Dokument liefert Motion-Inhalt read-only",
    own["content"] == "<p>Dokument-Inhalt als Rede</p>"
    and own["content_readonly"] is True
    and own["linked_document"]["title"] == "Rede-Dokument",
    str(own),
)
# Auswahlliste verknüpfbarer Dokumente
resp = c_admin.get(f"{BASE_A}/speech-documents/")
check(
    "Verknüpfbare Dokumente der Org gelistet",
    any(d["title"] == "Rede-Dokument" for d in resp.json()["documents"]),
)
# Verknüpfung lösen
resp = api_post(c_admin, speech_url, {"linked_document": None})
resp = c_admin.get(speech_url)
check("Verknüpfung lösen -> eigener Inhalt wieder da", resp.json()["own"]["content"] == evil_html)

# --- 10. Vorlagen-Anhänge (share_across_committees) -------------------------------
print("=== 10. Vorlagen-Anhänge (übergreifend teilbar) ===")
supp_url_m1 = f"{BASE_A}/{meeting_1.id}/supplementary/{item_m1_paper.id}/"
supp_url_m2 = f"{BASE_A}/{meeting_2.id}/supplementary/{item_m2_paper.id}/"
resp = api_post(
    c_admin,
    supp_url_m1,
    {
        "document_type": "link",
        "title": "Geteiltes Gutachten",
        "url": "https://example.org/gutachten.pdf",
        "paper_id": str(paper.id),
        "share_across_committees": True,
    },
)
check(
    "Vorlagen-Anhang mit Flag anlegen",
    resp.status_code == 200 and resp.json()["document"]["share_across_committees"] is True,
)
resp = api_post(
    c_admin,
    supp_url_m1,
    {
        "document_type": "link",
        "title": "Nur hier sichtbar",
        "url": "https://example.org/lokal.pdf",
        "paper_id": str(paper.id),
        "share_across_committees": False,
    },
)
check("Vorlagen-Anhang ohne Flag anlegen", resp.status_code == 200)

titles_m2 = [d["title"] for d in c_admin.get(supp_url_m2).json()["documents"]]
check("Mit Flag: in zweiter Gremien-Vorbereitung sichtbar", "Geteiltes Gutachten" in titles_m2, str(titles_m2))
check("Ohne Flag: nicht in zweiter Vorbereitung", "Nur hier sichtbar" not in titles_m2)
titles_m1 = [d["title"] for d in c_admin.get(supp_url_m1).json()["documents"]]
check("Ohne Flag: im Ursprungs-TOP sichtbar", "Nur hier sichtbar" in titles_m1)
titles_foreign = [
    d["title"] for d in c_foreign.get(f"{BASE_B}/{meeting_2.id}/supplementary/{item_m2_paper.id}/").json()["documents"]
]
check("Org-Grenze strikt: fremde Org sieht nie etwas", titles_foreign == [], str(titles_foreign))
# Anker-Validierung: fremdes Paper wird ignoriert
other_paper = OParlPaper.objects.create(
    external_id="https://oparl.example.org/paper/99", body=body, name="Anderes Paper"
)
resp = api_post(
    c_admin,
    supp_url_m1,
    {
        "document_type": "link",
        "title": "Falscher Anker",
        "url": "https://example.org/x.pdf",
        "paper_id": str(other_paper.id),
        "share_across_committees": True,
    },
)
doc = AgendaSupplementaryDocument.objects.get(title="Falscher Anker")
check("Paper-Anker nur für tatsächlich beratene Vorlage", doc.paper_id is None and not doc.share_across_committees)

# --- 11. Summary: alle Positionsarten ----------------------------------------------
print("=== 11. Summary (alle Positionsarten) ===")
# 7 TOPs in meeting_1, je eine Positionsart
codes = [c for c, _label in AgendaItemPosition.POSITION_CHOICES if c != "open"]
for idx, code in enumerate(codes):
    item = OParlAgendaItem.objects.create(
        external_id=f"https://oparl.example.org/agendaitem/summary-{idx}",
        meeting=meeting_1,
        number=str(10 + idx),
        name=f"Summary-TOP {code}",
    )
    api_post(c_admin, f"{BASE_A}/{meeting_1.id}/position/{item.id}/", {"position": code})
resp = c_admin.get(f"{BASE_A}/{meeting_1.id}/summary/")
summary_html = resp.content.decode("utf-8")
labels = [label for code, label in AgendaItemPosition.POSITION_CHOICES if code != "open"]
missing = [label for label in labels if label not in summary_html]
check("Alle Positionsarten im Summary", resp.status_code == 200 and not missing, f"fehlt: {missing}")
check("Keine 'discuss'-Leiche im Summary", "Diskussionsbedarf" not in summary_html)
check("Summary zeigt Ergebnis der Beratung", "Ergebnis:" in summary_html)

# --- 12. Channels-Consumer ----------------------------------------------------------
print("=== 12. Channels-Consumer (PreparationConsumer) ===")


async def test_consumer():
    results = {}
    app = URLRouter(websocket_urlpatterns)

    # Zugriff: Mitglied der Org
    comm = WebsocketCommunicator(app, f"/ws/preparation/{org_a.slug}/paper/{paper.id}/")
    comm.scope["user"] = user_member
    connected, _ = await comm.connect()
    results["member_connect"] = connected
    if connected:
        hello = await comm.receive_json_from()
        results["hello"] = hello.get("type") == "connected"

    # Fremde Org an org_a-Gruppe -> abgelehnt
    comm_foreign = WebsocketCommunicator(app, f"/ws/preparation/{org_a.slug}/paper/{paper.id}/")
    comm_foreign.scope["user"] = user_foreign
    connected_foreign, _ = await comm_foreign.connect()
    results["foreign_rejected"] = not connected_foreign
    await comm_foreign.disconnect()

    # Anonym -> abgelehnt
    from django.contrib.auth.models import AnonymousUser

    comm_anon = WebsocketCommunicator(app, f"/ws/preparation/{org_a.slug}/paper/{paper.id}/")
    comm_anon.scope["user"] = AnonymousUser()
    connected_anon, _ = await comm_anon.connect()
    results["anon_rejected"] = not connected_anon
    await comm_anon.disconnect()

    # Broadcast: Kommentar-POST über REST -> Event am Socket
    def _post_comment():
        return api_post(
            c_admin,
            f"{BASE_A}/{meeting_1.id}/notes/{item_m1_paper.id}/",
            {"content": "Echtzeit-Kommentar", "visibility": "organization"},
        )

    resp = await sync_to_async(_post_comment)()
    results["post_ok"] = resp.status_code == 200
    try:
        event = await comm.receive_json_from(timeout=5)
        results["broadcast"] = (
            event.get("type") == "comment"
            and event.get("event") == "created"
            and event["comment"]["content"] == "Echtzeit-Kommentar"
        )
    except Exception as e:
        results["broadcast"] = False
        results["broadcast_error"] = str(e)

    await comm.disconnect()
    return results


results = asyncio.run(test_consumer())
check("Consumer: Org-Mitglied verbunden", results.get("member_connect") and results.get("hello"))
check("Consumer: fremde Org abgelehnt", results.get("foreign_rejected"))
check("Consumer: anonym abgelehnt", results.get("anon_rejected"))
check(
    "Broadcast bei Kommentar-POST",
    results.get("post_ok") and results.get("broadcast"),
    results.get("broadcast_error", ""),
)

# --- 13. Regression: bestehende Flows -------------------------------------------------
print("=== 13. Regression ===")
resp = c_admin.get(f"{BASE_A}/")
check("Meeting-Liste lädt", resp.status_code == 200)
resp = c_admin.get(f"{BASE_A}/{meeting_1.id}/")
check("Meeting-Detail lädt", resp.status_code == 200)
resp = c_admin.post(
    f"{BASE_A}/{meeting_1.id}/prepare/",
    {"action": "save_notes", "notes": "Org-weite Sitzungsnotiz"},
)
prep.refresh_from_db()
check(
    "Org-Notizen speichern (Form-POST)",
    resp.status_code in (200, 302) and prep.get_notes_decrypted() == "Org-weite Sitzungsnotiz",
)
resp = api_post(c_admin, f"{BASE_A}/{meeting_1.id}/prepare/", {"notes": "Auto-Save Notiz"})
prep.refresh_from_db()
check(
    "Org-Notizen speichern (JSON-Auto-Save)",
    resp.status_code == 200 and prep.get_notes_decrypted() == "Auto-Save Notiz",
)
# Deprecated mark_prepared bleibt funktionsfähig
resp = c_admin.post(f"{BASE_A}/{meeting_1.id}/prepare/", {"action": "unmark_prepared"})
prep.refresh_from_db()
check("Deprecated unmark_prepared funktioniert weiter", prep.is_prepared is False)
# Redebeitrag löschen (DELETE)
resp = c_admin.delete(speech_url)
check(
    "Redebeitrag löschen",
    resp.status_code == 200 and not AgendaSpeechNote.objects.filter(author=m_admin, agenda_item=item_m1_paper).exists(),
)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
