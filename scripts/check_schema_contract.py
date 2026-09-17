# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Gate: Schema-Contract zwischen Django-Modellen und den SQLAlchemy-Tabellen des Ingestors.

    python scripts/check_schema_contract.py            # Exit 1 bei Fehlern
    python scripts/check_schema_contract.py --strict   # auch Warnungen sind Fehler

Braucht die Django-Abhängigkeiten (mandari/requirements.lock) und ``sqlalchemy``; der Ingestor
selbst muss nicht installiert sein, nur sein Quellbaum (``ingestor/``).
Prüft außerdem, dass der Ingestor nur in die Elasticsearch-Indizes schreibt, die Django anlegt,
und selbst keine Mappings definiert (Issue #215).

Hintergrund: docs/adr/20260909-schema-contract-django-ingestor.md (Issue #161).
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT / "mandari"
INGESTOR_DIR = ROOT / "ingestor"


def main() -> int:
    # Windows-Konsole (cp1252) soll Umlaute und Pfeile nicht zum Absturz bringen
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.path.insert(0, str(PROJECT_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mandari.settings_test")
    os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
    os.environ.setdefault("ENCRYPTION_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())

    import django

    django.setup()

    from insight_core.schema_contract import (
        compare,
        django_schema,
        elasticsearch_contract,
        format_report,
        sqlalchemy_schema,
    )

    report = compare(django_schema(), sqlalchemy_schema(INGESTOR_DIR))
    print(format_report(report))

    # Suchindizes: Django legt an, der Ingestor schreibt nur (Issue #215)
    from insight_search.management.commands.setup_elasticsearch import Command as SetupElasticsearch

    django_indices = set(SetupElasticsearch()._get_index_configs([]).keys())
    es_probleme = elasticsearch_contract(INGESTOR_DIR, django_indices)
    if es_probleme:
        print("\nElasticsearch-Indizes:")
        for problem in es_probleme:
            print(f"  ✗ {problem}")
    else:
        print(f"\nElasticsearch-Indizes: OK ({', '.join(sorted(django_indices))}; nur Django legt an)")

    strict = "--strict" in sys.argv
    if not report.ok or es_probleme or (strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
