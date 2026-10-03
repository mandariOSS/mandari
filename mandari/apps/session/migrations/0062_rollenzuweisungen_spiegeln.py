# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollen des Bestands als Zuweisungen spiegeln (Issue #772) – ohne Verhaltensänderung.

1. **Mitlöschen in PostgreSQL:** Die Fremdschlüssel der Zuweisung bekommen ``ON DELETE CASCADE`` (Mandant, Konto,
   Rolle) bzw. ``ON DELETE SET NULL`` (angelegt von, aufgehoben von). Ein älteres Image kennt die Tabelle nicht;
   ohne diese Regeln schlüge dort das Löschen eines Kontos, einer Rolle oder eines Mandanten fehl. Das neue Image
   löscht wie gewohnt über Django mit. Ändert eine spätere Migration diese Fremdschlüssel, muss sie die Regeln neu
   setzen (``test_fremdschluessel_loeschen_in_postgresql_selbst_mit`` prüft das in der CI). Andere Datenbanken
   (SQLite in Tests) brauchen das nicht. Djangos ``DB_CASCADE``/``DB_SET_NULL`` scheiden aus: Konto, Rolle und
   Mandant verweisen selbst mit Python-Löschregeln weiter, gemischte Ketten verbietet Django (``fields.E323``).
2. **Spiegel:** Je Paar aus ``SessionUser.roles`` eine mandantenweite, unbefristete Zuweisung mit Quelle
   „Migration“. Die bisherigen Rechte ändern sich nicht: ``SessionUser.roles`` bleibt maßgeblich.

Idempotent: Ein zweiter Lauf legt nichts an. Denselben Abgleich macht nach jedem ``migrate``
``rechte.zuweisungen.abgleichen`` (Änderungen eines älteren Images); hier steht eine eingefrorene Kopie, damit die
Migration nicht vom späteren Stand des Dienstes abhängt. Rückwärts ist nichts zu tun (die Tabelle entfällt mit
0061). Nicht ``elidable``: Beim Zusammenfassen der Migrationen muss der Schritt erhalten bleiben.
"""

from django.db import migrations

TABELLE = "session_role_assignments"

#: Spalte → Regel beim Löschen des Ziels
MITLOESCHEN = {
    "tenant_id": "CASCADE",
    "user_id": "CASCADE",
    "role_id": "CASCADE",
    "created_by_id": "SET NULL",
    "revoked_by_id": "SET NULL",
}

VERMERK = "Übernommen aus den Rollen des Kontos"


def mitloeschen(apps, schema_editor):
    verbindung = schema_editor.connection
    if verbindung.vendor != "postgresql":
        return
    with verbindung.cursor() as cursor:
        regeln = verbindung.introspection.get_constraints(cursor, TABELLE)
    qn = schema_editor.quote_name
    for name, info in sorted(regeln.items()):
        ziel = info.get("foreign_key")
        if not ziel or len(info["columns"]) != 1 or info["columns"][0] not in MITLOESCHEN:
            continue
        spalte = info["columns"][0]
        schema_editor.execute(
            f"ALTER TABLE {qn(TABELLE)} DROP CONSTRAINT {qn(name)}, "
            f"ADD CONSTRAINT {qn(name)} FOREIGN KEY ({qn(spalte)}) REFERENCES {qn(ziel[0])} ({qn(ziel[1])}) "
            f"ON DELETE {MITLOESCHEN[spalte]} DEFERRABLE INITIALLY DEFERRED"
        )


def spiegeln(apps, schema_editor):
    Konto = apps.get_model("session", "SessionUser")
    Zuweisung = apps.get_model("session", "SessionRoleAssignment")

    vorhanden = set(
        Zuweisung.objects.filter(
            revoked_at__isnull=True, scope_type="mandant", valid_from__isnull=True, valid_until__isnull=True
        ).values_list("user_id", "role_id")
    )
    mandanten = dict(Konto.objects.values_list("pk", "tenant_id"))
    paare = Konto.roles.through.objects.values_list("sessionuser_id", "sessionrole_id")
    Zuweisung.objects.bulk_create(
        [
            Zuweisung(tenant_id=mandanten[konto], user_id=konto, role_id=rolle, source="migration", note=VERMERK)
            for konto, rolle in paare.iterator()
            if (konto, rolle) not in vorhanden and konto in mandanten
        ],
        batch_size=1000,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0061_rollenzuweisungen"),
    ]

    operations = [
        migrations.RunPython(mitloeschen, migrations.RunPython.noop),
        migrations.RunPython(spiegeln, migrations.RunPython.noop),
    ]
