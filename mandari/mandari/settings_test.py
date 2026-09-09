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

# Tests brauchen kein gebautes Manifest: Dev-URLs erzeugen, ohne Vite-Server zu benötigen
DJANGO_VITE = {"default": {**DJANGO_VITE["default"], "dev_mode": True}}  # noqa: F405
