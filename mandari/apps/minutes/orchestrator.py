# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Planung der GPU-Rechenknoten.

Die Entscheidung, wann ein GPU-vServer erstellt oder gelöscht wird, ist eine
reine Funktion über den aktuellen Zustand. Der Management-Command
``minutes_orchestrator`` liest den Zustand aus der Datenbank, ruft
:func:`plan` auf und führt die Aktionen gegen die centron-API aus.

Zwei Eigenschaften der centron-Abrechnung bestimmen die Regeln:

1. Abgerechnet wird von der Erstellung bis zur Löschung. Auch ausgeschaltete
   VMs kosten, weil die Ressourcen auf dem Hypervisor reserviert bleiben.
   Einen Knoten nur anzuhalten spart also nichts — er muss gelöscht werden.
2. Abgerechnet wird stundenweise mit einer Stunde Mindestgebühr. Eine
   angebrochene Stunde ist bezahlt; ein leerlaufender Knoten wird deshalb
   erst kurz vor Ablauf seiner laufenden Abrechnungsstunde gelöscht. Kommt
   bis dahin neue Arbeit, läuft er ohne Mehrkosten weiter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class NodeState(StrEnum):
    """Lebenszyklus eines GPU-Knotens."""

    REQUESTED = "requested"  # in mandari angelegt, noch nicht bei centron
    PROVISIONING = "provisioning"  # centron erstellt die VM
    BOOTING = "booting"  # VM läuft, Treiber und Worker werden eingerichtet
    READY = "ready"  # Worker hat sich registriert, nimmt Aufträge an
    BUSY = "busy"  # Worker bearbeitet einen Auftrag
    DELETING = "deleting"  # Löschung bei centron angestoßen
    DELETED = "deleted"  # bei centron gelöscht, Abrechnung beendet
    FAILED = "failed"  # Fehler, muss gelöscht werden


ALIVE_STATES = frozenset(
    {NodeState.REQUESTED, NodeState.PROVISIONING, NodeState.BOOTING, NodeState.READY, NodeState.BUSY}
)
STARTING_STATES = frozenset({NodeState.REQUESTED, NodeState.PROVISIONING, NodeState.BOOTING})


class ActionKind(StrEnum):
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True)
class NodeView:
    """Was der Planer über einen Knoten wissen muss."""

    id: str
    state: str
    created_at: datetime
    ready_at: datetime | None = None
    last_activity_at: datetime | None = None
    has_running_job: bool = False


@dataclass(frozen=True)
class Demand:
    """
    Aktueller Bedarf.

    Attributes:
        queued_jobs: Wartende Verarbeitungsaufträge.
        active_recordings: Laufende Aufzeichnungen. Während einer Sitzung wird
            ein Knoten vorgehalten, damit abgeschlossene TOPs sofort
            verarbeitet werden und der Entwurf zum Sitzungsende vorliegt.
    """

    queued_jobs: int = 0
    active_recordings: int = 0

    @property
    def exists(self) -> bool:
        return self.queued_jobs > 0 or self.active_recordings > 0


@dataclass(frozen=True)
class Limits:
    """Betriebsgrenzen aus den zentralen Einstellungen."""

    max_nodes: int = 1
    jobs_per_node: int = 30
    idle_minutes: int = 10
    provisioning_timeout_minutes: int = 30
    max_lifetime_hours: int = 12
    billing_boundary_minute: int = 55


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    reason: str
    node_id: str | None = None


def minutes_into_billing_hour(created_at: datetime, now: datetime) -> int:
    """Minute innerhalb der laufenden Abrechnungsstunde (0–59)."""
    elapsed = max(0, int((now - created_at).total_seconds() // 60))
    return elapsed % 60


def desired_nodes(demand: Demand, limits: Limits) -> int:
    """Wie viele Knoten der Bedarf rechtfertigt, gedeckelt auf ``max_nodes``."""
    if not demand.exists or limits.max_nodes <= 0:
        return 0
    by_queue = math.ceil(demand.queued_jobs / max(1, limits.jobs_per_node))
    return min(limits.max_nodes, max(1, by_queue))


def _safety_deletions(nodes: list[NodeView], limits: Limits, now: datetime) -> list[Action]:
    """Knoten, die unabhängig vom Bedarf weg müssen."""
    actions: list[Action] = []
    lifetime = timedelta(hours=limits.max_lifetime_hours)
    provisioning_timeout = timedelta(minutes=limits.provisioning_timeout_minutes)
    for node in nodes:
        if node.state == NodeState.FAILED:
            actions.append(Action(ActionKind.DELETE, "Knoten fehlgeschlagen", node.id))
        elif node.state in ALIVE_STATES and now - node.created_at >= lifetime:
            # Schutz gegen vergessene Knoten, die unbemerkt weiterlaufen.
            actions.append(Action(ActionKind.DELETE, "Höchstlaufzeit überschritten", node.id))
        elif node.state in STARTING_STATES and now - node.created_at >= provisioning_timeout:
            actions.append(Action(ActionKind.DELETE, "Bereitstellung nicht rechtzeitig abgeschlossen", node.id))
    return actions


def _is_idle(node: NodeView, limits: Limits, now: datetime) -> bool:
    if node.state != NodeState.READY or node.has_running_job:
        return False
    last = node.last_activity_at or node.ready_at or node.created_at
    return now - last >= timedelta(minutes=limits.idle_minutes)


def plan(nodes: list[NodeView], demand: Demand, limits: Limits, now: datetime) -> list[Action]:
    """
    Aktionen für den aktuellen Zustand bestimmen.

    Reihenfolge der Regeln:

    1. Sicherheitslöschungen (fehlgeschlagen, Höchstlaufzeit, hängende
       Bereitstellung) greifen immer.
    2. Fehlen Knoten für den Bedarf, werden neue angefordert. Knoten, die
       gerade hochfahren, zählen mit — sonst entstünden bei jedem Durchlauf
       weitere, kostenpflichtige VMs.
    3. Überzählige, leerlaufende Knoten werden nur im Löschfenster am Ende
       ihrer Abrechnungsstunde entfernt. Knoten mit laufendem Auftrag werden
       nie wegen Leerlauf gelöscht.
    """
    actions = _safety_deletions(nodes, limits, now)
    removed = {action.node_id for action in actions}
    alive = [node for node in nodes if node.state in ALIVE_STATES and node.id not in removed]

    wanted = desired_nodes(demand, limits)
    for _ in range(wanted - len(alive)):
        actions.append(Action(ActionKind.CREATE, "Bedarf ohne verfügbaren Knoten"))

    surplus = len(alive) - wanted
    if surplus <= 0:
        return actions

    # Die am längsten leerlaufenden Knoten zuerst abbauen.
    candidates = sorted(
        (node for node in alive if _is_idle(node, limits, now)),
        key=lambda node: node.last_activity_at or node.ready_at or node.created_at,
    )
    for node in candidates:
        if surplus <= 0:
            break
        if minutes_into_billing_hour(node.created_at, now) >= limits.billing_boundary_minute:
            actions.append(Action(ActionKind.DELETE, "Leerlauf am Ende der Abrechnungsstunde", node.id))
            surplus -= 1
    return actions
