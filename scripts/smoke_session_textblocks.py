# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Textbausteine und Standard-Tagesordnungspunkte (Issue #85).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_textblocks.py

Prüft:
- Verwaltungsseite: Standard-TOPs und Textbausteine anlegen/löschen (+ Audit)
- Automatische Übernahme der Standard-TOPs beim Anlegen einer Sitzung
  (Anfang/Ende, gremiumsspezifisch vs. alle Gremien, NÖ-Flag)
- Ende-TOPs bleiben hinter später ergänzten TOPs
- Übernahme auch bei Serienterminen der Jahresplanung
- Textbaustein-Auswahl erscheint in Vorlagen- und Protokollformular
- Berechtigungen und Tenant-Isolation
"""

import base64
import os
import secrets
import sys
import tempfile
from datetime import timedelta
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
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

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
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionProtocol,
    SessionRole,
    SessionStandardAgendaItem,
    SessionTenant,
    SessionTextBlock,
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
        print(f"  FAIL {name} {detail}")


# =============================================================================
# Setup
# =============================================================================
now = timezone.now()

tenant = SessionTenant.objects.create(name="Bausteinstadt", slug="bausteinstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-tb")

org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
org2 = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin_user = User.objects.create_user(email="admin-tb@example.org", password="pw-Smoke-1!")
su_admin = SessionUser.objects.create(user=admin_user, tenant=tenant)
su_admin.roles.add(admin_role)
admin = Client()
admin.force_login(admin_user)

viewer_role = SessionRole.objects.create(tenant=tenant, name="Leser", can_view_meetings=True)
viewer_user = User.objects.create_user(email="leser-tb@example.org", password="pw-Smoke-1!")
su_viewer = SessionUser.objects.create(user=viewer_user, tenant=tenant)
su_viewer.roles.add(viewer_role)
viewer = Client()
viewer.force_login(viewer_user)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Verwaltungsseite ===")
resp = admin.get(f"{base}/settings/textblocks/")
check("Verwaltungsseite -> 200", resp.status_code == 200, f"got {resp.status_code}")

resp = admin.post(
    f"{base}/settings/textblocks/standard/",
    {"name": "STD-EROEFFNUNG", "placement": "start", "order": "1", "is_public": "1"},
)
resp = admin.post(
    f"{base}/settings/textblocks/standard/",
    {"name": "STD-NIEDERSCHRIFT", "placement": "start", "order": "2", "is_public": "1"},
)
resp = admin.post(
    f"{base}/settings/textblocks/standard/",
    {"name": "STD-ANFRAGEN", "placement": "end", "order": "1", "is_public": "1"},
)
resp = admin.post(
    f"{base}/settings/textblocks/standard/",
    {"name": "STD-NUR-RAT-NOE", "placement": "end", "order": "2", "organization": str(org.id)},
)
check("4 Standard-TOPs angelegt", SessionStandardAgendaItem.objects.filter(tenant=tenant).count() == 4)
check(
    "NÖ-Flag gespeichert",
    SessionStandardAgendaItem.objects.get(name="STD-NUR-RAT-NOE").is_public is False,
)
check(
    "Audit: Standard-TOP angelegt",
    SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionStandardAgendaItem", action="create").count() == 4,
)

resp = admin.post(
    f"{base}/settings/textblocks/block/",
    {"title": "TB-EINSTIMMIG", "content": "Der {gremium} beschließt einstimmig.", "category": "resolution"},
)
resp = admin.post(
    f"{base}/settings/textblocks/block/",
    {"title": "TB-PROTOKOLL", "content": "Ohne Aussprache am {datum}.", "category": "protocol"},
)
check("2 Textbausteine angelegt", SessionTextBlock.objects.filter(tenant=tenant).count() == 2)

resp = admin.post(f"{base}/settings/textblocks/standard/", {"name": ""})
check("Standard-TOP ohne Betreff abgelehnt", SessionStandardAgendaItem.objects.filter(tenant=tenant).count() == 4)
resp = admin.post(f"{base}/settings/textblocks/block/", {"title": "X", "content": ""})
check("Textbaustein ohne Text abgelehnt", SessionTextBlock.objects.filter(tenant=tenant).count() == 2)

resp = viewer.get(f"{base}/settings/textblocks/")
check("Verwaltungsseite ohne manage_settings -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = viewer.post(f"{base}/settings/textblocks/standard/", {"name": "HACK"})
check("Anlegen ohne manage_settings -> 403", resp.status_code == 403, f"got {resp.status_code}")

# =============================================================================
print()
print("=== Phase B: Automatische Übernahme ===")
start = (now + timedelta(days=14)).replace(microsecond=0)
resp = admin.post(
    f"{base}/meetings/create/",
    {
        "name": "SITZUNG-MIT-STANDARD",
        "organization": str(org.id),
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "is_public": "on",
    },
)
meeting = SessionMeeting.objects.filter(tenant=tenant, name="SITZUNG-MIT-STANDARD").first()
check("Sitzung angelegt", meeting is not None, f"status {resp.status_code}")
items = list(meeting.agenda_items.order_by("order")) if meeting else []
check("4 Standard-TOPs übernommen (Rat)", len(items) == 4, f"got {len(items)}")
if len(items) == 4:
    check("Anfang zuerst", items[0].name == "STD-EROEFFNUNG" and items[1].name == "STD-NIEDERSCHRIFT")
    check("Ende zuletzt", items[2].name == "STD-ANFRAGEN" and items[3].name == "STD-NUR-RAT-NOE")
    check("NÖ-Standard-TOP als NÖ übernommen", items[3].is_public is False)
    check("Fortlaufende Nummern", [i.number for i in items] == ["1", "2", "3", "4"])

# Später ergänzter TOP landet vor den Ende-TOPs
resp = admin.post(
    f"{base}/meetings/{meeting.id}/agenda/add/",
    {"name": "NEUER-SACHTOP", "number": "9", "is_public": "on"},
)
items = list(meeting.agenda_items.order_by("order"))
names = [i.name for i in items]
check(
    "Neuer TOP vor den Ende-TOPs",
    names.index("NEUER-SACHTOP") < names.index("STD-ANFRAGEN"),
    str(names),
)

# Bauausschuss: nur die "alle Gremien"-TOPs (3 Stück)
resp = admin.post(
    f"{base}/meetings/create/",
    {
        "name": "SITZUNG-BAU",
        "organization": str(org2.id),
        "start": (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        "is_public": "on",
    },
)
meeting_bau = SessionMeeting.objects.filter(tenant=tenant, name="SITZUNG-BAU").first()
check(
    "Gremiumsspezifischer TOP nicht im Bauausschuss",
    meeting_bau is not None and meeting_bau.agenda_items.count() == 3
    and not meeting_bau.agenda_items.filter(name="STD-NUR-RAT-NOE").exists(),
)

# Fremd-Mandant: Sitzung dort bekommt keine TOPs dieses Mandanten
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdrat")
from apps.session.services import textblock_service  # noqa: E402

meeting_b = SessionMeeting.objects.create(
    tenant=tenant_b, name="FREMD-SITZUNG", organization=org_b, start=now + timedelta(days=5)
)
applied = textblock_service.apply_standard_items(meeting_b)
check("Tenant-Isolation der Standard-TOPs", applied == 0 and meeting_b.agenda_items.count() == 0)

# =============================================================================
print()
print("=== Phase C: Serientermine übernehmen Standard-TOPs ===")
resp = admin.post(
    f"{base}/meetings/plan/",
    {
        "organization": str(org2.id),
        "name": "SERIE-MIT-TOPS",
        "rhythm": "monthly_1",
        "weekday": "1",
        "time": "18:00",
        "date_from": "2027-03-01",
        "date_to": "2027-04-30",
        "is_public": "1",
        "action": "create",
    },
)
serie = SessionMeeting.objects.filter(tenant=tenant, name="SERIE-MIT-TOPS")
check("Serie angelegt", serie.count() == 2, f"got {serie.count()}")
check(
    "Serien-Sitzungen mit Standard-TOPs",
    all(m.agenda_items.count() == 3 for m in serie),
)

# =============================================================================
print()
print("=== Phase D: Editor-Auswahl ===")
resp = admin.get(f"{base}/papers/create/")
html = resp.content.decode("utf-8")
check("Vorlagen-Formular zeigt Bausteine", "TB-EINSTIMMIG" in html)
check("Protokoll-Baustein nicht im Vorlagen-Formular", "TB-PROTOKOLL" not in html)
check("Platzhalter-Inhalt im Formular", "Der {gremium} beschließt einstimmig." in html)

SessionProtocol.objects.create(meeting=meeting, created_by=su_admin)
resp = admin.get(f"{base}/meetings/{meeting.id}/protocol/edit/")
html = resp.content.decode("utf-8")
check("Protokoll-Formular -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Protokoll-Formular zeigt Protokoll-Baustein", "TB-PROTOKOLL" in html)
check("Beschluss-Baustein nicht im Protokoll-Formular", "TB-EINSTIMMIG" not in html)

# Löschen
block = SessionTextBlock.objects.get(title="TB-PROTOKOLL")
resp = admin.post(
    f"{base}/settings/textblocks/block/",
    {"block_id": str(block.id), "action": "delete"},
)
check("Textbaustein gelöscht", not SessionTextBlock.objects.filter(pk=block.id).exists())
std = SessionStandardAgendaItem.objects.get(name="STD-ANFRAGEN")
resp = admin.post(
    f"{base}/settings/textblocks/standard/",
    {"item_id": str(std.id), "action": "delete"},
)
check("Standard-TOP gelöscht", not SessionStandardAgendaItem.objects.filter(pk=std.id).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
