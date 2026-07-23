# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration (Issue #39): Bestandsdaten einer Default-Wahlperiode zuordnen.

Für jeden Mandanten mit Sitzungen oder Besetzungen ohne Wahlperiode:
- Existieren bereits Perioden, wird je Objekt die zum Datum passende
  Periode gewählt (Sitzungsdatum bzw. Besetzungsbeginn), sonst die
  aktuelle/jüngste Periode.
- Existiert keine Periode, wird eine Default-Periode "Aktuelle
  Wahlperiode" angelegt und allen Bestandsdaten zugeordnet.

Rückwärts: No-Op (die Zuordnung bleibt bestehen, Felder sind nullable).
"""

from django.db import migrations


def _term_for_date(terms, target_date):
    """Passende Periode zum Datum (offene Grenzen zählen mit), sonst None."""
    if target_date is None:
        return None
    for term in terms:
        if term.start_date and target_date < term.start_date:
            continue
        if term.end_date and target_date > term.end_date:
            continue
        if term.start_date or term.end_date:
            return term
    return None


def _default_term(terms, today):
    """Aktuelle Periode (enthält heute), sonst die jüngste."""
    current = _term_for_date(terms, today)
    if current is not None:
        return current
    dated = [t for t in terms if t.start_date]
    if dated:
        return max(dated, key=lambda t: t.start_date)
    return terms[0]


def assign_default_terms(apps, schema_editor):
    import datetime

    SessionTenant = apps.get_model("session", "SessionTenant")
    SessionLegislativeTerm = apps.get_model("session", "SessionLegislativeTerm")
    SessionMeeting = apps.get_model("session", "SessionMeeting")
    SessionOrganizationMembership = apps.get_model("session", "SessionOrganizationMembership")

    today = datetime.date.today()

    for tenant in SessionTenant.objects.all():
        meetings = SessionMeeting.objects.filter(tenant=tenant, legislative_term__isnull=True)
        memberships = SessionOrganizationMembership.objects.filter(
            organization__tenant=tenant, legislative_term__isnull=True
        )
        if not meetings.exists() and not memberships.exists():
            continue

        terms = list(SessionLegislativeTerm.objects.filter(tenant=tenant))
        if not terms:
            terms = [SessionLegislativeTerm.objects.create(tenant=tenant, name="Aktuelle Wahlperiode")]
        default = _default_term(terms, today)

        for meeting in meetings:
            meeting_date = meeting.start.date() if meeting.start else None
            meeting.legislative_term = _term_for_date(terms, meeting_date) or default
            meeting.save(update_fields=["legislative_term"])

        for membership in memberships:
            membership.legislative_term = _term_for_date(terms, membership.start_date) or default
            membership.save(update_fields=["legislative_term"])


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0014_wahlperioden_zuordnung"),
    ]

    operations = [
        migrations.RunPython(assign_default_terms, migrations.RunPython.noop),
    ]
