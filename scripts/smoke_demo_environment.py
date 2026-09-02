# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Demo-/Musterumgebung (setup_demo_environment).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_demo_environment.py

Prüfungen:
  1. Command läuft zweimal durch (Idempotenz): Objektzähler bleiben stabil,
     keine Duplikate.
  2. Demo-Logins funktionieren (Vorsitz, Mitglied, Gast, Verwaltung) mit den
     im Command-Output ausgegebenen Passwörtern.
  3. Insight-Seiten der Musterstadt rendern (Portal, Termine, Vorgänge,
     Gremien, Personen, Sitzungs-/Vorgangs-Detail).
  4. Work-Dashboard der Musterfraktion rendert; Gast sieht das freigegebene
     Dokument über die Ordner-Freigabe.
  5. Session-Dashboard der Stadtverwaltung Musterstadt rendert.
  6. --reset entfernt alle Demo-Objekte restlos.
"""

import base64
import io
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_demo_")) / "smoke.sqlite3"
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

sys.argv = ["manage.py", "smoke_demo_environment"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
_overrides = override_settings(MEDIA_ROOT=str(_media_root))
_overrides.enable()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionApplication,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionTenant,
    SessionUser,
)
from apps.tenants.models import Membership, Organization, PartyGroup  # noqa: E402
from apps.work.meetings.models import AgendaItemPosition  # noqa: E402
from apps.work.models import FactionMeeting, MeetingPreparation, Motion, Task  # noqa: E402
from apps.work.motions.models import DocumentFolder, FolderGuestShare  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    status = "OK  " if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def run_command(*args) -> str:
    buf = io.StringIO()
    call_command("setup_demo_environment", *args, stdout=buf)
    return buf.getvalue()


def demo_counts() -> dict:
    body = OParlBody.objects.filter(slug="musterstadt-demo").first()
    return {
        "sources": OParlSource.objects.filter(url__startswith="https://demo.mandari.invalid/").count(),
        "bodies": OParlBody.objects.filter(slug="musterstadt-demo").count(),
        "oparl_orgs": OParlOrganization.objects.filter(body=body).count() if body else 0,
        "oparl_persons": OParlPerson.objects.filter(body=body).count() if body else 0,
        "oparl_meetings": OParlMeeting.objects.filter(body=body).count() if body else 0,
        "oparl_agenda_items": OParlAgendaItem.objects.filter(meeting__body=body).count() if body else 0,
        "oparl_papers": OParlPaper.objects.filter(body=body).count() if body else 0,
        "oparl_files": OParlFile.objects.filter(body=body).count() if body else 0,
        "oparl_consultations": OParlConsultation.objects.filter(body=body).count() if body else 0,
        "oparl_memberships": OParlMembership.objects.filter(person__body=body).count() if body else 0,
        "work_orgs": Organization.objects.filter(slug="musterfraktion-demo").count(),
        "party_groups": PartyGroup.objects.filter(slug="musterpartei-demo").count(),
        "users": User.objects.filter(email__endswith="@demo.mandari.de").count(),
        "memberships": Membership.objects.filter(organization__slug="musterfraktion-demo").count(),
        "folders": DocumentFolder.objects.filter(organization__slug="musterfraktion-demo").count(),
        "guest_shares": FolderGuestShare.objects.filter(folder__organization__slug="musterfraktion-demo").count(),
        "motions": Motion.objects.filter(organization__slug="musterfraktion-demo").count(),
        "tasks": Task.objects.filter(organization__slug="musterfraktion-demo").count(),
        "faction_meetings": FactionMeeting.objects.filter(organization__slug="musterfraktion-demo").count(),
        "preparations": MeetingPreparation.objects.filter(organization__slug="musterfraktion-demo").count(),
        "positions": AgendaItemPosition.objects.filter(organization__slug="musterfraktion-demo").count(),
        "session_tenants": SessionTenant.objects.filter(slug="stadtverwaltung-musterstadt-demo").count(),
        "session_orgs": SessionOrganization.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").count(),
        "session_persons": SessionPerson.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").count(),
        "session_meetings": SessionMeeting.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").count(),
        "session_papers": SessionPaper.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").count(),
        "session_applications": SessionApplication.objects.filter(
            tenant__slug="stadtverwaltung-musterstadt-demo"
        ).count(),
        "session_users": SessionUser.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").count(),
        "session_attendances": SessionAttendance.objects.filter(
            meeting__tenant__slug="stadtverwaltung-musterstadt-demo"
        ).count(),
        "session_protocols": SessionProtocol.objects.filter(
            meeting__tenant__slug="stadtverwaltung-musterstadt-demo"
        ).count(),
    }


def parse_demo_logins(output: str) -> dict:
    demo_logins = {}
    for line in output.splitlines():
        match = re.match(r"\s+(\S+@demo\.mandari\.de)\s+->\s+(\S+)", line)
        if match:
            demo_logins[match.group(1)] = match.group(2)
    return demo_logins


def login_client(email: str, password: str) -> tuple[Client, object]:
    client = Client()
    response = client.post("/accounts/login/", {"email": email, "password": password})
    return client, response


# ---------------------------------------------------------------------------
print("\n=== 1. Idempotenz: Command zweimal ausführen ===")
run_command()
counts_first = demo_counts()
output_second = run_command()
counts_second = demo_counts()

check("Erster Lauf legt Demo-Daten an", counts_first["bodies"] == 1 and counts_first["users"] == 7)
check(
    "Zweiter Lauf dupliziert nichts (alle Zähler stabil)",
    counts_first == counts_second,
    detail=f"first={counts_first}\nsecond={counts_second}",
)
check("Insight: 5 Gremien/Fraktionen", counts_second["oparl_orgs"] == 5)
check("Insight: 8 Personen", counts_second["oparl_persons"] == 8)
check("Insight: 6 Sitzungen", counts_second["oparl_meetings"] == 6)
check("Insight: 12 Vorlagen", counts_second["oparl_papers"] == 12)
check("Insight: 2 PDF-Dateien", counts_second["oparl_files"] == 2)
check("Insight: Beratungen vorhanden", counts_second["oparl_consultations"] >= 10)
check("Work: 3 Mitgliedschaften", counts_second["memberships"] == 3)
check("Work: 2 Dokumente", counts_second["motions"] == 2)
check("Work: 3 Aufgaben", counts_second["tasks"] == 3)
check("Work: 1 Fraktionssitzung", counts_second["faction_meetings"] == 1)
check("Work: Ordner-Freigabe für Gast", counts_second["guest_shares"] == 1)
check("Session: 3 Sitzungen", counts_second["session_meetings"] == 3)
check("Session: 4 Vorlagen", counts_second["session_papers"] == 4)
check("Session: 2 Anträge", counts_second["session_applications"] == 2)
check("Session: 4 Verwaltungsnutzer (je Rolle)", counts_second["session_users"] == 4)
check("Session: 3 Anwesenheiten (vergangene Sitzung)", counts_second["session_attendances"] == 3)
check("Session: 1 genehmigtes Protokoll", counts_second["session_protocols"] == 1)

demo_logins = parse_demo_logins(output_second)
check("Passwörter im Output (7 Nutzer)", len(demo_logins) == 7, detail=f"{len(demo_logins)} gefunden")

# PDF-Dateien physisch vorhanden + text_content gesetzt
pdf_ok = all(
    f.text_content and f.mime_type == "application/pdf" and f.size > 0
    for f in OParlFile.objects.filter(external_id__startswith="https://demo.mandari.invalid/")
)
check("PDF-Dateien mit text_content (kein OCR nötig)", pdf_ok)

# Verschlüsselte Felder lesbar (Accessoren)
person = SessionPerson.objects.filter(tenant__slug="stadtverwaltung-musterstadt-demo").first()
check(
    "SessionPerson: verschlüsseltes Telefon lesbar",
    person is not None and person.get_phone_decrypted().startswith("+49"),
)
motion = Motion.objects.filter(organization__slug="musterfraktion-demo", status="submitted").first()
check("Motion: verschlüsselter Inhalt lesbar", motion is not None and "Trinkwasserbrunnen" in motion.content)

# ---------------------------------------------------------------------------
print("\n=== 2. Demo-Logins ===")
sessions = {}
for email in sorted(demo_logins):
    client, response = login_client(email, demo_logins[email])
    ok = response.status_code == 302
    check(f"Login {email} -> 302", ok, detail=f"status={response.status_code}")
    sessions[email] = client

vorsitz = sessions.get("demo-vorsitz@demo.mandari.de")
gast = sessions.get("demo-gast@demo.mandari.de")
verwaltung = sessions.get("demo-verwaltung@demo.mandari.de")
sachbearbeitung = sessions.get("demo-sachbearbeitung@demo.mandari.de")
protokoll = sessions.get("demo-protokoll@demo.mandari.de")
lesezugriff = sessions.get("demo-lesezugriff@demo.mandari.de")

# ---------------------------------------------------------------------------
print("\n=== 3. Insight-Seiten der Musterstadt ===")
anon = Client()
body = OParlBody.objects.get(slug="musterstadt-demo")
resp = anon.get(f"/insight/kommune/{body.id}/", follow=True)
check("Kommune-Auswahl Musterstadt", resp.status_code == 200)

insight_pages = [
    "/insight/",
    "/insight/termine/",
    "/insight/vorgaenge/",
    "/insight/gremien/",
    "/insight/personen/",
    "/insight/suche/?q=Spielplatz",
]
for url in insight_pages:
    resp = anon.get(url)
    check(f"GET {url}", resp.status_code == 200, detail=f"status={resp.status_code}")

meeting = OParlMeeting.objects.filter(body=body).order_by("-start").first()
resp = anon.get(f"/insight/termine/{meeting.id}/")
check("Sitzungs-Detail rendert", resp.status_code == 200 and b"Musterstadt" in resp.content)

paper = OParlPaper.objects.filter(body=body, reference="V/2026/D-001").first()
resp = anon.get(f"/insight/vorgaenge/{paper.id}/")
check("Vorgangs-Detail rendert (Spielplatz)", resp.status_code == 200)

org_detail = OParlOrganization.objects.filter(body=body, name__icontains="Rat der Stadt").first()
resp = anon.get(f"/insight/gremien/{org_detail.id}/")
check("Gremium-Detail rendert (Rat)", resp.status_code == 200)

person_detail = OParlPerson.objects.filter(body=body).first()
resp = anon.get(f"/insight/personen/{person_detail.id}/")
check("Personen-Detail rendert", resp.status_code == 200)

# ---------------------------------------------------------------------------
print("\n=== 4. Work-Portal Musterfraktion ===")
if vorsitz:
    resp = vorsitz.get("/work/musterfraktion-demo/")
    check("Work-Dashboard (Vorsitz) rendert", resp.status_code == 200, detail=f"status={resp.status_code}")
    resp = vorsitz.get("/work/musterfraktion-demo/documents/")
    check("Dokumentliste (Vorsitz) rendert", resp.status_code == 200)
    resp = vorsitz.get("/work/musterfraktion-demo/tasks/")
    check("Aufgaben (Vorsitz) rendern", resp.status_code == 200)

if gast:
    resp = gast.get("/work/musterfraktion-demo/documents/", follow=True)
    check("Gast erreicht Dokumentbereich", resp.status_code == 200, detail=f"status={resp.status_code}")
    shared_motion = Motion.objects.get(organization__slug="musterfraktion-demo", status="submitted")
    gast_membership = Membership.objects.get(
        organization__slug="musterfraktion-demo", user__email="demo-gast@demo.mandari.de"
    )
    check("Gast sieht Dokument über Ordner-Freigabe", shared_motion.get_guest_share_level(gast_membership) == "view")

# ---------------------------------------------------------------------------
print("\n=== 5. Session-Portal Stadtverwaltung Musterstadt ===")
if verwaltung:
    resp = verwaltung.get("/session/stadtverwaltung-musterstadt-demo/")
    check("Session-Dashboard rendert", resp.status_code == 200, detail=f"status={resp.status_code}")
    resp = verwaltung.get("/session/stadtverwaltung-musterstadt-demo/meetings/")
    check("Session-Sitzungsliste rendert", resp.status_code == 200)
    resp = verwaltung.get("/session/stadtverwaltung-musterstadt-demo/papers/")
    check("Session-Vorlagenliste rendert", resp.status_code == 200)
    past = SessionMeeting.objects.get(
        tenant__slug="stadtverwaltung-musterstadt-demo", name="Hauptausschuss (Demo, vergangen)"
    )
    resp = verwaltung.get(f"/session/stadtverwaltung-musterstadt-demo/meetings/{past.id}/protocol/")
    check("Session-Protokoll (genehmigt) rendert", resp.status_code == 200)

# Rollen erlebbar: Sachbearbeitung darf Vorlagen anlegen, Lesezugriff nicht
if sachbearbeitung and lesezugriff:
    create_url = "/session/stadtverwaltung-musterstadt-demo/papers/create/"
    resp = sachbearbeitung.get(create_url)
    check("Sachbearbeitung darf Vorlage anlegen (200)", resp.status_code == 200, detail=f"status={resp.status_code}")
    resp = lesezugriff.get(create_url, follow=False)
    check(
        "Lesezugriff darf keine Vorlage anlegen (302/403)",
        resp.status_code in (302, 403),
        detail=f"status={resp.status_code}",
    )

# ---------------------------------------------------------------------------
print("\n=== 6. --reset entfernt alles ===")
run_command("--reset")
counts_after_reset = demo_counts()
check(
    "Alle Demo-Zähler nach --reset auf 0",
    all(v == 0 for v in counts_after_reset.values()),
    detail=str({k: v for k, v in counts_after_reset.items() if v}),
)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"SMOKE FAILED: {len(FAILURES)} Fehler:")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("SMOKE OK: Demo-Umgebung vollständig geprüft.")
