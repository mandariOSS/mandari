# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumente: Zustand des gemeinsamen Editors (Yjs) verschlüsselt speichern.

Der Inhalt eines Dokuments ist mit dem Organisationsschlüssel verschlüsselt, der
Yjs-Zustand mit demselben Inhalt lag bisher im Klartext daneben. Diese Migration

1. benennt das Feld im Modell in ``yjs_document_legacy`` um – nur im Modellzustand; die
   Spalte ``yjs_document`` bleibt, damit eine ältere Version nach einem Rückfall weiterläuft,
2. legt ``yjs_document_encrypted`` an (AES-256-GCM mit dem Organisationsschlüssel),
3. verschlüsselt vorhandene Zustände und ersetzt sie in der alten Spalte durch ``b""``.
   Die leere Markierung zeigt ``Motion.get_yjs_state()``, dass der verschlüsselte Stand
   gilt; eine ältere Version liest sie als „kein Zustand“ und baut ihn aus dem Inhalt auf.

Schritt 3 ist wiederholbar: Er fasst nur Zeilen mit Klartext an. Gibt es welche, aber keinen
gültigen Hauptschlüssel, bricht die Migration ab, bevor sich etwas ändert. Geschrieben wird
nur, wenn die Zeile noch den gelesenen Stand hat; schreibt eine laufende ältere Version
währenddessen, bleibt ihre (jüngere) Fassung stehen. Seitenweise, weil Zustände mit
eingebetteten Bildern mehrere MB groß sein können.

Rückwärts entfällt der verschlüsselte Zustand. Der Editor baut ihn beim nächsten Öffnen aus
dem gespeicherten Inhalt neu auf; es geht kein Inhalt verloren.
"""

from django.db import migrations, models

PAGE_SIZE = 50


def _require_master_key() -> None:
    from apps.common.encryption import get_master_key

    try:
        get_master_key()
    except ValueError:
        raise RuntimeError(
            "Dokumente enthalten einen unverschlüsselten Editor-Zustand, aber ENCRYPTION_MASTER_KEY fehlt "
            "oder ist ungültig. Es wurde nichts geändert; bitte den Schlüssel setzen und die Migration wiederholen."
        ) from None


def encrypt_editor_states(apps, schema_editor):
    from apps.common.encryption import TenantEncryption

    Motion = apps.get_model("work", "Motion")
    Organization = apps.get_model("tenants", "Organization")

    plaintext = (
        Motion.objects.filter(yjs_document_legacy__isnull=False).exclude(yjs_document_legacy=b"").order_by("pk")
    )
    if not plaintext.exists():
        return
    _require_master_key()

    encryptions = {}  # Organisation → TenantEncryption (Schlüssel nur einmal auspacken)
    last_pk = None
    while True:
        page = plaintext if last_pk is None else plaintext.filter(pk__gt=last_pk)
        rows = list(page.values_list("pk", "organization_id", "yjs_document_legacy")[:PAGE_SIZE])
        if not rows:
            return
        for pk, organization_id, state in rows:
            encryption = encryptions.get(organization_id)
            if encryption is None:
                encryption = TenantEncryption(Organization.objects.get(pk=organization_id))
                encryptions[organization_id] = encryption
            Motion.objects.filter(pk=pk, yjs_document_legacy=state).update(
                yjs_document_encrypted=encryption.encrypt_bytes(bytes(state)),
                yjs_document_legacy=b"",
            )
        last_pk = rows[-1][0]


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0056_anhaenge_zufallspfade"),
        ("tenants", "0022_vorheriger_mandantenschluessel"),
    ]

    operations = [
        # 1) Klartextfeld umbenennen, ohne die Spalte anzufassen
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name="motion",
                    old_name="yjs_document",
                    new_name="yjs_document_legacy",
                ),
                migrations.AlterField(
                    model_name="motion",
                    name="yjs_document_legacy",
                    field=models.BinaryField(
                        blank=True,
                        db_column="yjs_document",
                        null=True,
                        verbose_name="Editor-Zustand (Altbestand)",
                    ),
                ),
            ],
            database_operations=[],
        ),
        # 2) Verschlüsselter Zustand
        migrations.AddField(
            model_name="motion",
            name="yjs_document_encrypted",
            field=models.BinaryField(
                blank=True,
                help_text="Yjs-Zustand des gemeinsamen Editors, AES-256-GCM mit dem Organisationsschlüssel",
                null=True,
                verbose_name="Editor-Zustand (verschlüsselt)",
            ),
        ),
        # 3) Bestand verschlüsseln
        migrations.RunPython(encrypt_editor_states, migrations.RunPython.noop),
    ]
