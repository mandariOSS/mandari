# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Sitzungsvorbereitung Etappe 3 — PDF-Inline-Vorschau + Datei-Anmerkungen.

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_pdf_annotations.py

Prüft:
- FileAnnotation-CRUD über die API (beide Anker-Typen: oparl / doc)
- CheckConstraint: beide oder kein Anker gesetzt -> IntegrityError
- Org-Grenze strikt: fremde Organisation bekommt 404 (GET/POST/DELETE)
- DELETE durch fremden Autor derselben Org -> 403, durch Autor -> Erfolg
- Zähler-Badge (annotations) im gerenderten Kontext-JSON und in der
  Supplementary-API
- Vorschau-Container (pdf-preview-panel) + Kommentarspur (annotation-rail)
  im HTML, file_proxy-Vorschau-URL, "Im neuen Tab öffnen"
- Reihenfolge im DOM: Position & Ergebnis -> RIS-Dokumente -> Redebeitrag
- Akzent-Farbklassen der Abschnitte (indigo/sky/violet/amber/emerald)
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

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_ann_")) / "smoke.sqlite3"
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
sys.argv = ["manage.py", "smoke_pdf_annotations"]

django.setup()

from django.conf import settings  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import IntegrityError, transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

settings.MEDIA_ROOT = str(Path(tempfile.mkdtemp(prefix="mandari_smoke_ann_media_")))

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.meetings.models import AgendaSupplementaryDocument, FileAnnotation  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
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


def api_post(c, url, payload):
    import json as _json

    return c.post(url, _json.dumps(payload), content_type="application/json")


print("=== Setup ===")
source = OParlSource.objects.create(name="Smoke-Quelle", url="https://oparl.example.org/system")
body = OParlBody.objects.create(source=source, external_id="https://oparl.example.org/body/1", name="Musterstadt")
body2 = OParlBody.objects.create(source=source, external_id="https://oparl.example.org/body/2", name="Andersstadt")

org = Organization.objects.create(name="Fraktion PDF", slug="fraktion-pdf", body=body)
role = Role.objects.filter(organization=org, is_admin=True).first()
if role is None:
    role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
TenantEncryption(org).key

org2 = Organization.objects.create(name="Fremde Fraktion", slug="fremde-fraktion", body=body2)
role2 = Role.objects.filter(organization=org2, is_admin=True).first()
if role2 is None:
    role2 = Role.objects.create(organization=org2, name="Administrator", is_admin=True)
TenantEncryption(org2).key

user = User.objects.create_user(email="ann@example.org", password="test1234!")
membership = Membership.objects.create(user=user, organization=org)
membership.roles.add(role)

# Zweites Mitglied derselben Org (für DELETE-403-Test)
user_kollege = User.objects.create_user(email="kollege@example.org", password="test1234!")
membership_kollege = Membership.objects.create(user=user_kollege, organization=org)
membership_kollege.roles.add(role)

# Mitglied der fremden Org
user_fremd = User.objects.create_user(email="fremd@example.org", password="test1234!")
membership_fremd = Membership.objects.create(user=user_fremd, organization=org2)
membership_fremd.roles.add(role2)

client = Client()
client.force_login(user)
client_kollege = Client()
client_kollege.force_login(user_kollege)
client_fremd = Client()
client_fremd.force_login(user_fremd)

committee = OParlOrganization.objects.create(
    external_id="https://oparl.example.org/organization/ann-1",
    body=body,
    name="Hauptausschuss",
    organization_type="committee",
)
now = timezone.now()
meeting = OParlMeeting.objects.create(
    external_id="https://oparl.example.org/meeting/ann-1",
    body=body,
    name="Hauptausschuss-Sitzung",
    start=now + timezone.timedelta(days=3),
)
meeting.organizations.add(committee)

paper = OParlPaper.objects.create(
    external_id="https://oparl.example.org/paper/ann-1",
    body=body,
    name="Radwegeausbau Innenstadt",
    reference="V/2026/042",
)
item = OParlAgendaItem.objects.create(
    external_id="https://oparl.example.org/agendaitem/ann-1",
    meeting=meeting,
    number="1",
    name="Beratung Radwegeausbau",
)
OParlConsultation.objects.create(
    external_id="https://oparl.example.org/consultation/ann-1",
    body=body,
    paper=paper,
    agenda_item_external_id=item.external_id,
    meeting_external_id=meeting.external_id,
    role="Vorberatung",
)
pdf_file = OParlFile.objects.create(
    external_id="https://oparl.example.org/file/ann-1",
    body=body,
    paper=paper,
    name="Beschlussvorlage Radwege",
    file_name="vorlage.pdf",
    mime_type="application/pdf",
    size=123456,
    access_url="https://oparl.example.org/files/vorlage.pdf",
    download_url="https://oparl.example.org/files/vorlage.pdf?dl=1",
    page_count=12,
)
other_file = OParlFile.objects.create(
    external_id="https://oparl.example.org/file/ann-2",
    body=body,
    paper=paper,
    name="Anlage Karte",
    file_name="karte.png",
    mime_type="image/png",
    access_url="https://oparl.example.org/files/karte.png",
)

# Eigene PDF-Anlage (Upload)
own_doc = AgendaSupplementaryDocument.objects.create(
    organization=org,
    added_by=membership,
    agenda_item=item,
    document_type="file",
    title="Eigenes Gutachten",
    file=SimpleUploadedFile("gutachten.pdf", b"%PDF-1.4 smoke", content_type="application/pdf"),
    filename="gutachten.pdf",
    mime_type="application/pdf",
    file_size=14,
)

BASE = f"/work/{org.slug}/meetings"
ANN_OPARL = f"{BASE}/annotations/oparl/{pdf_file.id}/"
ANN_DOC = f"{BASE}/annotations/doc/{own_doc.id}/"

# --- 1. CRUD: Anker oparl_file -------------------------------------------------
print("=== 1. FileAnnotation-API: Anker OParl-Datei ===")
resp = client.get(ANN_OPARL)
check("GET leer (200)", resp.status_code == 200 and resp.json()["annotations"] == [], f"status={resp.status_code}")

resp = api_post(client, ANN_OPARL, {"page": 3, "content": "Kostenschätzung prüfen"})
check("POST legt Anmerkung an", resp.status_code == 200 and resp.json().get("success"))
data = resp.json()
check(
    "POST liefert Seite + Inhalt",
    data["annotation"]["page"] == 3 and data["annotation"]["content"] == "Kostenschätzung prüfen",
    str(data),
)
check("POST liefert Zähler 1", data.get("count") == 1)
ann_id = data["annotation"]["id"]

resp = api_post(client, ANN_OPARL, {"page": 1, "content": "Deckblatt: Datum falsch"})
check("Zweite Anmerkung (Seite 1)", resp.status_code == 200 and resp.json().get("count") == 2)

resp = client.get(ANN_OPARL)
notes = resp.json()["annotations"]
check(
    "GET liefert beide, sortiert nach Seite",
    len(notes) == 2 and notes[0]["page"] == 1 and notes[1]["page"] == 3,
    str(notes),
)
check("Autor + Zeit serialisiert", bool(notes[0]["author"]) and bool(notes[0]["created_at"]))
check("is_own für Autor", notes[0]["is_own"] is True)

# Org-weite Sichtbarkeit: Kollege sieht die Anmerkungen
resp = client_kollege.get(ANN_OPARL)
check("Org-weit sichtbar (Kollege)", resp.status_code == 200 and len(resp.json()["annotations"]) == 2)
check("is_own=False für Kollegen", all(n["is_own"] is False for n in resp.json()["annotations"]))

# Ungültige Seite fällt auf 1 zurück, leerer Inhalt -> 400
resp = api_post(client, ANN_OPARL, {"page": "quatsch", "content": "Fallback-Seite"})
check("Ungültige Seite -> 1", resp.status_code == 200 and resp.json()["annotation"]["page"] == 1)
resp = api_post(client, ANN_OPARL, {"page": 2, "content": "   "})
check("Leerer Inhalt -> 400", resp.status_code == 400)

# --- 2. CRUD: Anker eigene Anlage ----------------------------------------------
print("=== 2. FileAnnotation-API: Anker eigene Anlage ===")
resp = api_post(client, ANN_DOC, {"page": 2, "content": "Gutachten Kapitel 2 zitieren"})
check("POST an eigener Anlage", resp.status_code == 200 and resp.json().get("success"))
doc_ann_id = resp.json()["annotation"]["id"]
resp = client.get(ANN_DOC)
check("GET an eigener Anlage", resp.status_code == 200 and len(resp.json()["annotations"]) == 1)

# Supplementary-API liefert Zähler + Vorschau-Infos
resp = client.get(f"{BASE}/{meeting.id}/supplementary/{item.id}/")
docs = resp.json()["documents"]
own = [d for d in docs if d["id"] == str(own_doc.id)]
check("Supplementary-API: Anlage vorhanden", len(own) == 1, str(docs))
check("Supplementary-API: annotations=1", own and own[0]["annotations"] == 1, str(own))
check(
    "Supplementary-API: is_pdf + preview_kind=doc",
    own and own[0]["is_pdf"] is True and own[0]["preview_kind"] == "doc",
    str(own),
)
check("Supplementary-API: preview_url gesetzt", own and bool(own[0]["preview_url"]))

# --- 3. CheckConstraint: genau ein Anker ----------------------------------------
print("=== 3. CheckConstraint (genau ein Anker) ===")
failed_both = False
try:
    with transaction.atomic():
        a = FileAnnotation(
            organization=org,
            author=membership,
            page=1,
            oparl_file=pdf_file,
            supplementary_document=own_doc,
        )
        a.set_content_encrypted("beide Anker")
        a.save()
except IntegrityError:
    failed_both = True
check("Beide Anker gesetzt -> IntegrityError", failed_both)

failed_none = False
try:
    with transaction.atomic():
        a = FileAnnotation(organization=org, author=membership, page=1)
        a.set_content_encrypted("kein Anker")
        a.save()
except IntegrityError:
    failed_none = True
check("Kein Anker gesetzt -> IntegrityError", failed_none)

# --- 4. Org-Grenze strikt ---------------------------------------------------------
print("=== 4. Org-Grenze (fremde Organisation) ===")
FOREIGN_BASE = f"/work/{org2.slug}/meetings"
resp = client_fremd.get(f"{FOREIGN_BASE}/annotations/oparl/{pdf_file.id}/")
check("GET fremde Org (oparl) -> 404", resp.status_code == 404, f"status={resp.status_code}")
resp = api_post(client_fremd, f"{FOREIGN_BASE}/annotations/oparl/{pdf_file.id}/", {"page": 1, "content": "fremd"})
check("POST fremde Org (oparl) -> 404", resp.status_code == 404, f"status={resp.status_code}")
resp = client_fremd.get(f"{FOREIGN_BASE}/annotations/doc/{own_doc.id}/")
check("GET fremde Org (doc) -> 404", resp.status_code == 404, f"status={resp.status_code}")
resp = client_fremd.delete(f"{FOREIGN_BASE}/annotations/{ann_id}/delete/")
check("DELETE fremde Org -> 404", resp.status_code == 404, f"status={resp.status_code}")
check("Keine fremden Anmerkungen entstanden", FileAnnotation.objects.filter(organization=org2).count() == 0)

# Unbekannter Anker-Typ -> 404
resp = client.get(f"{BASE}/annotations/sonstwas/{pdf_file.id}/")
check("Unbekannter Anker-Typ -> 404", resp.status_code == 404, f"status={resp.status_code}")

# --- 5. DELETE: nur der Autor -------------------------------------------------------
print("=== 5. DELETE-Regeln ===")
resp = client_kollege.delete(f"{BASE}/annotations/{ann_id}/delete/")
check("DELETE fremder Autor (gleiche Org) -> 403", resp.status_code == 403, f"status={resp.status_code}")
resp = client.delete(f"{BASE}/annotations/{ann_id}/delete/")
check("DELETE durch Autor -> Erfolg", resp.status_code == 200 and resp.json().get("success"))
resp = client.get(ANN_OPARL)
check("Anmerkung ist weg, Rest bleibt", resp.status_code == 200 and len(resp.json()["annotations"]) == 2)
resp = client.delete(f"{BASE}/annotations/{doc_ann_id}/delete/")
check("DELETE doc-Anmerkung durch Autor", resp.status_code == 200 and resp.json().get("success"))

# --- 6. prepare.html: Vorschau, Kommentarspur, Reihenfolge, Farben -------------------
print("=== 6. prepare.html: Vorschau + Farben + Reihenfolge ===")
resp = client.get(f"{BASE}/{meeting.id}/prepare/")
html = resp.content.decode("utf-8")
check("Seite lädt (200)", resp.status_code == 200, f"status={resp.status_code}")
check("Vorschau-Container vorhanden", 'id="pdf-preview-panel"' in html)
check("Kommentarspur vorhanden", 'id="annotation-rail"' in html)
check("RIS-Sektion vorhanden", 'id="section-ris"' in html)
check("'Im neuen Tab öffnen' in der Vorschau", "Im neuen Tab öffnen" in html)
check("Anmerkungs-Eingabe (Seitenwahl)", "Anmerkung zu Seite" in html)
check("Seitensprung-Anker (#page=N)", "#page=" in html)
check("iframe-Vorschau (RIS-Muster)", "preview.frameSrc" in html and "<iframe" in html)

# Reihenfolge im DOM: Position -> RIS-Dokumente -> Redebeitrag -> Anhänge
pos_i = html.find('id="section-position"')
ris_i = html.find('id="section-ris"')
speech_i = html.find('id="section-speech"')
att_i = html.find('id="section-attachments"')
check("Reihenfolge: Position vor RIS", 0 < pos_i < ris_i, f"pos={pos_i}, ris={ris_i}")
check("Reihenfolge: RIS vor Redebeitrag", 0 < ris_i < speech_i, f"ris={ris_i}, speech={speech_i}")
check("Reihenfolge: Redebeitrag vor Anhängen", 0 < speech_i < att_i, f"speech={speech_i}, att={att_i}")

# Kontext-JSON der RIS-Dateien: Vorschau-URL (file_proxy), isPdf, Zähler-Badge
check("files-JSON: previewUrl (file_proxy)", "previewUrl" in html and "/preview/" in html)
check("files-JSON: isPdf erkannt", '"isPdf": true' in html)
check("files-JSON: Nicht-PDF bleibt isPdf=false", '"isPdf": false' in html)
check("files-JSON: Zähler-Badge (2 Anmerkungen)", '"annotations": 2' in html)
check("Badge-Text 'Anmerkungen' im Template", "Anmerkungen" in html)
check("Vorschau-Button an RIS-Karte", "Vorschau" in html and "openPreview('oparl'" in html)

# Akzentfarben der Abschnitte (inkl. Dark-Mode-Variante)
for cls in [
    "border-l-indigo-400",
    "border-l-sky-400",
    "border-l-violet-400",
    "border-l-amber-400",
    "border-l-emerald-400",
    "dark:border-l-indigo-600",
    "dark:border-l-sky-600",
    "text-indigo-500",
    "text-sky-500",
    "text-violet-500",
    "text-amber-500",
    "text-emerald-500",
]:
    check(f"Farbklasse {cls}", cls in html)

# Legende: Abschnittsfarben konsistent aufgeführt
check("Legende: Abschnitte-Block", "Abschnitte" in html and "bg-sky-400" in html and "bg-violet-400" in html)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
