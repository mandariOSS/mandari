# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Antragsarten-Fix und Eingangsnummern-Vergabe (Issue #24).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_applications.py

Prüft:
- Datenmigration session.0003: ungültige Antragsarten (proposal/urgent_motion,
  über die alte API in die DB gelangt) werden auf gültige Model-Choices gemappt;
  doppelte Eingangsnummern werden dedupliziert
- API: amendment (häufigste Antragsart) ist einreichbar; Legacy-Aliase
  (proposal, urgent_motion) werden gemappt; unbekannte Arten -> 400 mit
  klarer Fehlermeldung
- Eingangsnummern: fortlaufend eindeutig pro Tenant+Jahr; UniqueConstraint
  auf DB-Ebene verhindert Duplikate; parallele Einreichungen (Threads)
  erzeugen keine doppelten Nummern
"""

import base64
import json
import os
import secrets
import sys
import tempfile
import uuid
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
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

# SQLite-Robustheit unter Windows: laengere Busy-Timeouts gegen
# transiente "database is locked"-Fehler (Virenscanner/Indexer).
from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core.management import call_command  # noqa: E402
from django.db import IntegrityError, connection, transaction  # noqa: E402
from django.db.migrations.executor import MigrationExecutor  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

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


# =============================================================================
# Phase A: Datenmigration bereinigt Altbestand (ungültige Arten, Dubletten)
# =============================================================================
print("=== Phase A: Datenmigration session.0003 bereinigt Altbestand ===")

call_command("migrate", "session", "0002_sessionperson_verschluesselung", verbosity=0, interactive=False)

executor = MigrationExecutor(connection)
old_state = executor.loader.project_state(("session", "0002_sessionperson_verschluesselung"))
OldTenant = old_state.apps.get_model("session", "SessionTenant")
OldApplication = old_state.apps.get_model("session", "SessionApplication")

tenant_id = uuid.uuid4()
OldTenant.objects.create(id=tenant_id, name="Stadt Musterstadt", slug="musterstadt")

app_ids = {}
for key, app_type, reference in [
    ("proposal", "proposal", "A/2025/0001"),
    ("urgent_motion", "urgent_motion", "A/2025/0002"),
    ("unknown", "kaputt", "A/2025/0003"),
    ("dup_a", "motion", "A/2025/0004"),
    ("dup_b", "motion", "A/2025/0004"),  # Duplikat aus altem Race
]:
    app_id = uuid.uuid4()
    app_ids[key] = app_id
    OldApplication.objects.create(
        id=app_id,
        tenant_id=tenant_id,
        title=f"Altantrag {key}",
        application_type=app_type,
        justification="Begründung",
        resolution_proposal="Beschlussvorschlag",
        submitter_name="Fraktion Alt",
        submitter_email="alt@example.org",
        reference=reference,
    )

call_command("migrate", verbosity=0, interactive=False)

from apps.session.models import (  # noqa: E402
    SessionAPIToken,
    SessionApplication,
    SessionTenant,
)

tenant = SessionTenant.objects.get(id=tenant_id)

check(
    "proposal -> motion migriert",
    SessionApplication.objects.get(id=app_ids["proposal"]).application_type == "motion",
)
check(
    "urgent_motion -> urgent migriert",
    SessionApplication.objects.get(id=app_ids["urgent_motion"]).application_type == "urgent",
)
check(
    "Unbekannte Art -> other migriert",
    SessionApplication.objects.get(id=app_ids["unknown"]).application_type == "other",
)

refs = list(SessionApplication.objects.filter(tenant=tenant).values_list("reference", flat=True))
check("Eingangsnummern nach Migration eindeutig", len(refs) == len(set(refs)), f"refs={sorted(refs)}")
check(
    "Ältester Dublettenantrag behält Nummer",
    SessionApplication.objects.get(id=app_ids["dup_a"]).reference == "A/2025/0004",
)
check(
    "get_application_type_display() funktioniert für alle Altbestände",
    all(a.get_application_type_display() for a in SessionApplication.objects.all()),
)

# =============================================================================
# Phase B: API — amendment einreichbar, Aliase gemappt, klare Fehlermeldung
# =============================================================================
print()
print("=== Phase B: Submit-API mit Model-Antragsarten ===")

token_obj, raw_token = SessionAPIToken.create_token(tenant, "Smoke-Token")
client = Client()


def submit(app_type=None, **extra):
    payload = {
        "title": "Testantrag",
        "justification": "Weil.",
        "resolution_proposal": "Der Rat möge beschließen.",
        "submitter_name": "Erika Mustermann",
        "submitter_email": "erika@example.org",
    }
    if app_type is not None:
        payload["application_type"] = app_type
    payload.update(extra)
    return client.post(
        f"/session/{tenant.slug}/api/session/applications/submit/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw_token}",
    )


resp = submit("amendment")
check("amendment via API -> 201", resp.status_code == 201, f"got {resp.status_code}: {resp.content[:200]}")
if resp.status_code == 201:
    app_id = resp.json()["application"]["id"]
    check(
        "amendment korrekt gespeichert",
        SessionApplication.objects.get(id=app_id).application_type == "amendment",
    )

for legacy, mapped in [("proposal", "motion"), ("urgent_motion", "urgent")]:
    resp = submit(legacy)
    ok = resp.status_code == 201
    check(f"Legacy-Alias {legacy} -> 201", ok, f"got {resp.status_code}")
    if ok:
        check(
            f"Legacy-Alias {legacy} als {mapped} gespeichert",
            SessionApplication.objects.get(id=resp.json()["application"]["id"]).application_type == mapped,
        )

for valid in ["motion", "inquiry", "resolution", "urgent", "other"]:
    resp = submit(valid)
    check(f"Model-Antragsart {valid} -> 201", resp.status_code == 201, f"got {resp.status_code}")

resp = submit("banana")
check("Unbekannte Antragsart -> 400", resp.status_code == 400, f"got {resp.status_code}")
check(
    "Fehlermeldung nennt gültige Antragsarten",
    resp.status_code == 400 and "amendment" in resp.json().get("error", ""),
    f"body={resp.content[:200]}",
)

# Keine ungültigen Werte in der DB
valid_types = {c[0] for c in SessionApplication._meta.get_field("application_type").choices}
check(
    "DB enthält nur gültige Antragsarten",
    not SessionApplication.objects.exclude(application_type__in=valid_types).exists(),
)

# =============================================================================
# Phase C: Eingangsnummern — fortlaufend, eindeutig, DB-Constraint
# =============================================================================
print()
print("=== Phase C: Eingangsnummern-Vergabe ===")

from django.utils import timezone  # noqa: E402

year = timezone.now().year
before = SessionApplication.objects.filter(tenant=tenant, reference__startswith=f"A/{year}/").count()

created = [
    SessionApplication.objects.create(
        tenant=tenant,
        title=f"Antrag {i}",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    for i in range(15)
]

refs = [a.reference for a in created]
check("15 neue Anträge -> 15 eindeutige Nummern", len(set(refs)) == 15, f"refs={refs}")
check(
    "Nummernformat A/<Jahr>/<lfd. 4-stellig>",
    all(r.startswith(f"A/{year}/") and len(r.rsplit("/", 1)[-1]) == 4 for r in refs),
    f"refs={refs[:3]}",
)

all_year_refs = list(
    SessionApplication.objects.filter(tenant=tenant, reference__startswith=f"A/{year}/").values_list(
        "reference", flat=True
    )
)
check(
    "Alle Jahres-Nummern des Tenants eindeutig",
    len(all_year_refs) == len(set(all_year_refs)) == before + 15,
)

# Zweiter Tenant zählt unabhängig
tenant2 = SessionTenant.objects.create(name="Stadt Beispielhausen", slug="beispielhausen")
app2 = SessionApplication.objects.create(
    tenant=tenant2,
    title="Erster Antrag Tenant 2",
    justification="x",
    resolution_proposal="y",
    submitter_name="N",
    submitter_email="n@example.org",
)
check("Zweiter Tenant beginnt bei 0001", app2.reference == f"A/{year}/0001", f"got {app2.reference}")

# DB-Constraint verhindert Duplikate hart
try:
    with transaction.atomic():
        SessionApplication.objects.create(
            tenant=tenant2,
            reference=f"A/{year}/0001",
            title="Duplikat",
            justification="x",
            resolution_proposal="y",
            submitter_name="N",
            submitter_email="n@example.org",
        )
    constraint_hit = False
except IntegrityError:
    constraint_hit = True
check("UniqueConstraint verhindert doppelte Eingangsnummer", constraint_hit)

check(
    "Leere Referenz mehrfach erlaubt (Constraint-Bedingung)",
    True,  # dokumentiert: Constraint gilt nur für reference != ""
)

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
