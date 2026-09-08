# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Beschlusskontrolle für Mandatsträger in Work (Issue #37, Sichtbarkeit).

- Öffentliche Beschlüsse der verknüpften Verwaltung mit Umsetzungsstand, Ampel, Zuständigkeit, Frist
- Nicht-öffentliche TOPs/Sitzungen und abgesetzte TOPs erscheinen nie
- Filter: Gremium, Jahr, Umsetzungsstand, überfällig, Suche
- Ohne Session-Mandant: Hinweis; ohne Recht ris.view: kein Zugriff
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

from apps.accounts.models import User  # noqa: E402
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from insight_core.models import OParlBody, OParlSource  # noqa: E402

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


def login(user):
    c = Client()
    c.force_login(user)
    return c


print("=== Setup ===")
source = OParlSource.objects.create(name="RIS", url="https://ris.example/oparl/system")
body = OParlBody.objects.create(external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Planhausen")
body_other = OParlBody.objects.create(external_id="https://ris.example/oparl/body/2", source=source, name="Anderswo")
org = Organization.objects.create(name="Fraktion Zukunft", slug="fraktion-zukunft", body=body)
org_other = Organization.objects.create(name="Fraktion Anderswo", slug="fraktion-anderswo", body=body_other)
admin_role = Role.objects.filter(organization=org, is_admin=True).first()
u_admin = User.objects.create_user(email="vorsitz@zukunft.example", password="pw-Smoke-1!")
m_admin = Membership.objects.create(user=u_admin, organization=org)
m_admin.roles.add(admin_role)
c_admin = login(u_admin)
u_other = User.objects.create_user(email="andere@anderswo.example", password="pw-Smoke-1!")
m_other = Membership.objects.create(user=u_other, organization=org_other)
m_other.roles.add(Role.objects.filter(organization=org_other, is_admin=True).first())
c_other = login(u_other)
WORK = f"/work/{org.slug}"

tenant = SessionTenant.objects.create(name="Stadt Planhausen", slug="planhausen", oparl_body=body)
council = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
committee = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")
now = timezone.now()
today = timezone.localdate()

m_pub = SessionMeeting.objects.create(
    tenant=tenant, name="Rat 03/2026", organization=council, start=now - timedelta(days=120)
)
m_pub2 = SessionMeeting.objects.create(
    tenant=tenant, name="Bauausschuss 05/2026", organization=committee, start=now - timedelta(days=60)
)
m_old = SessionMeeting.objects.create(
    tenant=tenant, name="Rat 2024", organization=council, start=now.replace(year=now.year - 2)
)
m_np = SessionMeeting.objects.create(
    tenant=tenant, name="Geheime Sitzung", organization=council, start=now - timedelta(days=30), is_public=False
)


def top(meeting, number, name, **kw):
    defaults = {"vote_result": "approved", "is_public": True, "order": int(number)}
    defaults.update(kw)
    return SessionAgendaItem.objects.create(meeting=meeting, number=number, name=name, **defaults)


t_open = top(
    m_pub,
    "3",
    "Radweg Hauptstraße",
    resolution_number="B/2026/0003",
    implementation_recipient="Tiefbauamt",
    implementation_deadline=today + timedelta(days=90),
    resolution_text="Die Verwaltung plant einen Radweg.",
)
t_overdue = top(
    m_pub,
    "4",
    "Spielplatz Süd",
    implementation_status="in_progress",
    implementation_recipient="Grünflächenamt",
    implementation_deadline=today - timedelta(days=10),
    implementation_note="Ausschreibung läuft.",
)
t_done = top(
    m_pub2, "2", "Zebrastreifen Schulweg", implementation_status="done", implementation_note="Markiert am 01.08."
)
t_rejected = top(m_pub2, "5", "Parkhaus Innenstadt", vote_result="rejected")
t_old = top(m_old, "7", "Ortsdurchfahrt Sanierung", implementation_status="deferred")
t_np_item = top(m_pub, "9", "GEHEIMER-GRUNDSTUECKSKAUF", is_public=False)
t_np_meeting = top(m_np, "1", "GEHEIME-PERSONALIE")
t_withdrawn = top(m_pub, "10", "ABGESETZTER-TOP", is_withdrawn=True)
t_undecided = top(m_pub, "11", "NOCH-OFFENER-TOP", vote_result="")

print()
print("=== 1. Übersicht ===")
resp = c_admin.get(f"{WORK}/ris/decisions/")
page = html(resp)
check(
    "Seite -> 200 mit Navigation", resp.status_code == 200 and "Beschlusskontrolle" in page and "ris/decisions/" in page
)
check(
    "Öffentliche Beschlüsse gelistet",
    all(
        t in page
        for t in (
            "Radweg Hauptstraße",
            "Spielplatz Süd",
            "Zebrastreifen Schulweg",
            "Parkhaus Innenstadt",
            "Ortsdurchfahrt Sanierung",
        )
    ),
)
check(
    "Nicht-öffentliche, abgesetzte und unentschiedene TOPs fehlen",
    all(
        t not in page
        for t in ("GEHEIMER-GRUNDSTUECKSKAUF", "GEHEIME-PERSONALIE", "ABGESETZTER-TOP", "NOCH-OFFENER-TOP")
    ),
)
check(
    "Ampel: 1 offen, 1 in Umsetzung, 1 erledigt, 1 überfällig",
    resp.context["tracking_stats"]["open"] == 1
    and resp.context["tracking_stats"]["in_progress"] == 1
    and resp.context["tracking_stats"]["done"] == 1
    and resp.context["tracking_stats"]["overdue"] == 1
    and resp.context["tracking_stats"]["deferred"] == 1,
    str(resp.context["tracking_stats"]),
)
check(
    "Zuständigkeit, Frist und Vermerk sichtbar",
    "Tiefbauamt" in page and "Ausschreibung läuft." in page and "Frist" in page,
)
check("Überfällig markiert", "Überfällig" in page and "B/2026/0003" in page)
check("Abgelehnter Beschluss ohne Umsetzung", "keine Umsetzung" in page)

print()
print("=== 2. Filter ===")
resp = c_admin.get(f"{WORK}/ris/decisions/?overdue=1")
page = html(resp)
check("Filter überfällig", "Spielplatz Süd" in page and "Radweg Hauptstraße" not in page)
resp = c_admin.get(f"{WORK}/ris/decisions/?status=done")
page = html(resp)
check("Filter erledigt", "Zebrastreifen" in page and "Spielplatz" not in page and "Parkhaus" not in page)
resp = c_admin.get(f"{WORK}/ris/decisions/?organization={committee.id}")
page = html(resp)
check("Filter Gremium", "Zebrastreifen" in page and "Parkhaus" in page and "Radweg" not in page)
resp = c_admin.get(f"{WORK}/ris/decisions/?year={now.year - 2}")
page = html(resp)
check("Filter Jahr (frühere Wahlperiode)", "Ortsdurchfahrt" in page and "Radweg" not in page)
resp = c_admin.get(f"{WORK}/ris/decisions/?q=Grünflächenamt")
page = html(resp)
check("Suche nach zuständigem Amt", "Spielplatz Süd" in page and "Radweg" not in page)
resp = c_admin.get(f"{WORK}/ris/decisions/?q=GEHEIM")
check(
    "Suche findet keine nicht-öffentlichen Inhalte",
    "GEHEIMER-GRUNDSTUECKSKAUF" not in html(resp) and "GEHEIME-PERSONALIE" not in html(resp),
)
check("Zurücksetzen-Link bei Filter", "Zurücksetzen" in page)

print()
print("=== 3. Zugriff ===")
resp = c_other.get(f"/work/{org_other.slug}/ris/decisions/")
check(
    "Kommune ohne Session-Mandant -> Hinweis",
    resp.status_code == 200 and "Noch keine Beschlusskontrolle" in html(resp) and "Radweg" not in html(resp),
)
u_guest = User.objects.create_user(email="gast@example.org", password="pw-Smoke-1!")
Membership.objects.create(user=u_guest, organization=org)  # keine Rolle → kein ris.view
c_guest = login(u_guest)
check("Ohne ris.view kein Zugriff", c_guest.get(f"{WORK}/ris/decisions/").status_code in (302, 403))
check("Fremde Organisation kein Zugriff", c_other.get(f"{WORK}/ris/decisions/").status_code in (302, 403))

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
