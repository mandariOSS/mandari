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

# Verschlüsselte Felder erscheinen nie im Klartext
entry_int.set_content_encrypted("SUPERGEHEIM-NEU-999")
entry_int.save()
masked_entry = audit_qs.filter(model_name="FactionProtocolEntry", object_id=entry_int.id, action="update").first()
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
check("Detail: NÖ-Protokolleintrag NICHT sichtbar", "SUPERGEHEIM-NEU-999" not in html)
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
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
