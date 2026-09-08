# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Öffentlicher Sitzungsplan + ICS-Abo in Insight (Issue #82, Rest).

- Jahresübersicht je Gremium × Monat, abgesagte Termine markiert, Jahreswechsel
- ?kommune=<uuid> wählt die Kommune (Deep-Link aus dem Session-RIS)
- ICS-Feed (gesamt / je Gremium), abgesagte Sitzungen als STATUS:CANCELLED
- Kalenderseite verlinkt Jahresplan und Abo; Events-Endpoint filtert je Gremium
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

_tmp = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'smoke.sqlite3').as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["SITE_URL"] = "https://insight.example"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlSource  # noqa: E402

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


def html(resp):
    return resp.content.decode("utf-8", errors="replace")


# =============================================================================
print("=== Setup ===")
now = timezone.now()
year = now.year + 1
source = OParlSource.objects.create(name="RIS", url="https://ris.example/oparl/system")
body = OParlBody.objects.create(
    external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Planhausen", slug="planhausen"
)
other = OParlBody.objects.create(
    external_id="https://ris.example/oparl/body/2", source=source, name="Anderswo", slug="anderswo"
)
rat = OParlOrganization.objects.create(
    external_id="https://ris.example/oparl/org/rat", body=body, name="Rat", short_name="Rat"
)
bau = OParlOrganization.objects.create(external_id="https://ris.example/oparl/org/bau", body=body, name="Bauausschuss")


def meeting(num, org, when, cancelled=False, b=None):
    m = OParlMeeting.objects.create(
        external_id=f"https://ris.example/oparl/meeting/{num}",
        body=b or body,
        name=f"Sitzung {num}",
        start=when,
        end=when + timedelta(hours=2),
        location_name="Rathaus",
        cancelled=cancelled,
    )
    if org:
        m.organizations.add(org)
    return m


jan = timezone.make_aware(timezone.datetime(year, 1, 12, 17, 0))
m1 = meeting(1, rat, jan)
m2 = meeting(2, rat, timezone.make_aware(timezone.datetime(year, 3, 9, 17, 0)))
m3 = meeting(3, rat, timezone.make_aware(timezone.datetime(year, 6, 15, 17, 0)), cancelled=True)
m4 = meeting(4, bau, timezone.make_aware(timezone.datetime(year, 2, 3, 16, 30)))
m5 = meeting(5, None, timezone.make_aware(timezone.datetime(year, 5, 20, 18, 0)))
m_prev = meeting(6, rat, timezone.make_aware(timezone.datetime(year - 1, 11, 5, 17, 0)))
m_other = meeting(7, None, jan, b=other)
m_soon = meeting(8, bau, now + timedelta(days=10))

client = Client()
sess = client.session
sess["active_body_id"] = str(body.id)
sess.save()

# =============================================================================
print()
print("=== 1. Jahresplan ===")
resp = client.get(f"/insight/termine/jahresplan/?year={year}")
page = html(resp)
check("Jahresplan -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Titel + Zähler", f"Sitzungsplan {year}" in page and "5 Termine" in page and "1 abgesagt" in page)
check("Zeilen je Gremium + Sonstige", "Bauausschuss" in page and ">Rat<" in page and "Sonstige Sitzungen" in page)
check("Tages-Chips verlinken Sitzungen", f"/insight/termine/{m1.id}/" in page and ">12.<" in page and ">9.<" in page)
check("Abgesagte Sitzung durchgestrichen", "line-through" in page and f"/insight/termine/{m3.id}/" in page)
check("Fremde Kommune nicht enthalten", f"/insight/termine/{m_other.id}/" not in page)
check("Vorjahres-Sitzung nicht enthalten", f"/insight/termine/{m_prev.id}/" not in page)
check("Jahreswechsel-Links", f"?year={year - 1}" in page and f"?year={year + 1}" in page)
check("Abo-Link je Gremium", f"kalender.ics?gremium={rat.id}" in page)
page_prev = html(client.get(f"/insight/termine/jahresplan/?year={year - 1}"))
check("Vorjahr zeigt die Vorjahres-Sitzung", f"/insight/termine/{m_prev.id}/" in page_prev)
page_empty = html(client.get(f"/insight/termine/jahresplan/?year={year + 5}"))
check("Leeres Jahr: Hinweis auf Veröffentlichung", "noch keine Sitzungstermine" in page_empty)
check(
    "Ungültiges Jahr fällt auf aktuelles zurück", client.get("/insight/termine/jahresplan/?year=abc").status_code == 200
)

fresh = Client()
resp = fresh.get(f"/insight/termine/jahresplan/?kommune={other.id}&year={year}")
check(
    "?kommune wählt Kommune für neue Sitzung",
    resp.status_code == 200
    and f"/insight/termine/{m_other.id}/" in html(resp)
    and fresh.session.get("active_body_id") == str(other.id),
)

# =============================================================================
print()
print("=== 2. ICS-Abo ===")
resp = client.get("/insight/termine/kalender.ics")
ics = resp.content.decode("utf-8").replace("\r\n ", "")  # ICS-Zeilenfaltung aufheben
check("Feed -> 200 text/calendar", resp.status_code == 200 and resp["Content-Type"].startswith("text/calendar"))
check("Kalendername enthält Kommune", "X-WR-CALNAME:Stadt Planhausen" in ics)
check("Enthält kommende Sitzungen (nächstes Jahr)", f"meeting-{m1.id}@" in ics and f"meeting-{m_soon.id}@" in ics)
check("Abgesagt als STATUS:CANCELLED", "STATUS:CANCELLED" in ics and "STATUS:CONFIRMED" in ics)
check("Fremde Kommune nicht im Feed", f"meeting-{m_other.id}@" not in ics)
check("Ort + Link in der Beschreibung", "LOCATION:Rathaus" in ics and f"/insight/termine/{m1.id}/" in ics)
resp = client.get(f"/insight/termine/kalender.ics?gremium={bau.id}")
ics_bau = resp.content.decode("utf-8")
check(
    "Feed je Gremium filtert",
    f"meeting-{m4.id}@" in ics_bau and f"meeting-{m1.id}@" not in ics_bau and "Bauausschuss" in ics_bau,
)
check("Unbekanntes Gremium -> 404", client.get(f"/insight/termine/kalender.ics?gremium={other.id}").status_code == 404)
resp = fresh.get(f"/insight/termine/kalender.ics?kommune={body.id}")
check("?kommune im Feed (ohne Session)", resp.status_code == 200 and f"meeting-{m1.id}@" in resp.content.decode())

# =============================================================================
print()
print("=== 3. Kalenderseite + Events ===")
page = html(client.get("/insight/termine/kalender/"))
check("Kalender verlinkt Jahresplan + Abo", "/insight/termine/jahresplan/" in page and "kalender.ics" in page)
check("Gremien-Auswahl für Abo", "Bauausschuss" in page and str(bau.id) in page)
events = client.get(
    f"/insight/termine/partials/calendar-events/?gremium={rat.id}&start={year}-01-01T00:00:00&end={year}-12-31T23:59:59"
).json()
check(
    "Events je Gremium gefiltert (ohne Abgesagte)",
    {e["id"] for e in events} == {str(m1.id), str(m2.id)},
    str([e["title"] for e in events]),
)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
