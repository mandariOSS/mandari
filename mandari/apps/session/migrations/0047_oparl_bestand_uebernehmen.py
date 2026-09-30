# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand bei Einführung der OParl-Freischaltung übernehmen (Issue #319) – ohne Bruch in Produktion.

1. Mandanten, deren OParl-Schnittstelle bisher erreichbar war (aktiv) oder die im Bürgerportal
   veröffentlichen, bleiben freigeschaltet – mit dem Datum ihrer Anlage.
2. Ratsmitglieder (aktive Person mit laufender Mitgliedschaft im Rat als Mitglied, Vorsitz oder
   Stellvertretung) mit E-Mail-Adresse behalten die veröffentlichte Adresse; der Nachweis nennt die
   Übernahme aus dem Bestand.
3. Bei allen übrigen Personen mit E-Mail-Adresse entfällt die Adresse in der Schnittstelle. Ihr
   Änderungszeitpunkt wird gesetzt, damit inkrementelle Abnehmer (``modified_since``, darunter der
   Bürgerportal-Spiegel) die Person neu abrufen und die Adresse entfernen.

Idempotent: Freigeschaltete Mandanten und gekennzeichnete Personen bleiben unberührt; ein erneuter
Lauf setzt höchstens Änderungszeitpunkte neu. Rückwärts ist nichts zu tun (die Spalten entfallen mit
0046). Nicht ``elidable``: Beim Zusammenfassen der Migrationen muss der Schritt erhalten bleiben, sonst
wären Bestände, die erst danach migrieren, gesperrt.
"""

from django.db import migrations
from django.db.models import F, Q
from django.utils import timezone

#: Funktionen im Rat, deren E-Mail-Adresse übernommen wird (nicht: sachkundige Bürger, Beratende, Gäste)
RATSFUNKTIONEN = ("member", "chair", "deputy_chair")
NACHWEIS = "Übernahme aus dem Bestand (Ratsmitglied, Adresse war bereits veröffentlicht)"


def bestand_uebernehmen(apps, schema_editor):
    Tenant = apps.get_model("session", "SessionTenant")
    Person = apps.get_model("session", "SessionPerson")
    Membership = apps.get_model("session", "SessionOrganizationMembership")

    Tenant.objects.filter(oparl_public_since__isnull=True).filter(Q(is_active=True) | Q(insight_publish=True)).update(
        oparl_public_since=F("created_at")
    )

    heute = timezone.localdate()
    ratsmitglieder = (
        Membership.objects.filter(organization__organization_type="council", role__in=RATSFUNKTIONEN)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=heute))
        .values("person_id")
    )
    (
        Person.objects.filter(pk__in=ratsmitglieder, is_active=True, contact_publish=False)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=heute))
        .exclude(email="")
        .update(contact_publish=True, contact_consent_date=heute, contact_consent_evidence=NACHWEIS)
    )

    Person.objects.filter(contact_publish=False).exclude(email="").update(updated_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0046_oparl_freischaltung"),
    ]

    operations = [
        migrations.RunPython(bestand_uebernehmen, migrations.RunPython.noop),
    ]
