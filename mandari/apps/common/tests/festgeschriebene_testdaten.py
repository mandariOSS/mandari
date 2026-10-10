# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfen für modulweite Testdaten, die außerhalb der Testtransaktion festgeschrieben werden.

Fixtures mit ``django_db_blocker.unblock()`` legen ihre Welt einmal je Modul an und räumen sie am
Ende selbst ab. Meldet eine solche Welt Konten an (``Client.force_login``), schreibt das Signal
``user_logged_in`` Einträge ins plattformweite Sicherheitsprotokoll – für Konten ohne Session-Mandant
dort und nicht im Mandantenprotokoll, das mit dem Mandanten verschwindet. Diese Einträge (und der
Kettenkopf) blieben sonst für alle späteren Tests desselben xdist-Workers stehen und ließen Tests
scheitern, die ein leeres Protokoll erwarten (reihenfolgeabhängig, nur in der CI sichtbar).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def sicherheitsprotokoll_zuruecksetzen() -> Iterator[None]:
    """Sicherheitsprotokoll und dessen Kettenkopf nach dem Block auf den Stand davor zurücksetzen.

    Nur innerhalb von ``django_db_blocker.unblock()`` verwenden. Protokolleinträge sind über das
    Modell nicht löschbar (Revisionssicherheit); hier geht es um Testdaten, daher ``_raw_delete``.
    """
    from django.db import connection

    from apps.accounts.models import SecurityAuditLog
    from apps.common import audit_chain
    from apps.common.models import AuditChainHead

    schluessel = audit_chain.SECURITY.scope_key(None)
    vorher = set(SecurityAuditLog.objects.values_list("pk", flat=True))
    kopf = AuditChainHead.objects.filter(scope=schluessel).values().first()
    try:
        yield
    finally:
        SecurityAuditLog.objects.exclude(pk__in=vorher)._raw_delete(connection.alias)
        if kopf is None:
            AuditChainHead.objects.filter(scope=schluessel).delete()
        else:
            AuditChainHead.objects.update_or_create(scope=schluessel, defaults=kopf)
