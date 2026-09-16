# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migriertes Schema für die Smoke-Skripte bereitstellen.

Jedes Smoke-Skript lief bisher gegen eine frische SQLite-Datei und spielte dafür
alle Migrationen ab. Das dauert rund 38 Sekunden — bei 42 Skripten in der CI also
etwa 26 Minuten, in denen 42-mal exakt dasselbe Schema entsteht. Die eigentliche
Prüfarbeit eines Skripts dauert wenige Sekunden.

Deshalb: einmal migrieren, das Ergebnis als Vorlage ablegen, danach kopieren.
``scripts/build_smoke_db_template.py`` erzeugt die Vorlage und trägt ihren Pfad
in ``MANDARI_SMOKE_DB_TEMPLATE`` ein.

Ohne gesetzte Umgebungsvariable wird ganz normal migriert — die Abkürzung ist
also ausdrücklich einzuschalten und ändert für niemanden etwas ungefragt.

**Nicht** verwendbar für Skripte, die Datenmigrationen prüfen (erst auf einen
alten Stand migrieren, Altdaten anlegen, dann weitermigrieren):
``smoke_session_encryption.py`` und ``smoke_session_applications.py`` migrieren
weiterhin echt.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

FINGERPRINT_SUFFIX = ".migrations"


def migration_fingerprint(project_dir: Path) -> str:
    """Fingerabdruck über alle Migrationsdateien.

    Schützt davor, dass eine Vorlage nach einer neuen Migration stillschweigend
    ein veraltetes Schema unterschiebt. Passt der Fingerabdruck nicht, wird
    normal migriert statt kopiert.
    """
    digest = hashlib.sha256()
    for path in sorted(project_dir.glob("**/migrations/*.py")):
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _usable_template(project_dir: Path) -> Path | None:
    raw = os.environ.get("MANDARI_SMOKE_DB_TEMPLATE", "").strip()
    if not raw:
        return None
    template = Path(raw)
    fingerprint = template.with_suffix(template.suffix + FINGERPRINT_SUFFIX)
    if not template.is_file() or not fingerprint.is_file():
        return None
    if fingerprint.read_text(encoding="utf-8").strip() != migration_fingerprint(project_dir):
        return None
    return template


def prepare_database(project_dir: Path) -> bool:
    """Schema bereitstellen. Liefert ``True``, wenn die Vorlage benutzt wurde."""
    from django.core.management import call_command
    from django.db import connections

    template = _usable_template(project_dir)
    if template is None:
        call_command("migrate", verbosity=0, interactive=False)
        return False

    target = Path(connections["default"].settings_dict["NAME"])
    connections.close_all()
    shutil.copyfile(template, target)
    return True
