# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migrierte SQLite-Vorlage für die Smoke-Skripte erzeugen.

    python scripts/build_smoke_db_template.py [ZIELPFAD]

Migriert einmal vollständig und legt das Ergebnis samt Fingerabdruck der
Migrationsdateien ab. Die Smoke-Skripte kopieren diese Datei anschließend,
statt jeweils erneut zu migrieren (siehe ``scripts/_smoke_db.py``).

Ohne Zielpfad wird ``<TMPDIR>/mandari-smoke-template.sqlite3`` benutzt. Der
Pfad wird auf der Standardausgabe ausgegeben und — falls ``GITHUB_ENV``
gesetzt ist — als ``MANDARI_SMOKE_DB_TEMPLATE`` für die folgenden Schritte
hinterlegt.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "mandari-smoke-template.sqlite3"
target.parent.mkdir(parents=True, exist_ok=True)
target.unlink(missing_ok=True)

# Dieselbe Umgebung wie in den Smoke-Skripten, damit dasselbe Schema entsteht.
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"
os.environ.setdefault("ENCRYPTION_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from _smoke_db import FINGERPRINT_SUFFIX, migration_fingerprint  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connections  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)
connections.close_all()

fingerprint = target.with_suffix(target.suffix + FINGERPRINT_SUFFIX)
fingerprint.write_text(migration_fingerprint(PROJECT_DIR), encoding="utf-8")

print(f"Vorlage: {target} ({target.stat().st_size / 1024:.0f} KB)")

github_env = os.environ.get("GITHUB_ENV")
if github_env:
    with open(github_env, "a", encoding="utf-8") as handle:
        handle.write(f"MANDARI_SMOKE_DB_TEMPLATE={target}\n")
