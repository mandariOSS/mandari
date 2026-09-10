# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Brücke zwischen Planer, Datenbank und centron-API.

:func:`run_once` ist ein vollständiger Durchlauf des Orchestrators: Zustand
lesen, planen, Aktionen ausführen. Er ist idempotent — ein abgebrochener
Durchlauf wird beim nächsten Mal einfach fortgesetzt, weil der Zustand in der
Datenbank steht und nicht im Prozess.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any, Protocol

from django.utils import timezone

from .models import JobStatus, RecordingStatus, TranscriptionJob
from .models_compute import ComputeSettings, GpuNode, hash_node_token
from .orchestrator import Action, ActionKind, Demand, NodeState, NodeView, plan
from .provisioning.centron import CentronError, ServerSpec
from .provisioning.cloud_init import NodeBootstrap, render_user_data

logger = logging.getLogger(__name__)

AVAILABILITY_KEYS = ("available", "availability", "free", "count", "stock")
RUNNING_PROVIDER_STATES = frozenset({"running", "started", "on"})


class ProviderClient(Protocol):
    """Was der Orchestrator von einem Anbieter-Client braucht."""

    def list_gpus(self) -> list[dict[str, Any]]: ...

    def create_server(self, spec: ServerSpec) -> dict[str, Any]: ...

    def server_state(self, hostname: str) -> str: ...

    def delete_server(self, hostname: str) -> None: ...


# -- Zustand lesen --------------------------------------------------------------


def current_demand() -> Demand:
    from .models import Recording

    return Demand(
        queued_jobs=TranscriptionJob.objects.filter(status=JobStatus.QUEUED).count(),
        active_recordings=Recording.objects.filter(
            status__in=[RecordingStatus.RECORDING, RecordingStatus.PAUSED]
        ).count(),
    )


def node_views() -> list[NodeView]:
    busy_node_ids = set(
        TranscriptionJob.objects.filter(status=JobStatus.RUNNING, node__isnull=False).values_list("node_id", flat=True)
    )
    return [
        NodeView(
            id=str(node.pk),
            state=node.state,
            created_at=node.created_at,
            ready_at=node.ready_at,
            last_activity_at=node.last_activity_at,
            has_running_job=node.pk in busy_node_ids,
        )
        for node in GpuNode.objects.exclude(state=NodeState.DELETED)
    ]


# -- Aktionen -------------------------------------------------------------------


def gpu_available(gpus: list[dict[str, Any]], model: str) -> bool:
    """
    Ist das gewünschte GPU-Modell laut Anbieter verfügbar?

    Das Antwortformat von ``GET /ccloud/servers/gpus`` ist nicht öffentlich
    beschrieben. Geprüft wird deshalb tolerant: Ein Eintrag passt, wenn einer
    seiner Textwerte das Modell nennt. Enthält er eine Mengenangabe, muss sie
    größer null sein.
    """
    wanted = model.casefold()
    for entry in gpus:
        names = [value for value in entry.values() if isinstance(value, str)]
        if not any(wanted in name.casefold() for name in names):
            continue
        amounts = [entry[key] for key in AVAILABILITY_KEYS if isinstance(entry.get(key), int | float | bool)]
        if not amounts or any(amount > 0 for amount in amounts):
            return True
    return False


def new_hostname(now: datetime) -> str:
    return f"mandari-gpu-{now:%Y%m%d-%H%M}-{secrets.token_hex(2)}"


def create_node(client: ProviderClient, config: ComputeSettings, now: datetime) -> GpuNode | None:
    if not gpu_available(client.list_gpus(), config.gpu_model):
        logger.warning("GPU %s derzeit nicht verfügbar; kein Knoten erstellt", config.gpu_model)
        return None

    token = secrets.token_urlsafe(32)
    node = GpuNode.objects.create(
        hostname=new_hostname(now),
        gpu_model=config.gpu_model,
        token_hash=hash_node_token(token),
        state=NodeState.REQUESTED,
    )
    user_data = render_user_data(
        NodeBootstrap(
            hostname=node.hostname,
            mandari_url=config.effective_mandari_url(),
            node_token=token,
            worker_image=config.worker_image,
            prepared_image=config.prepared_image,
        )
    )
    spec = ServerSpec(
        project_id=config.project_id or 0,
        pool=config.pool,
        hostname=node.hostname,
        image=config.image,
        cores=config.cores,
        memory_gb=config.memory_gb,
        disk_gb=config.disk_gb,
        user_data=user_data,
        ssh_keys=(config.ssh_public_key.strip(),) if config.ssh_public_key.strip() else (),
    )
    try:
        client.create_server(spec)
    except CentronError as exc:
        node.state = NodeState.FAILED
        node.error = str(exc)
        node.save(update_fields=["state", "error"])
        logger.error("Knoten %s konnte nicht erstellt werden: %s", node.hostname, exc)
        return node

    node.state = NodeState.PROVISIONING
    node.save(update_fields=["state"])
    return node


def delete_node(client: ProviderClient, node: GpuNode, reason: str) -> None:
    node.state = NodeState.DELETING
    node.error = reason if node.error == "" else node.error
    node.save(update_fields=["state", "error"])
    try:
        client.delete_server(node.hostname)
    except CentronError as exc:
        if exc.status_code != 404:
            # Bleibt in DELETING und wird im nächsten Durchlauf erneut gelöscht.
            logger.error("Löschen von %s fehlgeschlagen: %s", node.hostname, exc)
            return
    node.state = NodeState.DELETED
    node.deleted_at = timezone.now()
    node.save(update_fields=["state", "deleted_at"])


def advance_provisioning(client: ProviderClient) -> None:
    """Knoten, deren VM läuft, in die Einrichtung überführen."""
    for node in GpuNode.objects.filter(state=NodeState.PROVISIONING):
        try:
            state = client.server_state(node.hostname)
        except CentronError as exc:
            logger.warning("Zustand von %s nicht abrufbar: %s", node.hostname, exc)
            continue
        if state.casefold() in RUNNING_PROVIDER_STATES:
            node.state = NodeState.BOOTING
            node.save(update_fields=["state"])


def retry_pending_deletions(client: ProviderClient) -> None:
    for node in GpuNode.objects.filter(state=NodeState.DELETING):
        delete_node(client, node, node.error or "Löschung wiederholt")


def run_once(client: ProviderClient, config: ComputeSettings, now: datetime | None = None) -> list[Action]:
    """Ein vollständiger Orchestrator-Durchlauf."""
    now = now or timezone.now()
    retry_pending_deletions(client)
    advance_provisioning(client)

    actions = plan(node_views(), current_demand(), config.limits(), now)
    for action in actions:
        if action.kind == ActionKind.CREATE:
            create_node(client, config, now)
        elif action.kind == ActionKind.DELETE and action.node_id is not None:
            node = GpuNode.objects.filter(pk=action.node_id).first()
            if node is not None:
                delete_node(client, node, action.reason)
    return actions
