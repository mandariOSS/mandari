# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adressen der Konten klein schreiben (Issue #1397).

Die Anmeldung und jede Suche nach einem Konto beachten die Groß-/Kleinschreibung nicht mehr; gespeichert wird
nur noch die klein geschriebene Adresse. Diese Migration stellt den Bestand um. Ein Konto bleibt unverändert,
wenn ein anderes die klein geschriebene Adresse schon trägt oder zwei Konten auf dieselbe Adresse fielen: Solche
Paare führt niemand automatisch zusammen. Die Anmeldung findet sie weiter in ihrer eigenen Schreibweise; die
Zahl der übersprungenen Konten steht in der Ausgabe von ``migrate``.

Rückwärts geschieht nichts (die alte Schreibweise ist nicht gespeichert). Ältere Images kommen mit den klein
geschriebenen Adressen zurecht.
"""

from collections import Counter
from typing import Any

from django.db import migrations


def lowercase_emails(apps: Any, schema_editor: Any) -> None:
    User = apps.get_model("accounts", "User")
    konten = list(User.objects.values_list("pk", "email"))
    ziele = Counter(email.strip().lower() for _pk, email in konten)
    skipped = 0
    for pk, email in konten:
        wanted = email.strip().lower()
        if wanted == email:
            continue
        if ziele[wanted] > 1:
            skipped += 1
            continue
        User.objects.filter(pk=pk).update(email=wanted)
    if skipped:
        print(
            f"\n  accounts.0012: {skipped} Konten behalten ihre Schreibweise, weil ein anderes Konto dieselbe "
            "Adresse in anderer Schreibweise trägt. Bitte von Hand klären (Django-Admin, Benutzer)."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_oidc_bindung_schluessel"),
    ]

    operations = [
        migrations.RunPython(lowercase_emails, migrations.RunPython.noop),
    ]
