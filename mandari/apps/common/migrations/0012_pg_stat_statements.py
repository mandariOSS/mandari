# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erweiterung ``pg_stat_statements`` anlegen, wenn das geht (Issue #1024).

Die Erweiterung zählt Aufrufe und Zeit je SQL-Abfrage; gesammelt wird nur, wenn PostgreSQL die Bibliothek beim
Start lädt (``shared_preload_libraries``, in ``docker-compose.yml`` und im Helm-Chart voreingestellt). Auswertung:
DEPLOYMENT.md, „Teuerste Abfragen“.

Warum eine Migration und kein Skript in ``docker-entrypoint-initdb.d``: Ein solches Skript läuft nur beim ersten
Start mit leerem Datenverzeichnis. Bestehende Installationen, das Helm-Chart und verwaltete Datenbanken bekämen die
Erweiterung so nie. Die Migration läuft überall, wo mandari migriert, und nur in der Datenbank von mandari.

Sie scheitert nie: Ohne PostgreSQL tut sie nichts. Fehlt die Erweiterung auf dem Server oder darf die
Datenbankrolle sie nicht anlegen (``pg_stat_statements`` ist nicht „trusted“, sie braucht einen Superuser oder die
Admin-Rolle eines verwalteten Dienstes), bleibt es bei einer Warnung im Protokoll. Der Versuch läuft in einem
Sicherungspunkt, damit ein Fehler die Transaktion der Migration nicht abbricht. Ohne geladene Bibliothek lässt sich
die Erweiterung anlegen, nur ihre Abfrage meldet dann einen Fehler; die Anwendung fragt sie nie ab.

Rückwärts bleibt die Erweiterung bestehen: Andere Werkzeuge (Überwachung, Betrieb) können sie nutzen.
"""

import logging

from django.db import DatabaseError, migrations, transaction

logger = logging.getLogger(__name__)

ERWEITERUNG = "pg_stat_statements"


def erweiterung_anlegen(apps, schema_editor):
    verbindung = schema_editor.connection
    if verbindung.vendor != "postgresql":
        return
    with verbindung.cursor() as cursor:
        cursor.execute(
            "SELECT installed_version IS NOT NULL FROM pg_available_extensions WHERE name = %s", [ERWEITERUNG]
        )
        zeile = cursor.fetchone()
    if zeile is None:
        logger.warning(
            "%s ist auf diesem PostgreSQL-Server nicht verfügbar; ohne die Erweiterung fehlt nur die Statistik "
            "je Abfrage (DEPLOYMENT.md, „Teuerste Abfragen“)",
            ERWEITERUNG,
        )
        return
    if zeile[0]:
        return
    try:
        with transaction.atomic(using=verbindung.alias), verbindung.cursor() as cursor:
            cursor.execute(f"CREATE EXTENSION IF NOT EXISTS {ERWEITERUNG}")
    except DatabaseError as fehler:
        # Nur die Art des Fehlers: fehlende Rechte (InsufficientPrivilege) oder eine Bibliothek, die fehlt
        logger.warning(
            "%s nicht angelegt (%s); ein Superuser bzw. die Admin-Rolle der verwalteten Datenbank kann sie mit "
            "CREATE EXTENSION %s anlegen (DEPLOYMENT.md, „Teuerste Abfragen“)",
            ERWEITERUNG,
            type(getattr(fehler, "__cause__", None) or fehler).__name__,
            ERWEITERUNG,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0011_ki_anbieter_europa"),
    ]

    operations = [
        migrations.RunPython(erweiterung_anlegen, migrations.RunPython.noop),
    ]
