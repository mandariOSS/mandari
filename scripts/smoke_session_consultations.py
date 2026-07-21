# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Beratungsfolge für Vorlagen (Issue #34).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_consultations.py

Prüft:
- Kette anlegen/bearbeiten/umsortieren/löschen (inkl. Reihenfolge-Pflege)
- TOP-Erzeugung aus der Beratungsfolge (Ö/NÖ-Nummerierung aus Issue #26,
  Vorlagen-Status -> "Terminiert")
- Ergebnis-Rückschreibung: Abstimmungsergebnis am TOP erscheint an der
  Station (Signal), Vorberatungsergebnis ist in der Folgesitzung sichtbar
- Weiterleitung an die nächste Station
- Akzeptanzkriterium: Vorlage mit Beratungsfolge Ausschuss -> Rat erscheint
  auf beiden Tagesordnungen
- Berechtigungen (403 ohne Recht, keine Mutation), Tenant-Isolation (404),
  Ö/NÖ (NÖ-Vorlage ohne NÖ-Recht unsichtbar; NÖ-Vorlage -> NÖ-TOP)
- Audit-Log-Einträge für Stationen
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

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_consult_"))
_db_path = _tmp_dir / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
_dj_settings.MEDIA_ROOT = str(_tmp_dir / "media")

from datetime import timedelta  # noqa: E402

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
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

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


def make_user(tenant, name, perms=None, is_admin=False):
    flags = {}
    if perms:
        flags = {f"can_{p}": True for p in perms}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle_{name}", is_admin=is_admin, **flags)
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-Test-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")

committee = SessionOrganization.objects.create(
    tenant=tenant, name="Bauausschuss", short_name="BauA", organization_type="committee"
)
council = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat", organization_type="council")
other_org = SessionOrganization.objects.create(tenant=tenant, name="Sozialausschuss", organization_type="committee")

now = timezone.now()
meeting_committee = SessionMeeting.objects.create(
    tenant=tenant, name="Bauausschuss-Sitzung", organization=committee, start=now + timedelta(days=7)
)
meeting_council = SessionMeeting.objects.create(
    tenant=tenant, name="Ratssitzung", organization=council, start=now + timedelta(days=21)
)
meeting_wrong_org = SessionMeeting.objects.create(
    tenant=tenant, name="Sozialausschuss-Sitzung", organization=other_org, start=now + timedelta(days=10)
)

paper = SessionPaper.objects.create(
    tenant=tenant,
    reference="V/2026/0042",
    name="SPIELPLATZ-VORLAGE",
    status="approved",
    is_public=True,
)
paper_np = SessionPaper.objects.create(
    tenant=tenant,
    reference="V/2026/0043",
    name="GEHEIME-GRUNDSTUECKS-VORLAGE",
    status="approved",
    is_public=False,
)

# Fremd-Tenant-Daten
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdgremium")
paper_b = SessionPaper.objects.create(tenant=tenant_b, reference="V/2026/9001", name="FREMD-VORLAGE")
consultation_b = SessionConsultation.objects.create(paper=paper_b, organization=org_b, order=1)

admin = make_user(tenant, "admin", is_admin=True)
clerk = make_user(
    tenant,
    "clerk",
    [
        "view_papers",
        "edit_papers",
        "view_meetings",
        "edit_meetings",
        "view_non_public_papers",
        "view_non_public_meetings",
    ],
)
paper_editor = make_user(tenant, "papiere", ["view_papers", "edit_papers"])
meeting_editor = make_user(tenant, "sitzungen", ["view_meetings", "edit_meetings", "view_papers"])
no_perm = make_user(tenant, "nichts", [])
viewer = make_user(tenant, "leser", ["view_papers", "view_meetings"])

base = f"/session/{tenant.slug}"

# =============================================================================
# Phase A: Kette anlegen
# =============================================================================
print("=== Phase A: Beratungsfolge anlegen ===")

resp = clerk.post(
    f"{base}/papers/{paper.id}/consultations/add/",
    {"organization": str(committee.id), "role": "preliminary", "meeting": str(meeting_committee.id)},
)
check("Station 1 anlegen -> Redirect", resp.status_code == 302, f"got {resp.status_code}")

resp = clerk.post(
    f"{base}/papers/{paper.id}/consultations/add/",
    {"organization": str(council.id), "role": "decision", "meeting": str(meeting_council.id)},
)
check("Station 2 anlegen -> Redirect", resp.status_code == 302, f"got {resp.status_code}")

stations = list(paper.consultations.order_by("order"))
check("Zwei Stationen mit Reihenfolge 1,2", [s.order for s in stations] == [1, 2], str([s.order for s in stations]))
check(
    "Station 1: Vorberatung, nicht authoritative", stations[0].role == "preliminary" and not stations[0].authoritative
)
check("Station 2: Entscheidung automatisch authoritative", stations[1].role == "decision" and stations[1].authoritative)
check(
    "Stationen mit Zielsitzung verknüpft",
    stations[0].meeting_id == meeting_committee.id and stations[1].meeting_id == meeting_council.id,
)

# Validierung: Sitzung eines fremden Gremiums wird abgelehnt
resp = clerk.post(
    f"{base}/papers/{paper.id}/consultations/add/",
    {"organization": str(committee.id), "role": "hearing", "meeting": str(meeting_wrong_org.id)},
)
check(
    "Sitzung eines anderen Gremiums wird abgelehnt",
    paper.consultations.count() == 2,
    f"{paper.consultations.count()} Stationen",
)

# Kette in der Vorlagen-Detailansicht sichtbar
resp = clerk.get(f"{base}/papers/{paper.id}/")
check(
    "Vorlagen-Detail zeigt Beratungsfolge",
    resp.status_code == 200 and b"Beratungsfolge" in resp.content and b"Bauausschuss" in resp.content,
    f"status={resp.status_code}",
)

# Umsortieren + zurück
station1, station2 = stations
clerk.post(f"{base}/consultations/{station2.id}/move/", {"direction": "up"})
orders = {s.id: s.order for s in paper.consultations.all()}
check("Umsortieren: Station 2 nach oben", orders[station2.id] == 1 and orders[station1.id] == 2, str(orders))
clerk.post(f"{base}/consultations/{station2.id}/move/", {"direction": "down"})
orders = {s.id: s.order for s in paper.consultations.all()}
check(
    "Umsortieren: wieder ursprüngliche Reihenfolge", orders[station1.id] == 1 and orders[station2.id] == 2, str(orders)
)

# =============================================================================
# Phase B: TOP-Erzeugung aus der Beratungsfolge
# =============================================================================
print()
print("=== Phase B: Terminierung (TOP-Erzeugung) ===")

resp = clerk.post(f"{base}/consultations/{station1.id}/schedule/", {})
station1.refresh_from_db()
check("Station 1 terminiert -> TOP angelegt", station1.agenda_item_id is not None)
top1 = station1.agenda_item
check("TOP gehört zur Ausschusssitzung", top1 is not None and top1.meeting_id == meeting_committee.id)
check("TOP verweist auf die Vorlage", top1 is not None and top1.paper_id == paper.id)
check(
    "TOP öffentlich (wie Vorlage), Nummer vergeben",
    top1 is not None and top1.is_public and top1.number == "1",
    f"number={top1.number if top1 else None}",
)

paper.refresh_from_db()
check("Vorlagen-Status -> Terminiert", paper.status == "scheduled", paper.status)

# Doppelte Terminierung ist idempotent
before = SessionAgendaItem.objects.count()
clerk.post(f"{base}/consultations/{station1.id}/schedule/", {})
check("Erneutes Terminieren legt keinen zweiten TOP an", SessionAgendaItem.objects.count() == before)

# =============================================================================
# Phase C: Ergebnis-Rückschreibung + Weiterleitung
# =============================================================================
print()
print("=== Phase C: Ergebnis + Weiterleitung ===")

# Weiterleitung ohne Ergebnis wird abgelehnt
clerk.post(f"{base}/consultations/{station1.id}/forward/", {})
station2.refresh_from_db()
check("Weiterleitung ohne Ergebnis legt keinen TOP an", station2.agenda_item_id is None)

# Abstimmungsergebnis am TOP erfassen (wie Niederschrift, Issue #31)
top1.vote_result = "approved"
top1.votes_yes = 9
top1.votes_no = 2
top1.save()
station1.refresh_from_db()
check("Ergebnis-Rückschreibung an Station 1", station1.result == "approved", station1.result)

# Weiterleitung an die nächste Station
clerk.post(f"{base}/consultations/{station1.id}/forward/", {})
station2.refresh_from_db()
check("Weiterleitung terminiert Station 2", station2.agenda_item_id is not None)
top2 = station2.agenda_item

# Akzeptanzkriterium: Vorlage erscheint auf beiden Tagesordnungen
on_committee = SessionAgendaItem.objects.filter(meeting=meeting_committee, paper=paper).exists()
on_council = SessionAgendaItem.objects.filter(meeting=meeting_council, paper=paper).exists()
check("Vorlage erscheint auf beiden Tagesordnungen", on_committee and on_council)

# Vorberatungsergebnis in der Ratssitzung sichtbar
resp = clerk.get(f"{base}/meetings/{meeting_council.id}/")
check(
    "Ratssitzung zeigt Vorberatungsergebnis der Ausschussstation",
    resp.status_code == 200 and b"Vorberatung" in resp.content and b"Angenommen" in resp.content,
    f"status={resp.status_code}",
)

# Ergebnis Station 2 (Entscheidung)
top2.vote_result = "approved"
top2.save()
station2.refresh_from_db()
check("Ergebnis-Rückschreibung an Station 2", station2.result == "approved", station2.result)

# Manuelles Ergebnis nur ohne TOP: Update darf Ergebnis nicht überschreiben
clerk.post(
    f"{base}/consultations/{station1.id}/update/",
    {"role": "preliminary", "meeting": str(meeting_committee.id), "result": "rejected", "authoritative": ""},
)
station1.refresh_from_db()
check("Manuelles Ergebnis bei terminierter Station ignoriert", station1.result == "approved", station1.result)

# Zielsitzungswechsel bei terminierter Station blockiert
clerk.post(
    f"{base}/consultations/{station1.id}/update/",
    {"role": "preliminary", "meeting": "", "authoritative": ""},
)
station1.refresh_from_db()
check("Zielsitzung bei terminierter Station unveränderbar", station1.meeting_id == meeting_committee.id)

# =============================================================================
# Phase D: Berechtigungen
# =============================================================================
print()
print("=== Phase D: Berechtigungen ===")

mutations = [
    (f"{base}/papers/{paper.id}/consultations/add/", {"organization": str(committee.id)}),
    (f"{base}/consultations/{station1.id}/update/", {"role": "hearing"}),
    (f"{base}/consultations/{station1.id}/delete/", {}),
    (f"{base}/consultations/{station1.id}/move/", {"direction": "up"}),
    (f"{base}/consultations/{station1.id}/schedule/", {}),
    (f"{base}/consultations/{station1.id}/forward/", {}),
]
before_state = (
    SessionConsultation.objects.count(),
    SessionAgendaItem.objects.count(),
    list(paper.consultations.order_by("order").values_list("id", "order", "role")),
)
bad = [url for url, data in mutations if no_perm.post(url, data).status_code != 403]
check("Alle Beratungsfolge-Mutationen ohne Rechte -> 403", not bad, "; ".join(bad))
after_state = (
    SessionConsultation.objects.count(),
    SessionAgendaItem.objects.count(),
    list(paper.consultations.order_by("order").values_list("id", "order", "role")),
)
check("Keine Mutation ohne Berechtigung ausgeführt", before_state == after_state)

# edit_papers reicht NICHT für Terminierung (Tagesordnungs-Mutation)
resp = paper_editor.post(f"{base}/consultations/{station1.id}/schedule/", {})
check("Terminieren ohne edit_meetings -> 403", resp.status_code == 403, f"got {resp.status_code}")

# edit_meetings reicht NICHT für Ketten-Bearbeitung
resp = meeting_editor.post(f"{base}/papers/{paper.id}/consultations/add/", {"organization": str(committee.id)})
check("Station anlegen ohne edit_papers -> 403", resp.status_code == 403, f"got {resp.status_code}")

# Nur-Leser sieht die Kette, aber keine Bearbeitungs-Formulare
resp = viewer.get(f"{base}/papers/{paper.id}/")
check(
    "Leser sieht Kette ohne Bearbeitungsaktionen",
    resp.status_code == 200 and b"Beratungsfolge" in resp.content and b"consultations/add" not in resp.content,
    f"status={resp.status_code}",
)

# =============================================================================
# Phase E: Tenant-Isolation + Ö/NÖ
# =============================================================================
print()
print("=== Phase E: Tenant-Isolation + Ö/NÖ ===")

wrong = []
for url, data in [
    (f"{base}/consultations/{consultation_b.id}/update/", {"role": "hearing"}),
    (f"{base}/consultations/{consultation_b.id}/delete/", {}),
    (f"{base}/consultations/{consultation_b.id}/schedule/", {}),
    (f"{base}/papers/{paper_b.id}/consultations/add/", {"organization": str(org_b.id)}),
]:
    status = admin.post(url, data).status_code
    if status != 404:
        wrong.append(f"{url}: {status}")
check("Fremde Stationen/Vorlagen unter eigenem Slug -> 404", not wrong, "; ".join(wrong))
check("Fremde Station unverändert", SessionConsultation.objects.get(pk=consultation_b.pk).role == "preliminary")

# NÖ-Vorlage: Stationen für Nutzer ohne NÖ-Recht unsichtbar (404)
np_station = SessionConsultation.objects.create(paper=paper_np, organization=council, order=1, meeting=meeting_council)
resp = paper_editor.post(f"{base}/consultations/{np_station.id}/update/", {"role": "decision"})
check("NÖ-Vorlagen-Station ohne NÖ-Recht -> 404", resp.status_code == 404, f"got {resp.status_code}")

# NÖ-Vorlage -> TOP wird NÖ angelegt
clerk.post(f"{base}/consultations/{np_station.id}/schedule/", {})
np_station.refresh_from_db()
check(
    "NÖ-Vorlage erzeugt NÖ-TOP (getrennte N-Nummerierung)",
    np_station.agenda_item is not None
    and np_station.agenda_item.is_public is False
    and np_station.agenda_item.number.startswith("N"),
    f"item={np_station.agenda_item}",
)

# =============================================================================
# Phase F: Löschen + Audit
# =============================================================================
print()
print("=== Phase F: Löschen + Audit ===")

extra = SessionConsultation.objects.create(paper=paper, organization=other_org, role="hearing", order=3)
clerk.post(f"{base}/consultations/{extra.id}/delete/", {})
check("Station gelöscht", not SessionConsultation.objects.filter(pk=extra.pk).exists())
orders = list(paper.consultations.order_by("order").values_list("order", flat=True))
check("Reihenfolge nach Löschen lückenlos", orders == [1, 2], str(orders))

audit_entries = SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionConsultation")
check("Audit: create-Einträge für Stationen", audit_entries.filter(action="create").count() >= 3)
check("Audit: update-Einträge (u. a. Ergebnis-Rückschreibung)", audit_entries.filter(action="update").exists())
check("Audit: delete-Eintrag für entfernte Station", audit_entries.filter(action="delete").exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
