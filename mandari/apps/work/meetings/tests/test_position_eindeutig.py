# SPDX-License-Identifier: AGPL-3.0-or-later
"""Eine Position je Organisation und TOP (#926): Migration führt Dubletten verlustfrei zusammen."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.work.meetings.models import AgendaItemPosition
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

VORHER = [("work", "0074_sitzungsreihe_automatik")]
NACHHER = [("work", "0075_position_eindeutig")]
FELDER = ("position", "is_final", "reasoning_encrypted", "outcome", "set_by_id", "preparation_id", "created_at")


def _tops(org: Any, anzahl: int) -> list[OParlAgendaItem]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now()
    )
    return [
        OParlAgendaItem.objects.create(
            external_id=f"https://ris.example.org/agenda/{nr}", meeting=meeting, number=str(nr), name=f"TOP {nr}"
        )
        for nr in range(1, anzahl + 1)
    ]


def _stand(model: Any, pk: Any) -> dict[str, Any]:
    zeile: dict[str, Any] = model.objects.filter(pk=pk).values(*FELDER, "updated_at").get()
    if zeile["reasoning_encrypted"] is not None:
        zeile["reasoning_encrypted"] = bytes(zeile["reasoning_encrypted"])
    return zeile


def _anleger(position: Any, org: Any) -> Any:
    """Zeilen im historischen Modell anlegen, mit festen Zeitpunkten (Stunden nach t0)."""
    t0 = timezone.now() - timedelta(days=10)

    def anlegen(top: Any, angelegt: int, geaendert: int, organisation: Any = org, **werte: Any) -> Any:
        zeile = position.objects.create(
            organization_id=getattr(organisation, "pk", None), agenda_item_id=top.pk, **werte
        )
        position.objects.filter(pk=zeile.pk).update(
            created_at=t0 + timedelta(hours=angelegt), updated_at=t0 + timedelta(hours=geaendert)
        )
        return zeile.pk

    return anlegen


@pytest.mark.django_db(transaction=True)
def test_migration_fuehrt_dubletten_verlustfrei_zusammen(org: Any, make_member: Any) -> None:
    erste, zweite = make_member(org, email="erste@example.org"), make_member(org, email="zweite@example.org")
    top_doppelt, top_einzeln, top_altlast = _tops(org, 3)

    executor = MigrationExecutor(connection)
    executor.migrate(VORHER)
    try:
        position = executor.loader.project_state(VORHER).apps.get_model("work", "AgendaItemPosition")
        anlegen = _anleger(position, org)

        # Drei Zeilen zum selben TOP, die sich ergänzen: die jüngste ist fast leer, die älteren tragen die Inhalte
        alt = anlegen(top_doppelt, 0, 1, position="for", is_final=True, reasoning_encrypted=b"alt", set_by_id=erste.pk)
        mittel = anlegen(top_doppelt, 2, 3, set_by_id=zweite.pk)
        neu = anlegen(top_doppelt, 4, 5, outcome="rejected")
        einzeln = anlegen(top_einzeln, 0, 1, position="abstain", reasoning_encrypted=b"einzeln", set_by_id=erste.pk)
        altlast = [anlegen(top_altlast, 0, 1, organisation=None, position=wert) for wert in ("for", "against")]
        vorher = {pk: _stand(position, pk) for pk in (alt, mittel, neu, einzeln, *altlast)}

        executor = MigrationExecutor(connection)
        executor.migrate(NACHHER)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    assert list(AgendaItemPosition.objects.filter(agenda_item=top_doppelt).values_list("pk", flat=True)) == [neu]
    zusammen = _stand(AgendaItemPosition, neu)
    assert zusammen == {
        # Leere Felder der erhaltenen Zeile kommen aus der jüngsten Zeile mit Wert, „Endgültig“ mit der Position
        "position": vorher[alt]["position"],
        "is_final": vorher[alt]["is_final"],
        "reasoning_encrypted": vorher[alt]["reasoning_encrypted"],
        # Eigene Werte der erhaltenen Zeile bleiben
        "outcome": vorher[neu]["outcome"],
        "set_by_id": vorher[mittel]["set_by_id"],
        "preparation_id": None,
        "created_at": vorher[alt]["created_at"],
        "updated_at": vorher[neu]["updated_at"],
    }
    # Unbeteiligte Zeilen und Altlast ohne Organisation bleiben Feld für Feld unverändert
    for pk in (einzeln, *altlast):
        assert _stand(AgendaItemPosition, pk) == vorher[pk]

    # Danach verhindert die Datenbank neue Dubletten
    with pytest.raises(IntegrityError), transaction.atomic():
        AgendaItemPosition.objects.create(organization=org, agenda_item=top_doppelt)


@pytest.mark.parametrize(
    ("eins", "zwei", "feld"),
    [
        ({"outcome": "accepted"}, {"outcome": "rejected"}, "Ergebnis"),
        ({"position": "for"}, {"position": "against"}, "Position"),
        ({"reasoning_encrypted": b"eins"}, {"reasoning_encrypted": b"zwei"}, "Begründung"),
        # „Endgültig“ ohne Position darf nicht an die Position einer anderen Zeile wandern
        ({"is_final": True}, {"position": "for"}, "Endgültig"),
        ({"position": "for", "is_final": True}, {"position": "against"}, "Position, Endgültig"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_migration_bricht_bei_widerspruch_ab_und_aendert_nichts(
    org: Any, eins: dict[str, Any], zwei: dict[str, Any], feld: str
) -> None:
    top_widerspruch, top_ergaenzend = _tops(org, 2)

    executor = MigrationExecutor(connection)
    executor.migrate(VORHER)
    position = executor.loader.project_state(VORHER).apps.get_model("work", "AgendaItemPosition")
    try:
        anlegen = _anleger(position, org)
        zeilen = [
            anlegen(top_widerspruch, 0, 1, **eins),
            anlegen(top_widerspruch, 2, 3, **zwei),
            # Eine Gruppe, die sich ergänzt, wird wegen der anderen ebenfalls nicht angefasst
            anlegen(top_ergaenzend, 0, 1, position="for"),
            anlegen(top_ergaenzend, 2, 3, outcome="accepted"),
        ]
        vorher = {pk: _stand(position, pk) for pk in zeilen}

        with pytest.raises(
            RuntimeError, match=f"(?s)nichts geändert.*TOP {top_widerspruch.pk}: 2 Zeilen, widersprüchlich in {feld}$"
        ):
            MigrationExecutor(connection).migrate(NACHHER)

        assert {pk: _stand(position, pk) for pk in zeilen} == vorher
        assert position.objects.count() == len(zeilen)
    finally:
        position.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
