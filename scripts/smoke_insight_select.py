# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Kommune-Auswahl & öffentliche Insight-Seiten (Werbe-Reife).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_insight_select.py

Prüfungen:
  1. Kommune-Auswahl (leer / 1 Kommune / 8 Kommunen):
     - 0 Kommunen: freundlicher Empty-State statt Fehler
     - 1 Kommune (Self-Hosting): automatische Auswahl + Redirect, kein Auswahlzwang
     - 8 Kommunen: Auswahlseite mit Suchfeld, Karten (Name, Bundesland aus AGS,
       Kennzahlen aus echten Daten), "Alle Kommunen durchsuchen", ohne Wappen/Logos
  2. set_body-/clear_body-Flow: Session-Persistenz, Redirects, HTMX-HX-Redirect
  3. Kommunenübergreifende Suche im "Alle Kommunen"-Modus (Django-Fallback)
  4. Kommune-Filter: Listen-Partials liefern nur Daten der aktiven Kommune
  5. Anonyme Render-Smokes aller öffentlichen Insight-Seiten mit realistischen
     Fixtures: Status < 400, Title-/Meta-/OG-Tags gesetzt, Query-Budget der
     Auswahlseite wächst nicht linear mit der Kommunen-Zahl (N+1-Guard)
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_select_")) / "smoke.sqlite3"
_media_root = _db_path.parent / "media"
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

sys.argv = ["manage.py", "smoke_insight_select"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext, setup_test_environment  # noqa: E402

setup_test_environment()
_overrides = override_settings(MEDIA_ROOT=str(_media_root))
_overrides.enable()
call_command("migrate", verbosity=0, interactive=False)

from django.utils import timezone  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlBody,
    OParlFile,
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
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def html(resp):
    return resp.content.decode("utf-8", errors="replace")


# =============================================================================
# 1. Leerer Zustand: keine Kommune synchronisiert
# =============================================================================
print("=== 1. Auswahl ohne Kommunen (Empty-State) ===")

client = Client()
resp = client.get("/insight/")
check("GET /insight/ ohne Kommunen: 200", resp.status_code == 200, f"status={resp.status_code}")
check("Empty-State freundlich", "Noch keine Kommune verf" in html(resp))
check("Kein Karten-Grid", 'data-search="' not in html(resp))

# =============================================================================
# 2. Self-Hosting-Fall: genau eine Kommune → Auto-Auswahl
# =============================================================================
print("=== 2. Eine Kommune: automatische Auswahl ===")

source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
solo = OParlBody.objects.create(
    external_id="https://ris.example.org/body/solo",
    source=source,
    name="Solostadt",
    slug="solostadt",
    ags="05515000",
)

client = Client()
resp = client.get("/insight/")
check("GET /insight/ mit 1 Kommune: Redirect", resp.status_code == 302, f"status={resp.status_code}")
check("Session auf Kommune gesetzt", client.session.get("active_body_id") == str(solo.id))
resp = client.get("/insight/")
check("Nach Auto-Auswahl: Kommune-Home 200", resp.status_code == 200, f"status={resp.status_code}")
check("Kommune-Home zeigt Kommune", "Solostadt" in html(resp))
check("Kein Auswahlzwang (keine Auswahlseite)", "hle deine Kommune" not in html(resp))

# =============================================================================
# 3. Acht Kommunen: Auswahlseite als Aushängeschild
# =============================================================================
print("=== 3. Acht Kommunen: Auswahlseite ===")

CITY_SPECS = [
    ("Musterhausen", "05315000"),  # NRW
    ("Beispielfurt", "09162000"),  # Bayern
    ("Nordersiel", "01001000"),  # Schleswig-Holstein
    ("Elbwiesen", "02000000"),  # Hamburg
    ("Rheintal", "07111000"),  # Rheinland-Pfalz
    ("Neckarstadt", "08111000"),  # Baden-Württemberg
    ("Spreeblick", "11000000"),  # Berlin
    ("Saalemünde", None),  # ohne AGS → Fallback
]

bodies = [solo]
for i, (city_name, ags) in enumerate(CITY_SPECS[1:], start=2):
    bodies.append(
        OParlBody.objects.create(
            external_id=f"https://ris.example.org/body/{i}",
            source=source,
            name=city_name,
            slug=f"stadt-{i}",
            ags=ags,
            classification="Kreisfreie Stadt",
        )
    )
solo.name = CITY_SPECS[0][0]
solo.save()

# Realistische Fixtures für die erste Kommune (Musterhausen)
body_a, body_b = bodies[0], bodies[1]
now = timezone.now()
org_a = OParlOrganization.objects.create(
    external_id="https://ris.example.org/organization/a1",
    body=body_a,
    name="Rat",
)
person_a = OParlPerson.objects.create(
    external_id="https://ris.example.org/person/a1",
    body=body_a,
    name="Erika Beispiel",
    family_name="Beispiel",
    given_name="Erika",
)
OParlMembership.objects.create(
    external_id="https://ris.example.org/membership/a1",
    person=person_a,
    organization=org_a,
    role="Ratsmitglied",
)
meeting_a = OParlMeeting.objects.create(
    external_id="https://ris.example.org/meeting/a1",
    body=body_a,
    name="Ratssitzung Musterhausen",
    start=now + timedelta(days=3),
)
meeting_a.organizations.add(org_a)
paper_a = OParlPaper.objects.create(
    external_id="https://ris.example.org/paper/a1",
    body=body_a,
    name="Radwegekonzept Innenstadt",
    reference="V/2026/001",
    date=now.date(),
)
OParlFile.objects.create(
    external_id="https://ris.example.org/file/a1",
    body=body_a,
    paper=paper_a,
    name="Radwegekonzept.pdf",
    file_name="radwegekonzept.pdf",
)
# Daten in einer zweiten Kommune (für Filter- und Cross-Suche-Checks)
meeting_b = OParlMeeting.objects.create(
    external_id="https://ris.example.org/meeting/b1",
    body=body_b,
    name="Stadtratssitzung Beispielfurt",
    start=now + timedelta(days=5),
)
paper_b = OParlPaper.objects.create(
    external_id="https://ris.example.org/paper/b1",
    body=body_b,
    name="Klimaschutzprogramm Beispielfurt",
    reference="B/2026/077",
    date=now.date(),
)

client = Client()
resp = client.get("/insight/")
page = html(resp)
check("GET /insight/ mit 8 Kommunen: 200 (Auswahlseite)", resp.status_code == 200, f"status={resp.status_code}")
check("Headline vorhanden", "hle deine Kommune" in page)
check("Suchfeld vorhanden", 'id="body-select-search"' in page)
check("Alle 8 Kommunen gelistet", all(name in page for name, _ in CITY_SPECS))
check("Client-Filter-Attribute (data-search)", page.count("data-search") >= 8)
check("Bundesland aus AGS (NRW)", "Nordrhein-Westfalen" in page)
check("Bundesland aus AGS (Bayern)", "Bayern" in page)
check("Fallback ohne AGS (classification)", "Kreisfreie Stadt" in page)
check("Kennzahlen aus echten Daten", ">1</strong> Vorg" in page.replace("\n", ""))
check("'Alle Kommunen durchsuchen' vorhanden", "Alle Kommunen durchsuchen" in page)
check("Keine Wappen/Logos auf Karten", "bodies/logos" not in page)
check("Set-Body-Links vorhanden", f"/insight/kommune/{body_a.id}/" in page)

# SEO der Auswahlseite
check("SEO: <title> gesetzt", "<title>Kommune w" in page)
check("SEO: Meta-Description", 'name="description" content="W' in page)
check("SEO: Canonical", 'rel="canonical"' in page)
check("SEO: og:title", 'property="og:title"' in page)

# N+1-Guard: Query-Anzahl der Auswahlseite ist konstant (gruppierte Counts)
client_q = Client()
with CaptureQueriesContext(connection) as ctx:
    client_q.get("/insight/")
check(
    "Query-Budget Auswahlseite (< 25 Queries bei 8 Kommunen)",
    len(ctx.captured_queries) < 25,
    f"queries={len(ctx.captured_queries)}",
)

# =============================================================================
# 4. set_body / clear_body Flow
# =============================================================================
print("=== 4. set_body / clear_body ===")

resp = client.get(f"/insight/kommune/{body_a.id}/")
check("set_body: Redirect", resp.status_code == 302, f"status={resp.status_code}")
check("set_body: Session persistiert", client.session.get("active_body_id") == str(body_a.id))
resp = client.get("/insight/")
page = html(resp)
check("Nach Auswahl: Kommune-Home 200", resp.status_code == 200, f"status={resp.status_code}")
check("Kommune-Home zeigt Musterhausen", "Musterhausen transparent" in page)
check("Kommende Sitzung sichtbar", f"/insight/termine/{meeting_a.id}/" in page)
check("Neuester Vorgang sichtbar", "Radwegekonzept Innenstadt" in page)
check("'Kommune wechseln' erreichbar", "Kommune wechseln" in page)

# HTMX-Variante
resp = client.get(f"/insight/kommune/{body_b.id}/", HTTP_HX_REQUEST="true")
check("set_body (HTMX): 200 + HX-Redirect", resp.status_code == 200 and resp.headers.get("HX-Redirect"))
check("set_body (HTMX): Session gewechselt", client.session.get("active_body_id") == str(body_b.id))

# clear_body → zurück zur Auswahl
resp = client.get("/insight/kommune/alle/")
check("clear_body: Redirect", resp.status_code == 302, f"status={resp.status_code}")
check("clear_body: Session 'all'", client.session.get("active_body_id") == "all")
resp = client.get("/insight/")
check("Nach clear_body: Auswahlseite", "hle deine Kommune" in html(resp))

# Unbekannte Body-ID: kein Crash
import uuid as uuid_mod  # noqa: E402

resp = client.get(f"/insight/kommune/{uuid_mod.uuid4()}/")
check("set_body unbekannte ID: Redirect ohne Fehler", resp.status_code == 302, f"status={resp.status_code}")

# =============================================================================
# 4b. "Alle Kommunen"-Modus: Kommune-gebundene Seiten leiten zur Auswahl
# =============================================================================
print("=== 4b. Alle-Kommunen-Modus: Redirects ===")

# Session steht auf "all" (clear_body oben)
for url in [
    "/insight/termine/",
    "/insight/vorgaenge/",
    "/insight/gremien/",
    "/insight/personen/",
    "/insight/dokumente/",
    "/insight/karte/",
    "/insight/nachbarschaft/",
    "/insight/termine/kalender/",
    "/insight/benachrichtigungen/",
    "/insight/chat/",
]:
    resp = client.get(url)
    check(
        f"All-Modus: {url} → Auswahlseite",
        resp.status_code == 302 and resp.headers.get("Location", "").endswith("/insight/"),
        f"status={resp.status_code} loc={resp.headers.get('Location')}",
    )

# Suche bleibt kommunenübergreifend erreichbar (gleichwertige Option)
resp = client.get("/insight/suche/")
check("All-Modus: Suche bleibt erreichbar (200)", resp.status_code == 200, f"status={resp.status_code}")
# Merkliste ist Kommune-unabhängig
resp = client.get("/insight/gespeichert/")
check("All-Modus: Merkliste bleibt erreichbar (200)", resp.status_code == 200, f"status={resp.status_code}")
check("Merkliste: noindex gesetzt", "noindex" in html(resp))

# =============================================================================
# 4c. robots.txt + Sitemap-Index
# =============================================================================
print("=== 4c. robots.txt + Sitemap-Index ===")

resp = client.get("/robots.txt")
check("robots.txt: 200", resp.status_code == 200, f"status={resp.status_code}")
check("robots.txt: Sitemap-Verweis", "Sitemap:" in html(resp) and "sitemap-insight-index.xml" in html(resp))
resp = client.get("/sitemap-insight-index.xml")
check("Sitemap-Index: 200", resp.status_code == 200, f"status={resp.status_code}")
check(
    "Sitemap-Index: listet Body-Sitemaps",
    "<sitemapindex" in html(resp) and "sitemap-insight-solostadt.xml" in html(resp),
)
resp = client.get("/sitemap-insight-solostadt.xml")
check("Body-Sitemap: 200", resp.status_code == 200, f"status={resp.status_code}")


# =============================================================================
# 5. Kommunenübergreifende Suche im "Alle Kommunen"-Modus
# =============================================================================
print("=== 5. Kommunenübergreifende Suche ===")

# Session steht auf "all" (clear_body oben). Elasticsearch ist nicht erreichbar
# → Django-Fallback. Treffer aus Kommune B müssen erscheinen, obwohl Kommune A
# die "erste" Kommune ist.
resp = client.get("/insight/suche/partials/results/", {"q": "Klimaschutzprogramm"})
check("Suche (alle Kommunen): 200", resp.status_code == 200, f"status={resp.status_code}")
check("Treffer aus Kommune B enthalten", "Klimaschutzprogramm Beispielfurt" in html(resp))
resp = client.get("/insight/suche/partials/results/", {"q": "Radwegekonzept"})
check("Treffer aus Kommune A enthalten", "Radwegekonzept Innenstadt" in html(resp))

# Mit gewählter Kommune A: nur A-Treffer
client.get(f"/insight/kommune/{body_a.id}/")
resp = client.get("/insight/suche/partials/results/", {"q": "Klimaschutzprogramm"})
check("Suche (Kommune A): kein Fremdtreffer", "Klimaschutzprogramm Beispielfurt" not in html(resp))

# =============================================================================
# 6. Kommune-Filter in Listen-Partials
# =============================================================================
print("=== 6. Kommune-Filter Listen ===")

resp = client.get("/insight/termine/partials/list/")
check("MeetingListPartial: nur aktive Kommune", "Stadtratssitzung Beispielfurt" not in html(resp))
resp = client.get("/insight/termine/", HTTP_HX_REQUEST="true")
check("MeetingList (HTMX): nur aktive Kommune", "Stadtratssitzung Beispielfurt" not in html(resp))

# =============================================================================
# 7. Anonyme Render-Smokes der öffentlichen Seiten (mit Kommune A)
# =============================================================================
print("=== 7. Öffentliche Seiten (anonym, Kommune A) ===")

PUBLIC_PAGES = [
    ("/insight/", "Portal-Home"),
    ("/insight/gremien/", "Gremien-Liste"),
    (f"/insight/gremien/{org_a.id}/", "Gremium-Detail"),
    ("/insight/personen/", "Personen-Liste"),
    (f"/insight/personen/{person_a.id}/", "Person-Detail"),
    (f"/insight/personen/{person_a.id}/frage-stellen/", "Frage stellen"),
    ("/insight/vorgaenge/", "Vorgänge-Liste"),
    (f"/insight/vorgaenge/{paper_a.id}/", "Vorgang-Detail"),
    ("/insight/termine/", "Termine-Liste"),
    ("/insight/termine/kalender/", "Kalender"),
    (f"/insight/termine/{meeting_a.id}/", "Termin-Detail"),
    ("/insight/dokumente/", "Dokumente-Liste"),
    ("/insight/suche/", "Suche"),
    ("/insight/karte/", "Karte"),
    ("/insight/nachbarschaft/", "Nachbarschaft"),
    ("/insight/gespeichert/", "Merkliste"),
    ("/insight/benachrichtigungen/", "Benachrichtigungen"),
    ("/insight/chat/", "KI-Chat"),
    ("/sitemap-insight-solostadt.xml", "Body-Sitemap"),
]

anon = Client()
anon.get(f"/insight/kommune/{body_a.id}/")
for url, label in PUBLIC_PAGES:
    try:
        resp = anon.get(url)
    except Exception as exc:
        check(f"{label} ({url})", False, f"EXCEPTION {type(exc).__name__}: {exc}")
        continue
    check(f"{label} ({url}) < 400", resp.status_code < 400, f"status={resp.status_code}")
    if resp.status_code == 200 and "xml" not in url:
        content = html(resp)
        check(f"{label}: <title> nicht leer", "<title> | Mandari" not in content and "<title>" in content)

# Leere Kommune (ohne Daten): freundliche Empty-States statt Fehler
anon_empty = Client()
empty_body = bodies[-1]  # Saalemünde, ohne Fixtures
anon_empty.get(f"/insight/kommune/{empty_body.id}/")
for url, label in [
    ("/insight/", "Portal-Home leer"),
    ("/insight/gremien/", "Gremien leer"),
    ("/insight/vorgaenge/", "Vorgänge leer"),
    ("/insight/termine/", "Termine leer"),
    ("/insight/personen/", "Personen leer"),
]:
    resp = anon_empty.get(url)
    check(f"{label} ({url}) 200", resp.status_code == 200, f"status={resp.status_code}")

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
