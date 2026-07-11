# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Sitzungsvorbereitungs-Umbau Etappe 2 (UI-Neubau prepare.html).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_prepare_ui.py

Prüft das gerenderte prepare.html:
- TOP-Navigator, drei Abschnitts-Überschriften (Position & Ergebnis /
  Redebeitrag / Anhänge)
- Outcome-Select mit allen Optionen, Begründungsfeld, Beratungsverlauf
  (cross-positions-Container)
- WYSIWYG-Init (MandariEditor.createEditor) + Editor-Bundle eingebunden
- Getrennte Anhang-Sektionen (Ratsinformationssystem / Eigene Anlagen)
- Diskussions-Thread mit Sichtbarkeits-Select + "Position der Fraktion"
- KEIN "Als vorbereitet markieren"-Button mehr (mark_prepared entfernt)
- Auto-Save-Statuselement, WebSocket-URL, Teleprompter-Link
- Keine verwaisten Alpine-Referenzen (discussion_note, mark_prepared)
- Interaktion über die APIs: Position setzen -> GET spiegelt;
  Kommentar-POST -> erscheint im Notes-GET; Org-Notizen-Auto-Save
- Zusammenfassung + Teleprompter rendern
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_ui_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

# Sync-Watchdog (insight_sync.apps.ready) nicht starten (SQLite-Lock)
sys.argv = ["manage.py", "smoke_prepare_ui"]

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.meetings.models import AgendaItemPosition  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
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


print("=== Setup ===")
source = OParlSource.objects.create(name="Smoke-Quelle", url="https://oparl.example.org/system")
body = OParlBody.objects.create(source=source, external_id="https://oparl.example.org/body/1", name="Musterstadt")

org = Organization.objects.create(name="Fraktion UI", slug="fraktion-ui", body=body)
role = Role.objects.filter(organization=org, is_admin=True).first()
if role is None:
    role = Role.objects.create(organization=org, name="Administrator", is_admin=True)

from apps.common.encryption import TenantEncryption  # noqa: E402

TenantEncryption(org).key

user = User.objects.create_user(email="ui@example.org", password="test1234!")
membership = Membership.objects.create(user=user, organization=org)
membership.roles.add(role)

client = Client()
client.force_login(user)

from insight_core.models import OParlOrganization  # noqa: E402

committee = OParlOrganization.objects.create(
    external_id="https://oparl.example.org/organization/ui-1",
    body=body,
    name="Hauptausschuss",
    organization_type="committee",
)
now = timezone.now()
meeting = OParlMeeting.objects.create(
    external_id="https://oparl.example.org/meeting/ui-1",
    body=body,
    name="Hauptausschuss-Sitzung",
    start=now + timezone.timedelta(days=3),
    location_name="Rathaus, Sitzungssaal 1",
)
meeting.organizations.add(committee)

paper = OParlPaper.objects.create(
    external_id="https://oparl.example.org/paper/ui-1",
    body=body,
    name="Radwegeausbau Innenstadt",
    reference="V/2026/042",
)
item_paper = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/ui-1",
    meeting=meeting,
    number="1",
    name="Beratung Radwegeausbau mit einem sehr langen Titel, der im Navigator nicht abgeschnitten werden darf",
)
item_plain = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/ui-2",
    meeting=meeting,
    number="2",
    name="Verschiedenes",
)
OParlConsultation.objects.create(
    external_id="https://oparl.example.org/consultation/ui-1",
    body=body,
    paper=paper,
    agenda_item_external_id=item_paper.external_id,
    meeting_external_id=meeting.external_id,
    role="Vorberatung",
)

BASE = f"/work/{org.slug}/meetings"


def api_post(c, url, payload):
    import json as _json

    return c.post(url, _json.dumps(payload), content_type="application/json")


# --- 1. Seite rendert mit allen Kernelementen ---------------------------------
print("=== 1. prepare.html: Kernelemente ===")
resp = client.get(f"{BASE}/{meeting.id}/prepare/")
html = resp.content.decode("utf-8")
check("Seite lädt (200)", resp.status_code == 200, f"status={resp.status_code}")
check("TOP-Navigator vorhanden", 'id="top-navigator"' in html)
check("Abschnitt 'Position & Ergebnis'", "Position &amp; Ergebnis" in html)
check("Abschnitt 'Redebeitrag'", 'id="section-speech"' in html and "Redebeitrag" in html)
check("Abschnitt 'Anhänge'", 'id="section-attachments"' in html and "Anhänge" in html)
check("Outcome-Select vorhanden", 'id="outcome-select"' in html)
outcome_labels = [label for code, label in AgendaItemPosition.OUTCOME_CHOICES if code]
missing_outcomes = [label for label in outcome_labels if label not in html]
check("Alle Outcome-Optionen", not missing_outcomes, f"fehlt: {missing_outcomes}")
check("Begründungsfeld vorhanden", 'id="reasoning-input"' in html)
check("Beratungsverlauf-Container", "cross-positions" in html and "Im Beratungsverlauf" in html)
check("crossPositions im Kontext-JSON", "crossPositions" in html)
check("WYSIWYG-Init (MandariEditor.createEditor)", "MandariEditor.createEditor" in html)
check("Editor-Bundle eingebunden", "js/editor.bundle.js" in html)
check("Anhänge: RIS-Sektion", "Aus dem Ratsinformationssystem" in html)
check("Anhänge: Eigene Anlagen getrennt", "Eigene Anlagen" in html)
check("Vorlagen-Anker-Checkbox", "An der Vorlage speichern" in html)
check("Thread: Sichtbarkeits-Select", 'id="thread-visibility"' in html)
check("Thread: 'Position der Fraktion'", "Position der Fraktion" in html)
check("Auto-Save-Statuselement", 'id="autosave-status"' in html)
check("WebSocket-URL im JS", "/ws/preparation/" in html)
check("Teleprompter-Link", 'id="teleprompter-link"' in html and "/teleprompter/" in html)
check("Org-Sitzungsnotizen-Panel", "Allgemeine Notizen zur Sitzung" in html)
check("Zusammenfassungs-Button", "Zusammenfassung" in html)

# Alle 8 Positionslabels (Buttons + Legende)
position_labels = [label for _code, label in AgendaItemPosition.POSITION_CHOICES]
missing_positions = [label for label in position_labels if label not in html]
check("Alle 8 Positions-Buttons/Labels", not missing_positions, f"fehlt: {missing_positions}")
check("aria-pressed an Positions-Buttons", "aria-pressed" in html)
check("Titel-Tooltip + line-clamp im Navigator", "line-clamp-2" in html and ':title="item.name"' in html)
check("Tastaturnavigation (Pfeiltasten)", "ArrowDown" in html and "ArrowUp" in html)
check("Mobile Tabs (TOPs/Vorbereitung/Diskussion)", "mobileTab" in html and "Vorbereitung" in html)

# --- 2. Deprecated / verwaiste Referenzen --------------------------------------
print("=== 2. Keine Alt-Lasten ===")
check("Kein mark_prepared im Rendering", "mark_prepared" not in html)
check("Kein discussion_note im Rendering", "discussion_note" not in html)
check("Kein 'Als vorbereitet markieren'", "Als vorbereitet markieren" not in html)
check("'Überweisen' bleibt entfernt", "Überweisen" not in html)
check("'Zur Kenntnis' bleibt entfernt", "Zur Kenntnis" not in html)

template_dir = PROJECT_DIR / "templates" / "work" / "meetings"
prepare_src = (template_dir / "prepare.html").read_text(encoding="utf-8")
check("prepare.html: kein mark_prepared", "mark_prepared" not in prepare_src)
check("prepare.html: kein discussion_note", "discussion_note" not in prepare_src)

# --- 3. Interaktion: Position setzen -> GET spiegelt ---------------------------
print("=== 3. Interaktion: Position ===")
pos_url = f"{BASE}/{meeting.id}/position/{item_paper.id}/"
resp = api_post(client, pos_url, {"position": "for", "reasoning": "Klimafreundlich", "outcome": "accepted"})
check("Position speichern", resp.status_code == 200 and resp.json().get("success"))
resp = client.get(pos_url)
data = resp.json()["position"]
check(
    "GET spiegelt Position/Begründung/Outcome",
    data["position"] == "for" and data["reasoning"] == "Klimafreundlich" and data["outcome"] == "accepted",
    str(data),
)
resp = api_post(client, pos_url, {"is_final": True})
check("Endgültig-Flag speichern", resp.status_code == 200 and resp.json()["is_final"] is True)

# Nach dem Speichern erscheint die Position im frisch gerenderten Kontext
resp = client.get(f"{BASE}/{meeting.id}/prepare/")
check("Kontext-JSON enthält gespeicherte Begründung", "Klimafreundlich" in resp.content.decode("utf-8"))

# --- 4. Interaktion: Kommentar -> Notes-GET ------------------------------------
print("=== 4. Interaktion: Diskussions-Thread ===")
notes_url = f"{BASE}/{meeting.id}/notes/{item_paper.id}/"
resp = api_post(
    client,
    notes_url,
    {"content": "Wir sollten die Kreuzung Nord priorisieren", "visibility": "organization", "is_decision": True},
)
check("Kommentar speichern", resp.status_code == 200 and resp.json().get("success"))
resp = client.get(notes_url)
notes = resp.json()["notes"]
match = [n for n in notes if n["content"] == "Wir sollten die Kreuzung Nord priorisieren"]
check("Kommentar erscheint im Notes-GET", len(match) == 1, str(notes))
check(
    "Flag 'Position der Fraktion' gesetzt",
    match and (match[0]["is_recommendation"] or match[0]["is_decision"]),
)
check("TOP mit Vorlage -> PaperComment-Thread", match and match[0]["source"] == "paper_comment")

# TOP ohne Vorlage -> AgendaItemNote
notes_url_plain = f"{BASE}/{meeting.id}/notes/{item_plain.id}/"
resp = api_post(client, notes_url_plain, {"content": "Ohne Vorlage", "visibility": "organization"})
resp = client.get(notes_url_plain)
plain_notes = resp.json()["notes"]
check(
    "TOP ohne Vorlage -> AgendaItemNote-Thread",
    any(n["content"] == "Ohne Vorlage" and n["source"] == "agenda_note" for n in plain_notes),
)

# --- 5. Interaktion: Org-Notizen-Auto-Save --------------------------------------
print("=== 5. Interaktion: Org-Sitzungsnotizen ===")
resp = api_post(client, f"{BASE}/{meeting.id}/prepare/", {"notes": "Treffpunkt 17:30 vor dem Saal"})
check("Org-Notizen speichern (JSON)", resp.status_code == 200 and resp.json().get("success"))
resp = client.get(f"{BASE}/{meeting.id}/prepare/")
check("Org-Notizen im Rendering", "Treffpunkt 17:30 vor dem Saal" in resp.content.decode("utf-8"))

# --- 6. Redebeitrag + Teleprompter ----------------------------------------------
print("=== 6. Redebeitrag + Teleprompter ===")
speech_url = f"{BASE}/{meeting.id}/speech/{item_paper.id}/"
resp = api_post(
    client,
    speech_url,
    {"content": "<p>Sehr geehrte <b>Damen und Herren</b></p>", "title": "Radwege-Rede", "estimated_duration": 90},
)
check("Redebeitrag speichern", resp.status_code == 200)
resp = client.get(speech_url)
own = resp.json()["own"]
check("Redebeitrag-Rundlauf (HTML + Dauer)", "<b>Damen und Herren</b>" in own["content"] and own["estimated_duration"] == 90)
resp = client.get(f"{BASE}/{meeting.id}/teleprompter/{item_paper.id}/")
tele = resp.content.decode("utf-8")
check("Teleprompter lädt", resp.status_code == 200)
check("Teleprompter rendert HTML-Redetext", "<b>Damen und Herren</b>" in tele)
check("Teleprompter: Schriftgrößen-Regler", "fontSize" in tele)
check("Teleprompter: Auto-Scroll-Regler", "scrollSpeed" in tele)

# --- 7. Zusammenfassung ----------------------------------------------------------
print("=== 7. Zusammenfassung ===")
resp = client.get(f"{BASE}/{meeting.id}/summary/")
summary_html = resp.content.decode("utf-8")
check("Summary lädt", resp.status_code == 200)
check("Summary zeigt Position + Outcome", "Zustimmung" in summary_html and "Ergebnis:" in summary_html)
check("Summary zeigt geteilte Redebeiträge nur wenn geteilt", "Radwege-Rede" not in summary_html)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
