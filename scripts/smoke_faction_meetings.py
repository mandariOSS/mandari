# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Fraktionssitzungen — Einladungen, Genehmigungskette, Protokoll-PDF
(Issues #58, #59, #60).

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
"""

import base64
import io
import os
import secrets
import sys
import tempfile
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
from apps.work.faction.models import (  # noqa: E402
    FactionAgendaItem,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.faction.services import run_faction_reminder_pass  # noqa: E402
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
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
