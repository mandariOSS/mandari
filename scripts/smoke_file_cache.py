# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Lokaler Dokument-Cache + Datei-Proxy (Issues #87, #86).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_file_cache.py
"""

import base64
import io
import os
import secrets
import shutil
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
os.environ["ELASTICSEARCH_URL"] = ""
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["OPARL_FILES_ROOT"] = str(_tmp / "files")
os.environ["FILE_CACHE_MAX_MB"] = "1"
os.environ["FILE_CACHE_MIN_FREE_GB"] = "0"

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
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource  # noqa: E402
from insight_core.services import file_cache, source_health  # noqa: E402
from insight_core.views import files as files_view  # noqa: E402

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


print("=== Phase 0: Migrationsstand ===")
try:
    call_command("makemigrations", "insight_core", check=True, dry_run=True, verbosity=0)
    check("Keine fehlenden Migrationen", True)
except SystemExit:
    check("Keine fehlenden Migrationen", False)

# =============================================================================
# Fixture
# =============================================================================
now = timezone.now()
source = OParlSource.objects.create(name="RIS", url="https://ris.example/oparl/system")
body = OParlBody.objects.create(external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Köln (Test)")
paper = OParlPaper.objects.create(external_id="https://ris.example/oparl/paper/1", body=body, name="Vorlage")

PDF = b"%PDF-1.4\n%mandari\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
HTML = b"<!DOCTYPE html><html><body>Wartungsarbeiten</body></html>"


def make_file(num, mime="application/pdf", url=None, when=None, size=None):
    return OParlFile.objects.create(
        external_id=f"https://ris.example/oparl/file/{num}",
        body=body,
        paper=paper,
        name=f"Dokument {num}",
        file_name=f"dok{num}.pdf",
        mime_type=mime,
        access_url=url or f"https://ris.example/getfile/{num}.pdf",
        file_date=when or now,
        size=size,
    )


f_ok = make_file(1, when=now - timedelta(days=1))
f_404 = make_file(2, url="https://ris.example/getfile/404.pdf")
f_500 = make_file(3, url="https://ris.example/getfile/500.pdf")
f_html = make_file(4, url="https://ris.example/getfile/html.pdf")
f_big = make_file(5, url="https://ris.example/getfile/big.pdf")
f_bonn = make_file(6, mime="pdf", when=now - timedelta(days=400))
f_old = make_file(7, when=now - timedelta(days=800))


class FakeResponse:
    def __init__(self, status, content=b"", ctype="application/pdf"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype, "content-length": str(len(content))}

    def iter_bytes(self):
        for i in range(0, len(self.content), 4096):
            yield self.content[i : i + 4096]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            request = httpx.Request("GET", "https://ris.example/x")
            raise httpx.HTTPStatusError(
                "err", request=request, response=httpx.Response(self.status_code, request=request)
            )


class FakeClient:
    calls = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    @staticmethod
    def respond(url):
        FakeClient.calls.append(url)
        if url.endswith("404.pdf"):
            return FakeResponse(404, b"", "text/html")
        if url.endswith("500.pdf"):
            return FakeResponse(500, b"", "text/html")
        if url.endswith("html.pdf"):
            return FakeResponse(200, HTML, "text/html")
        if url.endswith("big.pdf"):
            return FakeResponse(200, b"%PDF" + b"0" * (2 * 1024 * 1024))
        if url.endswith("down.pdf"):
            import httpx

            raise httpx.ConnectError("Verbindung abgelehnt")
        return FakeResponse(200, PDF)

    def stream(self, method, url):
        return self.respond(url)

    def get(self, url, **kwargs):
        return self.respond(url)


# =============================================================================
print()
print("=== Phase 1: Cache-Service ===")
check("Verzeichnisname aus Kommune (Umlaute transliteriert)", file_cache.body_dir_name(body) == "stadt-koeln-test")
check("MIME „pdf“ (Bonn) -> application/pdf", file_cache.content_type_for(f_bonn) == "application/pdf")

status = file_cache.fetch_and_cache(f_ok, client=FakeClient())
f_ok.refresh_from_db()
path = Path(f_ok.local_path)
check("Abruf ok + Datei gespeichert", status == "ok" and f_ok.local_status == "ok" and path.is_file())
check(
    "Layout <root>/<kommune>/<jahr>/<id>.pdf",
    path.parent.parent.name == "stadt-koeln-test"
    and path.parent.name == str((now - timedelta(days=1)).year)
    and path.name == f"{f_ok.id}.pdf",
    str(path),
)
check(
    "Größe + SHA256 gesetzt", f_ok.size == len(PDF) and len(f_ok.sha256_hash) == 64 and f_ok.local_cached_at is not None
)
check("Erneuter Abruf übersprungen", file_cache.fetch_and_cache(f_ok, client=FakeClient()) == "skipped")

check("404 -> missing", file_cache.fetch_and_cache(f_404, client=FakeClient()) == "missing")
check(
    "500 -> error mit Meldung",
    file_cache.fetch_and_cache(f_500, client=FakeClient()) == "error"
    and "HTTP 500" in OParlFile.objects.get(id=f_500.id).local_error,
)
check(
    "HTML-Fehlerseite wird nicht als PDF gespeichert",
    file_cache.fetch_and_cache(f_html, client=FakeClient()) == "error"
    and "HTML" in OParlFile.objects.get(id=f_html.id).local_error,
)
check("Zu groß -> too_large", file_cache.fetch_and_cache(f_big, client=FakeClient()) == "too_large")

# Veraltete lokale Kopie (Datei gelöscht) wird erkannt
path.unlink()
check("Fehlende Datei trotz local_path -> None", file_cache.local_file(f_ok) is None)
check(
    "Fehlende Datei wird neu geladen", file_cache.fetch_and_cache(f_ok, client=FakeClient()) == "ok" and path.is_file()
)

# Nachladen: neueste zuerst, dann Festplatten-Schutz
pending = list(file_cache.pending_queryset().values_list("id", flat=True))
check(
    "Offen: nur Dateien ohne Kopie, neueste zuerst",
    pending[0] == f_bonn.id and pending[-1] == f_old.id and f_ok.id not in pending,
    str(pending),
)

import httpx  # noqa: E402

_real_client = httpx.Client
httpx.Client = FakeClient
try:
    results = file_cache.cache_pending(limit=10, sleep=0)
    check("cache_pending lädt Bonn-Datei + alte Datei", results["ok"] == 2, str(results))
    f_bonn.refresh_from_db()
    check("Jahr aus file_date", Path(f_bonn.local_path).parent.name == str((now - timedelta(days=400)).year))
    results = file_cache.cache_pending(limit=10, retry_errors=True, sleep=0)
    check("retry-errors versucht Fehler erneut", results["error"] >= 1, str(results))

    _real_disk = file_cache.disk_free_bytes
    file_cache.disk_free_bytes = lambda: 10 * 1024**3
    _dj_settings.FILE_CACHE_MIN_FREE_GB = 15
    check(
        "Festplatten-Schutz stoppt den Lauf",
        file_cache.cache_pending(limit=10, retry_errors=True, sleep=0)["disk_full"] == 1,
    )
    _dj_settings.FILE_CACHE_MIN_FREE_GB = 0
    file_cache.disk_free_bytes = _real_disk

    out = io.StringIO()
    call_command("cache_files", body="Köln", limit=5, sleep=0, stdout=out)
    check(
        "Command cache_files läuft mit Statistik",
        "Fertig:" in out.getvalue() and "Dokumenten lokal" in out.getvalue(),
        out.getvalue(),
    )
finally:
    httpx.Client = _real_client

stats = file_cache.cache_stats()
check(
    "Statistik: Abdeckung + Belegung",
    stats["ok"] == 3 and stats["total"] == 7 and stats["cached_bytes"] > 0 and stats["per_body"][0]["files"] == 7,
    str(stats),
)

# =============================================================================
print()
print("=== Phase 2: Datei-Proxy ===")
client = Client()
_real_get = files_view.httpx.get
files_view.httpx.get = lambda url, **kw: FakeClient.respond(url)
try:
    resp = client.get(f"/insight/dokumente/{f_ok.id}/preview/")
    body_bytes = b"".join(resp.streaming_content) if resp.streaming else resp.content
    check(
        "Lokale Kopie wird ausgeliefert (Cache-Hit)",
        resp.status_code == 200
        and resp["X-Mandari-Cache"] == "hit"
        and body_bytes == PDF
        and resp["Content-Type"].startswith("application/pdf"),
    )
    resp.close()
    resp = client.get(f"/insight/dokumente/{f_ok.id}/preview/?download=1")
    check(
        "Download-Header bei ?download=1",
        "attachment" in resp["Content-Disposition"] and "dok1.pdf" in resp["Content-Disposition"],
    )
    resp.close()

    f_live = make_file(8, url="https://ris.example/getfile/live.pdf")
    FakeClient.calls.clear()
    resp = client.get(f"/insight/dokumente/{f_live.id}/preview/")
    f_live.refresh_from_db()
    check(
        "Live-Abruf ohne Kopie -> Miss + Write-Through",
        resp.status_code == 200
        and resp["X-Mandari-Cache"] == "miss"
        and f_live.local_status == "ok"
        and Path(f_live.local_path).is_file(),
    )
    FakeClient.calls.clear()
    resp = client.get(f"/insight/dokumente/{f_live.id}/preview/")
    check(
        "Zweiter Aufruf kommt aus dem Cache ohne RIS-Zugriff",
        resp["X-Mandari-Cache"] == "hit" and FakeClient.calls == [],
    )
    resp.close()

    resp = client.get(f"/insight/dokumente/{f_404.id}/preview/")
    check("RIS 404 -> Fehlerseite", resp.status_code == 200 and "Datei nicht gefunden" in resp.content.decode())
    f_down = make_file(9, url="https://ris.example/getfile/down.pdf")
    resp = client.get(f"/insight/dokumente/{f_down.id}/preview/")
    page = resp.content.decode()
    check(
        "RIS nicht erreichbar -> Fehlerseite mit Cache-Hinweis",
        "nicht erreichbar" in page and "Zwischenspeicher" in page,
    )
    f_html2 = make_file(10, url="https://ris.example/getfile/html.pdf")
    resp = client.get(f"/insight/dokumente/{f_html2.id}/preview/")
    f_html2.refresh_from_db()
    check(
        "HTML-Fehlerseite der Quelle wird nicht als PDF gezeigt",
        "keine Datei" in resp.content.decode() and f_html2.local_status != "ok",
    )
finally:
    files_view.httpx.get = _real_get

# =============================================================================
print()
print("=== Phase 3: Monitor + Admin ===")
checks = source_health.collect_system_health()
cache_check = next((c for c in checks if c["name"] == "Dokument-Cache"), None)
check(
    "Betriebsmonitor hat Dokument-Cache-Check",
    cache_check is not None and "Dokumenten lokal" in cache_check["detail"],
    str(cache_check),
)
actions = source_health.collect_action_items()
check(
    "Handlungsbedarf: Dokumente mit Cache-Fehler",
    any("Cache-Fehler" in a["label"] for a in actions),
    str([a["label"] for a in actions]),
)

admin_user = User.objects.create_superuser(email="admin@example.org", password="pw-Smoke-1!")
admin = Client()
admin.force_login(admin_user)
resp = admin.get("/admin/insight_core/oparlfile/?local_status__exact=ok")
check("Admin-Dateiliste mit Cache-Filter", resp.status_code == 200 and "Dokument 1" in resp.content.decode())
resp = admin.get("/admin/monitoring/")
check("Betriebsmonitor zeigt Dokument-Cache", resp.status_code == 200 and "Dokument-Cache" in resp.content.decode())

# purge_deleted entfernt lokale Kopien
f_ok.deleted = True
f_ok.deleted_at = now - timedelta(days=400)
f_ok.save(update_fields=["deleted", "deleted_at"])
try:
    call_command("purge_deleted", ids=[str(f_ok.id)], yes=True, stdout=io.StringIO())
    check("purge_deleted entfernt lokale Kopie", not Path(f_ok.local_path).is_file())
except Exception as exc:  # Command hat evtl. andere Pflichtparameter
    check("purge_deleted entfernt lokale Kopie", False, str(exc))

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
