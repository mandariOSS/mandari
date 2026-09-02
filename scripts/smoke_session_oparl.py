# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Spec-konforme Session-OParl-API je Mandant (Issue #35).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_oparl.py

Prüft:
- Auflösbare JSON-Objekt-Endpunkte: JEDE in der API referenzierte id ist
  per GET abrufbar und liefert JSON mit identischer id (kein HTML)
- Spec-Struktur: type-URLs (schema.oparl.org/1.1), created/modified mit
  Zeitzone, System -> Body -> Listen-Navigation
- Echte Pagination (links.next, Seitengröße, 400/404 bei Fehlbedienung)
- modified_since/created_since (Zeitzonen-Pflicht, naive -> 400)
- Tombstones (OParl 1.1 §2.8): Löschung und Ö->NÖ-Wechsel liefern
  gekürzte Objekte (deleted: true), erscheinen in modified_since-Listen,
  verschwinden bei erneuter Veröffentlichung
- mainFile/auxiliaryFile, eingebettete Memberships/AgendaItems/Consultations
- Ö/NÖ-BEWEIS: Die gesamte API-Oberfläche (alle Listen + alle erreichbaren
  Objekt-Endpunkte) enthält NIEMALS NÖ-Inhalte, verschlüsselte Personendaten
  oder Fremd-Tenant-Daten; NÖ-Objekt-Endpunkte liefern 404
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

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_oparl_session_"))
_db_path = _tmp_dir / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["OPARL_API_RATE_LIMIT"] = "100000"
os.environ["OPARL_API_PAGE_SIZE"] = "2"  # kleine Seiten -> Pagination testbar

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
_dj_settings.MEDIA_ROOT = str(_tmp_dir / "media")

from datetime import timedelta  # noqa: E402

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOParlTombstone,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionTenant,
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


client = Client()


def get_json(url, expect=200):
    resp = client.get(url)
    if resp.status_code != expect:
        return resp.status_code, None
    try:
        return resp.status_code, json.loads(resp.content)
    except ValueError:
        return resp.status_code, None


# =============================================================================
# Setup: Mandant mit Ö/NÖ-Daten, verschlüsselten Personendaten, Fremd-Tenant
# =============================================================================
tenant = SessionTenant.objects.create(
    name="Stadt Musterstadt",
    slug="musterstadt",
    short_name="Musterstadt",
    contact_email="ris@musterstadt.example",
)
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

council = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
committee = SessionOrganization.objects.create(
    tenant=tenant, name="Bauausschuss", organization_type="committee", parent=council
)

term = SessionLegislativeTerm.objects.create(
    tenant=tenant, name="Wahlperiode 2025-2030", start_date="2025-01-01", end_date="2030-12-31"
)

person = SessionPerson.objects.create(
    tenant=tenant, given_name="Anna", family_name="Beispiel", title="Dr.", email="anna@musterstadt.example"
)
person.set_phone_encrypted("GEHEIM-TELEFON-0123456")
person.set_address_encrypted("GEHEIME-ADRESSE Hinterhof 1")
person.set_bank_iban_encrypted("DE99GEHEIMIBAN0000")
person.save()
membership = SessionOrganizationMembership.objects.create(organization=council, person=person, role="chair")

now = timezone.now()
meeting_pub = SessionMeeting.objects.create(
    tenant=tenant,
    name="OEFFENTLICHE-RATSSITZUNG",
    organization=council,
    start=now + timedelta(days=7),
    location="Rathaus",
    room="Saal 1",
)
meeting_np = SessionMeeting.objects.create(
    tenant=tenant,
    name="GEHEIME-SONDERSITZUNG",
    organization=council,
    start=now + timedelta(days=8),
    is_public=False,
)

paper = SessionPaper.objects.create(
    tenant=tenant, reference="V/2026/0100", name="OEFFENTLICHE-VORLAGE-SPIELPLATZ", is_public=True
)
paper_np = SessionPaper.objects.create(
    tenant=tenant, reference="V/2026/0101", name="GEHEIME-GRUNDSTUECKSVORLAGE", is_public=False
)
paper2 = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0102", name="ZWEITE-VORLAGE", is_public=True)
paper3 = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0103", name="DRITTE-VORLAGE", is_public=True)

top_pub = SessionAgendaItem.objects.create(
    meeting=meeting_pub,
    number="1",
    order=1,
    name="OEFFENTLICHER-TOP-SPIELPLATZ",
    is_public=True,
    paper=paper,
    resolution_text="Der Rat beschließt den Spielplatz.",
    vote_result="approved",
    resolution_number="B/2026/0001",
)
top_np = SessionAgendaItem.objects.create(
    meeting=meeting_pub, number="N1", order=2, name="GEHEIMER-TOP-PERSONALIE", is_public=False
)
top_np.set_resolution_text_encrypted("GEHEIMER-BESCHLUSSTEXT-PERSONALIE")
top_np.save()
top_in_np_meeting = SessionAgendaItem.objects.create(
    meeting=meeting_np, number="1", order=1, name="TOP-IN-GEHEIMER-SITZUNG", is_public=True
)

file_main = SessionFile.objects.create(
    tenant=tenant,
    name="hauptdokument.txt",
    file=SimpleUploadedFile("hauptdokument.txt", b"oeffentlicher inhalt hauptdokument"),
    is_public=True,
    paper=paper,
    mime_type="text/plain",
    size=34,
)
file_aux = SessionFile.objects.create(
    tenant=tenant,
    name="anlage-2.txt",
    file=SimpleUploadedFile("anlage-2.txt", b"oeffentliche anlage 2"),
    is_public=True,
    paper=paper,
    mime_type="text/plain",
    size=21,
)
file_np = SessionFile.objects.create(
    tenant=tenant,
    name="GEHEIME-ANLAGE.txt",
    file=SimpleUploadedFile("GEHEIME-ANLAGE.txt", b"geheimer inhalt"),
    is_public=False,
    paper=paper,
)
# Ö-Datei an NÖ-Vorlage: darf trotz is_public NIE erscheinen
file_pub_on_np_paper = SessionFile.objects.create(
    tenant=tenant,
    name="OEFFENTLICH-MARKIERTE-ANLAGE-AN-GEHEIMER-VORLAGE.txt",
    file=SimpleUploadedFile("leak.txt", b"inhalt"),
    is_public=True,
    paper=paper_np,
)

# Beratungsfolge: Station 1 terminiert (Ö), Station 2 zeigt auf NÖ-Sitzung
consultation1 = SessionConsultation.objects.create(
    paper=paper, organization=committee, role="preliminary", order=1, meeting=meeting_pub, agenda_item=top_pub
)
consultation2 = SessionConsultation.objects.create(
    paper=paper, organization=council, role="decision", authoritative=True, order=2, meeting=meeting_np
)
# Beratung an NÖ-Vorlage: nie in der API
consultation_np = SessionConsultation.objects.create(paper=paper_np, organization=council, order=1)

# Fremd-Tenant-Daten
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="FREMDGREMIUM-XYZ")
SessionMeeting.objects.create(tenant=tenant_b, name="FREMD-SITZUNG-XYZ", organization=org_b, start=now)
SessionPaper.objects.create(tenant=tenant_b, reference="V/9", name="FREMD-VORLAGE-XYZ")

BASE = f"/session/{tenant.slug}/api/oparl/"

NON_PUBLIC_MARKERS = [
    "GEHEIME-SONDERSITZUNG",
    "GEHEIMER-TOP-PERSONALIE",
    "GEHEIME-GRUNDSTUECKSVORLAGE",
    "GEHEIMER-BESCHLUSSTEXT",
    "GEHEIME-ANLAGE",
    "OEFFENTLICH-MARKIERTE-ANLAGE-AN-GEHEIMER-VORLAGE",
    "TOP-IN-GEHEIMER-SITZUNG",
    "GEHEIM-TELEFON",
    "GEHEIME-ADRESSE",
    "DE99GEHEIMIBAN",
    "FREMDGREMIUM-XYZ",
    "FREMD-SITZUNG-XYZ",
    "FREMD-VORLAGE-XYZ",
]

# =============================================================================
# Phase A: System -> Body -> Listen (Navigation + Struktur)
# =============================================================================
print("=== Phase A: System/Body/Navigation ===")

status, system = get_json(BASE)
check("System-Endpoint -> 200 JSON", system is not None, f"status={status}")
check(
    "System: type/oparlVersion korrekt",
    system is not None
    and system.get("type") == "https://schema.oparl.org/1.1/System"
    and system.get("oparlVersion") == "https://schema.oparl.org/1.1/",
)
check("System: id == abgerufene URL", system is not None and system["id"].endswith(BASE))
check(
    "System: created/modified mit Zeitzone",
    system is not None and ("+" in system["created"] or system["created"].endswith("Z")),
)

status, bodies = get_json(system["body"])
check(
    "Body-Liste -> 200, Envelope mit data/links/pagination",
    bodies is not None and set(bodies) >= {"data", "links", "pagination"},
)
body = bodies["data"][0]
check("Body: type + system-Rückverweis", body["type"].endswith("/Body") and body["system"] == system["id"])
check("Body: legislativeTerm eingebettet", any(t["name"] == term.name for t in body.get("legislativeTerm", [])))

status, body_direct = get_json(body["id"])
check("Body-Objekt einzeln auflösbar (id identisch)", body_direct is not None and body_direct["id"] == body["id"])

# Alle Listen-URLs des Body sind erreichbar
list_fields = [
    "organization",
    "person",
    "meeting",
    "paper",
    "membership",
    "agendaItem",
    "consultation",
    "file",
    "legislativeTermList",
]
bad = []
collected = {}
for field in list_fields:
    url = body.get(field)
    if not url:
        bad.append(f"{field}: fehlt")
        continue
    items = []
    next_url = url
    while next_url:
        status, page = get_json(next_url)
        if page is None:
            bad.append(f"{field}: status {status}")
            break
        items.extend(page["data"])
        next_url = page["links"].get("next")
    collected[field] = items
check("Alle Body-Listen erreichbar (mit Pagination durchlaufen)", not bad, "; ".join(bad))

# =============================================================================
# Phase B: Pagination
# =============================================================================
print()
print("=== Phase B: Pagination ===")

status, page1 = get_json(f"{BASE}papers/")
check(
    "Papers Seite 1: elementsPerPage==2, next-Link",
    page1 is not None and page1["pagination"]["elementsPerPage"] == 2 and "next" in page1["links"],
)
check("Papers: totalElements == 3 (nur öffentliche)", page1 is not None and page1["pagination"]["totalElements"] == 3)
status, page2 = get_json(page1["links"]["next"])
check("Papers Seite 2 über links.next erreichbar", page2 is not None and len(page2["data"]) == 1)
check("Papers Seite 2: prev-Link vorhanden", page2 is not None and "prev" in page2["links"])
resp = client.get(f"{BASE}papers/?page=abc")
check("Ungültige Seitennummer -> 400", resp.status_code == 400, f"got {resp.status_code}")
resp = client.get(f"{BASE}papers/?page=99")
check("Seite jenseits des Endes -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = client.get(f"{BASE}unbekannteliste/")
check("Unbekannte Liste -> 404 (JSON)", resp.status_code == 404 and b"error" in resp.content, f"got {resp.status_code}")

# =============================================================================
# Phase C: Zeitfilter
# =============================================================================
print()
print("=== Phase C: modified_since/created_since ===")

resp = client.get(f"{BASE}papers/?modified_since=2026-01-01T00:00:00")
check("Naiver Zeitstempel -> 400 mit Hinweis", resp.status_code == 400 and "Zeitzone" in resp.content.decode("utf-8"))
status, filtered = get_json(f"{BASE}papers/?modified_since=2099-01-01T00:00:00%2B00:00")
check("modified_since in der Zukunft -> leer", filtered is not None and filtered["pagination"]["totalElements"] == 0)
status, filtered = get_json(f"{BASE}papers/?modified_since=2020-01-01T00:00:00Z")
check(
    "modified_since in der Vergangenheit -> alle", filtered is not None and filtered["pagination"]["totalElements"] == 3
)
status, filtered = get_json(f"{BASE}papers/?created_until=2020-01-01T00:00:00Z")
check(
    "created_until in der Vergangenheit -> leer", filtered is not None and filtered["pagination"]["totalElements"] == 0
)

# =============================================================================
# Phase D: Objekt-Inhalte (mainFile, Einbettungen, Beratungsfolge)
# =============================================================================
print()
print("=== Phase D: Objekt-Inhalte ===")

status, paper_obj = get_json(f"{BASE}paper/{paper.id}/")
check("Paper-Objekt auflösbar", paper_obj is not None, f"status={status}")
check(
    "Paper: mainFile gesetzt (älteste Ö-Datei)",
    paper_obj is not None and paper_obj.get("mainFile", {}).get("name") == "hauptdokument.txt",
)
check(
    "Paper: auxiliaryFile enthält zweite Datei",
    paper_obj is not None and [f["name"] for f in paper_obj.get("auxiliaryFile", [])] == ["anlage-2.txt"],
)
check(
    "Paper: Consultations eingebettet (2 Ö-Stationen)",
    paper_obj is not None and len(paper_obj.get("consultation", [])) == 2,
)
cons1 = paper_obj["consultation"][0]
cons2 = paper_obj["consultation"][1]
check("Consultation 1: agendaItem + meeting referenziert", "agendaItem" in cons1 and "meeting" in cons1)
check("Consultation 1: role/authoritative", cons1.get("role") == "Vorberatung" and cons1.get("authoritative") is False)
check(
    "Consultation 2: NÖ-Sitzungs-Referenz ausgelassen, authoritative",
    "meeting" not in cons2 and "agendaItem" not in cons2 and cons2.get("authoritative") is True,
)

status, meeting_obj = get_json(f"{BASE}meeting/{meeting_pub.id}/")
embedded_items = meeting_obj.get("agendaItem", []) if meeting_obj else []
check(
    "Meeting: nur Ö-TOP eingebettet",
    len(embedded_items) == 1 and embedded_items[0]["name"] == "OEFFENTLICHER-TOP-SPIELPLATZ",
)
check(
    "AgendaItem: result + resolutionText (öffentlich)",
    embedded_items
    and embedded_items[0].get("result") == "Angenommen"
    and "Spielplatz" in embedded_items[0].get("resolutionText", ""),
)
check("AgendaItem: consultation-Referenz", embedded_items and "consultation" in embedded_items[0])

status, person_obj = get_json(f"{BASE}person/{person.id}/")
check("Person: Membership eingebettet", person_obj is not None and len(person_obj.get("membership", [])) == 1)
check("Person: E-Mail als Liste", person_obj is not None and person_obj.get("email") == ["anna@musterstadt.example"])
status, membership_obj = get_json(f"{BASE}membership/{membership.id}/")
check("Membership einzeln auflösbar", membership_obj is not None and membership_obj.get("role") == "Vorsitzende/r")

status, org_obj = get_json(f"{BASE}organization/{committee.id}/")
check(
    "Organization: subOrganizationOf gesetzt",
    org_obj is not None and org_obj.get("subOrganizationOf", "").endswith(f"/organization/{council.id}/"),
)

# Datei-Download (anonym, nur öffentlich sichtbare Dateien)
resp = client.get(f"{BASE}file/{file_main.id}/download/")
check(
    "Ö-Datei-Download -> 200 mit Inhalt",
    resp.status_code == 200 and b"hauptdokument" in b"".join(resp.streaming_content),
)

# =============================================================================
# Phase E: Auflösbarkeit ALLER referenzierten IDs + Ö/NÖ-Beweis
# =============================================================================
print()
print("=== Phase E: Auflösbarkeit + Ö/NÖ-Beweis ===")


def walk_ids(node, found):
    """Sammelt alle 'id'-URLs dieser API aus einem JSON-Baum."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in (
                "id",
                "paper",
                "meeting",
                "agendaItem",
                "consultation",
                "person",
                "organization",
                "membership",
                "subOrganizationOf",
                "originatorPerson",
                "originatorOrganization",
                "underDirectionOf",
            ):
                for candidate in value if isinstance(value, list) else [value]:
                    if (
                        isinstance(candidate, str)
                        and "/api/oparl/" in candidate
                        and not candidate.rstrip("/").endswith("api/oparl")
                    ):
                        found.add(candidate)
                    elif isinstance(candidate, dict):
                        walk_ids(candidate, found)
            else:
                walk_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_ids(item, found)


all_payloads = []
all_ids = set()
for field, items in collected.items():
    all_payloads.append(json.dumps(items, ensure_ascii=False))
    walk_ids(items, all_ids)

# Listen-URLs aussortieren (enden auf Segment ohne UUID) — nur Objekt-URLs prüfen
object_ids = {u for u in all_ids if u.rstrip("/").split("/")[-1].count("-") == 4}
unresolvable = []
for url in sorted(object_ids):
    if url.endswith("/download/"):
        continue
    status, obj = get_json(url)
    if obj is None or obj.get("id") != url:
        unresolvable.append(f"{url}: {status}")
    else:
        all_payloads.append(json.dumps(obj, ensure_ascii=False))
check(
    f"Alle {len(object_ids)} referenzierten Objekt-IDs auflösbar (id identisch)",
    not unresolvable,
    "; ".join(unresolvable[:5]),
)

surface = "\n".join(all_payloads)
leaks = [marker for marker in NON_PUBLIC_MARKERS if marker in surface]
check(
    "Ö/NÖ-BEWEIS: keine NÖ-/verschlüsselten/fremden Inhalte in der gesamten API-Oberfläche", not leaks, ", ".join(leaks)
)
check(
    "Öffentliche Inhalte vorhanden (Gegenprobe)",
    "OEFFENTLICHE-VORLAGE-SPIELPLATZ" in surface and "OEFFENTLICHE-RATSSITZUNG" in surface,
)

# NÖ-Objekt-Endpunkte -> 404 (nie veröffentlicht)
wrong = []
for kind, pk in [
    ("meeting", meeting_np.id),
    ("paper", paper_np.id),
    ("agendaitem", top_np.id),
    ("agendaitem", top_in_np_meeting.id),
    ("file", file_np.id),
    ("file", file_pub_on_np_paper.id),
    ("consultation", consultation_np.id),
]:
    status = client.get(f"{BASE}{kind}/{pk}/").status_code
    if status != 404:
        wrong.append(f"{kind}/{pk}: {status}")
check("NÖ-Objekt-Endpunkte -> 404", not wrong, "; ".join(wrong))

resp = client.get(f"{BASE}file/{file_np.id}/download/")
check("NÖ-Datei-Download -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = client.get(f"{BASE}file/{file_pub_on_np_paper.id}/download/")
check("Ö-Datei an NÖ-Vorlage: Download -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Schreibzugriffe abgelehnt
resp = client.post(f"{BASE}papers/", {})
check("POST auf OParl-API -> 405", resp.status_code == 405, f"got {resp.status_code}")

# Fremder Mandanten-Slug -> 404 JSON
status, _ = get_json("/session/gibtsnicht/api/oparl/", expect=404)
check("Unbekannter Mandant -> 404", status == 404, f"got {status}")

# =============================================================================
# Phase F: Tombstones (Löschung + Ö->NÖ-Wechsel)
# =============================================================================
print()
print("=== Phase F: Tombstones ===")

cutoff = timezone.now()
cutoff_param = cutoff.isoformat().replace("+", "%2B")

paper2_id = paper2.id
paper2.delete()
status, tomb = get_json(f"{BASE}paper/{paper2_id}/")
check("Gelöschte Vorlage: Objekt-Endpunkt -> 200 Tombstone", tomb is not None and tomb.get("deleted") is True)
check(
    "Tombstone: exakt die Pflichtfelder",
    tomb is not None and set(tomb) == {"id", "type", "created", "modified", "deleted"},
    str(sorted(tomb) if tomb else None),
)

status, plain_list = get_json(f"{BASE}papers/")
check(
    "Ungefilterte Liste ohne Tombstones",
    plain_list is not None and all(not item.get("deleted") for item in plain_list["data"]),
)
status, inc_list = get_json(f"{BASE}papers/?modified_since={cutoff_param}")
check(
    "modified_since-Liste enthält den Tombstone",
    inc_list is not None and any(item.get("deleted") and str(paper2_id) in item["id"] for item in inc_list["data"]),
)

# Ö -> NÖ: Sitzung wird nachträglich nicht-öffentlich
meeting_pub.is_public = False
meeting_pub.save()
status, tomb = get_json(f"{BASE}meeting/{meeting_pub.id}/")
check("Entöffentlichte Sitzung -> Tombstone", tomb is not None and tomb.get("deleted") is True)
status, tomb = get_json(f"{BASE}agendaitem/{top_pub.id}/")
check("TOP der entöffentlichten Sitzung -> Tombstone", tomb is not None and tomb.get("deleted") is True)
status, meetings_now = get_json(f"{BASE}meetings/")
check(
    "Sitzung aus Voll-Liste verschwunden", meetings_now is not None and meetings_now["pagination"]["totalElements"] == 0
)
status, inc_meetings = get_json(f"{BASE}meetings/?modified_since={cutoff_param}")
check(
    "Sitzungs-Tombstone in modified_since-Liste",
    inc_meetings is not None and any(item.get("deleted") for item in inc_meetings["data"]),
)

# NÖ -> Ö: erneut veröffentlichen — Tombstones verschwinden
meeting_pub.is_public = True
meeting_pub.save()
status, obj = get_json(f"{BASE}meeting/{meeting_pub.id}/")
check(
    "Wieder veröffentlichte Sitzung liefert Vollobjekt",
    obj is not None and obj.get("deleted") is None and obj.get("name") == "OEFFENTLICHE-RATSSITZUNG",
)
check(
    "Tombstones der Sitzung entfernt",
    not SessionOParlTombstone.objects.filter(tenant=tenant, oparl_type="meeting", object_id=meeting_pub.id).exists(),
)
status, obj = get_json(f"{BASE}agendaitem/{top_pub.id}/")
check("TOP wieder als Vollobjekt", obj is not None and obj.get("deleted") is None)

# Tombstones enthalten keine Inhalte
tomb_payload = json.dumps([get_json(f"{BASE}paper/{paper2_id}/")[1]])
check("Tombstone ohne Inhalte (kein Name/Referenz)", "ZWEITE-VORLAGE" not in tomb_payload)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
