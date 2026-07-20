# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Tagesordnungs-Verwaltung (Issue #26).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_agenda.py

Prüft:
- TOP anlegen/bearbeiten/löschen/absetzen (Absetzung dokumentiert)
- Automatische Neu-Nummerierung: Ö-Teil 1..n, NÖ-Teil getrennt (N1..Nm)
- Umsortieren per Reorder-Endpoint (Drag-and-drop) und Auf/Ab
- Unterpunkte (5.1, 5.2), Vorlagenzuordnung änderbar
- Nachtrags-Kennzeichnung nach Ladungsversand
- Ö/NÖ-Gruppierung in der Detailansicht; NÖ-Teil für Unberechtigte unsichtbar
- Audit-Einträge für alle Änderungen
- Berechtigungen: edit_meetings erforderlich
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

# SQLite-Robustheit unter Windows: laengere Busy-Timeouts gegen
# transiente "database is locked"-Fehler (Virenscanner/Indexer).
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
    SessionAgendaItem,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import agenda_service  # noqa: E402

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


def numbers(meeting):
    return {i.name: i.number for i in meeting.agenda_items.all()}


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")

clerk_user = User.objects.create_user(email="clerk@example.org", password="pw-Smoke-Test-1!")
viewer_user = User.objects.create_user(email="viewer@example.org", password="pw-Smoke-Test-1!")

roles = SessionRole.create_default_roles(tenant)
su_clerk = SessionUser.objects.create(user=clerk_user, tenant=tenant)
su_clerk.roles.add(roles["clerk"])
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(roles["viewer"])

org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
meeting = SessionMeeting.objects.create(tenant=tenant, name="Ratssitzung", organization=org, start=timezone.now())
paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0001", name="Vorlage A")
paper2 = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0002", name="Vorlage B")

clerk = Client()
clerk.force_login(clerk_user)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Anlegen mit automatischer Ö/NÖ-Nummerierung
# =============================================================================
print("=== Phase A: Anlegen + Nummerierung ===")

for name, is_public in [
    ("Eröffnung", True),
    ("Haushalt", True),
    ("Grundstücksverkauf", False),
    ("Personalangelegenheit", False),
    ("Verschiedenes", True),
]:
    data = {"name": name}
    if is_public:
        data["is_public"] = "on"
    resp = clerk.post(f"{base}/meetings/{meeting.id}/agenda/add/", data)
    check(f"TOP „{name}“ angelegt", resp.status_code in (204, 302), f"got {resp.status_code}")

nums = numbers(meeting)
check("Ö-Teil nummeriert 1..3", [nums["Eröffnung"], nums["Haushalt"], nums["Verschiedenes"]] == ["1", "2", "3"], nums)
check(
    "NÖ-Teil getrennt nummeriert N1..N2",
    [nums["Grundstücksverkauf"], nums["Personalangelegenheit"]] == ["N1", "N2"],
    nums,
)

item_haushalt = meeting.agenda_items.get(name="Haushalt")
check(
    "Audit: create-Einträge für TOPs",
    SessionAuditLog.objects.filter(object_id=item_haushalt.id, action="create").exists(),
)

# Unterpunkt anlegen
resp = clerk.post(
    f"{base}/meetings/{meeting.id}/agenda/add/",
    {"name": "Haushaltssatzung", "is_public": "on", "parent": str(item_haushalt.id), "paper": str(paper.id)},
)
check("Unterpunkt angelegt", resp.status_code in (204, 302), f"got {resp.status_code}")
sub = meeting.agenda_items.get(name="Haushaltssatzung")
check("Unterpunkt nummeriert als 2.1", sub.number == "2.1", f"got {sub.number}")
check("Unterpunkt mit Vorlage verknüpft", sub.paper_id == paper.id)

# =============================================================================
# Phase B: Bearbeiten (Ö/NÖ-Wechsel, Vorlagenzuordnung)
# =============================================================================
print()
print("=== Phase B: Bearbeiten ===")

resp = clerk.get(f"{base}/agenda/{sub.id}/edit/")
check("Edit-Formular -> 200", resp.status_code == 200, f"got {resp.status_code}")

# Vorlagenzuordnung ändern
resp = clerk.post(
    f"{base}/agenda/{sub.id}/edit/",
    {"name": "Haushaltssatzung 2026", "is_public": "on", "parent": str(item_haushalt.id), "paper": str(paper2.id)},
)
check("TOP bearbeitet -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
sub.refresh_from_db()
check("Betreff geändert", sub.name == "Haushaltssatzung 2026")
check("Vorlagenzuordnung geändert", sub.paper_id == paper2.id)
check(
    "Audit: update-Eintrag für Bearbeitung",
    SessionAuditLog.objects.filter(object_id=sub.id, action="update").exists(),
)

# Ö -> NÖ verschieben: Neu-Nummerierung beider Teile
item_versch = meeting.agenda_items.get(name="Verschiedenes")
resp = clerk.post(f"{base}/agenda/{item_versch.id}/edit/", {"name": "Verschiedenes"})
item_versch.refresh_from_db()
check("TOP in NÖ-Teil verschoben", not item_versch.is_public)
check("TOP im NÖ-Teil neu nummeriert (N3)", item_versch.number == "N3", f"got {item_versch.number}")

# =============================================================================
# Phase C: Umsortieren (Auf/Ab + Reorder)
# =============================================================================
print()
print("=== Phase C: Umsortieren ===")

resp = clerk.post(f"{base}/agenda/{item_haushalt.id}/move/", {"direction": "up"})
check("Move up -> Redirect", resp.status_code == 302, f"got {resp.status_code}")
nums = numbers(meeting)
check("Haushalt jetzt TOP 1", nums["Haushalt"] == "1", nums)
check("Eröffnung jetzt TOP 2", nums["Eröffnung"] == "2", nums)
check("Unterpunkt folgt Eltern-TOP (1.1)", nums["Haushaltssatzung 2026"] == "1.1", nums)

# Reorder-Endpoint (Drag-and-drop): Reihenfolge im Ö-Teil zurückdrehen
item_eroeff = meeting.agenda_items.get(name="Eröffnung")
resp = clerk.post(
    f"{base}/meetings/{meeting.id}/agenda/reorder/",
    {"order": f"{item_eroeff.id},{item_haushalt.id}"},
    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
)
check("Reorder-Endpoint -> 200", resp.status_code == 200, f"got {resp.status_code}")
nums = numbers(meeting)
check("Nach Reorder: Eröffnung TOP 1", nums["Eröffnung"] == "1", nums)
check("Nach Reorder: Haushalt TOP 2", nums["Haushalt"] == "2", nums)
check("NÖ-Nummerierung unverändert getrennt", nums["Grundstücksverkauf"].startswith("N"), nums)

# =============================================================================
# Phase D: Absetzen (dokumentiert) + Löschen
# =============================================================================
print()
print("=== Phase D: Absetzen + Löschen ===")

item_grund = meeting.agenda_items.get(name="Grundstücksverkauf")
resp = clerk.post(f"{base}/agenda/{item_grund.id}/withdraw/", {"reason": "Vertagung auf nächste Sitzung"})
item_grund.refresh_from_db()
check("TOP abgesetzt", item_grund.is_withdrawn)
check("Absetzungsgrund dokumentiert", item_grund.withdrawn_reason == "Vertagung auf nächste Sitzung")
check("Abgesetzter TOP bleibt erhalten", SessionAgendaItem.objects.filter(id=item_grund.id).exists())
check(
    "Audit: withdraw-Aktion protokolliert",
    SessionAuditLog.objects.filter(object_id=item_grund.id, action="withdraw").exists(),
)

# Absetzung aufheben
resp = clerk.post(f"{base}/agenda/{item_grund.id}/withdraw/", {"restore": "1"})
item_grund.refresh_from_db()
check("Absetzung aufhebbar", not item_grund.is_withdrawn)

# Löschen inkl. Neu-Nummerierung
item_pers = meeting.agenda_items.get(name="Personalangelegenheit")
pers_id = item_pers.id
resp = clerk.post(f"{base}/agenda/{item_pers.id}/delete/")
check("TOP gelöscht", not SessionAgendaItem.objects.filter(id=pers_id).exists())
check("Audit: delete-Eintrag", SessionAuditLog.objects.filter(object_id=pers_id, action="delete").exists())
nums = numbers(meeting)
check("NÖ-Teil nach Löschung neu nummeriert", nums["Verschiedenes"] == "N2", nums)

# =============================================================================
# Phase E: Nachtrag nach Ladungsversand
# =============================================================================
print()
print("=== Phase E: Nachtrag ===")

meeting.meeting_state = "invitation_sent"
meeting.invitation_sent_at = timezone.now()
meeting.save()

resp = clerk.post(f"{base}/meetings/{meeting.id}/agenda/add/", {"name": "Dringlicher Antrag", "is_public": "on"})
nachtrag = meeting.agenda_items.get(name="Dringlicher Antrag")
check("TOP nach Ladungsversand als Nachtrag gekennzeichnet", nachtrag.is_supplementary)

resp = clerk.get(f"{base}/meetings/{meeting.id}/")
check("Detailansicht zeigt Nachtrags-Badge", b"Nachtrag" in resp.content)

# =============================================================================
# Phase F: Ö/NÖ-Gruppierung + Berechtigungen
# =============================================================================
print()
print("=== Phase F: Gruppierung + Berechtigungen ===")

resp = clerk.get(f"{base}/meetings/{meeting.id}/")
html = resp.content.decode("utf-8")
check("Detailansicht: Öffentlicher Teil ausgewiesen", "Öffentlicher Teil" in html)
check("Detailansicht: Nichtöffentlicher Teil ausgewiesen", "Nichtöffentlicher Teil" in html)
check("NÖ-TOP für Berechtigte sichtbar", "Grundstücksverkauf" in html)

resp = viewer.get(f"{base}/meetings/{meeting.id}/")
html = resp.content.decode("utf-8")
check("Viewer sieht öffentliche TOPs", "Eröffnung" in html)
check("Viewer sieht KEINE NÖ-TOPs", "Grundstücksverkauf" not in html)
check("Viewer sieht keinen NÖ-Abschnitt", "Nichtöffentlicher Teil" not in html)

# Mutationen ohne edit_meetings -> 403 und keine Änderung
resp = viewer.post(f"{base}/meetings/{meeting.id}/agenda/add/", {"name": "Einschleuser", "is_public": "on"})
check("TOP-Anlage ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("Kein TOP angelegt", not meeting.agenda_items.filter(name="Einschleuser").exists())

resp = viewer.post(f"{base}/agenda/{item_eroeff.id}/delete/")
check("TOP-Löschung ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")
check("TOP existiert weiterhin", SessionAgendaItem.objects.filter(id=item_eroeff.id).exists())

resp = viewer.post(f"{base}/meetings/{meeting.id}/agenda/reorder/", {"order": ""})
check("Reorder ohne Berechtigung -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Tenant-Isolation: TOP eines fremden Tenants nicht bearbeitbar
tenant2 = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")
org2 = SessionOrganization.objects.create(tenant=tenant2, name="Fremdrat")
meeting2 = SessionMeeting.objects.create(tenant=tenant2, name="Fremdsitzung", organization=org2, start=timezone.now())
foreign_item = SessionAgendaItem.objects.create(meeting=meeting2, number="1", name="Fremd-TOP")

resp = clerk.post(f"{base}/agenda/{foreign_item.id}/edit/", {"name": "Gekapert"})
check("Fremd-TOP über eigenen Tenant-Slug -> 404", resp.status_code == 404, f"got {resp.status_code}")
foreign_item.refresh_from_db()
check("Fremd-TOP unverändert", foreign_item.name == "Fremd-TOP")

# Service: grouped_agenda liefert saubere Struktur
agenda = agenda_service.grouped_agenda(meeting)
check(
    "grouped_agenda: Ö-Teil enthält nur öffentliche Top-Level-TOPs",
    all(i.is_public and i.parent_id is None for i in agenda["public"]),
)
check(
    "grouped_agenda: NÖ-Teil enthält nur nichtöffentliche TOPs",
    all(not i.is_public for i in agenda["non_public"]),
)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
