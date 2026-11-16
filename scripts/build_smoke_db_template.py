# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migrierte SQLite-Vorlage für die Smoke-Skripte erzeugen.

    python scripts/build_smoke_db_template.py [ZIELPFAD] [--aus VORLAGE]

Migriert einmal vollständig und legt das Ergebnis samt Fingerabdruck der
Migrationsdateien ab. Die Smoke-Skripte kopieren diese Datei anschließend,
statt jeweils erneut zu migrieren (siehe ``scripts/_smoke_db.py``).

Ohne Zielpfad wird ``<TMPDIR>/mandari-smoke-template.sqlite3`` benutzt. Der
Pfad wird auf der Standardausgabe ausgegeben und — falls ``GITHUB_ENV``
gesetzt ist — als ``MANDARI_SMOKE_DB_TEMPLATE`` für die folgenden Schritte
hinterlegt.

``--aus VORLAGE`` (Issue #1315): Liegt dort eine fertige Vorlage, deren Fingerabdruck zu den Migrationsdateien
des Arbeitsstands passt, wird sie kopiert statt neu migriert (die CI holt sie aus dem Vorrat von
``.github/workflows/testvorlage.yml``). Fehlt sie oder passt sie nicht, wird wie sonst migriert.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _smoke_db import FINGERPRINT_SUFFIX, migration_fingerprint  # noqa: E402

parser = argparse.ArgumentParser(description="Migrierte SQLite-Vorlage für die Smoke-Skripte erzeugen.")
parser.add_argument("ziel", nargs="?", default=str(Path(tempfile.gettempdir()) / "mandari-smoke-template.sqlite3"))
parser.add_argument("--aus", metavar="VORLAGE", help="fertige Vorlage kopieren, wenn ihr Fingerabdruck passt")
args = parser.parse_args()

target = Path(args.ziel)
target.parent.mkdir(parents=True, exist_ok=True)
target.unlink(missing_ok=True)
fingerprint = target.with_suffix(target.suffix + FINGERPRINT_SUFFIX)


def _melden(weg: str) -> None:
    print(f"Vorlage: {target} ({target.stat().st_size / 1024:.0f} KB, {weg})")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"MANDARI_SMOKE_DB_TEMPLATE={target}\n")


if args.aus:
    quelle = Path(args.aus)
    quelle_fingerprint = quelle.with_suffix(quelle.suffix + FINGERPRINT_SUFFIX)
    if (
        quelle.is_file()
        and quelle_fingerprint.is_file()
        and quelle_fingerprint.read_text(encoding="utf-8").strip() == migration_fingerprint(PROJECT_DIR)
    ):
        shutil.copyfile(quelle, target)
        shutil.copyfile(quelle_fingerprint, fingerprint)
        _melden("aus dem Vorrat kopiert")
        sys.exit(0)
    print(f"Keine passende Vorlage unter {quelle}, migriere")

# Dieselbe Umgebung wie in den Smoke-Skripten, damit dasselbe Schema entsteht.
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"
os.environ.setdefault("ENCRYPTION_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connections  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)
connections.close_all()

fingerprint.write_text(migration_fingerprint(PROJECT_DIR), encoding="utf-8")
_melden("migriert")
