# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nur zur Prüfung in der CI (Issue #810), wird vor dem Merge wieder entfernt.

Baut den Pool vor jedem der vier Pool-Tests neu auf, wie es ``override_settings(TIME_ZONE=…)``
in einem früheren Test tut, und wiederholt das 20-mal. Mit der alten Messung scheiterte jeder Lauf.
"""

import pytest
from django.db import connection
from django.test import override_settings

from apps.common.tests import test_db_connections as pooltests


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("runde", range(20))
@pytest.mark.parametrize(
    ("klasse", "name"),
    [
        ("TestAbgebrocheneAnfrageGibtVerbindungZurueck", "test_mit_middleware_kommt_die_verbindung_zurueck"),
        (
            "TestAbgebrocheneAnfrageGibtVerbindungZurueck",
            "test_gegenprobe_ohne_middleware_geht_die_verbindung_verloren",
        ),
        ("TestFehlerseitenGebenVerbindungZurueck", "test_mit_dekorator_kommt_die_verbindung_zurueck"),
        (
            "TestFehlerseitenGebenVerbindungZurueck",
            "test_gegenprobe_ohne_dekorator_bleibt_die_verbindung_im_executor_thread",
        ),
    ],
)
def test_nach_neuaufbau_des_pools(runde: int, klasse: str, name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    with override_settings(TIME_ZONE="Europe/Berlin"):
        pass
    getattr(getattr(pooltests, klasse)(), name)()
