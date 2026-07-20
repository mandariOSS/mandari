# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: OParlFile.size_human darf self.size nicht mutieren (P1-Fix).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_oparl_file_size.py

Repro des Bugs: Die Property teilte `self.size` in der Einheiten-Schleife
durch 1024 (statt einer lokalen Variable). Nach einem Zugriff auf
`size_human` stand z. B. bei size=2048 nur noch 2.0 im Feld — ein
späteres save() schrieb den korrumpierten Wert in die Datenbank.

Prüft:
- size_human liefert weiterhin die erwartete Formatierung (B/KB/MB/GB/TB)
- self.size bleibt nach (mehrfachem) Zugriff unverändert
- save() nach size_human-Zugriff schreibt den Originalwert in die DB
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

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)

from insight_core.models import OParlFile  # noqa: E402

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


print("=== OParlFile.size_human: keine Mutation von self.size ===")

# Kern-Repro: size=2048 -> size_human -> size unverändert -> save() schreibt Original
f = OParlFile.objects.create(external_id="smoke://file/size-human/1", size=2048)
human = f.size_human
check("size_human formatiert 2048 als '2.0 KB'", human == "2.0 KB", f"got {human!r}")
check("self.size nach size_human-Zugriff unverändert (2048)", f.size == 2048, f"got {f.size!r}")

human2 = f.size_human
check("Zweiter Zugriff liefert identisches Ergebnis", human2 == "2.0 KB", f"got {human2!r}")
check("self.size auch nach mehrfachem Zugriff unverändert", f.size == 2048, f"got {f.size!r}")

f.save()
with connection.cursor() as cur:
    cur.execute("SELECT size FROM oparl_files WHERE external_id = %s", ["smoke://file/size-human/1"])
    db_size = cur.fetchone()[0]
check("save() nach size_human schreibt Originalwert 2048 in die DB", db_size == 2048, f"got {db_size!r}")

f.refresh_from_db()
check("refresh_from_db bestätigt size=2048", f.size == 2048, f"got {f.size!r}")

# Formatierung über alle Einheiten
cases = [
    (512, "512.0 B"),
    (1536, "1.5 KB"),
    (5 * 1024**2, "5.0 MB"),
    (3 * 1024**3, "3.0 GB"),
    (2 * 1024**4, "2.0 TB"),
]
for raw, expected in cases:
    obj = OParlFile(external_id=f"smoke://file/size-human/fmt-{raw}", size=raw)
    got = obj.size_human
    check(f"Formatierung {raw} -> {expected}", got == expected, f"got {got!r}")
    check(f"size bleibt {raw}", obj.size == raw, f"got {obj.size!r}")

# Leere Werte
check("size=None -> ''", OParlFile(external_id="smoke://file/none").size_human == "")
check("size=0 -> ''", OParlFile(external_id="smoke://file/zero", size=0).size_human == "")

print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
