# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Fraktionssitzungen — Einladungen, Genehmigungskette, Protokoll-PDF,
Änderungshistorie, Sitzungserzeugung, NÖ-Abschottung
(Issues #58, #59, #60, #61, #64, #66).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_faction_meetings.py

Prüft:
- Einladung mit ICS- und Tagesordnungs-PDF-Anhang je Vereidigten-Status
  (Nicht-Vereidigte erhalten nur den Ö-Teil), Deep-Link + RSVP-Hinweis
- Nachladung/Aktualisierung nach TO-Änderungen (ICS-SEQUENCE erhöht,
  Abgesagte werden nicht erneut angeschrieben)
- Erinnerungs-Auslösung 48 h vor Sitzungsbeginn (einmalig je Sitzung)
- Genehmigungskette: Genehmigungs-TOP der Folgesitzung -> Abstimmung ->
  Vorprotokoll genehmigt (protocol_status + Metadaten)
- Öffentliche Protokolle erst nach Opt-in (publish_protocols, Default aus);
  Schalter nur mit protocols.publish änderbar
- Leck-Beweis: TOP-lose und NÖ-Protokolleinträge erscheinen nie öffentlich
- Niederschrift-PDF: öffentliche und interne Fassung, Berechtigungsprüfung
- Permission-Matrix der Aktions-Handler (invite/start/approve/add_entry/...)
- Änderungshistorie (Issue #66): Audit-Einträge für alle Aktionen inkl.
  Spezial-Ereignisse, Attribution, Unveränderbarkeit, Einsichts-View mit
  Berechtigung + NÖ-Maskierung, verschlüsselte Felder nie im Klartext,
  Kaskadenlösch-Schutz beim Löschen einer Organisation
- Sitzungserzeugung (Issue #61): rollierender Horizont, Idempotenz,
  Ausfallregeln-Matrix (Urlaubszeitraum + RIS-Regel "nach Gremium X"),
  ersatzlos gestrichene Termine als "entfällt" sichtbar
- NÖ strikt (Issue #64): Nicht-Vereidigte sehen von NÖ-TOPs NICHTS außer
  "Gesperrte Information" — über alle Ausgabewege (Detail, Panel, Aktionen,
  Historie, Mails/PDFs); auch das Vorschlagen von NÖ-TOPs nur für Vereidigte

Welle 2 (Issues #62, #63, #65, #67, #69):
- Einladungslogik (#62): Opt-in/Opt-out-Matrix, konfigurierbarer Vorlauf,
  Freigabe-Modus mit Hinweisen an Vorstand/Vorsitz (E-Mail abschaltbar),
  Freigabe durch den stellv. Vorsitz ohne Delegation (auditiert WER),
  automatischer Versand zum Vorlaufzeitpunkt (einmalig)
- Standard-TOP 1 (#63): verbindlicher Default-Text, "Ja mit Änderungen"
  genehmigt, danach ENDGÜLTIGE Sperre (Views UND Modellebene, auch für
  Admins), Korrekturen nur als sichtbarer Nachtrag (auditiert)
- Teilnahme-Workflow (#67): Teilnahmeart vor Ort/online, finale
  Bestätigung durch den Vorstand (Zeitstempel + Bestätiger, Vertreter-Fall),
  danach Teilnahme-Änderungen gesperrt
- Quorum (#69): beschlussfähig ab MEHR als 50 % der Stimmberechtigten,
  gemeinsamer Baustein mit dem Session-RIS
- Eigene Absender-Mail (#65): Versand über organisationseigenes SMTP
  (gemockt) mit Fallback-Verhalten, Testmail, SPF/DKIM-Hinweis,
  Passwort nur verschlüsselt und nie im Klartext sichtbar

Welle 3 (Issues #68, #70, #71):
- Teilnahmenachweis (#68): PDF ausschließlich aus vom Vorstand bestätigten
  Teilnahmen, dokumentierte Ausstellung (Token/Prüfsumme/Audit), öffentliche
  Verifikations-Seite OHNE Personenbezug (Response-Scan-Beweis), opakes
  Token, Sammel-Export nur für den Vorstand (PDF/CSV)
- In-App + iCal (#70): In-App-Benachrichtigungen für Einladung/Erinnerung/
  Vorschlags-Entscheidung/Protokoll-Genehmigung (je Typ abschaltbar),
  persönlicher iCal-Feed mit opakem Token (ohne Login), Fraktions- und
  RIS-Termine, Token-Erneuerung macht die alte URL sofort ungültig,
  keinerlei NÖ-/Protokollinhalte im Feed (Response-Scan-Beweis)
- Öffentliche API v1 (#71): Aktivierungspflicht je Organisation (Opt-in,
  Default aus), Zugriff nur über opakes Token (404 bei unbekannt/inaktiv),
  CORS + Cache-Header, keine Entwürfe, NÖ-/Protokoll-Freiheit als
  Response-Scan-Beweis, valides OpenAPI-Schema, Token-Erneuerung
"""

import base64
import io
import os
import secrets
import sys
import tempfile
from datetime import datetime as _datetime
from datetime import time as _time
from datetime import timedelta
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_faction_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["SITE_URL"] = "http://testserver"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_faction_meetings"]
django.setup()

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.tenants.models import Membership, Organization, Permission, Role  # noqa: E402
from apps.work.faction.generation import run_faction_schedule_pass  # noqa: E402
from apps.work.faction.models import (  # noqa: E402
    FactionAgendaItem,
    FactionAuditLog,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
    FactionProtocolEntry,
    FactionSuspensionRule,
)
from apps.work.faction.services import run_faction_reminder_pass  # noqa: E402
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


def perm(code):
    obj, _ = Permission.objects.get_or_create(codename=code, defaults={"name": code, "category": code.split(".")[0]})
    return obj


def make_member(org, email, perm_codes, sworn=False):
    user = User.objects.create_user(email=email, password="pw-Smoke-Test-1!")
    ms = Membership.objects.create(user=user, organization=org, is_sworn_in=sworn)
    if perm_codes:
        role = Role.objects.create(organization=org, name=f"R-{email}", is_admin=False)
        role.permissions.add(*[perm(code) for code in perm_codes])
        ms.roles.add(role)
    c = Client()
    c.force_login(user)
    return user, ms, c


def pdf_text(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(bytes(pdf_bytes)))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def ics_text(content):
    return content if isinstance(content, str) else bytes(content).decode("utf-8")


# =============================================================================
# Setup: Kommune, Organisation, Mitglieder mit abgestuften Rechten
# =============================================================================
print("=== Setup ===")

source = OParlSource.objects.create(name="Test-RIS", url="https://ris.musterstadt.example/system")
body = OParlBody.objects.create(
    source=source,
    external_id="https://ris.musterstadt.example/body/1",
    name="Stadt Musterstadt",
    slug="musterstadt",
)

org = Organization.objects.create(name="Fraktion Testpartei", slug="fraktion-test", body=body)
TenantEncryption(org).key

ALL_PERMS = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.create",
    "faction.edit",
    "faction.start",
    "faction.invite",
    "faction.manage",
    "faction.view_audit",
    "protocols.view_public",
    "protocols.view_full",
    "protocols.create",
    "protocols.edit",
    "protocols.approve",
    "protocols.publish",
    "agenda.create",
    "agenda.manage",
]

chair_user, chair_ms, chair = make_member(org, "vorsitz@example.org", ALL_PERMS, sworn=True)
manager_user, manager_ms, manager = make_member(
    org,
    "personal@example.org",
    ["faction.view_public", "faction.view_non_public", "faction.manage"],
    sworn=True,
)
sworn_user, sworn_ms, sworn = make_member(
    org,
    "vereidigt@example.org",
    ["faction.view_public", "faction.view_non_public", "protocols.view_public", "protocols.view_full"],
    sworn=True,
)
unsworn_user, unsworn_ms, unsworn = make_member(
    org,
    "sachkundig@example.org",
    ["faction.view_public", "protocols.view_public"],
    sworn=False,
)

base = f"/work/{org.slug}"
now = timezone.now()

# =============================================================================
# Phase A: Sitzung anlegen + Einladung mit ICS/PDF je Vereidigten-Status
# =============================================================================
print()
print("=== Phase A: Einladung mit ICS/PDF-Anhängen ===")

start1 = (now + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Fraktionssitzung Eins",
        "start_date": timezone.localtime(start1).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start1).strftime("%H:%M"),
        "location": "Fraktionsbüro Raum 1",
    },
)
check("Sitzung anlegen -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
meeting1 = FactionMeeting.objects.filter(organization=org, title="Fraktionssitzung Eins").first()
check("Sitzung existiert", meeting1 is not None)
check("Anwesenheiten für alle Mitglieder", meeting1.attendances.count() == 4, str(meeting1.attendances.count()))
approval1 = meeting1.agenda_items.filter(is_approval_item=True).first()
check("Genehmigungs-TOP automatisch erstellt", approval1 is not None)

top_pub = FactionAgendaItem.objects.create(
    meeting=meeting1, title="OEFF-TOP-ALPHA", number="2", visibility="public", order=2
)
top_int = FactionAgendaItem.objects.create(
    meeting=meeting1, title="GEHEIM-TOP-OMEGA", number="NÖ 1", visibility="internal", order=3
)

mail.outbox = []
resp = chair.post(f"{base}/faction/{meeting1.id}/action/", {"action": "invite"})
check("Einladungsversand -> OK", resp.status_code in (200, 302), f"got {resp.status_code}")
check("4 Einladungen versendet", len(mail.outbox) == 4, f"outbox={len(mail.outbox)}")

meeting1.refresh_from_db()
check("Status = invited, invitation_sent", meeting1.status == "invited" and meeting1.invitation_sent is True)
check("ICS-Sequenz initial 0", meeting1.invitation_sequence == 0)

by_recipient = {m.to[0]: m for m in mail.outbox}
sworn_mail = by_recipient.get("vereidigt@example.org")
unsworn_mail = by_recipient.get("sachkundig@example.org")
check("Vereidigtes Mitglied angeschrieben", sworn_mail is not None)
check("Nicht-vereidigtes Mitglied angeschrieben", unsworn_mail is not None)

att_names = sorted(name for name, _c, _m in sworn_mail.attachments)
check("Anhänge: PDF + ICS", att_names == ["sitzung.ics", "tagesordnung.pdf"], str(att_names))

pdf_sworn = next(c for n, c, _m in sworn_mail.attachments if n.endswith(".pdf"))
pdf_unsworn = next(c for n, c, _m in unsworn_mail.attachments if n.endswith(".pdf"))
check("PDF gültig (Magic Bytes)", bytes(pdf_sworn[:4]) == b"%PDF")
check("Vereidigten-PDF enthält Ö-TOP", "OEFF-TOP-ALPHA" in pdf_text(pdf_sworn))
check("Vereidigten-PDF enthält NÖ-TOP", "GEHEIM-TOP-OMEGA" in pdf_text(pdf_sworn))
check("Nicht-Vereidigten-PDF enthält Ö-TOP", "OEFF-TOP-ALPHA" in pdf_text(pdf_unsworn))
check("Nicht-Vereidigten-PDF OHNE NÖ-TOP", "GEHEIM-TOP-OMEGA" not in pdf_text(pdf_unsworn))

ics = ics_text(next(c for n, c, _m in sworn_mail.attachments if n.endswith(".ics")))
check("ICS: VCALENDAR/VEVENT", "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics)
check("ICS: SEQUENCE:0", "SEQUENCE:0" in ics)
check("ICS: UID stabil je Sitzung", f"faction-meeting-{meeting1.pk}@mandari" in ics)
check("ICS: Ort enthalten", "Fraktionsbüro Raum 1" in ics)

deep_link = f"/work/{org.slug}/faction/{meeting1.id}/"
check("E-Mail: Deep-Link zur Sitzung", deep_link in sworn_mail.body)
check("E-Mail: RSVP-Hinweis", "Zusagen" in sworn_mail.body or "zu oder ab" in sworn_mail.body)
check("E-Mail: NÖ-Titel nicht im Text an Nicht-Vereidigte", "GEHEIM-TOP-OMEGA" not in unsworn_mail.body)

# =============================================================================
# Phase B: Nachladung/Aktualisierung nach TO-Änderung (statt harter Sperre)
# =============================================================================
print()
print("=== Phase B: Aktualisierung/Nachladung ===")

# Nicht-Vereidigter sagt ab -> erhält keine Aktualisierung mehr
resp = unsworn.post(f"{base}/faction/{meeting1.id}/action/", {"action": "respond", "status": "declined"})
check("Absage -> OK", resp.status_code in (200, 302), f"got {resp.status_code}")

FactionAgendaItem.objects.create(meeting=meeting1, title="NACHTRAGS-TOP-NEU", number="3", visibility="public", order=4)

mail.outbox = []
resp = chair.post(f"{base}/faction/{meeting1.id}/action/", {"action": "invite"})
check("Erneuter Versand nicht gesperrt", resp.status_code in (200, 302), f"got {resp.status_code}")
check("Aktualisierung: 3 E-Mails (ohne Abgesagte)", len(mail.outbox) == 3, f"outbox={len(mail.outbox)}")

meeting1.refresh_from_db()
check("ICS-Sequenz erhöht", meeting1.invitation_sequence == 1, str(meeting1.invitation_sequence))
check("Aktualisierungszeitpunkt gesetzt", meeting1.invitation_updated_at is not None)

update_mail = next((m for m in mail.outbox if m.to[0] == "vereidigt@example.org"), None)
check("Betreff 'Aktualisierte Einladung'", update_mail is not None and "Aktualisierte Einladung" in update_mail.subject)
ics2 = ics_text(next(c for n, c, _m in update_mail.attachments if n.endswith(".ics")))
check("ICS: SEQUENCE:1", "SEQUENCE:1" in ics2)
pdf_update = next(c for n, c, _m in update_mail.attachments if n.endswith(".pdf"))
check("Aktualisierungs-PDF enthält neuen TOP", "NACHTRAGS-TOP-NEU" in pdf_text(pdf_update))
check(
    "Abgesagtes Mitglied nicht erneut angeschrieben",
    all(m.to[0] != "sachkundig@example.org" for m in mail.outbox),
)

# =============================================================================
# Phase C: Erinnerungs-Auslösung (48 h vor Beginn, einmalig)
# =============================================================================
print()
print("=== Phase C: Erinnerungen ===")

# Sitzung liegt noch außerhalb des 48h-Fensters -> kein Versand
mail.outbox = []
stats = run_faction_reminder_pass()
check("Außerhalb 48h: keine Erinnerung", len(mail.outbox) == 0 and stats.get("meetings", 0) == 0, str(stats))

# Zusage des Vereidigten, Sitzung in 24 h -> Erinnerung an Zusagen/Vielleicht
resp = sworn.post(f"{base}/faction/{meeting1.id}/action/", {"action": "respond", "status": "confirmed"})
FactionMeeting.objects.filter(pk=meeting1.pk).update(start=now + timedelta(hours=24))
meeting1.refresh_from_db()

mail.outbox = []
stats = run_faction_reminder_pass()
check("Erinnerungslauf: 1 Sitzung", stats.get("meetings") == 1, str(stats))
check("Erinnerung nur an Zusagen", len(mail.outbox) == 1 and mail.outbox[0].to == ["vereidigt@example.org"])
check("Erinnerungs-Betreff", "Erinnerung" in mail.outbox[0].subject)
check("Erinnerung mit Deep-Link", deep_link in mail.outbox[0].body)

meeting1.refresh_from_db()
check("reminder_sent_at gesetzt", meeting1.reminder_sent_at is not None)

mail.outbox = []
stats = run_faction_reminder_pass()
check("Zweiter Lauf: keine weitere Erinnerung", len(mail.outbox) == 0 and stats.get("meetings", 0) == 0, str(stats))

# =============================================================================
# Phase D: Protokoll + Genehmigungskette über den Genehmigungs-TOP
# =============================================================================
print()
print("=== Phase D: Genehmigungskette ===")

# Sitzung 1 abschließen und protokollieren (Ö-, NÖ- und TOP-loser Eintrag)
FactionMeeting.objects.filter(pk=meeting1.pk).update(status="completed", end=now)
meeting1.refresh_from_db()

entry_pub = FactionProtocolEntry(meeting=meeting1, agenda_item=top_pub, entry_type="note", created_by=chair_ms, order=1)
entry_pub.set_content_encrypted("OEFFENTLICHER-EINTRAG-123")
entry_pub.save()
entry_int = FactionProtocolEntry(meeting=meeting1, agenda_item=top_int, entry_type="note", created_by=chair_ms, order=2)
entry_int.set_content_encrypted("GEHEIMER-EINTRAG-456")
entry_int.save()
entry_none = FactionProtocolEntry(meeting=meeting1, agenda_item=None, entry_type="note", created_by=chair_ms, order=3)
entry_none.set_content_encrypted("TOPLOSER-EINTRAG-789")
entry_none.save()

# Folgesitzung anlegen -> Genehmigungs-TOP + Vorprotokoll "Zur Genehmigung"
start2 = now + timedelta(days=14)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Fraktionssitzung Zwei",
        "start_date": timezone.localtime(start2).strftime("%Y-%m-%d"),
        "start_time": "18:00",
    },
)
meeting2 = FactionMeeting.objects.filter(organization=org, title="Fraktionssitzung Zwei").first()
check("Folgesitzung existiert", meeting2 is not None)
check("Vorherige Sitzung verkettet", meeting2.previous_meeting_id == meeting1.id)

approval2 = meeting2.agenda_items.filter(is_approval_item=True).first()
check("Genehmigungs-TOP der Folgesitzung", approval2 is not None and approval2.approves_meeting_id == meeting1.id)
meeting1.refresh_from_db()
check("Vorprotokoll im Status 'Zur Genehmigung'", meeting1.protocol_status == "pending", meeting1.protocol_status)

# Ohne protocols.approve: direkte Genehmigung wird abgelehnt
resp = sworn.post(f"{base}/faction/{meeting1.id}/action/", {"action": "approve_protocol"})
meeting1.refresh_from_db()
check(
    "Direkte Genehmigung ohne protocols.approve wirkungslos",
    meeting1.protocol_approved is False and meeting1.protocol_status == "pending",
)

# Abstimmung auf dem Genehmigungs-TOP (angenommen) -> Vorprotokoll genehmigt
resp = chair.post(
    f"{base}/faction/{meeting2.id}/action/",
    {
        "action": "record_decision",
        "agenda_item_id": str(approval2.id),
        "votes_yes": "5",
        "votes_no": "0",
        "votes_abstain": "1",
        "result": "accepted",
    },
)
check("Abstimmung erfasst -> OK", resp.status_code in (200, 302), f"got {resp.status_code}")
meeting1.refresh_from_db()
check("Vorprotokoll genehmigt (Flag)", meeting1.protocol_approved is True)
check("protocol_status = approved", meeting1.protocol_status == "approved", meeting1.protocol_status)
check("Genehmigt in Folgesitzung", meeting1.protocol_approved_in_id == meeting2.id)
check("Genehmigt durch Vorsitz", meeting1.protocol_approved_by_id == chair_ms.id)

# =============================================================================
# Phase E: Öffentliche Protokolle erst nach Opt-in + Leck-Beweis
# =============================================================================
print()
print("=== Phase E: Opt-in und Leck-Beweis ===")

anon = Client()
resp = anon.get(f"/public/{body.slug}/protokolle/")
check("Liste ohne Opt-in -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Genehmigtes Protokoll ohne Opt-in NICHT gelistet", "Fraktionssitzung Eins" not in resp.content.decode("utf-8"))
resp = anon.get(f"/public/{body.slug}/protokolle/{meeting1.id}/")
check("Detail ohne Opt-in -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Schalter nur mit protocols.publish: faction.manage allein reicht nicht
resp = manager.post(f"{base}/organization/faction-settings/", {"publish_protocols": "on"})
org.refresh_from_db()
check("Opt-in ohne protocols.publish unverändert", org.publish_protocols is False)

resp = chair.post(f"{base}/organization/faction-settings/", {"publish_protocols": "on"})
org.refresh_from_db()
check("Opt-in mit protocols.publish aktiviert", org.publish_protocols is True)

resp = anon.get(f"/public/{body.slug}/protokolle/")
check("Liste nach Opt-in zeigt Protokoll", "Fraktionssitzung Eins" in resp.content.decode("utf-8"))

resp = anon.get(f"/public/{body.slug}/protokolle/{meeting1.id}/")
html = resp.content.decode("utf-8")
check("Detail nach Opt-in -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Öffentlicher Eintrag sichtbar", "OEFFENTLICHER-EINTRAG-123" in html)
check("NÖ-Eintrag NICHT öffentlich", "GEHEIMER-EINTRAG-456" not in html)
check("Leck-Beweis: TOP-loser Eintrag NICHT öffentlich", "TOPLOSER-EINTRAG-789" not in html)
check("NÖ-TOP-Titel NICHT öffentlich", "GEHEIM-TOP-OMEGA" not in html)

# Nicht genehmigte Protokolle bleiben unsichtbar
resp = anon.get(f"/public/{body.slug}/protokolle/{meeting2.id}/")
check("Ungenehmigtes Protokoll -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
# Phase F: Niederschrift-PDF (öffentliche und interne Fassung)
# =============================================================================
print()
print("=== Phase F: Niederschrift-PDF ===")

resp = chair.get(f"{base}/faction/{meeting1.id}/niederschrift/oeffentlich.pdf")
check("Ö-PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text_pub = pdf_text(resp.content)
check("Ö-PDF: öffentlicher Eintrag", "OEFFENTLICHER-EINTRAG-123" in text_pub)
check("Ö-PDF: OHNE NÖ-Eintrag", "GEHEIMER-EINTRAG-456" not in text_pub)
check("Ö-PDF: OHNE TOP-losen Eintrag", "TOPLOSER-EINTRAG-789" not in text_pub)
check("Ö-PDF: OHNE NÖ-TOP", "GEHEIM-TOP-OMEGA" not in text_pub)

resp = chair.get(f"{base}/faction/{meeting1.id}/niederschrift/intern.pdf")
check("Intern-PDF -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
text_int = pdf_text(resp.content)
check("Intern-PDF: NÖ-Eintrag enthalten", "GEHEIMER-EINTRAG-456" in text_int)
check("Intern-PDF: TOP-loser Eintrag enthalten", "TOPLOSER-EINTRAG-789" in text_int)
check("Intern-PDF: Abstimmung/Teilnehmer", "Teilnehmerverzeichnis" in text_int)

resp = sworn.get(f"{base}/faction/{meeting1.id}/niederschrift/intern.pdf")
check("Vereidigt + protocols.view_full -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = unsworn.get(f"{base}/faction/{meeting1.id}/niederschrift/intern.pdf")
check("Nicht-Vereidigter: interne Fassung -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = unsworn.get(f"{base}/faction/{meeting1.id}/niederschrift/oeffentlich.pdf")
check("Nicht-Vereidigter: Ö-Fassung -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = manager.get(f"{base}/faction/{meeting1.id}/niederschrift/oeffentlich.pdf")
check("Ohne protocols.view_public -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
# Phase G: Permission-Matrix der Aktions-Handler
# =============================================================================
print()
print("=== Phase G: Permission-Matrix ===")

mail.outbox = []
resp = sworn.post(f"{base}/faction/{meeting2.id}/action/", {"action": "invite"})
check("invite ohne faction.invite: keine Mails", len(mail.outbox) == 0)
meeting2.refresh_from_db()
check("invite ohne Recht: Status unverändert", meeting2.invitation_sent is False)

resp = sworn.post(f"{base}/faction/{meeting2.id}/action/", {"action": "start"})
meeting2.refresh_from_db()
check("start ohne faction.start wirkungslos", meeting2.status != "ongoing", meeting2.status)

entry_count = meeting2.protocol_entries.count()
resp = sworn.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "add_entry", "entry_type": "note", "content": "unberechtigt"},
)
check(
    "add_entry ohne Protokollrecht -> 403",
    resp.status_code == 403 and meeting2.protocol_entries.count() == entry_count,
    f"got {resp.status_code}",
)

item_count = meeting2.agenda_items.count()
resp = unsworn.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "add_item", "title": "unberechtigt"},
)
check(
    "add_item ohne Agenda-Recht -> 403",
    resp.status_code == 403 and meeting2.agenda_items.count() == item_count,
    f"got {resp.status_code}",
)

resp = sworn.post(f"{base}/faction/{meeting2.id}/action/", {"action": "cancel"})
meeting2.refresh_from_db()
check("cancel ohne Recht wirkungslos", meeting2.status != "cancelled", meeting2.status)

resp = sworn.post(f"{base}/faction/{meeting2.id}/action/", {"action": "delete"})
check("delete ohne Recht: Sitzung bleibt", FactionMeeting.objects.filter(pk=meeting2.pk).exists())

# Genehmigung direkt mit protocols.approve setzt Status vollständig
meeting3 = FactionMeeting.objects.create(
    organization=org,
    title="Fraktionssitzung Drei",
    start=now - timedelta(days=1),
    status="completed",
    created_by=chair_ms,
)
resp = chair.post(f"{base}/faction/{meeting3.id}/action/", {"action": "approve_protocol"})
meeting3.refresh_from_db()
check(
    "Direkte Genehmigung setzt Flag UND Status",
    meeting3.protocol_approved is True and meeting3.protocol_status == "approved",
    f"{meeting3.protocol_approved}/{meeting3.protocol_status}",
)
check("Direkte Genehmigung: approved_by gesetzt", meeting3.protocol_approved_by_id == chair_ms.id)

# =============================================================================
# Phase H: Änderungshistorie (Issue #66)
# =============================================================================
print()
print("=== Phase H: Änderungshistorie (Audit) ===")

audit_qs = FactionAuditLog.objects.filter(organization=org)
check("Audit-Einträge vorhanden", audit_qs.exists(), "keine Einträge")

created_entry = audit_qs.filter(action="create", model_name="FactionMeeting", object_id=meeting1.id).first()
check("Sitzung anlegen protokolliert", created_entry is not None)
check(
    "Attribution: Ersteller erfasst (wer)",
    created_entry is not None and created_entry.membership_id == chair_ms.id,
    str(created_entry.membership_id if created_entry else None),
)
check("Zeitpunkt erfasst (wann)", created_entry is not None and created_entry.created_at is not None)

check(
    "Einladungsversand protokolliert",
    audit_qs.filter(action="invitation_sent", object_id=meeting1.id).exists(),
)
check(
    "Aktualisierungsversand protokolliert",
    audit_qs.filter(action="invitation_updated", object_id=meeting1.id).exists(),
)
check(
    "Erinnerung protokolliert",
    audit_qs.filter(action="reminder_sent", object_id=meeting1.id).exists(),
)
check(
    "Protokoll-Genehmigung protokolliert",
    audit_qs.filter(action="protocol_approved", object_id=meeting1.id).exists(),
)
check(
    "Protokoll 'Zur Genehmigung' protokolliert",
    audit_qs.filter(action="protocol_submitted", object_id=meeting1.id).exists(),
)
participation_entry = audit_qs.filter(action="participation", membership=unsworn_ms).first()
check("Teilnahme-Änderung protokolliert (Absage)", participation_entry is not None)
check(
    "Abstimmung auf Genehmigungs-TOP protokolliert",
    audit_qs.filter(action="decision", object_id=approval2.id).exists(),
)
check(
    "TOP-Erstellung protokolliert (inkl. NÖ-Kennzeichnung)",
    audit_qs.filter(action="create", object_id=top_int.id, is_internal=True).exists(),
)
check(
    "Ö-TOP ohne NÖ-Kennzeichnung",
    audit_qs.filter(action="create", object_id=top_pub.id, is_internal=False).exists(),
)

# Verschlüsselte Felder erscheinen nie im Klartext — Edit auf einer noch
# NICHT genehmigten Sitzung (genehmigte Protokolle sind seit #63 endgültig
# gesperrt, siehe Phase L)
meeting_h = FactionMeeting.objects.create(
    organization=org,
    title="Audit-Sitzung H",
    start=now - timedelta(days=2),
    status="completed",
    created_by=chair_ms,
)
top_int_h = FactionAgendaItem.objects.create(
    meeting=meeting_h, title="GEHEIM-TOP-H", number="NÖ 1", visibility="internal"
)
entry_h = FactionProtocolEntry(meeting=meeting_h, agenda_item=top_int_h, entry_type="note", created_by=chair_ms, order=1)
entry_h.set_content_encrypted("GEHEIM-ALT-H")
entry_h.save()
entry_h.set_content_encrypted("SUPERGEHEIM-NEU-999")
entry_h.save()
masked_entry = audit_qs.filter(model_name="FactionProtocolEntry", object_id=entry_h.id, action="update").first()
check("Änderung an verschlüsseltem Feld protokolliert", masked_entry is not None)
if masked_entry:
    changes_text = str(masked_entry.changes)
    check("Verschlüsselter Inhalt maskiert", "[verschlüsselt geändert]" in changes_text, changes_text[:200])
    check("Klartext NICHT im Diff", "SUPERGEHEIM-NEU-999" not in changes_text)
    check("Klartext NICHT in der Objekt-Beschreibung", "GEHEIM" not in masked_entry.object_repr)

# Unveränderbarkeit (Save-/Delete-Guard)
guard_entry = audit_qs.first()
try:
    guard_entry.action = "update"
    guard_entry.save()
    check("Audit-Eintrag unveränderbar (save)", False)
except ValueError:
    check("Audit-Eintrag unveränderbar (save)", True)
try:
    guard_entry.delete()
    check("Audit-Eintrag unlöschbar (delete)", False)
except ValueError:
    check("Audit-Eintrag unlöschbar (delete)", True)

# Einsichts-View: nur mit faction.view_audit
resp = chair.get(f"{base}/faction/historie/")
check("Historie mit faction.view_audit -> 200", resp.status_code == 200, f"got {resp.status_code}")
resp = unsworn.get(f"{base}/faction/historie/")
check("Historie ohne faction.view_audit -> 403", resp.status_code == 403, f"got {resp.status_code}")

# NÖ-Maskierung in der Historie (Issue #64): Berechtigter, aber nicht vereidigt
auditor_user, auditor_ms, auditor = make_member(
    org, "auditor@example.org", ["faction.view_public", "faction.view_audit"], sworn=False
)
resp = auditor.get(f"{base}/faction/historie/?object={top_int.id}")
html = resp.content.decode("utf-8")
check("Historie für Nicht-Vereidigte -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Historie: NÖ-Eintrag als 'Gesperrte Information'", "Gesperrte Information" in html)
check("Historie: NÖ-Titel NICHT sichtbar", "GEHEIM-TOP-OMEGA" not in html)

resp = chair.get(f"{base}/faction/historie/?object={top_int.id}")
html = resp.content.decode("utf-8")
check("Historie: Vereidigter sieht NÖ-Titel", "GEHEIM-TOP-OMEGA" in html)

# Kaskadenlösch-Schutz (Muster aus Session-#56): Organisation löschen
org2 = Organization.objects.create(name="Wegwerf-Fraktion", slug="wegwerf-fraktion", body=body)
TenantEncryption(org2).key
scrap_meeting = FactionMeeting.objects.create(
    organization=org2, title="Wegwerf-Sitzung", start=now + timedelta(days=3), status="planned"
)
FactionAgendaItem.objects.create(meeting=scrap_meeting, title="Wegwerf-TOP", number="1", visibility="public")
org2_id = org2.pk
check("Wegwerf-Audit-Einträge vorhanden", FactionAuditLog.objects.filter(organization_id=org2_id).exists())
try:
    org2.delete()
    check("Organisations-Löschung ohne IntegrityError", True)
except Exception as exc:  # noqa: BLE001
    check("Organisations-Löschung ohne IntegrityError", False, str(exc))
check(
    "Audit-Einträge mitkaskadiert (keine Waisen)",
    FactionAuditLog.objects.filter(organization_id=org2_id).count() == 0,
)

# =============================================================================
# Phase I: Sitzungserzeugung aus der Sitzungsreihe + Ausfallregeln (Issue #61)
# =============================================================================
print()
print("=== Phase I: Sitzungserzeugung + Ausfallregeln ===")

today = timezone.localdate()
first_occ = today + timedelta(days=3)
weekday = first_occ.weekday()

# Reihe über die Einstellungs-UI anlegen (faction.manage erforderlich)
resp = chair.post(
    f"{base}/organization/faction-settings/",
    {
        "section": "add_schedule",
        "name": "Wöchentliche Fraktionssitzung",
        "recurrence": "weekly",
        "weekday": str(weekday),
        "time": "19:00",
        "duration_minutes": "90",
        "default_location": "Fraktionsbüro",
    },
)
schedule = FactionMeetingSchedule.objects.filter(organization=org, name="Wöchentliche Fraktionssitzung").first()
check("Sitzungsreihe über UI angelegt", schedule is not None)

# Ausfallregel 1: Urlaubszeitraum um den 2. Termin
occ2 = first_occ + timedelta(days=7)
occ3 = first_occ + timedelta(days=14)
resp = chair.post(
    f"{base}/organization/faction-settings/",
    {
        "section": "add_exception",
        "schedule_id": str(schedule.id),
        "original_date": (occ2 - timedelta(days=2)).isoformat(),
        "end_date": (occ2 + timedelta(days=2)).isoformat(),
        "reason": "Sommerpause",
    },
)
check(
    "Urlaubszeitraum gespeichert",
    FactionMeetingException.objects.filter(schedule=schedule, reason="Sommerpause").exists(),
)

# Ausfallregel 2: RIS-Regel — nach Ratssitzung entfällt die nächste Fraktionssitzung
rat = OParlOrganization.objects.create(
    body=body,
    external_id="https://ris.musterstadt.example/organization/rat",
    name="Rat der Stadt Musterstadt",
    organization_type="Gremium",
)
rat_meeting_start = timezone.make_aware(_datetime.combine(occ3 - timedelta(days=1), _time(hour=17)))
rat_meeting = OParlMeeting.objects.create(
    body=body,
    external_id="https://ris.musterstadt.example/meeting/rat-1",
    name="Ratssitzung",
    start=rat_meeting_start,
    cancelled=False,
)
rat_meeting.organizations.add(rat)

resp = chair.post(
    f"{base}/organization/faction-settings/",
    {
        "section": "add_rule",
        "schedule_id": str(schedule.id),
        "ris_organization_id": str(rat.id),
    },
)
check(
    "RIS-Ausfallregel gespeichert",
    FactionSuspensionRule.objects.filter(schedule=schedule, ris_organization=rat).exists(),
)

# Der Opt-in-POST in Phase E hat die übrigen Checkboxen (u.a.
# auto_create_approval_item) auf False gesetzt — für die Erzeugung wieder an
org.refresh_from_db()
_settings = org.settings or {}
_settings.setdefault("faction", {})["auto_create_approval_item"] = True
org.settings = _settings
org.save(update_fields=["settings"])

# Erzeugungslauf (rollierender Horizont, Standard 90 Tage)
stats = run_faction_schedule_pass()
expected_dates = []
d = first_occ
while d <= today + timedelta(days=90):
    expected_dates.append(d)
    d += timedelta(days=7)

generated = FactionMeeting.objects.filter(schedule=schedule, scheduled_date__isnull=False)
check(
    f"Alle Solltermine erzeugt ({len(expected_dates)})",
    generated.count() == len(expected_dates),
    f"stats={stats}, count={generated.count()}",
)

m_occ1 = generated.filter(scheduled_date=first_occ).first()
m_occ2 = generated.filter(scheduled_date=occ2).first()
m_occ3 = generated.filter(scheduled_date=occ3).first()
check("Termin 1 geplant", m_occ1 is not None and m_occ1.status == "planned")
check("Termin 1: Titel/Ort/Zeit aus der Reihe", m_occ1 is not None and m_occ1.location == "Fraktionsbüro")
check(
    "Termin 1: Anwesenheiten für alle Mitglieder",
    m_occ1 is not None and m_occ1.attendances.count() == org.memberships.filter(is_active=True).count(),
)
check(
    "Termin 1: Genehmigungs-TOP automatisch",
    m_occ1 is not None and m_occ1.agenda_items.filter(is_approval_item=True).exists(),
)
check("Termin 1: automatisch erzeugt (created_by leer)", m_occ1 is not None and m_occ1.created_by_id is None)

check(
    "Urlaubstermin entfällt ersatzlos",
    m_occ2 is not None and m_occ2.status == "cancelled" and "Sommerpause" in m_occ2.cancellation_reason,
    m_occ2.cancellation_reason if m_occ2 else "fehlt",
)
check(
    "Termin nach Ratssitzung entfällt (RIS-Regel)",
    m_occ3 is not None and m_occ3.status == "cancelled" and "Rat" in m_occ3.cancellation_reason,
    m_occ3.cancellation_reason if m_occ3 else "fehlt",
)
check(
    "Entfallene Termine ohne Einladung/Anwesenheiten",
    m_occ2 is not None and m_occ2.invitation_sent is False and m_occ2.attendances.count() == 0,
)
check(
    "Kein Verschieben: kein Ersatztermin",
    not FactionMeeting.objects.filter(schedule=schedule, scheduled_date__isnull=True).exists(),
)

# Ausfallregeln-Matrix: erwartete Zahl entfallener/geplanter Termine
cancelled_count = generated.filter(status="cancelled").count()
planned_count = generated.filter(status="planned").count()
check(
    "Ausfallregeln-Matrix: genau 2 Termine entfallen",
    cancelled_count == 2 and planned_count == len(expected_dates) - 2,
    f"cancelled={cancelled_count}, planned={planned_count}",
)

# Idempotenz: zweiter Lauf erzeugt nichts Neues
count_before = FactionMeeting.objects.filter(schedule=schedule).count()
stats2 = run_faction_schedule_pass()
check(
    "Zweiter Lauf idempotent",
    FactionMeeting.objects.filter(schedule=schedule).count() == count_before
    and stats2.get("created", 0) == 0
    and stats2.get("cancelled", 0) == 0,
    str(stats2),
)

# Historisierung (Issue #66): Erzeugung und Ausfälle im Audit
check(
    "Audit: automatisch erzeugte Termine protokolliert",
    FactionAuditLog.objects.filter(organization=org, action="generated").count() == planned_count,
)
check(
    "Audit: entfallene Termine protokolliert",
    FactionAuditLog.objects.filter(organization=org, action="auto_cancelled").count() == 2,
)

# Entfällt-Anzeige in Liste und Detail
resp = chair.get(f"{base}/faction/?time=all&status=cancelled")
html = resp.content.decode("utf-8")
check("Liste: entfallene Termine sichtbar", "Entfällt" in html and "Sommerpause" in html)
resp = chair.get(f"{base}/faction/{m_occ2.id}/")
check("Detail: Ausfallgrund sichtbar", "entfällt ersatzlos" in resp.content.decode("utf-8"))

# Pausierte Reihe erzeugt nichts
FactionMeetingSchedule.objects.filter(pk=schedule.pk).update(is_active=False)
FactionMeeting.objects.filter(schedule=schedule, scheduled_date=first_occ).delete()
stats3 = run_faction_schedule_pass()
check(
    "Pausierte Reihe erzeugt nichts",
    not FactionMeeting.objects.filter(schedule=schedule, scheduled_date=first_occ).exists(),
    str(stats3),
)
FactionMeetingSchedule.objects.filter(pk=schedule.pk).update(is_active=True)

# =============================================================================
# Phase J: NÖ strikt für Vereidigte (Issue #64)
# =============================================================================
print()
print("=== Phase J: NÖ-Abschottung über alle Ausgabewege ===")

# Detailansicht: Nicht-Vereidigte sehen NUR "Gesperrte Information"
resp = unsworn.get(f"{base}/faction/{meeting1.id}/")
html = resp.content.decode("utf-8")
check("Detail (Nicht-Vereidigter) -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Detail: Platzhalter 'Gesperrte Information'", "Gesperrte Information" in html)
check("Detail: NÖ-Titel NICHT sichtbar", "GEHEIM-TOP-OMEGA" not in html)
check("Detail: NÖ-Protokolleintrag NICHT sichtbar", "GEHEIMER-EINTRAG-456" not in html)
check("Detail: gesperrte Anzahl angezeigt", "gesperrt" in html)

resp = sworn.get(f"{base}/faction/{meeting1.id}/")
html = resp.content.decode("utf-8")
check("Detail: Vereidigter sieht NÖ-TOP", "GEHEIM-TOP-OMEGA" in html)

# Panel: NÖ-TOP serverseitig gesperrt (kein Titel, keine Inhalte, keine Anhänge)
resp = unsworn.get(f"{base}/faction/{meeting1.id}/item/{top_int.id}/panel/")
check("Panel NÖ-TOP (Nicht-Vereidigter) -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = sworn.get(f"{base}/faction/{meeting1.id}/item/{top_int.id}/panel/")
check("Panel NÖ-TOP (Vereidigter) -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Unvereidigter Verwalter: faction.manage ersetzt die Vereidigung NICHT
manager2_user, manager2_ms, manager2 = make_member(
    org,
    "verwaltung2@example.org",
    ["faction.view_public", "faction.manage", "agenda.manage", "protocols.create"],
    sworn=False,
)

resp = manager2.get(f"{base}/faction/{meeting1.id}/item/{top_int.id}/panel/")
check("Panel NÖ-TOP (unvereidigter Verwalter) -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = manager2.post(
    f"{base}/faction/{meeting1.id}/item/{top_int.id}/panel/action/",
    {"action": "update", "title": "gekapert"},
)
top_int.refresh_from_db()
check(
    "Panel-Aktion auf NÖ-TOP -> 403",
    resp.status_code == 403 and top_int.title == "GEHEIM-TOP-OMEGA",
    f"got {resp.status_code}",
)

# NÖ-TOP auf geplanter Sitzung für Aktions-Matrix
top_int2 = FactionAgendaItem.objects.create(
    meeting=meeting2, title="GEHEIM-ZWEI-PSI", number="NÖ 1", visibility="internal", order=5
)

item_count = meeting2.agenda_items.count()
resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "add_item", "title": "nö-versuch", "visibility": "internal"},
)
check(
    "add_item NÖ durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and meeting2.agenda_items.count() == item_count,
    f"got {resp.status_code}",
)
resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "edit_item", "item_id": str(top_int2.id), "title": "gekapert"},
)
top_int2.refresh_from_db()
check(
    "edit_item NÖ durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and top_int2.title == "GEHEIM-ZWEI-PSI",
    f"got {resp.status_code}",
)
resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "delete_item", "item_id": str(top_int2.id)},
)
check(
    "delete_item NÖ durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and FactionAgendaItem.objects.filter(pk=top_int2.pk).exists(),
    f"got {resp.status_code}",
)
entry_count = meeting2.protocol_entries.count()
resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "add_entry", "entry_type": "note", "content": "leck", "agenda_item_id": str(top_int2.id)},
)
check(
    "add_entry zu NÖ-TOP durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and meeting2.protocol_entries.count() == entry_count,
    f"got {resp.status_code}",
)
resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {
        "action": "record_decision",
        "agenda_item_id": str(top_int2.id),
        "votes_yes": "1",
        "votes_no": "0",
        "votes_abstain": "0",
        "result": "accepted",
    },
)
top_int2.refresh_from_db()
check(
    "record_decision auf NÖ-TOP durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and top_int2.has_decision is False,
    f"got {resp.status_code}",
)

# Vorschlagen von NÖ-TOPs: nur Vereidigte (serverseitig)
skb_user, skb_ms, skb = make_member(org, "skb2@example.org", ["faction.view_public", "agenda.propose"], sworn=False)
proposal_count = meeting2.agenda_items.filter(proposal_status="proposed").count()
resp = skb.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "propose", "title": "NÖ-VORSCHLAG-LECK", "visibility": "internal"},
)
check(
    "NÖ-Vorschlag durch Nicht-Vereidigten abgelehnt",
    meeting2.agenda_items.filter(proposal_status="proposed").count() == proposal_count,
    f"got {resp.status_code}",
)
resp = skb.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "propose", "title": "OEFF-VORSCHLAG-OK", "visibility": "public"},
)
check(
    "Ö-Vorschlag durch Nicht-Vereidigten möglich",
    meeting2.agenda_items.filter(proposal_status="proposed", title="OEFF-VORSCHLAG-OK").exists(),
)

# NÖ-Vorschlag eines Vereidigten ist für unvereidigte Verwalter unsichtbar
resp = chair.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "propose", "title": "GEHEIM-VORSCHLAG-XI", "visibility": "internal"},
)
noe_proposal = meeting2.agenda_items.filter(proposal_status="proposed", title="GEHEIM-VORSCHLAG-XI").first()
check("NÖ-Vorschlag durch Vereidigten angelegt", noe_proposal is not None)

resp = manager2.get(f"{base}/faction/{meeting2.id}/")
html = resp.content.decode("utf-8")
check("NÖ-Vorschlag NICHT in Ansicht des Nicht-Vereidigten", "GEHEIM-VORSCHLAG-XI" not in html)
check("Ö-Vorschlag sichtbar für Verwalter", "OEFF-VORSCHLAG-OK" in html)

resp = manager2.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "accept_proposal", "item_id": str(noe_proposal.id)},
)
noe_proposal.refresh_from_db()
check(
    "accept_proposal auf NÖ-Vorschlag durch Nicht-Vereidigten -> 403",
    resp.status_code == 403 and noe_proposal.proposal_status == "proposed",
    f"got {resp.status_code}",
)

# Zentrale Sichtbarkeitsfunktion speist auch die PDFs (#59/#60):
# Einladungs-PDF ohne NÖ (Phase A) und Ö-Niederschrift ohne NÖ (Phase F)
# wurden oben bereits bewiesen — hier der Panel-/Historien-Beweis:
resp = auditor.get(f"{base}/faction/historie/?object={top_int2.id}")
html = resp.content.decode("utf-8")
check("Historie: NÖ-TOP 2 maskiert", "GEHEIM-ZWEI-PSI" not in html)

# =============================================================================
# Phase K: Einladungslogik je Organisation (Issue #62)
# =============================================================================
print()
print("=== Phase K: Einladungslogik — Opt-in/Opt-out, Vorlauf, Freigabe ===")

from unittest import mock  # noqa: E402

from apps.tenants.models import Role as _Role  # noqa: E402
from apps.work.faction.invitations import (  # noqa: E402
    can_release_invitations,
    get_invitation_settings,
    is_board_member,
    run_faction_invitation_pass,
)
from apps.work.notifications.models import Notification, NotificationPreference  # noqa: E402

# Stellv. Vorsitz: Vorstands-Rolle OHNE faction.invite — beweist, dass die
# Rolle allein zur Freigabe berechtigt (Vertretung ohne formale Delegation)
stellv_role, _ = _Role.objects.get_or_create(organization=org, name="Stellv. Vorsitz")
stellv_role.is_admin = False
stellv_role.save()
stellv_role.permissions.set([perm("faction.view_public")])
stellv_user = User.objects.create_user(email="stellv@example.org", password="pw-Smoke-Test-1!")
stellv_ms = Membership.objects.create(user=stellv_user, organization=org)
stellv_ms.roles.add(stellv_role)
stellv = Client()
stellv.force_login(stellv_user)

check("Stellv. Vorsitz ist Vorstand (Rolle)", is_board_member(stellv_ms) is True)
check("Stellv. Vorsitz ohne faction.invite", stellv_ms.has_permission("faction.invite") is False)
check("Stellv. Vorsitz darf freigeben", can_release_invitations(stellv_ms) is True)
check("Vereidigter ohne Rolle/Recht darf NICHT freigeben", can_release_invitations(sworn_ms) is False)

# Einstellungen: Opt-out, 48 h Vorlauf, Versand nach Freigabe (über die UI)
resp = chair.post(
    f"{base}/organization/faction-settings/",
    {
        "auto_create_approval_item": "on",
        "link_previous_meeting": "on",
        "protocol_revision_safe": "on",
        "auto_lock_protocol_on_complete": "on",
        "require_protocol_approval": "on",
        "publish_protocols": "on",
        "invitation_mode": "opt_out",
        "invitation_lead_hours": "48",
        "invitation_dispatch": "approval",
        "quorum_rule": "majority",
    },
)
org.refresh_from_db()
inv_settings = get_invitation_settings(org)
check(
    "Einstellungen gespeichert (Opt-out, 48 h, Freigabe)",
    inv_settings["invitation_mode"] == "opt_out"
    and inv_settings["invitation_lead_hours"] == 48
    and inv_settings["invitation_dispatch"] == "approval",
    str(inv_settings),
)

# Sitzung in 36 h -> Versandzeitpunkt (Beginn - 48 h) liegt bereits in der
# Vergangenheit -> Freigabe-Hinweis (3 h) fällig, aber KEIN Versand
start4 = (now + timedelta(hours=36)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Freigabe-Sitzung K4",
        "start_date": timezone.localtime(start4).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start4).strftime("%H:%M"),
    },
)
meeting4 = FactionMeeting.objects.filter(organization=org, title="Freigabe-Sitzung K4").first()
check("Sitzung K4 existiert", meeting4 is not None)

mail.outbox = []
stats = run_faction_invitation_pass()
meeting4.refresh_from_db()
check("Freigabe-Modus: KEIN automatischer Versand", meeting4.invitation_sent is False, str(stats))
check("Freigabe-Hinweis versandt (Statistik)", stats.get("notices", 0) >= 1, str(stats))
check(
    "Hinweis-E-Mail nur an den Vorstand (Stellv. Vorsitz)",
    len(mail.outbox) == 1 and mail.outbox[0].to == ["stellv@example.org"],
    f"outbox={[(m.to, m.subject) for m in mail.outbox]}",
)
check("Hinweis-Betreff 'Freigabe erforderlich'", "Freigabe erforderlich" in mail.outbox[0].subject)
check(
    "In-App-Benachrichtigung für den Vorstand",
    Notification.objects.filter(
        recipient=stellv_ms, notification_type="faction_inv_release", metadata__meeting_id=str(meeting4.id)
    ).exists(),
)
check("Hinweis-Zeitstempel gesetzt", meeting4.release_notice_final_sent_at is not None)
check(
    "Freigabe-Hinweis auditiert",
    FactionAuditLog.objects.filter(organization=org, action="release_notice_sent", object_id=meeting4.id).exists(),
)

mail.outbox = []
stats2 = run_faction_invitation_pass()
check("Zweiter Lauf: kein doppelter Hinweis", len(mail.outbox) == 0, str(stats2))

# Nicht-Berechtigter kann nicht freigeben
resp = sworn.post(f"{base}/faction/{meeting4.id}/action/", {"action": "release_invitations"})
meeting4.refresh_from_db()
check("Freigabe durch Unberechtigten wirkungslos", meeting4.invitation_released_at is None)

# Freigabe durch den stellv. Vorsitz -> sofortiger Versand (Zeitpunkt überschritten)
mail.outbox = []
resp = stellv.post(f"{base}/faction/{meeting4.id}/action/", {"action": "release_invitations"})
meeting4.refresh_from_db()
check("Freigabe durch Stellv. -> OK", resp.status_code in (200, 302), f"got {resp.status_code}")
check("Nach Freigabe versendet", meeting4.invitation_sent is True and len(mail.outbox) > 0)
check("Freigebender auditiert (Feld)", meeting4.invitation_released_by_id == stellv_ms.id)
release_audit = FactionAuditLog.objects.filter(
    organization=org, action="invitation_released", object_id=meeting4.id
).first()
check(
    "Audit: invitation_released mit Akteur", release_audit is not None and release_audit.membership_id == stellv_ms.id
)

# Opt-out: alle angeschriebenen Mitglieder gelten als angemeldet ("Zugesagt")
member_states = list(meeting4.attendances.filter(membership__isnull=False).values_list("status", flat=True))
check(
    "Opt-out: alle Mitglieder auf 'Zugesagt'",
    member_states and all(s == "confirmed" for s in member_states),
    str(member_states),
)
optout_mail = mail.outbox[0]
check("Opt-out-Hinweistext in der E-Mail", "angemeldet" in optout_mail.body)

# Abmelden bleibt möglich (Opt-out = man meldet sich AB)
resp = sworn.post(f"{base}/faction/{meeting4.id}/action/", {"action": "respond", "status": "declined"})
check(
    "Opt-out: Absage weiterhin möglich",
    meeting4.attendances.get(membership=sworn_ms).status == "declined",
)

# E-Mail-Hinweise individuell abschaltbar (Benachrichtigungseinstellungen)
prefs, _ = NotificationPreference.objects.get_or_create(membership=stellv_ms)
prefs.type_settings = {"faction_inv_release": {"in_app": True, "email": False}}
prefs.save()

start5 = (now + timedelta(hours=30)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Freigabe-Sitzung K5",
        "start_date": timezone.localtime(start5).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start5).strftime("%H:%M"),
    },
)
meeting5 = FactionMeeting.objects.filter(organization=org, title="Freigabe-Sitzung K5").first()
mail.outbox = []
run_faction_invitation_pass()
check("Abgeschaltete Hinweis-Mail: keine E-Mail", len(mail.outbox) == 0)
check(
    "Abgeschaltete Hinweis-Mail: In-App bleibt",
    Notification.objects.filter(
        recipient=stellv_ms, notification_type="faction_inv_release", metadata__meeting_id=str(meeting5.id)
    ).exists(),
)
FactionMeeting.objects.filter(pk=meeting5.pk).update(status="cancelled")

# Opt-in + automatischer Versand zum Vorlaufzeitpunkt
_settings = org.settings or {}
_settings.setdefault("faction", {}).update({"invitation_mode": "opt_in", "invitation_dispatch": "automatic"})
org.settings = _settings
org.save(update_fields=["settings"])

start6 = (now + timedelta(hours=36)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Auto-Sitzung K6",
        "start_date": timezone.localtime(start6).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start6).strftime("%H:%M"),
    },
)
meeting6 = FactionMeeting.objects.filter(organization=org, title="Auto-Sitzung K6").first()
mail.outbox = []
stats = run_faction_invitation_pass()
meeting6.refresh_from_db()
check(
    "Automatischer Versand zum Vorlaufzeitpunkt", meeting6.invitation_sent is True and len(mail.outbox) > 0, str(stats)
)
check("Status nach Auto-Versand = invited", meeting6.status == "invited")
optin_states = set(meeting6.attendances.filter(membership__isnull=False).values_list("status", flat=True))
check("Opt-in: Status bleibt 'Eingeladen'", optin_states == {"invited"}, str(optin_states))
check(
    "Auto-Versand auditiert (invitation_sent)",
    FactionAuditLog.objects.filter(organization=org, action="invitation_sent", object_id=meeting6.id).exists(),
)

mail.outbox = []
stats2 = run_faction_invitation_pass()
check("Auto-Versand einmalig", len(mail.outbox) == 0, str(stats2))

# =============================================================================
# Phase L: Standard-TOP 1 + endgültige Protokollsperre + Nachtrag (Issue #63)
# =============================================================================
print()
print("=== Phase L: Standard-TOP 1, Endgültigkeit, Nachtrag ===")

from apps.work.faction.services import ProtocolApprovalService  # noqa: E402

# Verbindlicher Default-Text (leere Vorlagen fallen auf den Default zurück)
ml_a = FactionMeeting.objects.create(
    organization=org,
    title="L-Sitzung A",
    start=now - timedelta(days=3),
    status="completed",
    created_by=chair_ms,
)
ml_entry = FactionProtocolEntry(meeting=ml_a, entry_type="note", created_by=chair_ms, order=1)
ml_entry.set_content_encrypted("L-ORIGINAL-EINTRAG")
ml_entry.save()

ml_b = FactionMeeting.objects.create(
    organization=org,
    title="L-Sitzung B",
    start=now + timedelta(days=21),
    status="planned",
    created_by=chair_ms,
    previous_meeting=ml_a,
)
ml_approval = ProtocolApprovalService.auto_create_approval_item(ml_b)
check(
    "Standard-TOP 1: verbindlicher Default-Text",
    ml_approval is not None and ml_approval.title == "Tagesordnung festlegen und letztes Protokoll genehmigen",
    ml_approval.title if ml_approval else "fehlt",
)

# Während des TOPs: neue TOPs zur aktuellen Sitzung aufnehmen (Vorsitz)
resp = chair.post(
    f"{base}/faction/{ml_b.id}/action/",
    {"action": "add_item", "title": "L-NEUER-TOP-WAEHREND-TOP1"},
)
check(
    "Neue TOPs zur aktuellen Sitzung möglich",
    ml_b.agenda_items.filter(title="L-NEUER-TOP-WAEHREND-TOP1").exists(),
)

# Vorprotokoll-Einträge der VORHERIGEN Sitzung vor der Genehmigung anpassbar
resp = chair.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "edit_entry", "entry_id": str(ml_entry.id), "content": "L-ORIGINAL-EINTRAG-ANGEPASST"},
)
ml_entry.refresh_from_db()
check(
    "Vorprotokoll vor Genehmigung anpassbar (Vorsitz)",
    ml_entry.get_content_decrypted() == "L-ORIGINAL-EINTRAG-ANGEPASST",
)

# "Ja mit Änderungen" (modified) genehmigt das Vorprotokoll ebenfalls
resp = chair.post(
    f"{base}/faction/{ml_b.id}/action/",
    {
        "action": "record_decision",
        "agenda_item_id": str(ml_approval.id),
        "votes_yes": "4",
        "votes_no": "0",
        "votes_abstain": "1",
        "result": "modified",
    },
)
ml_a.refresh_from_db()
check(
    "'Ja mit Änderungen' genehmigt das Vorprotokoll",
    ml_a.protocol_approved is True and ml_a.protocol_status == "approved",
    f"{ml_a.protocol_approved}/{ml_a.protocol_status}",
)

# Endgültige Sperre — auch für den Vorsitz mit allen Rechten ("Admin")
resp = chair.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "edit_entry", "entry_id": str(ml_entry.id), "content": "HACK-VERSUCH"},
)
ml_entry.refresh_from_db()
check(
    "Nach Genehmigung: edit_entry -> 403, Inhalt unverändert",
    resp.status_code == 403 and ml_entry.get_content_decrypted() == "L-ORIGINAL-EINTRAG-ANGEPASST",
    f"got {resp.status_code}",
)
resp = chair.post(f"{base}/faction/{ml_a.id}/action/", {"action": "delete_entry", "entry_id": str(ml_entry.id)})
check(
    "Nach Genehmigung: delete_entry -> 403, Eintrag bleibt",
    resp.status_code == 403 and FactionProtocolEntry.objects.filter(pk=ml_entry.pk).exists(),
    f"got {resp.status_code}",
)

# Modellebene: Save-/Delete-Guard greift auch ohne View
try:
    ml_entry.set_content_encrypted("HACK-DIREKT")
    ml_entry.save()
    check("Modell-Sperre: save() verweigert", False)
except ValueError:
    check("Modell-Sperre: save() verweigert", True)
try:
    ml_entry.delete()
    check("Modell-Sperre: delete() verweigert", False)
except ValueError:
    check("Modell-Sperre: delete() verweigert", True)
ml_entry.refresh_from_db()
check("Original nach Sperr-Versuchen intakt", ml_entry.get_content_decrypted() == "L-ORIGINAL-EINTRAG-ANGEPASST")

# Normale Einträge nach Genehmigung: abgelehnt (nur Nachtrag)
entry_count = ml_a.protocol_entries.count()
resp = chair.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "add_entry", "entry_type": "note", "content": "kein-normaler-eintrag"},
)
check(
    "Nach Genehmigung: normale Einträge -> 403",
    resp.status_code == 403 and ml_a.protocol_entries.count() == entry_count,
    f"got {resp.status_code}",
)

# Nachtrag durch den Vorsitz: sichtbar gekennzeichnet + auditiert
resp = chair.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "add_entry", "entry_type": "addendum", "content": "NACHTRAG-KORREKTUR-001"},
)
addendum = ml_a.protocol_entries.filter(entry_type="addendum").first()
check("Nachtrag angelegt", addendum is not None and addendum.get_content_decrypted() == "NACHTRAG-KORREKTUR-001")
check("Nachtrag sichtbar gekennzeichnet", addendum is not None and addendum.get_entry_type_display() == "Nachtrag")
check(
    "Nachtrag auditiert",
    addendum is not None
    and FactionAuditLog.objects.filter(organization=org, action="addendum", object_id=addendum.id).exists(),
)

# Nachtrag ohne Protokollrechte -> 403
resp = sworn.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "add_entry", "entry_type": "addendum", "content": "unberechtigt"},
)
check(
    "Nachtrag ohne Protokollrecht -> 403",
    resp.status_code == 403 and ml_a.protocol_entries.filter(entry_type="addendum").count() == 1,
    f"got {resp.status_code}",
)

# Auch Nachträge sind nach dem Anlegen unveränderbar
resp = chair.post(
    f"{base}/faction/{ml_a.id}/action/",
    {"action": "edit_entry", "entry_id": str(addendum.id), "content": "nachtrag-manipuliert"},
)
addendum.refresh_from_db()
check(
    "Nachtrag selbst unveränderbar",
    resp.status_code == 403 and addendum.get_content_decrypted() == "NACHTRAG-KORREKTUR-001",
    f"got {resp.status_code}",
)

# =============================================================================
# Phase M: Teilnahme-Workflow — Teilnahmeart + Vorstands-Bestätigung (Issue #67)
# =============================================================================
print()
print("=== Phase M: Teilnahmeart + Vorstands-Bestätigung ===")

start_m = (now + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Teilnahme-Sitzung M",
        "start_date": timezone.localtime(start_m).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start_m).strftime("%H:%M"),
    },
)
m67 = FactionMeeting.objects.filter(organization=org, title="Teilnahme-Sitzung M").first()
check("Sitzung M existiert", m67 is not None)
FactionMeeting.objects.filter(pk=m67.pk).update(status="ongoing")
m67.refresh_from_db()

att_sworn = m67.attendances.get(membership=sworn_ms)
att_unsworn = m67.attendances.get(membership=unsworn_ms)

# Einchecken mit Teilnahmeart online (Geschäftsführung/Fraktionspersonal)
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "check_in", "attendance_id": str(att_sworn.id), "participation_type": "online"},
)
att_sworn.refresh_from_db()
check(
    "Check-in mit Teilnahmeart online",
    att_sworn.status == "present" and att_sworn.participation_type == "online",
    f"{att_sworn.status}/{att_sworn.participation_type}",
)

resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "check_in", "attendance_id": str(att_unsworn.id)},
)
att_unsworn.refresh_from_db()
check("Check-in Default vor Ort", att_unsworn.status == "present" and att_unsworn.participation_type == "onsite")

# Teilnahmeart umschalten
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "set_participation", "attendance_id": str(att_sworn.id), "participation_type": "onsite"},
)
att_sworn.refresh_from_db()
check("Teilnahmeart umschaltbar", att_sworn.participation_type == "onsite")
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "set_participation", "attendance_id": str(att_sworn.id), "participation_type": "online"},
)
att_sworn.refresh_from_db()
check("Teilnahmeart zurück auf online", att_sworn.participation_type == "online")

# Bestätigung erst NACH der Sitzung
resp = stellv.post(f"{base}/faction/{m67.id}/action/", {"action": "confirm_attendance"})
m67.refresh_from_db()
check("Bestätigung während der Sitzung abgelehnt", m67.attendance_confirmed_at is None)

FactionMeeting.objects.filter(pk=m67.pk).update(status="completed", end=timezone.now())
m67.refresh_from_db()

# Geschäftsführung (faction.manage, kein Vorstand) darf NICHT bestätigen
resp = manager.post(f"{base}/faction/{m67.id}/action/", {"action": "confirm_attendance"})
m67.refresh_from_db()
check("faction.manage ersetzt den Vorstand nicht", m67.attendance_confirmed_at is None)

# Stellv. Vorsitz bestätigt direkt (ohne formale Delegation) — dokumentiert
resp = stellv.post(f"{base}/faction/{m67.id}/action/", {"action": "confirm_attendance"})
m67.refresh_from_db()
check("Bestätigung durch Stellv. -> gesetzt", m67.attendance_confirmed_at is not None)
check("Bestätiger dokumentiert", m67.attendance_confirmed_by_id == stellv_ms.id)
att_sworn.refresh_from_db()
check(
    "Snapshot je Teilnahme (Zeitstempel + Bestätiger)",
    att_sworn.confirmed_final_at is not None and att_sworn.confirmed_final_by_id == stellv_ms.id,
)
confirm_audit = FactionAuditLog.objects.filter(
    organization=org, action="attendance_confirmed", object_id=m67.id
).first()
check("Bestätigung auditiert (wer/wann)", confirm_audit is not None and confirm_audit.membership_id == stellv_ms.id)

# Nach der Bestätigung: Teilnahme-Änderungen gesperrt
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "check_in", "attendance_id": str(att_unsworn.id)},
)
check("Nach Bestätigung: check_in -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "set_participation", "attendance_id": str(att_sworn.id), "participation_type": "onsite"},
)
att_sworn.refresh_from_db()
check(
    "Nach Bestätigung: Teilnahmeart gesperrt",
    resp.status_code == 403 and att_sworn.participation_type == "online",
    f"got {resp.status_code}",
)
resp = manager.post(
    f"{base}/faction/{m67.id}/action/",
    {"action": "add_attendee", "attendee_type": "guest", "guest_name": "Später Gast"},
)
check("Nach Bestätigung: add_attendee -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Anzeige in der Sitzungsansicht
resp = chair.get(f"{base}/faction/{m67.id}/")
html = resp.content.decode("utf-8")
check("Detail zeigt finale Bestätigung", "Teilnahmen final bestätigt" in html)

# =============================================================================
# Phase N: Quorum — beschlussfähig ab mehr als 50 % (Issue #69)
# =============================================================================
print()
print("=== Phase N: Quorum ===")

from apps.common.quorum import quorum_status as common_quorum_status  # noqa: E402
from apps.work.faction.quorum import faction_quorum_status  # noqa: E402

# Gemeinsamer Baustein: Grenzfälle
q = common_quorum_status(voting_total=4, voting_present=2)
check("Baustein: genau 50 % ist NICHT beschlussfähig", q["met"] is False and q["required"] == 3, str(q))
q = common_quorum_status(voting_total=4, voting_present=3)
check("Baustein: mehr als 50 % ist beschlussfähig", q["met"] is True, str(q))
q = common_quorum_status(voting_total=0, voting_present=0)
check("Baustein: ohne Stimmberechtigte nicht beschlussfähig", q["met"] is False)
q = common_quorum_status(voting_total=4, voting_present=3, rule="unbekannt")
check("Baustein: unbekannte Regel fällt auf Mehrheitsregel zurück", q["rule"] == "majority")

# Stimmrecht: genau 4 Mitglieder erhalten voting.participate
voting_role = Role.objects.create(organization=org, name="Stimmrecht-Test", is_admin=False)
voting_role.permissions.add(perm("voting.participate"))
for ms in (chair_ms, manager_ms, sworn_ms, unsworn_ms):
    ms.roles.add(voting_role)

start_n = (now + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Quorum-Sitzung N",
        "start_date": timezone.localtime(start_n).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start_n).strftime("%H:%M"),
    },
)
m69 = FactionMeeting.objects.filter(organization=org, title="Quorum-Sitzung N").first()
FactionMeeting.objects.filter(pk=m69.pk).update(status="ongoing")
m69.refresh_from_db()

q = faction_quorum_status(m69)
check("Fraktion: 4 Stimmberechtigte erkannt", q["voting_total"] == 4, str(q))
check("Fraktion: ohne Anwesende nicht beschlussfähig", q["met"] is False)

for ms in (chair_ms, sworn_ms):
    att = m69.attendances.get(membership=ms)
    manager.post(f"{base}/faction/{m69.id}/action/", {"action": "check_in", "attendance_id": str(att.id)})
q = faction_quorum_status(m69)
check("Fraktion: 2 von 4 (genau 50 %) -> NICHT beschlussfähig", q["voting_present"] == 2 and q["met"] is False, str(q))

# Anzeige in der Sitzungsansicht (nicht beschlussfähig)
resp = chair.get(f"{base}/faction/{m69.id}/")
html = resp.content.decode("utf-8")
check("Detail zeigt Beschlussfähigkeit", "Beschlussfähig" in html)
check("Detail: Nein bei 50 %", "Nein" in html and "2/4 stimmberechtigt" in html)

att = m69.attendances.get(membership=manager_ms)
manager.post(f"{base}/faction/{m69.id}/action/", {"action": "check_in", "attendance_id": str(att.id)})
q = faction_quorum_status(m69)
check("Fraktion: 3 von 4 -> beschlussfähig", q["voting_present"] == 3 and q["met"] is True, str(q))
resp = chair.get(f"{base}/faction/{m69.id}/")
html = resp.content.decode("utf-8")
check("Detail: Ja bei mehr als 50 %", "3/4 stimmberechtigt" in html)

# Online-Teilnahme zählt als Vollteilnahme (Teilnahmeart egal, #67)
att_online = m69.attendances.get(membership=sworn_ms)
manager.post(
    f"{base}/faction/{m69.id}/action/",
    {"action": "set_participation", "attendance_id": str(att_online.id), "participation_type": "online"},
)
q = faction_quorum_status(m69)
check("Online-Teilnahme zählt voll fürs Quorum", q["voting_present"] == 3 and q["met"] is True, str(q))

# =============================================================================
# Phase O: Eigene Absender-Mail — SMTP mit Fallback (Issue #65, SMTP gemockt)
# =============================================================================
print()
print("=== Phase O: Eigene Absender-Mail (SMTP gemockt) ===")

from apps.common.org_email import OrgMailError, send_org_email  # noqa: E402
from django.core.mail import get_connection as _get_connection  # noqa: E402

org.mail_sender_mode = "smtp"
org.smtp_host = "smtp.example.invalid"
org.smtp_port = 587
org.smtp_username = "fraktion"
org.smtp_from_email = "fraktion@example.org"
org.smtp_from_name = "Fraktion Testpartei"
org.smtp_fallback_to_mandari = True
org.set_smtp_password("geheimes-smtp-passwort")
org.save()
org.refresh_from_db()

check("SMTP-Passwort über Accessor lesbar", org.get_smtp_password() == "geheimes-smtp-passwort")
check(
    "SMTP-Passwort nicht im Klartext gespeichert",
    b"geheimes-smtp-passwort" not in bytes(org.smtp_password_encrypted),
)


def _locmem_connection(organization):
    return _get_connection("django.core.mail.backends.locmem.EmailBackend")


# Erfolgsfall: Versand über das (gemockte) Organisations-SMTP mit eigener Absender-Adresse
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", _locmem_connection):
    ok = send_org_email(org, subject="O-TEST-EIGENES-SMTP", body="Test", to=["vereidigt@example.org"])
check("Versand über eigenes SMTP -> OK", ok is True and len(mail.outbox) == 1)
check(
    "Eigene Absender-Adresse verwendet",
    mail.outbox[0].from_email == "Fraktion Testpartei <fraktion@example.org>",
    mail.outbox[0].from_email,
)

# Fehlerfall MIT Fallback: mandari-Versand übernimmt (sichtbar im Log, Mail kommt an)
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", side_effect=OSError("SMTP kaputt")):
    ok = send_org_email(org, subject="O-TEST-FALLBACK", body="Test", to=["vereidigt@example.org"])
check("SMTP-Fehler mit Fallback -> zugestellt", ok is True and len(mail.outbox) == 1)
check(
    "Fallback nutzt mandari-Absender (nicht die Org-Adresse)",
    "fraktion@example.org" not in mail.outbox[0].from_email,
    mail.outbox[0].from_email,
)

# Fehlerfall OHNE Fallback: Versand schlägt sichtbar fehl
org.smtp_fallback_to_mandari = False
org.save(update_fields=["smtp_fallback_to_mandari"])
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", side_effect=OSError("SMTP kaputt")):
    try:
        send_org_email(org, subject="O-TEST-HART", body="Test", to=["vereidigt@example.org"])
        check("Ohne Fallback: OrgMailError", False)
    except OrgMailError:
        check("Ohne Fallback: OrgMailError", True)
    ok = send_org_email(
        org, subject="O-TEST-HART-SILENT", body="Test", to=["vereidigt@example.org"], fail_silently=True
    )
check("Ohne Fallback: fail_silently -> False, keine Mail", ok is False and len(mail.outbox) == 0)

# Einladungen laufen über den konfigurierten Weg (Erstversand über Org-SMTP)
org.smtp_fallback_to_mandari = True
org.save(update_fields=["smtp_fallback_to_mandari"])
start_o = (now + timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "SMTP-Sitzung O",
        "start_date": timezone.localtime(start_o).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start_o).strftime("%H:%M"),
    },
)
mo = FactionMeeting.objects.filter(organization=org, title="SMTP-Sitzung O").first()
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", _locmem_connection):
    resp = chair.post(f"{base}/faction/{mo.id}/action/", {"action": "invite"})
check("Einladungen über Org-SMTP versendet", len(mail.outbox) > 0)
check(
    "Alle Einladungen mit eigener Absender-Adresse",
    all(m.from_email == "Fraktion Testpartei <fraktion@example.org>" for m in mail.outbox),
    str({m.from_email for m in mail.outbox}),
)

# Sichtbares Fehlschlagen beim Einladungsversand (ohne Fallback)
org.smtp_fallback_to_mandari = False
org.save(update_fields=["smtp_fallback_to_mandari"])
start_o2 = (now + timedelta(days=5)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "SMTP-Sitzung O2",
        "start_date": timezone.localtime(start_o2).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start_o2).strftime("%H:%M"),
    },
)
mo2 = FactionMeeting.objects.filter(organization=org, title="SMTP-Sitzung O2").first()
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", side_effect=OSError("SMTP kaputt")):
    resp = chair.post(f"{base}/faction/{mo2.id}/action/", {"action": "invite"})
check("Ohne Fallback: keine einzige Mail (sichtbares Fehlschlagen)", len(mail.outbox) == 0)

# Einstellungs-UI: Berechtigung, SPF/DKIM-Hinweis, Passwort nie im Klartext
chair_role = chair_ms.roles.first()
chair_role.permissions.add(perm("organization.edit"))

resp = manager.get(f"{base}/organization/email-settings/")
check("E-Mail-Einstellungen ohne organization.edit -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = chair.get(f"{base}/organization/email-settings/")
html = resp.content.decode("utf-8")
check("E-Mail-Einstellungen -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("SPF/DKIM-Hinweis vorhanden", "SPF" in html and "DKIM" in html)
check("Passwort NIE im Klartext in der UI", "geheimes-smtp-passwort" not in html)
check("Hinterlegtes Passwort nur als Status", "hinterlegt" in html)

resp = chair.post(
    f"{base}/organization/email-settings/",
    {
        "action": "save",
        "mail_sender_mode": "smtp",
        "smtp_fallback_to_mandari": "on",
        "smtp_host": "smtp2.example.org",
        "smtp_port": "2525",
        "smtp_username": "neuer-user",
        "smtp_password": "neues-geheimnis",
        "smtp_use_tls": "on",
        "smtp_from_email": "fraktion@example.org",
        "smtp_from_name": "Fraktion Testpartei",
    },
)
org.refresh_from_db()
check(
    "Einstellungen gespeichert (Host/Port/Fallback)",
    org.smtp_host == "smtp2.example.org" and org.smtp_port == 2525 and org.smtp_fallback_to_mandari is True,
)
check("Neues Passwort verschlüsselt übernommen", org.get_smtp_password() == "neues-geheimnis")

# Passwort leer lassen = unverändert
resp = chair.post(
    f"{base}/organization/email-settings/",
    {
        "action": "save",
        "mail_sender_mode": "smtp",
        "smtp_fallback_to_mandari": "on",
        "smtp_host": "smtp2.example.org",
        "smtp_port": "2525",
        "smtp_username": "neuer-user",
        "smtp_password": "",
        "smtp_use_tls": "on",
        "smtp_from_email": "fraktion@example.org",
        "smtp_from_name": "Fraktion Testpartei",
    },
)
org.refresh_from_db()
check("Leeres Passwortfeld lässt Passwort unverändert", org.get_smtp_password() == "neues-geheimnis")

# Testmail über den konfigurierten Weg
mail.outbox = []
with mock.patch("apps.common.org_email.get_organization_connection", _locmem_connection):
    resp = chair.post(f"{base}/organization/email-settings/", {"action": "send_test"})
check(
    "Testmail versendet (an den Auslöser, über Org-SMTP)",
    len(mail.outbox) == 1 and mail.outbox[0].to == ["vorsitz@example.org"] and "Testmail" in mail.outbox[0].subject,
    str([(m.to, m.subject, m.from_email) for m in mail.outbox]),
)

# Zurück auf mandari-Standard: Versand läuft ohne Org-SMTP
org.mail_sender_mode = "mandari"
org.save(update_fields=["mail_sender_mode"])
mail.outbox = []
ok = send_org_email(org, subject="O-TEST-STANDARD", body="Test", to=["vereidigt@example.org"])
check("mandari-Standard: Versand ohne Org-SMTP", ok is True and len(mail.outbox) == 1)
check("mandari-Standard: kein Org-Absender", "fraktion@example.org" not in mail.outbox[0].from_email)

# =============================================================================
# Phase P: Teilnahmenachweis + QR-Verifikation + Sammel-Export (Issue #68)
# =============================================================================
print()
print("=== Phase P: Teilnahmenachweis + Verifikation + Sammel-Export ===")

from apps.work.faction.models import FactionAttendanceCertificate  # noqa: E402

# Klarnamen setzen, um Personenbezug beweisbar zu machen
sworn_user.first_name = "Veraxa"
sworn_user.last_name = "Eidigmann"
sworn_user.save(update_fields=["first_name", "last_name"])

p_from = (now - timedelta(days=10)).date().isoformat()
p_to = (now + timedelta(days=10)).date().isoformat()

# Eigener Nachweis: nur vom Vorstand bestätigte Teilnahmen (m67), nichts Unbestätigtes (m69)
resp = sworn.get(f"{base}/faction/nachweis/?from={p_from}&to={p_to}")
check("Nachweis-Download -> PDF", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
cert_pdf = pdf_text(resp.content)
check("PDF enthält bestätigte Sitzung", "Teilnahme-Sitzung M" in cert_pdf)
check("PDF OHNE unbestätigte Sitzung (Quorum N)", "Quorum-Sitzung N" not in cert_pdf)
check("PDF nennt Inhaberin", "Veraxa Eidigmann" in cert_pdf)
check("PDF mit Bestätigungsvermerk", "Bestätigt durch" in cert_pdf and "stellv" in cert_pdf)
check("PDF mit Prüf-URL", "/nachweis/" in cert_pdf)

cert = FactionAttendanceCertificate.objects.filter(organization=org, membership=sworn_ms).order_by("-issued_at").first()
check("Ausstellung dokumentiert", cert is not None and cert.attendance_count == 1, str(cert and cert.attendance_count))
check("Prüfsumme gesetzt", cert is not None and len(cert.checksum) == 64)
check("Prüfcode im PDF", cert.token in cert_pdf)
check(
    "Ausstellung auditiert",
    FactionAuditLog.objects.filter(organization=org, action="certificate_issued", object_id=cert.id).exists(),
)

# Token-Opazität: kein Personen-/Organisationsbezug, nicht erratbar kurz
check(
    "Token opak (kein Personen-/Org-Bezug)",
    len(cert.token) >= 20
    and "veraxa" not in cert.token.lower()
    and "eidigmann" not in cert.token.lower()
    and org.slug not in cert.token.lower()
    and str(sworn_user.pk) not in cert.token
    and str(sworn_ms.pk) not in cert.token,
    cert.token,
)

# Öffentliche Verifikations-Seite (ohne Login): KEINERLEI Personenbezug
anon_p = Client()
resp = anon_p.get(f"/nachweis/{cert.token}/")
verify_html = resp.content.decode("utf-8")
check("Verifikation ohne Login -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Verifikation: 'Nachweis gültig'", "Nachweis gültig" in verify_html)
check("Verifikation: Organisation", org.name in verify_html)
check("Verifikation: Anzahl + Zeitraum", "Bestätigte Teilnahmen" in verify_html and "Zeitraum" in verify_html)
check(
    "Verifikation OHNE Personenbezug (Response-Scan)",
    "Veraxa" not in verify_html
    and "Eidigmann" not in verify_html
    and "vereidigt@example.org" not in verify_html
    and "@example.org" not in verify_html
    and "Teilnahme-Sitzung M" not in verify_html,
)

# Ungültige Token werden sauber abgewiesen
resp = anon_p.get("/nachweis/voellig-unbekanntes-token-123/")
check("Unbekanntes Token -> 404", resp.status_code == 404, f"got {resp.status_code}")
check("Unbekanntes Token: 'ungültig'", "ungültig" in resp.content.decode("utf-8"))

# Ohne bestätigte Teilnahmen wird KEIN Nachweis ausgestellt
before_count = FactionAttendanceCertificate.objects.count()
resp = sworn.get(f"{base}/faction/nachweis/?from=1990-01-01&to=1990-12-31")
check("Leerer Zeitraum -> Redirect ohne Ausstellung", resp.status_code == 302)
check("Leerer Zeitraum: keine Ausstellung dokumentiert", FactionAttendanceCertificate.objects.count() == before_count)

# Sammel-Export: nur Vorstand (Fallback faction.manage nur ohne besetzten Vorstand)
resp = unsworn.get(f"{base}/faction/nachweise/export/?from={p_from}&to={p_to}&format=csv")
check("Sammel-Export ohne Recht -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = manager.get(f"{base}/faction/nachweise/export/?from={p_from}&to={p_to}&format=csv")
check("faction.manage ersetzt den Vorstand nicht (Export)", resp.status_code == 403, f"got {resp.status_code}")

resp = stellv.get(f"{base}/faction/nachweise/export/?from={p_from}&to={p_to}&format=csv")
check("Sammel-Export CSV (Vorstand) -> 200", resp.status_code == 200, f"got {resp.status_code}")
csv_text = resp.content.decode("utf-8")
check("CSV enthält bestätigte Teilnahmen je Person", "Veraxa Eidigmann" in csv_text and "Teilnahme-Sitzung M" in csv_text)
check("CSV OHNE unbestätigte Sitzungen", "Quorum-Sitzung N" not in csv_text)

resp = stellv.get(f"{base}/faction/nachweise/export/?from={p_from}&to={p_to}&format=pdf")
check("Sammel-Export PDF (Vorstand) -> 200", resp.status_code == 200 and resp["Content-Type"] == "application/pdf")
export_pdf = pdf_text(resp.content)
check("Export-PDF gruppiert je Person", "Veraxa Eidigmann" in export_pdf and "Teilnahme-Sitzung M" in export_pdf)
check(
    "Sammel-Export auditiert",
    FactionAuditLog.objects.filter(organization=org, action="attendance_exported").count() >= 2,
)

# =============================================================================
# Phase Q: In-App-Benachrichtigungen + persönlicher iCal-Feed (Issue #70)
# =============================================================================
print()
print("=== Phase Q: In-App-Benachrichtigungen + iCal-Feed ===")

from apps.work.faction.models import CalendarFeedToken  # noqa: E402
from apps.work.notifications.models import Notification, NotificationPreference  # noqa: E402

# -- Workflow-Ereignisse der früheren Phasen haben In-App-Benachrichtigungen erzeugt
check(
    "Einladungsversand erzeugt In-App-Benachrichtigung",
    Notification.objects.filter(
        notification_type="faction_invitation", metadata__meeting_id=str(meeting1.id)
    ).exists(),
)
check(
    "Aktualisierter Versand als eigene Benachrichtigung",
    Notification.objects.filter(notification_type="faction_invitation", title="Aktualisierte Einladung").exists(),
)
check(
    "Erinnerung erzeugt In-App-Benachrichtigung",
    Notification.objects.filter(notification_type="faction_reminder", recipient=sworn_ms).exists(),
)
check(
    "Protokoll-Genehmigung benachrichtigt Mitglieder",
    Notification.objects.filter(
        notification_type="faction_prot_approved", recipient=sworn_ms, metadata__meeting_id=str(meeting1.id)
    ).exists(),
)
check(
    "Genehmigende Person wird nicht selbst benachrichtigt",
    not Notification.objects.filter(notification_type="faction_prot_approved", recipient=chair_ms).exists(),
)

# Vorschlags-Entscheidung: Annahme benachrichtigt die vorschlagende Person
pub_proposal = meeting2.agenda_items.filter(title="OEFF-VORSCHLAG-OK", proposal_status="proposed").first()
check("Offener Ö-Vorschlag vorhanden", pub_proposal is not None)
resp = chair.post(
    f"{base}/faction/{meeting2.id}/action/",
    {"action": "accept_proposal", "item_id": str(pub_proposal.id)},
)
pub_proposal.refresh_from_db()
check("Vorschlag angenommen", pub_proposal.proposal_status == "active", pub_proposal.proposal_status)
check(
    "Vorschlags-Entscheidung benachrichtigt Vorschlagende:n",
    Notification.objects.filter(notification_type="faction_prop_decided", recipient=skb_ms).exists(),
)

# Typ abschaltbar: deaktivierter Typ erzeugt keine In-App-Benachrichtigung
prefs_q, _ = NotificationPreference.objects.get_or_create(membership=unsworn_ms)
prefs_q.type_settings = {"faction_invitation": {"in_app": False, "email": False}}
prefs_q.save()

start_q = (now + timedelta(days=8)).replace(minute=0, second=0, microsecond=0)
resp = chair.post(
    f"{base}/faction/",
    {
        "title": "Benachrichtigungs-Sitzung Q",
        "start_date": timezone.localtime(start_q).strftime("%Y-%m-%d"),
        "start_time": timezone.localtime(start_q).strftime("%H:%M"),
    },
)
mq = FactionMeeting.objects.filter(organization=org, title="Benachrichtigungs-Sitzung Q").first()
mail.outbox = []
resp = chair.post(f"{base}/faction/{mq.id}/action/", {"action": "invite"})
check(
    "Abgeschalteter Typ: keine In-App-Benachrichtigung",
    not Notification.objects.filter(
        notification_type="faction_invitation", recipient=unsworn_ms, metadata__meeting_id=str(mq.id)
    ).exists(),
)
check(
    "Andere Mitglieder erhalten die Benachrichtigung",
    Notification.objects.filter(
        notification_type="faction_invitation", recipient=sworn_ms, metadata__meeting_id=str(mq.id)
    ).exists(),
)

# -- Persönlicher iCal-Feed ---------------------------------------------------
feed_token = CalendarFeedToken.for_user(sworn_user)
check(
    "Feed-Token opak (kein Personen-/Org-Bezug)",
    len(feed_token.token) >= 20
    and org.slug not in feed_token.token.lower()
    and "veraxa" not in feed_token.token.lower()
    and str(sworn_user.pk) not in feed_token.token,
    feed_token.token,
)

# RIS-Gremium zuordnen + RIS-Termin anlegen
ris_org_q = OParlOrganization.objects.create(
    body=body,
    external_id="https://ris.musterstadt.example/organization/feed-q",
    name="Feed-Gremium QQ",
    organization_type="Gremium",
)
ris_meeting_q = OParlMeeting.objects.create(
    body=body,
    external_id="https://ris.musterstadt.example/meeting/feed-q",
    name="RIS-FEED-TERMIN-QQ",
    start=now + timedelta(days=3),
    cancelled=False,
)
ris_meeting_q.organizations.add(ris_org_q)
sworn_ms.oparl_committees.add(ris_org_q)

# Entwurfs-Sitzungen erscheinen nie im Feed
FactionMeeting.objects.create(
    organization=org, title="ENTWURF-SITZUNG-QQ", start=now + timedelta(days=6), status="draft"
)

anon_q = Client()
resp = anon_q.get(f"/kalender/feed/{feed_token.token}.ics")
check("Feed ohne Login -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Feed: text/calendar", resp["Content-Type"].startswith("text/calendar"))
feed_text = resp.content.decode("utf-8")
check("Feed: VCALENDAR/VEVENT", "BEGIN:VCALENDAR" in feed_text and "BEGIN:VEVENT" in feed_text)
check("Feed enthält Fraktionssitzung", "Fraktionssitzung Eins" in feed_text)
check("Feed enthält RIS-Termin des zugeordneten Gremiums", "Feed-Gremium QQ" in feed_text)
check("Feed OHNE Entwurfs-Sitzungen", "ENTWURF-SITZUNG-QQ" not in feed_text)
check(
    "Feed OHNE NÖ-/Protokollinhalte (Response-Scan)",
    "GEHEIM-TOP-OMEGA" not in feed_text
    and "GEHEIMER-EINTRAG-456" not in feed_text
    and "OEFFENTLICHER-EINTRAG-123" not in feed_text
    and "TOPLOSER-EINTRAG-789" not in feed_text,
)

# Anderer User ohne Gremien-Zuordnung: Fraktionssitzungen ja, RIS-Termin nein
feed_token_unsworn = CalendarFeedToken.for_user(unsworn_user)
resp = anon_q.get(f"/kalender/feed/{feed_token_unsworn.token}.ics")
feed_text_unsworn = resp.content.decode("utf-8")
check("Feed je User individuell (RIS nur bei Zuordnung)", "Feed-Gremium QQ" not in feed_text_unsworn)
check("Feed des zweiten Users enthält Fraktionssitzungen", "Fraktionssitzung Eins" in feed_text_unsworn)

# Tokenschutz: unbekanntes Token -> 404
resp = anon_q.get("/kalender/feed/unbekanntes-token-999.ics")
check("Unbekanntes Feed-Token -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Erneuerung über die Profileinstellungen: alte URL sofort ungültig
sworn_role_q = sworn_ms.roles.first()
sworn_role_q.permissions.add(perm("dashboard.view"))
old_feed_token = feed_token.token
resp = sworn.get(f"{base}/profile/")
check("Profil zeigt Feed-URL", resp.status_code == 200 and old_feed_token in resp.content.decode("utf-8"))
resp = sworn.post(f"{base}/profile/", {"action": "regenerate_calendar_feed"})
check("Feed-Erneuerung -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
feed_token.refresh_from_db()
check("Token wurde erneuert", feed_token.token != old_feed_token)
resp = anon_q.get(f"/kalender/feed/{old_feed_token}.ics")
check("Alte Feed-URL nach Erneuerung -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = anon_q.get(f"/kalender/feed/{feed_token.token}.ics")
check("Neue Feed-URL -> 200", resp.status_code == 200, f"got {resp.status_code}")

# =============================================================================
# Phase R: Öffentliche API v1 — Opt-in, Token, NÖ-Freiheit, OpenAPI (Issue #71)
# =============================================================================
print()
print("=== Phase R: Öffentliche API v1 ===")

import json  # noqa: E402
import uuid as _uuid  # noqa: E402

from apps.work.faction.models import FactionPublicApiAccess  # noqa: E402

anon_r = Client()
access = FactionPublicApiAccess.for_organization(org)
check("Default: API deaktiviert (Opt-in)", access.is_enabled is False)

# Ohne Opt-in liefert die API nichts — auch mit korrektem (deaktiviertem) Token
resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Ohne Opt-in -> 404 (auch mit korrektem Token)", resp.status_code == 404, f"got {resp.status_code}")
resp = anon_r.get("/api/public/v1/fraktionen/voellig-unbekanntes-token/sitzungen/")
check("Unbekanntes Token -> 404", resp.status_code == 404, f"got {resp.status_code}")

# Aktivierung nur mit faction.manage
resp = unsworn.post(f"{base}/organization/api/", {"section": "api_save", "api_enabled": "on"})
access.refresh_from_db()
check("Aktivierung ohne faction.manage -> 403", resp.status_code == 403 and access.is_enabled is False)

resp = chair.post(
    f"{base}/organization/api/",
    {"section": "api_save", "api_enabled": "on", "api_past_days": "365", "api_show_agenda": "on", "api_show_location": "on"},
)
access.refresh_from_db()
check("Opt-in gespeichert (aktiv, 365 Tage)", access.is_enabled is True and access.past_days == 365)
check(
    "API-Konfiguration auditiert",
    FactionAuditLog.objects.filter(organization=org, action="api_settings_changed").exists(),
)
check(
    "API-Token opak (kein Org-Bezug, nicht erratbar)",
    len(access.token) >= 20 and org.slug not in access.token.lower(),
    access.token,
)

# Terminliste: JSON, CORS, Caching, keine Entwürfe
resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Terminliste -> 200 JSON", resp.status_code == 200 and resp["Content-Type"].startswith("application/json"))
check("CORS-Header gesetzt", resp["Access-Control-Allow-Origin"] == "*")
check("Cache-Header gesetzt", "max-age" in resp.get("Cache-Control", ""))
list_data = json.loads(resp.content)
list_titles = [m["title"] for m in list_data["meetings"]]
check("Liste enthält öffentliche Sitzungen", "Fraktionssitzung Eins" in list_titles, str(list_titles))
check("Liste OHNE Entwürfe", "ENTWURF-SITZUNG-QQ" not in list_titles)
list_text = resp.content.decode("utf-8")
check(
    "Liste ohne NÖ-/Protokoll-/Personen-Inhalte (Response-Scan)",
    "GEHEIM" not in list_text and "EINTRAG" not in list_text and "Veraxa" not in list_text,
)

# OPTIONS-Preflight für Browser-Einbindung
resp = anon_r.options(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("OPTIONS-Preflight mit CORS", resp.status_code == 204 and resp["Access-Control-Allow-Origin"] == "*")

# Sitzungsdetail: öffentliche TO, NÖ erscheint niemals (auch nicht als Platzhalter)
resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/{meeting1.id}/")
check("Detail -> 200", resp.status_code == 200, f"got {resp.status_code}")
detail_data = json.loads(resp.content)
agenda_titles = [item["title"] for item in detail_data.get("agenda", [])]
check("Detail mit öffentlicher Tagesordnung", "OEFF-TOP-ALPHA" in agenda_titles, str(agenda_titles))
check(
    "Genau die 3 Ö-TOPs, kein NÖ-Platzhalter",
    len(detail_data.get("agenda", [])) == 3,
    str(detail_data.get("agenda")),
)
detail_text = resp.content.decode("utf-8")
check(
    "NÖ-TOPs/Protokolle/Beschlüsse erscheinen niemals (Response-Scan)",
    "GEHEIM-TOP-OMEGA" not in detail_text
    and "GEHEIMER-EINTRAG-456" not in detail_text
    and "OEFFENTLICHER-EINTRAG-123" not in detail_text
    and "TOPLOSER-EINTRAG-789" not in detail_text
    and "Veraxa" not in detail_text,
)

resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/{_uuid.uuid4()}/")
check("Unbekannte Sitzung -> 404", resp.status_code == 404, f"got {resp.status_code}")

# OpenAPI-Schema: valide, versioniert, ohne Token abrufbar
resp = anon_r.get("/api/public/v1/openapi.json")
check("OpenAPI-Endpoint -> 200", resp.status_code == 200, f"got {resp.status_code}")
schema = json.loads(resp.content)
check(
    "OpenAPI-Schema valide (3.x, Pfade, Schemas)",
    schema.get("openapi", "").startswith("3.")
    and "/fraktionen/{token}/sitzungen/" in schema.get("paths", {})
    and "Meeting" in schema.get("components", {}).get("schemas", {}),
)

# Token-Erneuerung: alte URL sofort ungültig
old_api_token = access.token
resp = chair.post(f"{base}/organization/api/", {"section": "api_regenerate"})
access.refresh_from_db()
check("API-Token erneuert", access.token != old_api_token)
resp = anon_r.get(f"/api/public/v1/fraktionen/{old_api_token}/sitzungen/")
check("Altes API-Token -> 404", resp.status_code == 404, f"got {resp.status_code}")
resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Neues API-Token -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Deaktivierung wirkt sofort
resp = chair.post(f"{base}/organization/api/", {"section": "api_save", "api_past_days": "365"})
access.refresh_from_db()
check("Deaktivierung gespeichert", access.is_enabled is False)
resp = anon_r.get(f"/api/public/v1/fraktionen/{access.token}/sitzungen/")
check("Nach Deaktivierung -> 404", resp.status_code == 404, f"got {resp.status_code}")

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
