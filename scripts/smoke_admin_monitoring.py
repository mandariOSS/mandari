# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Betriebsmonitor (Quellen-Gesundheit, Admin-Seiten, Alarmierung, Datenstand-Hinweis).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_admin_monitoring.py
"""

import base64
import io
import os
import secrets
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["ELASTICSEARCH_URL"] = ""
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["SITE_URL"] = "https://insight.example"
os.environ["INSIGHT_ALERT_EMAILS"] = "betrieb@example.org"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core import mail  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from insight_core.models import OParlBody, OParlPerson, OParlSource, PublicQuestion  # noqa: E402
from insight_core.services import source_health  # noqa: E402
from insight_sync.models import SyncLog  # noqa: E402

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


print("=== Phase 0: Migrationsstand ===")
try:
    call_command("makemigrations", "insight_core", check=True, dry_run=True, verbosity=0)
    check("Keine fehlenden Migrationen für insight_core", True)
except SystemExit:
    check("Keine fehlenden Migrationen für insight_core", False)

# =============================================================================
# Fixture: Quellen in allen Zuständen
# =============================================================================
now = timezone.now()


def source(name, **kwargs):
    return OParlSource.objects.create(name=name, url=f"https://{name.lower()}.example/oparl/system", **kwargs)


ok_src = source("Frischstadt", last_sync=now - timedelta(hours=1))
warn_src = source("Wartestadt", last_sync=now - timedelta(days=3))
crit_src = source(
    "Blockstadt",
    last_sync=now - timedelta(days=200),
    last_error="HTTP 403",
    last_error_at=now - timedelta(hours=2),
    consecutive_failures=5,
)
fail_src = source("Wackelstadt", last_sync=now - timedelta(hours=1), last_error="Timeout", consecutive_failures=1)
never_src = source("Neustadt")
inactive_src = source("Altstadt", is_active=False, last_sync=now - timedelta(days=400))
old_never = source("Vergessenstadt")
OParlSource.objects.filter(pk=old_never.pk).update(created_at=now - timedelta(days=30))
old_never.refresh_from_db()

for src, name in [(ok_src, "Frischstadt"), (crit_src, "Blockstadt")]:
    OParlBody.objects.create(
        external_id=f"https://{name.lower()}.example/oparl/body/1",
        source=src,
        name=f"Stadt {name}",
        slug=name.lower(),
        last_sync=src.last_sync,
    )

admin_user = User.objects.create_superuser(email="admin@example.org", password="pw-Smoke-1!")
admin = Client()
admin.force_login(admin_user)

# =============================================================================
print()
print("=== Phase 1: Bewertung ===")
ev = source_health.evaluate_source
check("Frischer Sync -> ok", ev(ok_src)["status"] == "ok")
check("3 Tage alt -> warning", ev(warn_src)["status"] == "warning")
crit = ev(crit_src)
check("200 Tage + 5 Fehlversuche -> critical", crit["status"] == "critical")
check(
    "Gründe nennen Alter, Fehlversuche und Fehler",
    any("200 Tagen" in r for r in crit["reasons"])
    and any("5 Fehlversuche" in r for r in crit["reasons"])
    and any("HTTP 403" in r for r in crit["reasons"]),
    str(crit["reasons"]),
)
check("Frisch, aber 1 Fehlversuch -> warning", ev(fail_src)["status"] == "warning")
check("Neu angelegt ohne Sync -> never", ev(never_src)["status"] == "never")
check("30 Tage angelegt ohne Sync -> critical", ev(old_never)["status"] == "critical")
check("Inaktiv -> inactive (auch wenn alt)", ev(inactive_src)["status"] == "inactive")

health = source_health.collect_source_health()
order = [i["source"].name for i in health["items"]]
check(
    "Kritische zuerst, inaktive zuletzt",
    order[0] in ("Blockstadt", "Vergessenstadt") and order[-1] == "Altstadt",
    str(order),
)
check(
    "Zusammenfassung zählt",
    health["counts"]["critical"] == 2 and health["counts"]["warning"] == 2 and health["counts"]["ok"] == 1,
)
check("Gesamtstatus kritisch", health["overall"] == "critical")
blockstadt = next(i for i in health["items"] if i["source"] == crit_src)
check("Kommunen der Quelle zugeordnet", blockstadt["bodies"] == ["Stadt Blockstadt"])

system = source_health.collect_system_health()
names = {c["name"] for c in system}
check(
    "Systemchecks vorhanden",
    {"Datenbank", "Cache", "Elasticsearch", "Ingestor-Daemon", "Sync-Läufe (24 h)"} <= names,
    str(names),
)
check("Datenbank ok", next(c for c in system if c["name"] == "Datenbank")["status"] == "ok")
check(
    "Elasticsearch unkonfiguriert -> inaktiv",
    next(c for c in system if c["name"] == "Elasticsearch")["status"] == "inactive",
)
check("Daemon ohne Logs -> Warnung", next(c for c in system if c["name"] == "Ingestor-Daemon")["status"] == "warning")

SyncLog.objects.create(
    sync_type="incremental",
    status="failed",
    errors=["https://blockstadt.example/oparl/system (HTTP 403)"],
    entities_synced=0,
)
system = source_health.collect_system_health()
check(
    "Alle Läufe fehlgeschlagen -> kritisch",
    next(c for c in system if c["name"] == "Sync-Läufe (24 h)")["status"] == "critical",
)
check("Daemon mit frischem Log -> ok", next(c for c in system if c["name"] == "Ingestor-Daemon")["status"] == "ok")

body_block = OParlBody.objects.get(name="Stadt Blockstadt")
test_person = OParlPerson.objects.create(
    external_id="https://blockstadt.example/oparl/people/1", body=body_block, name="Test Person"
)
PublicQuestion.objects.create(
    body=body_block,
    recipient=test_person,
    questioner_name="F",
    questioner_email="f@example.org",
    subject="S",
    question_text="x" * 60,
    status="pending",
    privacy_accepted=True,
)
actions = source_health.collect_action_items()
check(
    "Handlungsbedarf: Frage wartet auf Freigabe",
    any(a["label"].startswith("Ratsfragen warten") and a["count"] == 1 for a in actions),
    str(actions),
)

# =============================================================================
print()
print("=== Phase 2: Admin-Oberflächen ===")
resp = admin.get("/admin/")
page = html(resp)
check("Dashboard -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Betriebsstatus-Panel kritisch", "Betriebsstatus: Kritisch" in page)
check("Kritische Quelle mit Grund im Panel", "Blockstadt" in page and "HTTP 403" in page)
check("Link zum Betriebsmonitor", "/admin/monitoring/" in page)
check("Handlungsbedarf im Panel", "Ratsfragen warten auf Freigabe" in page)

resp = admin.get("/admin/monitoring/")
page = html(resp)
check("Betriebsmonitor -> 200", resp.status_code == 200, f"got {resp.status_code}")
check(
    "Alle Quellen gelistet", all(n in page for n in ["Frischstadt", "Wartestadt", "Blockstadt", "Neustadt", "Altstadt"])
)
check(
    "Systemchecks + Sync-Läufe sichtbar",
    "Ingestor-Daemon" in page and "Fehlgeschlagen" in page and "(HTTP 403)" in page,
)
check("Schwellen erklärt", "kritisch ab 7 Tagen" in page)
anon = Client()
resp = anon.get("/admin/monitoring/")
check("Betriebsmonitor nur für Staff", resp.status_code in (302, 403))

resp = admin.get("/admin/insight_core/oparlsource/")
page = html(resp)
check(
    "Quellen-Liste -> 200 mit Gesundheitsspalte",
    resp.status_code == 200 and "Kritisch" in page and "Gesundheit" in page,
)
resp = admin.get("/admin/insight_core/oparlsource/?health=critical")
page = html(resp)
check("Filter kritisch", "Blockstadt" in page and "Vergessenstadt" in page and "Frischstadt" not in page)
resp = admin.get(f"/admin/insight_core/oparlsource/{crit_src.pk}/change/")
check("Detailseite zeigt Fehler", resp.status_code == 200 and "HTTP 403" in html(resp))

# =============================================================================
print()
print("=== Phase 3: Alarmierung ===")
mail.outbox.clear()
cache.clear()
result = source_health.send_health_alerts()
check("Alarme für beide kritischen Quellen", sorted(result["alerts"]) == ["Blockstadt", "Vergessenstadt"], str(result))
alert_mails = [m for m in mail.outbox if "kritisch" in m.subject]
check(
    "Alarm-Mails an konfigurierten Empfänger",
    len(alert_mails) == 2 and all(m.to == ["betrieb@example.org"] for m in alert_mails),
)
check(
    "Alarm nennt Grund + Monitor-Link",
    any("HTTP 403" in m.body and "/admin/monitoring/" in m.body for m in alert_mails),
)
crit_src.refresh_from_db()
check("health_alert_sent_at gesetzt", crit_src.health_alert_sent_at is not None)
check("Kein Daemon-Alarm bei frischem Log", not result["daemon_alert"])

mail.outbox.clear()
result = source_health.send_health_alerts()
check("Kein Doppel-Alarm", result["alerts"] == [] and len(mail.outbox) == 0)

crit_src.last_sync = now
crit_src.last_error = None
crit_src.consecutive_failures = 0
crit_src.save()
mail.outbox.clear()
result = source_health.send_health_alerts()
crit_src.refresh_from_db()
check(
    "Entwarnung nach Erholung",
    result["recoveries"] == ["Blockstadt"] and len(mail.outbox) == 1 and "wieder erreichbar" in mail.outbox[0].subject,
)
check("Alarm-Zeitstempel gelöscht", crit_src.health_alert_sent_at is None)

SyncLog.objects.all().update(started_at=now - timedelta(hours=8))
mail.outbox.clear()
result = source_health.send_health_alerts()
check(
    "Daemon-Alarm bei 8 h Stille", result["daemon_alert"] and any("Ingestor-Daemon" in m.subject for m in mail.outbox)
)
mail.outbox.clear()
result = source_health.send_health_alerts()
check("Daemon-Alarm nur einmal je 24 h", not result["daemon_alert"] and len(mail.outbox) == 0)

out = io.StringIO()
call_command("check_source_health", report=True, stdout=out)
text = out.getvalue()
check(
    "Command --report listet Quellen + Status",
    "Vergessenstadt" in text and "Kritisch" in text and "Gesamtstatus" in text,
)
out = io.StringIO()
call_command("check_source_health", dry_run=True, stdout=out)
check("Command --dry-run zählt fällige Alarme", "Alarme fällig" in out.getvalue())

# =============================================================================
print()
print("=== Phase 4: Datenstand-Hinweis im Portal ===")
client = Client()
sess = client.session
sess["active_body_id"] = str(body_block.id)
sess.save()
body_block.last_sync = now - timedelta(days=200)
body_block.save(update_fields=["last_sync"])
page = html(client.get("/insight/"))
check("Stale Kommune zeigt Datenstand-Hinweis", "Datenstand:" in page and "200 Tagen" in page)
sess = client.session
sess["active_body_id"] = str(OParlBody.objects.get(name="Stadt Frischstadt").id)
sess.save()
check("Frische Kommune ohne Hinweis", "Datenstand:" not in html(client.get("/insight/")))

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
