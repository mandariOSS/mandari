# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Georeferenzierung (Gazetteer, OParl-Locations, Karte, Marker-API).

Läuft gegen eine frische SQLite-Instanz ohne Netzwerkzugriffe:
    python scripts/smoke_georef.py

Prüft:
- Straßennamen-Normalisierung (straße/str./ss/Bindestriche/Case)
- Gazetteer-Matching: Straße mit Hausnummer (address_match),
  Schreibvarianten, Longest-Match (Neubrückenstraße vs. Brückenstraße)
- Personen-False-Positives: Nachname eines Ratsmitglieds != Straße (MST-Trick)
- OParl-Locations (paper.location via M2M) → paper.locations mit
  source="oparl", höchste Priorität, überschreibt nie manuelle Einträge
- Re-Runs der Extraktion zerstören oparl/manual-Einträge nicht
- Öffentliche Paper-Detailseite rendert Ortsliste + Leaflet-Karte
- map_markers: BBox-Filter-Parameter + Server-Cache (2. Aufruf ohne DB-Query)
- Automatischer Georef-Lauf (run_auto_georef_pass) verarbeitet pending Papers
"""

import base64
import datetime
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

# Windows-Konsole: UTF-8 erzwingen (cp1252 kann Pfeile/Umlaute nicht)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_georef_")) / "smoke.sqlite3"
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
sys.argv = ["manage.py", "smoke_georef"]

django.setup()

from django.core.cache import cache  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext, setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from insight_core.models import (  # noqa: E402
    OParlBody,
    OParlFile,
    OParlLocation,
    OParlPaper,
    OParlPerson,
    OParlSource,
    Street,
)
from insight_core.services.gazetteer import (  # noqa: E402
    StreetGazetteer,
    get_person_name_set,
    normalize_street_name,
)
from insight_core.services.georef_runner import run_auto_georef_pass  # noqa: E402
from insight_core.services.georeferencing import (  # noqa: E402
    extract_with_gazetteer,
    process_paper_georef,
    update_paper_georef,
)
from insight_core.services.oparl_locations import apply_oparl_locations  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# =============================================================================
# Testdaten
# =============================================================================

source = OParlSource.objects.create(
    id=uuid.uuid4(),
    url="https://smoke.example.org/system",
    name="Smoke-Quelle",
)
body = OParlBody.objects.create(
    id=uuid.uuid4(),
    external_id="https://smoke.example.org/body/1",
    source=source,
    name="Smokestadt",
    slug="smokestadt",
    latitude=51.96,
    longitude=7.63,
    bbox_south=51.85,
    bbox_north=52.06,
    bbox_west=7.47,
    bbox_east=7.77,
    osm_relation_id=999999,
)

_streets = [
    ("Wolbecker Straße", 51.9550, 7.6500),
    ("Neubrückenstraße", 51.9650, 7.6300),
    ("Brückenstraße", 51.9600, 7.6200),
    ("Johann-Krane-Weg", 51.9400, 7.6100),
    ("Kamp", 51.9580, 7.6280),  # Konflikt: auch Nachname eines Ratsmitglieds
    ("Hüfferstraße", 51.9610, 7.6130),
]
for i, (name, lat, lon) in enumerate(_streets):
    Street.objects.create(
        body=body,
        osm_id=1000 + i,
        name=name,
        normalized_name=normalize_street_name(name),
        latitude=lat,
        longitude=lon,
    )

OParlPerson.objects.create(
    id=uuid.uuid4(),
    external_id="https://smoke.example.org/person/1",
    body=body,
    name="Erika Kamp",
    family_name="Kamp",
    given_name="Erika",
)


def make_paper(ref: str, text: str) -> OParlPaper:
    paper = OParlPaper.objects.create(
        id=uuid.uuid4(),
        external_id=f"https://smoke.example.org/paper/{ref}",
        body=body,
        name=f"Vorlage {ref}",
        reference=ref,
        date=datetime.date(2026, 7, 1),
    )
    OParlFile.objects.create(
        id=uuid.uuid4(),
        external_id=f"https://smoke.example.org/file/{ref}",
        body=body,
        paper=paper,
        name=f"Anlage {ref}",
        text_content=text,
        text_extraction_status="completed",
    )
    return paper


# =============================================================================
# 1. Normalisierung
# =============================================================================
print("\n[1] Straßennamen-Normalisierung")
check("str.-Abkürzung", normalize_street_name("Wolbecker Str.") == "wolbecker strasse")
check("ß→ss + Case", normalize_street_name("WOLBECKER STRASSE") == normalize_street_name("Wolbecker Straße"))
check("Bindestriche", normalize_street_name("Johann-Krane-Weg") == "johann krane weg")
check("Suffix-Abkürzung angehängt", normalize_street_name("Schillerstr.") == "schillerstrasse")

# =============================================================================
# 2. Gazetteer-Matching
# =============================================================================
print("\n[2] Gazetteer-Matching")
gazetteer = StreetGazetteer(body)
person_names = get_person_name_set(body)
check("Gazetteer geladen", len(gazetteer) == len(_streets))

text_1 = (
    "Antrag zur Sanierung der Wolbecker Str. 45 in Smokestadt.\n"
    "Außerdem soll der Johann-Krane-Weg umgestaltet werden.\n"
    "Frau Kamp hat den Antrag eingebracht."
)
hits_1 = extract_with_gazetteer(text_1, gazetteer, person_names)
names_1 = {h["name"] for h in hits_1}
check("Straße mit Hausnummer erkannt", "Wolbecker Straße 45" in names_1, str(names_1))
check(
    "Hausnummer → source=address_match",
    any(h["source"] == "address_match" and h["name"].startswith("Wolbecker") for h in hits_1),
)
check("Bindestrich-Straße erkannt", "Johann-Krane-Weg" in names_1, str(names_1))
check(
    "Personenname nicht als Straße gewertet (MST-Trick)",
    not any(h["name"] == "Kamp" for h in hits_1),
    str(names_1),
)
check("Konfidenz gesetzt", all(0 < h.get("confidence", 0) <= 1 for h in hits_1))

# Schreibvarianten
text_2 = "Beleuchtung WOLBECKER STRASSE und am Johann Krane Weg prüfen."
names_2 = {h["name"] for h in extract_with_gazetteer(text_2, gazetteer, person_names)}
check("Großschreibung-Variante", "Wolbecker Straße" in names_2, str(names_2))
check("Leerzeichen statt Bindestrich", "Johann-Krane-Weg" in names_2, str(names_2))

# Longest-Match-Disambiguierung
text_3 = "Umbau der Neubrückenstraße 3 im Zentrum."
hits_3 = extract_with_gazetteer(text_3, gazetteer, person_names)
names_3 = {h["name"] for h in hits_3}
check("Longest-Match: Neubrückenstraße gewinnt", "Neubrückenstraße 3" in names_3, str(names_3))
check(
    "Kein Teil-Match Brückenstraße",
    not any("Brückenstraße" == h["name"].split(" ")[0] and h["name"].startswith("Brücken") for h in hits_3),
    str(names_3),
)

# Straße MIT Hausnummer, wenn Person gleichnamig: zählt trotzdem
text_4 = "Baustelle Kamp 12 wird eingerichtet."
names_4 = {h["name"] for h in extract_with_gazetteer(text_4, gazetteer, person_names)}
check("Personen-Straße mit Hausnummer zählt", "Kamp 12" in names_4, str(names_4))

# =============================================================================
# 3. Pipeline (process_paper_georef, lokal ohne Netz)
# =============================================================================
print("\n[3] Pipeline: process_paper_georef (mode=regex, Gazetteer)")
paper_1 = make_paper("2026/001", text_1)
result = process_paper_georef(paper_1, mode="regex")
check("Status completed", result["status"] == "completed", str(result))
check("Methode gazetteer", result.get("method") == "gazetteer", str(result.get("method")))
update_paper_georef(paper_1, result)
paper_1.refresh_from_db()
check("locations gespeichert", bool(paper_1.locations))
check(
    "Koordinaten im Body-BBox",
    all(51.85 <= loc["lat"] <= 52.06 and 7.47 <= loc["lon"] <= 7.77 for loc in paper_1.locations),
)

# =============================================================================
# 4. OParl-Locations: Quelle höchster Priorität
# =============================================================================
print("\n[4] OParl-Locations (source=oparl)")
paper_2 = make_paper("2026/002", "Neubau einer Kita an der Hüfferstraße 27.")
oparl_loc = OParlLocation.objects.create(
    id=uuid.uuid4(),
    external_id="https://smoke.example.org/location/1",
    body=body,
    street_address="Hüfferstraße 27",
    geojson={"type": "Feature", "geometry": {"type": "Point", "coordinates": [7.6131, 51.9611]}},
)
paper_2.oparl_locations.add(oparl_loc)

# Manueller Eintrag existiert bereits (weit genug entfernt, >50m)
manual_entry = {
    "lat": 51.9700,
    "lon": 7.7000,
    "name": "Manuell gepflegter Ort",
    "source": "manual",
    "confidence": 1.0,
}
paper_2.locations = [manual_entry]
paper_2.save(update_fields=["locations"])

changed = apply_oparl_locations(paper_2)
paper_2.refresh_from_db()
sources = [loc.get("source") for loc in paper_2.locations]
check("Backfill hat geändert", changed)
check("oparl-Eintrag vorhanden", "oparl" in sources, str(sources))
check("oparl steht vorn (höchste Priorität)", sources[0] == "oparl", str(sources))
check("Manueller Eintrag bleibt erhalten", "manual" in sources, str(sources))
check("Backfill idempotent", not apply_oparl_locations(paper_2))

# Extraktion darf oparl/manual nicht zerstören; Extraktions-Treffer <50m neben
# dem oparl-Punkt wird wegdedupliziert (oparl gewinnt)
result_2 = process_paper_georef(paper_2, mode="regex")
update_paper_georef(paper_2, result_2)
paper_2.refresh_from_db()
sources_2 = [loc.get("source") for loc in paper_2.locations]
check("Re-Run: oparl bleibt", "oparl" in sources_2, str(sources_2))
check("Re-Run: manual bleibt", "manual" in sources_2, str(sources_2))
check(
    "Dedup <50m: kein doppelter Hüfferstraßen-Punkt",
    sum(1 for loc in paper_2.locations if "Hüfferstraße" in (loc.get("name") or "")) == 1,
    str(paper_2.locations),
)

# =============================================================================
# 5. Automatischer Georef-Lauf
# =============================================================================
print("\n[5] Automatischer Georef-Lauf (run_auto_georef_pass)")
paper_3 = make_paper("2026/003", "Radweg an der Brückenstraße erneuern.")
stats = run_auto_georef_pass(limit=10)
paper_3.refresh_from_db()
check("Lauf hat Papers verarbeitet", stats.get("processed", 0) >= 1, str(stats))
check("pending Paper wurde georeferenziert", paper_3.georef_status == "completed", paper_3.georef_status)
check("Brückenstraße gefunden", any("Brückenstraße" in (loc.get("name") or "") for loc in paper_3.locations or []))
check("Lock freigegeben (2. Lauf möglich)", run_auto_georef_pass(limit=1).get("skipped") != "lock")

# =============================================================================
# 6. Öffentliche Paper-Detailseite: Ortsliste + Karte
# =============================================================================
print("\n[6] Paper-Detailseite")
client = Client()
resp = client.get(f"/insight/vorgaenge/{paper_2.id}/")
html = resp.content.decode("utf-8")
check("Detailseite lädt", resp.status_code == 200, str(resp.status_code))
check("Karte gerendert (paper-map)", 'id="paper-map"' in html)
check("Ortsname gelistet", "Hüfferstraße 27" in html)
check("Leaflet eingebunden", "vendor/leaflet/leaflet.js" in html)
check("offiziell-Badge für oparl-Quelle", ">offiziell<" in html)

paper_no_loc = make_paper("2026/004", "Allgemeiner Bericht ohne Ortsbezug.")
resp_2 = client.get(f"/insight/vorgaenge/{paper_no_loc.id}/")
html_2 = resp_2.content.decode("utf-8")
check("Ohne Orte keine Karte", 'id="paper-map"' not in html_2)

# =============================================================================
# 7. map_markers: BBox + Cache
# =============================================================================
print("\n[7] map_markers: BBox-Filter + Server-Cache")
cache.clear()
resp = client.get("/insight/karte/partials/markers/?all=1")
data = resp.json()
check("Marker-Endpoint lädt", resp.status_code == 200)
check("Marker vorhanden", len(data["features"]) >= 3, str(len(data.get("features", []))))
check("truncated-Flag vorhanden", "truncated" in data)

# Cache: 2. Aufruf ohne Paper-Query
with CaptureQueriesContext(connection) as ctx:
    resp_cached = client.get("/insight/karte/partials/markers/?all=1")
paper_queries = [q for q in ctx.captured_queries if "oparl_papers" in q["sql"]]
check("2. Aufruf aus Cache (keine Paper-Query)", len(paper_queries) == 0, f"{len(paper_queries)} Queries")
check("Cache liefert identische Features", resp_cached.json()["features"] == data["features"])

# BBox-Filter: nur der manuelle Ort (51.97, 7.70) liegt im Ausschnitt
resp_bbox = client.get("/insight/karte/partials/markers/?all=1&bbox=7.69,51.96,7.71,51.98")
bbox_features = resp_bbox.json()["features"]
check("BBox-Filter greift", 1 <= len(bbox_features) < len(data["features"]), str(len(bbox_features)))
check(
    "BBox-Features im Ausschnitt",
    all(7.69 <= f["geometry"]["coordinates"][0] <= 7.71 for f in bbox_features),
)
resp_bad = client.get("/insight/karte/partials/markers/?all=1&bbox=kaputt")
check(
    "Ungültige BBox ignoriert",
    resp_bad.status_code == 200 and len(resp_bad.json()["features"]) == len(data["features"]),
)

# =============================================================================
# Ergebnis
# =============================================================================
print(f"\n{'=' * 60}")
print(f"Ergebnis: {PASS} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
