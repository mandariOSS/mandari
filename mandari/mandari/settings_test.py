# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Test-Settings für pytest-django.

Baut auf den regulären Settings auf und ersetzt alles, was im Test nicht von außen kommen soll:
SQLite statt PostgreSQL (sofern keine DATABASE_URL gesetzt ist, z. B. in der CI), lokaler Cache
statt Redis, E-Mails in den Speicher, keine Suchindexierung, ein zufälliger Master-Schlüssel für
die Feldverschlüsselung und kein Watchdog-Thread des Sync-Dienstes.
"""

import base64
import os
import secrets
import tempfile
from pathlib import Path

# Muss VOR dem Import der Settings gesetzt sein (wird in AppConfig.ready gelesen)
os.environ.setdefault("MANDARI_SYNC_WATCHDOG", "0")
os.environ.setdefault("ELASTICSEARCH_AUTO_INDEX", "False")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost")
if not os.environ.get("DATABASE_URL"):
    _db = Path(tempfile.mkdtemp(prefix="mandari_pytest_")) / "test.sqlite3"
    os.environ["DATABASE_URL"] = f"sqlite:///{_db.as_posix()}"
os.environ.setdefault("REDIS_URL", "")

from .settings import *  # noqa: E402, F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
ELASTICSEARCH_AUTO_INDEX = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # schnelle Hashes nur im Test
TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

# Tests brauchen kein gebautes Manifest: Dev-URLs erzeugen, ohne Vite-Server zu benötigen.
# E2E-Tests (MANDARI_E2E=1) laden dagegen die gebauten Assets aus static/dist/ über den Live-Server.
DJANGO_VITE = {"default": {**DJANGO_VITE["default"], "dev_mode": os.environ.get("MANDARI_E2E") != "1"}}  # noqa: F405

# E2E: Der Browser stellt parallele Anfragen an den Live-Server (Polling, Nachladen). Mit der
# In-Memory-Testdatenbank teilen sich alle Server-Threads eine SQLite-Verbindung und scheitern mit
# "database table is locked" – Serverfehler im Test wären dann Umgebungsartefakte. Als Datei mit
# Lock-Timeout warten parallele Schreiber; die Tests laufen dafür transaktional (tests_e2e/conftest.py).
if os.environ.get("MANDARI_E2E") == "1" and DATABASES["default"]["ENGINE"].endswith("sqlite3"):  # noqa: F405
    DATABASES["default"]["TEST"] = {  # noqa: F405
        "NAME": str(Path(tempfile.mkdtemp(prefix="mandari_e2e_")) / "e2e.sqlite3"),
    }
    DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30  # noqa: F405

# Komponentenvorschau /dev/ui/ auch ohne DEBUG (Rendering- und E2E-Tests)
UI_KIT_PREVIEW = True

# Statische Dateien ohne Manifest: Tests (auch E2E mit DEBUG=false) brauchen kein collectstatic; der
# Live-Server liefert über die Finder. Fehlende Referenzen prüft apps/common/tests/test_static_references.py.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    **globals().get("STORAGES", {}),  # bei DEBUG=false definiert settings.py WhiteNoise-Storages
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# 2FA-Pflicht und Admin-Netze in Tests aus; die Durchsetzung testet apps/accounts/tests/test_two_factor_policy.py
TWO_FACTOR_ENFORCEMENT = False
ADMIN_ALLOWED_NETWORKS = []
WEBAUTHN_RP_ID = "testserver"
