# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Antrag digital bei der Verwaltung einreichen (Issue #40).

Fraktion (Work) → Verwaltung (Session) → Rückmeldung:
1. Verwaltung erstellt in Session einen Einreichungs-Token (nur einmal sichtbar) und kann ihn zurückziehen
2. Fraktion hinterlegt den Token (ungültiger Token wird abgelehnt), Reiter „Verwaltung“
3. Einreichen aus dem Editor: Vorschau mit vorbelegten Abschnitten, Antrag mit Eingangsnummer,
   Dokument „Eingereicht“, Verknüpfung, Token-Nutzung gezählt; doppelte Einreichung blockiert
4. Statusrückmeldung: eingegangen → „Bei Verwaltung“ + Benachrichtigung; Beratung terminiert →
   „Auf Tagesordnung“ + Beratungsfolge in Work; abgelehnt → „Abgelehnt“
5. Rechte: Gast/ohne Recht kein Zugriff; zurückgezogener Token blockiert Einreichung
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
os.environ["SITE_URL"] = "https://work.example"

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
from apps.session.models import (  # noqa: E402
    SessionAPIToken,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.motions import ris_submission  # noqa: E402
from apps.work.motions.models import AdministrationConnection, Motion  # noqa: E402
from apps.work.notifications.models import Notification  # noqa: E402
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


# =============================================================================
print("=== Setup ===")
source = OParlSource.objects.create(name="RIS", url="https://ris.example/oparl/system")
body = OParlBody.objects.create(external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Planhausen")
org = Organization.objects.create(name="Fraktion Zukunft", slug="fraktion-zukunft", body=body)
admin_role = Role.objects.filter(organization=org, is_admin=True).first()

u_admin = User.objects.create_user(
    email="vorsitz@zukunft.example", password="pw-Smoke-1!", first_name="Vera", last_name="Vorsitz"
)
m_admin = Membership.objects.create(user=u_admin, organization=org)
m_admin.roles.add(admin_role)
u_member = User.objects.create_user(email="mitglied@zukunft.example", password="pw-Smoke-1!")
m_member = Membership.objects.create(user=u_member, organization=org)
# Rolle mit Dokumentzugriff, aber ohne „Anträge ans RIS übermitteln“
member_role = next(
    r
    for r in Role.objects.filter(organization=org, is_admin=False).order_by("name")
    if r.permissions.filter(codename="motions.view").exists()
    and not r.permissions.filter(codename="motions.submit_to_ris").exists()
)
m_member.roles.add(member_role)
c_admin = login(u_admin)
c_member = login(u_member)
WORK = f"/work/{org.slug}"

tenant = SessionTenant.objects.create(name="Stadt Planhausen", slug="planhausen", oparl_body=body)
s_role = SessionRole.objects.create(tenant=tenant, name="Administration", is_admin=True)
u_clerk = User.objects.create_user(email="rat@planhausen.example", password="pw-Smoke-1!")
su_clerk = SessionUser.objects.create(user=u_clerk, tenant=tenant)
su_clerk.roles.add(s_role)
c_clerk = login(u_clerk)
SESSION = f"/session/{tenant.slug}"
council = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
committee = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")

CONTENT = (
    "<h1>Antrag: Radweg an der Hauptstraße</h1>"
    "<p>An den Rat der Stadt Planhausen</p>"
    "<h2>Beschlussvorschlag</h2>"
    "<p>Die Verwaltung wird beauftragt, einen geschützten Radweg zu planen.</p>"
    "<ul><li>Abschnitt Nord bis 2027</li><li>Abschnitt Süd bis 2028</li></ul>"
    "<h2>Begründung</h2>"
    "<p>Die Hauptstraße ist der gefährlichste Abschnitt im Stadtgebiet.</p>"
    "<p><strong>Finanzielle Auswirkungen:</strong></p>"
    "<p>Planungskosten ca. 80.000 Euro im Haushalt 2027.</p>"
)
motion = Motion.objects.create(organization=org, author=m_admin, title="Radweg Hauptstraße", visibility="organization")
motion.set_content_encrypted(CONTENT)
motion.status = "approved"
motion.save()

# =============================================================================
print()
print("=== 1. Verwaltung: Einreichungs-Zugang erstellen ===")
resp = c_clerk.get(f"{SESSION}/settings/")
check("Einstellungen verlinken Einreichungs-Zugänge", resp.status_code == 200 and "Einreichungs-Zugänge" in html(resp))
resp = c_clerk.get(f"{SESSION}/settings/api-tokens/")
check("Token-Seite -> 200, noch leer", resp.status_code == 200 and "Noch keine Zugänge" in html(resp))
resp = c_clerk.post(
    f"{SESSION}/settings/api-tokens/create/",
    {"name": "Fraktion Zukunft", "description": "Übergabe am 08.09.", "expires_at": ""},
    follow=True,
)
page = html(resp)
token_obj = SessionAPIToken.objects.get(tenant=tenant, name="Fraktion Zukunft")
import re  # noqa: E402

m = re.search(r'id="new-token"[^>]*>([0-9a-f]{64})<', page)
RAW_TOKEN = m.group(1) if m else ""
check(
    "Token einmalig angezeigt (64 Zeichen)",
    len(RAW_TOKEN) == 64 and RAW_TOKEN[:8] == token_obj.token_prefix,
    page[:200],
)
check("Token nur als Hash gespeichert", token_obj.token != RAW_TOKEN and token_obj.can_submit_applications)
resp = c_clerk.get(f"{SESSION}/settings/api-tokens/")
check("Zweiter Aufruf zeigt Token nicht mehr", RAW_TOKEN not in html(resp) and "noch nicht hinterlegt" in html(resp))
check(
    "Fraktionsmitglied ohne Session-Konto -> kein Zugriff",
    c_member.get(f"{SESSION}/settings/api-tokens/").status_code in (302, 403, 404),
)

# =============================================================================
print()
print("=== 2. Fraktion: Verbindung hinterlegen ===")
resp = c_admin.get(f"{WORK}/organization/verwaltung/")
check("Reiter Verwaltung -> 200, nicht verbunden", resp.status_code == 200 and "Nicht verbunden" in html(resp))
resp = c_admin.post(f"{WORK}/organization/verwaltung/", {"action": "connect", "token": "x" * 64}, follow=True)
check("Unbekannter Token abgelehnt", "nicht bekannt" in html(resp) and not AdministrationConnection.objects.exists())
resp = c_admin.post(f"{WORK}/organization/verwaltung/", {"action": "connect", "token": "kurz"}, follow=True)
check("Zu kurzer Token abgelehnt", "64 Zeichen" in html(resp))
resp = c_admin.post(f"{WORK}/organization/verwaltung/", {"action": "connect", "token": RAW_TOKEN}, follow=True)
conn = AdministrationConnection.objects.filter(organization=org).first()
check(
    "Gültiger Token verbindet mit der Verwaltung",
    conn is not None and conn.tenant == tenant and conn.token_hash == token_obj.token and "Verbunden" in html(resp),
)
check("Editor-Kontext: Verbindung nutzbar", ris_submission.connection_state(conn) == (True, ""))
check(
    "Mitglied ohne faction.manage -> kein Zugriff auf Reiter",
    c_member.get(f"{WORK}/organization/verwaltung/").status_code in (302, 403),
)
resp = c_clerk.get(f"{SESSION}/settings/api-tokens/")
check("Verwaltung sieht verbundene Fraktion", "Fraktion Zukunft" in html(resp) and org.name in html(resp))

# =============================================================================
print()
print("=== 3. Einreichen aus dem Editor ===")
resp = c_admin.get(f"{WORK}/documents/{motion.id}/")
page = html(resp)
check(
    "Editor zeigt „Bei Verwaltung einreichen“",
    resp.status_code == 200 and "Bei Verwaltung einreichen" in page and f"/documents/{motion.id}/submit-ris/" in page,
)

resp = c_admin.get(f"{WORK}/documents/{motion.id}/submit-ris/")
page = html(resp)
check(
    "Einreichseite -> 200 mit Vorschau",
    resp.status_code == 200 and "Vorschau des Dokuments" in page and "Stadt Planhausen" in page,
)
check("Beschlussvorschlag vorbelegt", "geschützten Radweg zu planen" in page and "Abschnitt Nord bis 2027" in page)
check("Begründung vorbelegt", "gefährlichste Abschnitt" in page)
check("Finanzielle Auswirkungen erkannt", "80.000 Euro" in page and "Abschnitte aus dem Dokument übernommen" in page)
check("Zielgremien der Verwaltung wählbar", "Bauausschuss" in page and str(committee.id) in page)

sections = ris_submission.extract_sections("<p>Nur ein Absatz ohne Struktur.</p>")
check(
    "Ohne Struktur: alles im Beschlussvorschlag",
    sections["resolution_proposal"] == "Nur ein Absatz ohne Struktur." and not sections["structured"],
)

resp = c_admin.post(
    f"{WORK}/documents/{motion.id}/submit-ris/",
    {"title": "Radweg Hauptstraße", "application_type": "motion", "resolution_proposal": "x", "justification": ""},
)
check(
    "Fehlende Begründung/Bestätigung -> Fehler, kein Antrag",
    resp.status_code == 200 and "Begründung darf nicht leer" in html(resp) and not SessionApplication.objects.exists(),
)

resp = c_admin.post(
    f"{WORK}/documents/{motion.id}/submit-ris/",
    {
        "title": "Radweg Hauptstraße",
        "application_type": "motion",
        "target_organization": str(committee.id),
        "resolution_proposal": "Die Verwaltung wird beauftragt, einen geschützten Radweg zu planen.",
        "justification": "Die Hauptstraße ist der gefährlichste Abschnitt im Stadtgebiet.",
        "financial_impact": "Planungskosten ca. 80.000 Euro.",
        "co_signers": "Max Mustermann",
        "deadline": (timezone.now() + timedelta(days=40)).date().isoformat(),
        "confirm": "on",
    },
    follow=True,
)
motion.refresh_from_db()
app = SessionApplication.objects.filter(tenant=tenant).first()
check(
    "Antrag bei der Verwaltung angelegt",
    app is not None and app.submitting_organization == org and app.target_organization == committee,
)
check("Eingangsnummer vergeben", app is not None and app.reference.startswith(f"A/{timezone.now().year}/"))
check(
    "Absender aus Konto übernommen",
    app is not None and app.submitter_email == u_admin.email and "Vera" in app.submitter_name,
)
check(
    "Dokument: Eingereicht + verknüpft",
    motion.status == "submitted" and motion.session_application_id == app.id and motion.submitted_at is not None,
)
check("Erfolgsmeldung mit Eingangsnummer", app.reference in html(resp))
token_obj.refresh_from_db()
conn.refresh_from_db()
check("Token-Nutzung gezählt", token_obj.usage_count == 1 and conn.last_used_at is not None)

resp = c_admin.get(f"{WORK}/documents/{motion.id}/submit-ris/")
page = html(resp)
check(
    "Statusseite nach Einreichung", app.reference in page and "Beratungsfolge" in page and "Noch keine Beratung" in page
)
resp = c_admin.post(
    f"{WORK}/documents/{motion.id}/submit-ris/",
    {"title": "x", "resolution_proposal": "x", "justification": "x", "confirm": "on"},
)
check("Doppelte Einreichung blockiert", SessionApplication.objects.count() == 1 and "bereits eingereicht" in html(resp))
resp = c_admin.get(f"{WORK}/documents/{motion.id}/")
check(
    "Editor zeigt Eingangsnummer + Sidebar Verwaltung",
    app.reference in html(resp) and "Details &amp; Beratungsfolge" in html(resp),
)
resp = c_clerk.get(f"{SESSION}/applications/{app.id}/")
check("Verwaltung sieht Herkunft Work", resp.status_code == 200 and "Eingereicht über" in html(resp))

# =============================================================================
print()
print("=== 4. Rückmeldung der Verwaltung ===")
Notification.objects.all().delete()
resp = c_clerk.post(
    f"{SESSION}/applications/{app.id}/process/",
    {
        "status": "received",
        "target_organization": str(committee.id),
        "processing_notes": "Wird im Bauausschuss beraten.",
    },
    follow=True,
)
motion.refresh_from_db()
app.refresh_from_db()
check("Verwaltung: eingegangen", resp.status_code == 200 and app.status == "received" and app.received_at is not None)
check("Work-Status folgt: Bei Verwaltung", motion.status == "at_admin", motion.status)
notif = Notification.objects.filter(recipient=m_admin).order_by("-created_at").first()
check(
    "Autor:in benachrichtigt (mit Hinweis)",
    notif is not None
    and "Eingegangen" in notif.title
    and "Bauausschuss beraten" in notif.message
    and "submit-ris" in notif.link,
    getattr(notif, "message", ""),
)

resp = c_clerk.post(f"{SESSION}/applications/{app.id}/convert/", {}, follow=True)
app.refresh_from_db()
paper = app.created_papers.first()
check("In Vorlage umgewandelt", app.status == "converted" and paper is not None, app.status)
motion.refresh_from_db()
check("Work bleibt bei Verwaltung (noch kein Termin)", motion.status == "at_admin")

Notification.objects.all().delete()
meeting = SessionMeeting.objects.create(
    tenant=tenant, name="Bauausschuss 10/2026", organization=committee, start=timezone.now() + timedelta(days=21)
)
SessionConsultation.objects.create(paper=paper, organization=committee, meeting=meeting, order=1)
SessionConsultation.objects.create(paper=paper, organization=council, order=2, authoritative=True)
motion.refresh_from_db()
check("Beratung terminiert -> Auf Tagesordnung", motion.status == "on_agenda", motion.status)
notif = Notification.objects.filter(recipient=m_admin).order_by("-created_at").first()
check(
    "Benachrichtigung nennt Gremium und Termin",
    notif is not None and "Bauausschuss" in notif.message and "Beratung terminiert" in notif.title,
)
timeline = ris_submission.consultation_timeline(app)
check(
    "Beratungsfolge: Termin zuerst, Rat ohne Termin danach",
    len(timeline) == 2 and timeline[0]["organization"] == "Bauausschuss" and timeline[1]["start"] is None,
)
resp = c_admin.get(f"{WORK}/documents/{motion.id}/submit-ris/")
page = html(resp)
check(
    "Statusseite zeigt Beratungsfolge",
    "Bauausschuss" in page and "Termin noch offen" in page and "In Vorlage umgewandelt" in page,
)
resp = c_admin.get(f"{WORK}/documents/{motion.id}/")
check("Sidebar zeigt Beratungstermin", "Bauausschuss" in html(resp) and "Termin offen" in html(resp))

# Ablehnung eines zweiten Antrags
motion2 = Motion.objects.create(
    organization=org, author=m_admin, title="Anfrage Straßenbeleuchtung", visibility="organization"
)
motion2.set_content_encrypted("<p>Wie viele Leuchten wurden 2025 getauscht?</p>")
motion2.status = "approved"
motion2.save()
resp = c_admin.get(f"{WORK}/documents/{motion2.id}/submit-ris/")
check("Antragsart aus Titel: Anfrage", 'value="inquiry" selected' in html(resp))
c_admin.post(
    f"{WORK}/documents/{motion2.id}/submit-ris/",
    {
        "title": "Anfrage Straßenbeleuchtung",
        "application_type": "inquiry",
        "resolution_proposal": "Auskunft",
        "justification": "Transparenz",
        "confirm": "on",
    },
)
app2 = SessionApplication.objects.get(title="Anfrage Straßenbeleuchtung")
check("Zweite Eingangsnummer fortlaufend", app2.reference.endswith("0002"), app2.reference)
c_clerk.post(
    f"{SESSION}/applications/{app2.id}/process/", {"status": "rejected", "processing_notes": "Nicht zuständig."}
)
motion2.refresh_from_db()
check("Ablehnung -> Work „Abgelehnt“", motion2.status == "rejected", motion2.status)

# =============================================================================
print()
print("=== 5. Rechte und Sperren ===")
motion3 = Motion.objects.create(organization=org, author=m_admin, title="Dritter Antrag", visibility="organization")
motion3.set_content_encrypted("<p>Inhalt</p>")
motion3.status = "approved"
motion3.save()
check(
    "Mitglied ohne Recht -> kein Einreichen",
    c_member.get(f"{WORK}/documents/{motion3.id}/submit-ris/").status_code in (302, 403),
)
resp = c_member.get(f"{WORK}/documents/{motion3.id}/")
_pg = html(resp)
_i = _pg.find("submit-ris")
check("Ohne Recht kein Einreich-Button", resp.status_code == 200 and _i < 0, f"status={resp.status_code} pos={_i}")

draft = Motion.objects.create(organization=org, author=m_admin, title="Entwurf", visibility="organization")
draft.set_content_encrypted("<p>Entwurf</p>")
allowed, reason = ris_submission.can_submit(draft, m_member)
check("Entwurf ohne Freigaberecht nicht einreichbar", not allowed and "freigegeben" in reason)
check("Admin (motions.approve) darf Entwurf einreichen", ris_submission.can_submit(draft, m_admin)[0])

resp = c_clerk.post(f"{SESSION}/settings/api-tokens/{token_obj.id}/revoke/", follow=True)
token_obj.refresh_from_db()
check("Token zurückgezogen", not token_obj.is_active and "Zurückgezogen" in html(resp))
resp = c_admin.post(
    f"{WORK}/documents/{motion3.id}/submit-ris/",
    {
        "title": "Dritter Antrag",
        "application_type": "motion",
        "resolution_proposal": "x",
        "justification": "y",
        "confirm": "on",
    },
)
check(
    "Zurückgezogener Token blockiert Einreichung",
    "abgelaufen oder wurde zurückgezogen" in html(resp) and SessionApplication.objects.count() == 2,
)
resp = c_admin.get(f"{WORK}/organization/verwaltung/")
check("Reiter zeigt Störung", "Gestört" in html(resp))
resp = c_admin.post(f"{WORK}/organization/verwaltung/", {"action": "disconnect"}, follow=True)
check("Verbindung trennen", "getrennt" in html(resp) and ris_submission.get_connection(org) is None)
resp = c_admin.get(f"{WORK}/documents/{motion3.id}/submit-ris/")
check(
    "Ohne Verbindung: Hinweis + Link zu Einstellungen",
    "Keine nutzbare Verbindung" in html(resp) and "/organization/verwaltung/" in html(resp),
)

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
