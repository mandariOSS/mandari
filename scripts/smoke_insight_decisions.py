# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Issue #48).

- Opt-in der Verwaltung (Mandant + je Beschluss), öffentliche Statusmeldung getrennt vom internen Vermerk
- Insight-Liste mit Ampel/Filtern und Detailseite mit Status-Zeitleiste; nur freigegebene, öffentliche Beschlüsse
- Abo mit Double-Opt-In, Benachrichtigung bei Statuswechsel, Abmeldung
- Ohne Freigabe: Hinweis statt Daten, Detailseite 404
"""

import base64
import os
import re
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

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from insight_core.models import DecisionSubscription, OParlBody, OParlSource  # noqa: E402
from insight_core.services import decision_tracking  # noqa: E402

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


print("=== Setup ===")
source = OParlSource.objects.create(name="RIS", url="https://ris.example/oparl/system")
body = OParlBody.objects.create(
    external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Planhausen", slug="planhausen"
)
tenant = SessionTenant.objects.create(name="Stadt Planhausen", slug="planhausen", oparl_body=body, insight_publish=True)
role = SessionRole.objects.create(tenant=tenant, name="Administration", is_admin=True)
clerk_user = User.objects.create_user(email="rat@planhausen.example", password="pw-Smoke-1!")
clerk = SessionUser.objects.create(user=clerk_user, tenant=tenant)
clerk.roles.add(role)
c_clerk = Client()
c_clerk.force_login(clerk_user)
SESSION = f"/session/{tenant.slug}"

council = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
now = timezone.now()
today = timezone.localdate()
m_pub = SessionMeeting.objects.create(
    tenant=tenant, name="Rat 03/2026", organization=council, start=now - timedelta(days=120)
)
m_np = SessionMeeting.objects.create(
    tenant=tenant, name="NÖ-Sitzung", organization=council, start=now - timedelta(days=30), is_public=False
)


def top(meeting, number, name, **kw):
    defaults = {"vote_result": "approved", "is_public": True, "order": int(number)}
    defaults.update(kw)
    return SessionAgendaItem.objects.create(meeting=meeting, number=number, name=name, **defaults)


radweg = top(
    m_pub,
    "3",
    "Radweg Hauptstraße",
    resolution_number="B/2026/0003",
    resolution_text="Die Verwaltung plant einen geschützten Radweg.",
    implementation_recipient="Tiefbauamt",
    implementation_deadline=today + timedelta(days=60),
    implementation_note="INTERN: Vergabevermerk 4711",
    implementation_public_note="Planung beauftragt, Baubeginn im Frühjahr.",
)
spielplatz = top(
    m_pub, "4", "Spielplatz Süd", implementation_status="done", implementation_public_note="Eröffnet am 01.08."
)
geheim = top(m_pub, "9", "GEHEIMER-GRUNDSTUECKSKAUF", is_public=False)
np_sitzung = top(m_np, "1", "GEHEIME-PERSONALIE")
abgelehnt = top(m_pub, "5", "Parkhaus Innenstadt", vote_result="rejected")
versteckt = top(m_pub, "6", "VERSTECKTER-BESCHLUSS", implementation_public=False)

anon = Client()
sess = anon.session
sess["active_body_id"] = str(body.id)
sess.save()

print()
print("=== 1. Ohne Freigabe der Verwaltung ===")
resp = anon.get("/insight/beschluesse/")
check(
    "Liste -> Hinweis, keine Daten",
    resp.status_code == 200 and "noch keine beschlusskontrolle" in html(resp).lower() and "Radweg" not in html(resp),
    "status=%s radweg=%s hint=%s" % (resp.status_code, "Radweg" in html(resp), "beschlusskontrolle" in html(resp).lower()),
)
check("Detail -> 404", anon.get(f"/insight/beschluesse/{radweg.id}/").status_code == 404)
check("Navigation zeigt Beschlüsse", "beschluesse/" in html(resp))

print()
print("=== 2. Verwaltung schaltet Veröffentlichung ein ===")
resp = c_clerk.get(f"{SESSION}/settings/")
check(
    "Einstellungen zeigen Beschluss-Tracking-Schalter",
    resp.status_code == 200 and "Umsetzungsstand veröffentlichen" in html(resp),
)
resp = c_clerk.post(f"{SESSION}/settings/implementation-publish/", {"publish": "1"}, follow=True)
tenant.refresh_from_db()
check("Schalter aktiv + Meldung", tenant.implementation_publish and "wird veröffentlicht" in html(resp))
resp = c_clerk.get(f"{SESSION}/resolutions/")
check(
    "Register zeigt Feld für öffentliche Statusmeldung",
    "Öffentliche Statusmeldung" in html(resp) and "öffentlich zeigen" in html(resp),
)

print()
print("=== 3. Insight: Liste und Detail ===")
resp = anon.get("/insight/beschluesse/")
page = html(resp)
check(
    "Liste -> 200 mit freigegebenen Beschlüssen",
    resp.status_code == 200 and "Radweg Hauptstraße" in page and "Spielplatz Süd" in page,
)
check(
    "Öffentliche Statusmeldung statt internem Vermerk",
    "Planung beauftragt" in page and "INTERN" not in page and "4711" not in page,
)
check(
    "Nicht-öffentliche, abgelehnte und nicht freigegebene Beschlüsse fehlen",
    all(
        t not in page
        for t in ("GEHEIMER-GRUNDSTUECKSKAUF", "GEHEIME-PERSONALIE", "Parkhaus Innenstadt", "VERSTECKTER-BESCHLUSS")
    ),
)
stats = decision_tracking.stats(body)
check("Statistik: 1 offen, 1 umgesetzt", stats["open"] == 1 and stats["done"] == 1 and stats["total"] == 2, str(stats))
check(
    "Filter nur offene",
    "Spielplatz" not in html(anon.get("/insight/beschluesse/?offen=1"))
    and "Radweg" in html(anon.get("/insight/beschluesse/?offen=1")),
)
check(
    "Suche in Statusmeldung",
    "Spielplatz" in html(anon.get("/insight/beschluesse/?q=Eröffnet"))
    and "Radweg" not in html(anon.get("/insight/beschluesse/?q=Eröffnet")),
)

resp = anon.get(f"/insight/beschluesse/{radweg.id}/")
page = html(resp)
check(
    "Detail -> 200 mit Zeitleiste",
    resp.status_code == 200 and "Was ist daraus geworden" in page and "Beschlossen" in page and "Umgesetzt" in page,
)
check(
    "Detail: Status, Zuständigkeit, Frist, Statusmeldung",
    "Umsetzung steht an" in page
    and "Tiefbauamt" in page
    and "Geplante Umsetzung" in page
    and "Planung beauftragt" in page,
)
check("Detail: interner Vermerk nicht sichtbar", "4711" not in page and "INTERN" not in page)
check("Versteckter Beschluss -> 404", anon.get(f"/insight/beschluesse/{versteckt.id}/").status_code == 404)
check("NÖ-Beschluss -> 404", anon.get(f"/insight/beschluesse/{geheim.id}/").status_code == 404)
steps = decision_tracking.timeline(spielplatz)
check("Zeitleiste umgesetzt: alle Schritte erreicht", all(s["reached"] for s in steps) and steps[-1]["key"] == "done")

print()
print("=== 4. Abo mit Double-Opt-In ===")
mail.outbox.clear()
resp = anon.post(f"/insight/beschluesse/{radweg.id}/", {"email": "buergerin@example.org", "privacy": "on"}, follow=True)
sub = DecisionSubscription.objects.filter(agenda_item=radweg, email="buergerin@example.org").first()
check(
    "Abo angelegt, unbestätigt, Bestätigungsmail",
    sub is not None and not sub.confirmed and len(mail.outbox) == 1 and "bestätigen" in mail.outbox[0].subject.lower(),
)
check("Hinweis auf Bestätigung", "Bestätigung" in html(resp))
m = re.search(r"/insight/beschluesse/abo/bestaetigen/([0-9a-f-]{36})/", mail.outbox[0].body)
check("Bestätigungslink in Mail", m is not None and m.group(1) == str(sub.token))
resp = anon.post(f"/insight/beschluesse/{radweg.id}/", {"email": "ungueltig", "privacy": "on"}, follow=True)
check("Ungültige Adresse abgelehnt", "gültige E-Mail" in html(resp) and DecisionSubscription.objects.count() == 1)

# Statuswechsel vor Bestätigung -> keine Mail
mail.outbox.clear()
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {
        "status": "in_progress",
        "recipient": "Tiefbauamt",
        "deadline": "",
        "note": "INTERN 2",
        "public_note": "Bauarbeiten haben begonnen.",
        "public": "1",
    },
)
radweg.refresh_from_db()
check(
    "Tracking-Formular speichert öffentliche Meldung",
    radweg.implementation_status == "in_progress"
    and radweg.implementation_public_note == "Bauarbeiten haben begonnen.",
)
check("Unbestätigtes Abo erhält keine Mail", len(mail.outbox) == 0)

resp = anon.get(f"/insight/beschluesse/abo/bestaetigen/{sub.token}/")
sub.refresh_from_db()
check("Bestätigung aktiviert Abo", resp.status_code == 200 and sub.confirmed and "Benachrichtigung aktiv" in html(resp))
resp = anon.get(f"/insight/beschluesse/{radweg.id}/")
check("Detail zählt Abonnent:innen", "1 Person verfolgt" in html(resp))

print()
print("=== 5. Benachrichtigung bei Statuswechsel ===")
mail.outbox.clear()
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {
        "status": "done",
        "recipient": "Tiefbauamt",
        "deadline": "",
        "note": "INTERN 3",
        "public_note": "Der Radweg ist fertig und freigegeben.",
        "public": "1",
    },
)
check(
    "Statuswechsel -> E-Mail an Abonnentin",
    len(mail.outbox) == 1 and mail.outbox[0].to == ["buergerin@example.org"],
    str([m.to for m in mail.outbox]),
)
if mail.outbox:
    msg = mail.outbox[0]
    check(
        "Mail nennt neuen Stand + öffentliche Meldung, nicht den internen Vermerk",
        "Umgesetzt" in msg.subject
        and "fertig und freigegeben" in msg.body
        and "INTERN" not in msg.body
        and "INTERN" not in (msg.alternatives[0][0] if msg.alternatives else ""),
    )
    check(
        "Mail enthält Link zum Beschluss + Abmeldelink",
        f"/insight/beschluesse/{radweg.id}/" in msg.body
        and f"/insight/beschluesse/abo/abmelden/{sub.token}/" in (msg.alternatives[0][0] if msg.alternatives else ""),
    )
mail.outbox.clear()
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {
        "status": "done",
        "recipient": "Tiefbauamt",
        "deadline": "",
        "note": "INTERN 4 (nur intern geändert)",
        "public_note": "Der Radweg ist fertig und freigegeben.",
        "public": "1",
    },
)
check("Nur interner Vermerk geändert -> keine Mail", len(mail.outbox) == 0)

# Verwaltung nimmt Beschluss aus der Veröffentlichung -> keine Mail mehr, Detail 404
mail.outbox.clear()
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {"status": "deferred", "recipient": "Tiefbauamt", "deadline": "", "note": "x", "public_note": "y", "public": "0"},
)
radweg.refresh_from_db()
check(
    "Beschluss ausgeblendet -> keine Mail, Detail 404",
    not radweg.implementation_public
    and len(mail.outbox) == 0
    and anon.get(f"/insight/beschluesse/{radweg.id}/").status_code == 404,
)

print()
print("=== 6. Abmelden ===")
resp = anon.get(f"/insight/beschluesse/abo/abmelden/{sub.token}/")
sub.refresh_from_db()
check("Abmeldung", resp.status_code == 200 and sub.unsubscribed_at is not None and "beendet" in html(resp).lower())
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {"status": "done", "recipient": "", "deadline": "", "note": "", "public_note": "Neu", "public": "1"},
)
mail.outbox.clear()
c_clerk.post(
    f"{SESSION}/agenda/{radweg.id}/tracking/",
    {"status": "in_progress", "recipient": "", "deadline": "", "note": "", "public_note": "Neu", "public": "1"},
)
check("Abgemeldete Adresse erhält nichts mehr", len(mail.outbox) == 0)

# Verwaltung schaltet Veröffentlichung ab
c_clerk.post(f"{SESSION}/settings/implementation-publish/", {"publish": "0"})
check("Nach Abschalten: Liste ohne Daten", "Radweg" not in html(anon.get("/insight/beschluesse/")))

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
