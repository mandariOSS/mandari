# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: OParl-1.1-Aggregations-API (oparl_api, Issue #17).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_oparl_api.py

Prüft:
- System-Objekt vollständig (Pflichtfelder, oparlVersion, body-Listen-URL)
- Body-Liste + Body-Objekt mit allen Listen-URLs und eingebetteten Wahlperioden
- Externe Listen (organizations/people/meetings/papers/locations) mit
  OParl-Pagination (150 Meetings -> 2 Seiten, links.next/last, Link-Header)
- Filter modified_since/_until + created_since/_until (tz-aware Pflicht,
  naive Zeitstempel -> 400 mit klarer Meldung)
- Alle 12 Objekt-Endpunkte: 200, korrekte type-Schema-URL, id = eigene URL,
  mandari:originalId vorhanden
- Referenz-Umschreibung: Meeting -> organization/agendaItem/consultation/
  location zeigen auf unsere API; keine Original-URLs außerhalb von
  mandari:*-Vendor-Attributen
- File-URLs zeigen auf unseren file_proxy (accessUrl/downloadUrl)
- Unbekannte UUID/unbekannter Typ -> 404 als JSON; POST -> 405
- CORS-Header (Access-Control-Allow-Origin: *)
- Rate-Limit -> 429
- Query-Count-Deckel (Meetings-Liste <= 10 Queries)
- Antwort-Cache: zweiter Aufruf einer ungefilterten Liste ohne DB-Queries
"""

import base64
import json
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_oparl_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
# Rate-Limit hoch setzen, damit der Smoke-Test selbst nicht limitiert wird
# (das 429-Verhalten wird gezielt mit override_settings geprüft)
os.environ["OPARL_API_RATE_LIMIT"] = "100000"

import django  # noqa: E402

# Sync-Watchdog (insight_sync.apps.ready) nicht starten (SQLite-Lock)
sys.argv = ["manage.py", "smoke_oparl_api"]

django.setup()

from datetime import UTC, datetime, timedelta  # noqa: E402

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext, setup_test_environment  # noqa: E402

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


BASE = settings.OPARL_BASE_URL  # http://localhost:8000/oparl
SITE = settings.SITE_URL.rstrip("/")
RIS = "https://ris.example.org/oparl"
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def ts(hours):
    return T0 + timedelta(hours=hours)


# =============================================================================
# Synthetische Daten (alle 12 Objekttypen)
# =============================================================================
print("== Testdaten anlegen ==")

source = OParlSource.objects.create(name="RIS Musterstadt", url=f"{RIS}/system")

body_a = OParlBody.objects.create(
    external_id=f"{RIS}/body/1",
    source=source,
    name="Stadt Musterstadt",
    short_name="Musterstadt",
    slug="musterstadt",
    website="https://musterstadt.example.org",
    license="CC0-1.0",
    classification="Stadt",
    raw_json={"ags": "05515000", "contactEmail": "ris@musterstadt.example.org"},
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
body_b = OParlBody.objects.create(
    external_id=f"{RIS}/body/2",
    source=source,
    name="Gemeinde Beispielhausen",
    slug="beispielhausen",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)

term1 = OParlLegislativeTerm.objects.create(
    external_id=f"{RIS}/term/1",
    body=body_a,
    name="Wahlperiode 2020-2025",
    start_date=datetime(2020, 11, 1, tzinfo=UTC).date(),
    end_date=datetime(2025, 10, 31, tzinfo=UTC).date(),
    oparl_created=ts(0),
    oparl_modified=ts(0),
)

loc1 = OParlLocation.objects.create(
    external_id=f"{RIS}/location/1",
    body=body_a,
    description="Rathaus, Ratssaal",
    street_address="Rathausplatz 1",
    room="Ratssaal",
    postal_code="12345",
    locality="Musterstadt",
    geojson={"type": "Point", "coordinates": [7.62, 51.96]},
    oparl_created=ts(0),
    oparl_modified=ts(0),
)

org1 = OParlOrganization.objects.create(
    external_id=f"{RIS}/organization/1",
    body=body_a,
    name="Rat der Stadt Musterstadt",
    short_name="Rat",
    organization_type="gremium",
    classification="Rat",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
org2 = OParlOrganization.objects.create(
    external_id=f"{RIS}/organization/2",
    body=body_a,
    name="Hauptausschuss",
    organization_type="gremium",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)

p1 = OParlPerson.objects.create(
    external_id=f"{RIS}/person/1",
    body=body_a,
    name="Dr. Erika Mustermann",
    family_name="Mustermann",
    given_name="Erika",
    title="Dr.",
    email="erika@example.org",
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
p2 = OParlPerson.objects.create(
    external_id=f"{RIS}/person/2",
    body=body_a,
    family_name="Beispiel",
    given_name="Bernd",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)
p3 = OParlPerson.objects.create(
    external_id=f"{RIS}/person/3",
    body=body_a,
    family_name="Test",
    given_name="Tina",
    oparl_created=ts(0),
    oparl_modified=ts(3),
)

m1 = OParlMembership.objects.create(
    external_id=f"{RIS}/membership/1",
    person=p1,
    organization=org1,
    role="Vorsitz",
    voting_right=True,
    start_date=datetime(2020, 11, 1, tzinfo=UTC).date(),
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
OParlMembership.objects.create(
    external_id=f"{RIS}/membership/2",
    person=p2,
    organization=org1,
    role="Mitglied",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)

# 150 Meetings für Body A -> 2 Seiten Pagination; Meeting 0 ist das "reiche" Meeting
file3_ext = f"{RIS}/file/3"
meeting1 = OParlMeeting.objects.create(
    external_id=f"{RIS}/meeting/0",
    body=body_a,
    name="Ratssitzung Januar",
    meeting_state="terminiert",
    start=ts(240),
    end=ts(243),
    location_name="Rathaus",
    raw_json={
        "location": {"id": loc1.external_id},
        "invitation": {"id": file3_ext},
        "organization": [f"{RIS}/organization/1"],
    },
    oparl_created=ts(0),
    oparl_modified=ts(0),
)
meeting1.organizations.add(org1)

OParlMeeting.objects.bulk_create(
    [
        OParlMeeting(
            external_id=f"{RIS}/meeting/{i}",
            body=body_a,
            name=f"Sitzung {i}",
            oparl_created=ts(i),
            oparl_modified=ts(i),
        )
        for i in range(1, 150)
    ]
)
OParlMeeting.objects.create(
    external_id=f"{RIS}/meeting/b1",
    body=body_b,
    name="Sitzung Beispielhausen",
    oparl_created=ts(0),
    oparl_modified=ts(0),
)

a1 = OParlAgendaItem.objects.create(
    external_id=f"{RIS}/agendaitem/1",
    meeting=meeting1,
    number="1",
    order=1,
    name="Haushaltssatzung 2024",
    public=True,
    result="beschlossen",
    oparl_created=ts(0),
    oparl_modified=ts(0),
)
OParlAgendaItem.objects.create(
    external_id=f"{RIS}/agendaitem/2",
    meeting=meeting1,
    number="2",
    order=2,
    name="Verschiedenes",
    oparl_created=ts(0),
    oparl_modified=ts(0),
)

file1_ext = f"{RIS}/file/1"
paper1 = OParlPaper.objects.create(
    external_id=f"{RIS}/paper/1",
    body=body_a,
    name="Haushaltssatzung 2024",
    reference="V/2024/001",
    paper_type="Beschlussvorlage",
    date=datetime(2024, 1, 5, tzinfo=UTC).date(),
    summary="KI-Zusammenfassung der Vorlage.",
    raw_json={"mainFile": {"id": file1_ext}},
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
OParlPaper.objects.create(
    external_id=f"{RIS}/paper/2",
    body=body_a,
    name="Anfrage Radwege",
    reference="A/2024/002",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)

f1 = OParlFile.objects.create(
    external_id=file1_ext,
    body=body_a,
    paper=paper1,
    name="Haushaltssatzung",
    file_name="haushalt_2024.pdf",
    mime_type="application/pdf",
    size=12345,
    access_url=f"{RIS}/files/haushalt_2024.pdf",
    download_url=f"{RIS}/files/haushalt_2024.pdf?dl=1",
    sha256_hash="ab" * 32,
    text_content="Volltext der Haushaltssatzung.",
    page_count=42,
    oparl_created=ts(0),
    oparl_modified=ts(1),
)
f2 = OParlFile.objects.create(
    external_id=f"{RIS}/file/2",
    body=body_a,
    paper=paper1,
    name="Anlage 1",
    file_name="anlage1.pdf",
    mime_type="application/pdf",
    oparl_created=ts(0),
    oparl_modified=ts(2),
)
f3 = OParlFile.objects.create(
    external_id=file3_ext,
    body=body_a,
    meeting=meeting1,
    name="Einladung",
    file_name="einladung.pdf",
    mime_type="application/pdf",
    oparl_created=ts(0),
    oparl_modified=ts(3),
)

c1 = OParlConsultation.objects.create(
    external_id=f"{RIS}/consultation/1",
    body=body_a,
    paper=paper1,
    paper_external_id=paper1.external_id,
    meeting_external_id=meeting1.external_id,
    agenda_item_external_id=a1.external_id,
    role="Entscheidung",
    authoritative=True,
    oparl_created=ts(0),
    oparl_modified=ts(1),
)

client = Client()


def get_json(path, **kwargs):
    resp = client.get(path, kwargs or None)
    try:
        return resp, json.loads(resp.content)
    except json.JSONDecodeError:
        return resp, None


def strip_mandari(value):
    """Entfernt mandari:*-Vendor-Attribute rekursiv (für Original-URL-Leck-Check)."""
    if isinstance(value, dict):
        return {k: strip_mandari(v) for k, v in value.items() if not k.startswith("mandari:")}
    if isinstance(value, list):
        return [strip_mandari(v) for v in value]
    return value


# =============================================================================
# 1. System-Objekt
# =============================================================================
print("== System-Objekt ==")
resp, system = get_json("/oparl/v1/system")
check("System: HTTP 200", resp.status_code == 200)
check("System: Content-Type JSON", resp["Content-Type"].startswith("application/json"))
check("System: CORS-Header", resp.get("Access-Control-Allow-Origin") == "*")
check("System: id = eigene URL", system and system.get("id") == f"{BASE}/v1/system", str(system))
check("System: type", system.get("type") == "https://schema.oparl.org/1.1/System")
check("System: oparlVersion", system.get("oparlVersion") == "https://schema.oparl.org/1.1/")
check("System: body-Listen-URL", system.get("body") == f"{BASE}/v1/bodies")
check(
    "System: Pflicht-/Kontaktfelder",
    all(system.get(k) for k in ("name", "contactEmail", "website", "vendor", "product")),
)

resp, root = get_json("/oparl/v1/")
check("Root-Übersicht: 200 + system-Link", resp.status_code == 200 and root.get("system") == f"{BASE}/v1/system")

resp_post = client.post("/oparl/v1/system")
check("POST -> 405", resp_post.status_code == 405)

# =============================================================================
# 2. Body-Liste + Body-Objekt
# =============================================================================
print("== Bodies ==")
resp, bodies = get_json("/oparl/v1/bodies")
check("Bodies: HTTP 200", resp.status_code == 200)
check("Bodies: 2 Einträge", bodies and bodies["pagination"]["totalElements"] == 2, str(bodies)[:200])
body_json = next((b for b in bodies["data"] if b.get("mandari:slug") == "musterstadt"), None)
check("Bodies: Musterstadt enthalten", body_json is not None)
check("Body: id = eigene URL", body_json["id"] == f"{BASE}/v1/body/{body_a.id}")
check("Body: system-Referenz", body_json["system"] == f"{BASE}/v1/system")
for seg, key in (("organizations", "organization"), ("people", "person"), ("meetings", "meeting"), ("papers", "paper")):
    check(f"Body: Listen-URL {key}", body_json.get(key) == f"{BASE}/v1/body/{body_a.id}/{seg}")
check(
    "Body: Wahlperioden eingebettet",
    body_json.get("legislativeTerm")
    and body_json["legislativeTerm"][0]["id"] == f"{BASE}/v1/legislativeterm/{term1.id}",
)
check("Body: ags aus raw_json", body_json.get("ags") == "05515000")
check("Body: mandari:originalId", body_json.get("mandari:originalId") == body_a.external_id)
check("Body: mandari:locationList", body_json.get("mandari:locationList") == f"{BASE}/v1/body/{body_a.id}/locations")

# =============================================================================
# 3. Meetings-Liste: Query-Deckel, Pagination, Cache
# =============================================================================
print("== Meetings-Liste (Pagination, Performance, Cache) ==")
meetings_url = f"/oparl/v1/body/{body_a.id}/meetings"
with CaptureQueriesContext(connection) as cq:
    resp, page1 = get_json(meetings_url)
check("Meetings: HTTP 200", resp.status_code == 200)
check(f"Meetings: Query-Deckel <= 10 (waren {len(cq)})", 0 < len(cq) <= 10)
check("Meetings: 100 Objekte auf Seite 1", len(page1["data"]) == 100)
check("Meetings: totalElements 150 (Body-Isolation)", page1["pagination"]["totalElements"] == 150)
check("Meetings: totalPages 2", page1["pagination"]["totalPages"] == 2)
check("Meetings: elementsPerPage 100", page1["pagination"]["elementsPerPage"] == 100)
expected_base = f"{BASE}/v1/body/{body_a.id}/meetings"
check("Meetings: links.next", page1["links"].get("next") == f"{expected_base}?page=2")
check(
    "Meetings: links.first/last",
    page1["links"]["first"] == expected_base and page1["links"]["last"] == f"{expected_base}?page=2",
)
check("Meetings: HTTP-Link-Header rel=next", 'rel="next"' in (resp.get("Link") or ""))
check(
    "Meetings: sortiert nach modified (ältestes zuerst)", page1["data"][0]["id"] == f"{BASE}/v1/meeting/{meeting1.id}"
)

with CaptureQueriesContext(connection) as cq2:
    resp_cached, _ = get_json(meetings_url)
check(f"Meetings: Cache greift (2. Aufruf {len(cq2)} Queries)", resp_cached.status_code == 200 and len(cq2) == 0)

resp, page2 = get_json(meetings_url, page=2)
check("Meetings Seite 2: 50 Objekte", resp.status_code == 200 and len(page2["data"]) == 50)
check("Meetings Seite 2: kein next", "next" not in page2["links"])
check("Meetings Seite 2: prev vorhanden", page2["links"].get("prev") == expected_base)
resp, _ = get_json(meetings_url, page=3)
check("Meetings Seite 3: 404", resp.status_code == 404)
resp, _ = get_json(meetings_url, page="abc")
check("Meetings page=abc: 400", resp.status_code == 400)

# =============================================================================
# 4. Filter (tz-aware Pflicht)
# =============================================================================
print("== Zeitfilter ==")
resp, filtered = get_json(meetings_url, modified_since="2024-01-05T04:00:00+00:00")
check(
    "modified_since (+00:00): 50 Treffer",
    resp.status_code == 200 and filtered["pagination"]["totalElements"] == 50,
    str(filtered.get("pagination")),
)
resp, filtered = get_json(meetings_url, modified_since="2024-01-05T05:00:00+01:00")
check("modified_since (+01:00, gleicher Moment): 50 Treffer", filtered["pagination"]["totalElements"] == 50)
resp, filtered = get_json(meetings_url, modified_until="2024-01-01T09:00:00Z")
check("modified_until (Z): 10 Treffer", filtered["pagination"]["totalElements"] == 10)
resp, filtered = get_json(meetings_url, created_since="2024-01-05T04:00:00+00:00")
check("created_since: 50 Treffer", filtered["pagination"]["totalElements"] == 50)
resp, filtered = get_json(
    meetings_url, modified_since="2024-01-05T04:00:00+00:00", modified_until="2024-01-05T13:00:00+00:00"
)
check("since+until kombiniert: 10 Treffer", filtered["pagination"]["totalElements"] == 10)

resp, err = get_json(meetings_url, modified_since="2024-01-05T04:00:00")
check("naiver Zeitstempel: 400", resp.status_code == 400)
check("naiver Zeitstempel: Meldung nennt Zeitzone", err and "Zeitzone" in err.get("error", ""), str(err))
check("naiver Zeitstempel: JSON-Fehler", resp["Content-Type"].startswith("application/json"))
resp, err = get_json(meetings_url, modified_since="kaputt")
check("ungültiger Zeitstempel: 400", resp.status_code == 400)

# =============================================================================
# 5. Weitere externe Listen
# =============================================================================
print("== Weitere Listen ==")
resp, orgs = get_json(f"/oparl/v1/body/{body_a.id}/organizations")
check("Organizations: 2 Einträge", resp.status_code == 200 and orgs["pagination"]["totalElements"] == 2)
org_json = next(o for o in orgs["data"] if o["id"] == f"{BASE}/v1/organization/{org1.id}")
check("Organization: body-Referenz umgeschrieben", org_json["body"] == f"{BASE}/v1/body/{body_a.id}")
check("Organization: membership-Referenzen", f"{BASE}/v1/membership/{m1.id}" in org_json.get("membership", []))

resp, people = get_json(f"/oparl/v1/body/{body_a.id}/people")
check("People: 3 Einträge", resp.status_code == 200 and people["pagination"]["totalElements"] == 3)
p1_json = next(p for p in people["data"] if p["id"] == f"{BASE}/v1/person/{p1.id}")
check("Person: email als Liste", p1_json.get("email") == ["erika@example.org"])
check("Person: title als Liste", p1_json.get("title") == ["Dr."])
check(
    "Person: Memberships eingebettet + Referenzen umgeschrieben",
    p1_json.get("membership")
    and p1_json["membership"][0]["id"] == f"{BASE}/v1/membership/{m1.id}"
    and p1_json["membership"][0]["organization"] == f"{BASE}/v1/organization/{org1.id}",
)

resp, papers = get_json(f"/oparl/v1/body/{body_a.id}/papers")
check("Papers: 2 Einträge", resp.status_code == 200 and papers["pagination"]["totalElements"] == 2)
paper_json = next(p for p in papers["data"] if p["id"] == f"{BASE}/v1/paper/{paper1.id}")
check("Paper: mainFile aufgelöst", paper_json.get("mainFile", {}).get("id") == f"{BASE}/v1/file/{f1.id}")
check("Paper: auxiliaryFile", any(f["id"] == f"{BASE}/v1/file/{f2.id}" for f in paper_json.get("auxiliaryFile", [])))
check(
    "Paper: Consultation eingebettet mit Meeting-/TOP-Referenzen",
    paper_json.get("consultation")
    and paper_json["consultation"][0]["meeting"] == f"{BASE}/v1/meeting/{meeting1.id}"
    and paper_json["consultation"][0]["agendaItem"] == f"{BASE}/v1/agendaitem/{a1.id}",
)
check("Paper: mandari:summary", paper_json.get("mandari:summary") == "KI-Zusammenfassung der Vorlage.")

resp, locs = get_json(f"/oparl/v1/body/{body_a.id}/locations")
check("Locations (Vendor-Liste): 1 Eintrag", resp.status_code == 200 and locs["pagination"]["totalElements"] == 1)

resp, _ = get_json(f"/oparl/v1/body/{uuid.uuid4()}/meetings")
check("Unbekannter Body: 404", resp.status_code == 404)
resp, _ = get_json(f"/oparl/v1/body/{body_a.id}/unbekannt")
check("Unbekanntes Listen-Segment: 404", resp.status_code == 404)

# =============================================================================
# 6. Referenz-Umschreibung im Meeting-Detail
# =============================================================================
print("== Meeting-Detail (Referenz-Umschreibung) ==")
resp, meeting_json = get_json(f"/oparl/v1/meeting/{meeting1.id}")
check("Meeting: HTTP 200", resp.status_code == 200)
check("Meeting: organization umgeschrieben", meeting_json.get("organization") == [f"{BASE}/v1/organization/{org1.id}"])
check(
    "Meeting: agendaItems eingebettet mit eigenen URLs",
    len(meeting_json.get("agendaItem", [])) == 2
    and meeting_json["agendaItem"][0]["id"] == f"{BASE}/v1/agendaitem/{a1.id}",
)
check(
    "Meeting: TOP -> consultation-Referenz",
    meeting_json["agendaItem"][0].get("consultation") == f"{BASE}/v1/consultation/{c1.id}",
)
check(
    "Meeting: location eingebettet mit eigener URL",
    meeting_json.get("location", {}).get("id") == f"{BASE}/v1/location/{loc1.id}",
)
check("Meeting: invitation aufgelöst", meeting_json.get("invitation", {}).get("id") == f"{BASE}/v1/file/{f3.id}")
check("Meeting: web = Insight-Detailseite", meeting_json.get("web") == f"{SITE}/insight/termine/{meeting1.id}/")
check("Meeting: mandari:originalId", meeting_json.get("mandari:originalId") == meeting1.external_id)
leak = json.dumps(strip_mandari(meeting_json), ensure_ascii=False)
check("Meeting: keine Original-URLs außerhalb mandari:*", "ris.example.org" not in leak, leak[:300])

# =============================================================================
# 7. Alle 12 Objekt-Endpunkte
# =============================================================================
print("== Objekt-Endpunkte (12 Typen) ==")
OBJECTS = [
    ("body", body_a.id, "Body"),
    ("organization", org1.id, "Organization"),
    ("person", p1.id, "Person"),
    ("membership", m1.id, "Membership"),
    ("meeting", meeting1.id, "Meeting"),
    ("agendaitem", a1.id, "AgendaItem"),
    ("paper", paper1.id, "Paper"),
    ("consultation", c1.id, "Consultation"),
    ("file", f1.id, "File"),
    ("location", loc1.id, "Location"),
    ("legislativeterm", term1.id, "LegislativeTerm"),
]
for kind, pk, schema_name in OBJECTS:
    resp, obj = get_json(f"/oparl/v1/{kind}/{pk}")
    check(
        f"{schema_name}: 200 + type + id + originalId",
        resp.status_code == 200
        and obj.get("type") == f"https://schema.oparl.org/1.1/{schema_name}"
        and obj.get("id") == f"{BASE}/v1/{kind}/{pk}"
        and obj.get("mandari:originalId"),
        f"status={resp.status_code} obj={str(obj)[:200]}",
    )
# System ist der 12. Typ (oben separat geprüft)
check("System als 12. Objekttyp geprüft", system.get("type") == "https://schema.oparl.org/1.1/System")

# =============================================================================
# 8. File-Proxy-URLs
# =============================================================================
print("== File-Objekt ==")
resp, file_json = get_json(f"/oparl/v1/file/{f1.id}")
proxy = f"{SITE}/insight/dokumente/{f1.id}/preview/"
check("File: accessUrl = Proxy", file_json.get("accessUrl") == proxy)
check("File: downloadUrl = Proxy + download=1", file_json.get("downloadUrl") == f"{proxy}?download=1")
check(
    "File: fileName/mimeType/size",
    file_json.get("fileName") == "haushalt_2024.pdf"
    and file_json.get("mimeType") == "application/pdf"
    and file_json.get("size") == 12345,
)
check("File: text im Detail", file_json.get("text") == "Volltext der Haushaltssatzung.")
check("File: paper-Rückreferenz", file_json.get("paper") == [f"{BASE}/v1/paper/{paper1.id}"])
check("File: mandari:sha256", file_json.get("mandari:sha256") == "ab" * 32)

# =============================================================================
# 9. Fehlerfälle + Rate-Limit
# =============================================================================
print("== Fehlerfälle & Rate-Limit ==")
resp, err = get_json(f"/oparl/v1/meeting/{uuid.uuid4()}")
check("Unbekannte UUID: 404 als JSON", resp.status_code == 404 and err and "error" in err)
resp, err = get_json(f"/oparl/v1/foobar/{uuid.uuid4()}")
check("Unbekannter Typ: 404 als JSON", resp.status_code == 404 and err and "error" in err)
check("404: CORS-Header auch bei Fehlern", resp.get("Access-Control-Allow-Origin") == "*")

with override_settings(OPARL_API_RATE_LIMIT=3):
    statuses = [client.get("/oparl/v1/system", REMOTE_ADDR="203.0.113.7").status_code for _ in range(5)]
check(
    "Rate-Limit: 3x 200, dann 429",
    statuses[:3] == [200, 200, 200] and statuses[3] == 429,
    str(statuses),
)
resp_429 = client.get("/oparl/v1/system", REMOTE_ADDR="203.0.113.7")
# Ohne Override gilt wieder das hohe Limit -> Fenster-Zähler bleibt, aber Limit hoch
check("Rate-Limit: nach Override wieder frei", resp_429.status_code == 200)

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n{PASS} bestanden, {FAIL} fehlgeschlagen")
sys.exit(1 if FAIL else 0)
