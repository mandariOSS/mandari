# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orchestrator-Durchlauf gegen Datenbank und simulierten Anbieter."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.minutes.models_compute import ComputeSettings, GpuNode
from apps.minutes.node_service import create_node, delete_node, gpu_available, run_once
from apps.minutes.orchestrator import NodeState
from apps.minutes.provisioning.centron import CentronError, ServerSpec


class FakeProvider:
    def __init__(self, *, gpus: list[dict[str, Any]] | None = None, fail_create: bool = False) -> None:
        self.gpus = gpus if gpus is not None else [{"model": "Quadro RTX 6000", "available": 2}]
        self.fail_create = fail_create
        self.created: list[ServerSpec] = []
        self.deleted: list[str] = []
        self.delete_error: CentronError | None = None

    def list_gpus(self) -> list[dict[str, Any]]:
        return self.gpus

    def create_server(self, spec: ServerSpec) -> dict[str, Any]:
        if self.fail_create:
            raise CentronError("POST /ccloud/servers fehlgeschlagen", 500)
        self.created.append(spec)
        return {"id": 1}

    def server_state(self, hostname: str) -> str:
        return "Running"

    def delete_server(self, hostname: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(hostname)


def configured() -> ComputeSettings:
    config = ComputeSettings.load(use_cache=False)
    config.client_id = "mandari"
    config.set_client_secret("geheim")
    config.project_id = 42
    config.pool = "GPU Pool"
    config.image = "ubuntu-24.04"
    config.mandari_url = "https://mandari.example.de"
    config.save()
    return config


def test_verfuegbarkeit_wird_tolerant_erkannt() -> None:
    assert gpu_available([{"name": "NVIDIA Quadro RTX 6000", "available": 1}], "Quadro RTX 6000")
    assert gpu_available([{"name": "Quadro RTX 6000"}], "quadro rtx 6000")
    assert not gpu_available([{"name": "Quadro RTX 6000", "available": 0}], "Quadro RTX 6000")
    assert not gpu_available([{"name": "RTX A4000", "available": 5}], "Quadro RTX 6000")


@pytest.mark.django_db
def test_secret_liegt_nicht_im_klartext() -> None:
    config = configured()
    assert config.get_client_secret() == "geheim"
    assert b"geheim" not in bytes(config.client_secret_encrypted or b"")


@pytest.mark.django_db
def test_einschalten_ohne_zugangsdaten_wird_abgelehnt() -> None:
    config = ComputeSettings.load(use_cache=False)
    config.enabled = True
    with pytest.raises(ValidationError):
        config.clean()


@pytest.mark.django_db
def test_es_gibt_nur_eine_zentrale_konfiguration() -> None:
    ComputeSettings(client_id="a").save()
    ComputeSettings(client_id="b").save()
    assert ComputeSettings.objects.count() == 1
    assert ComputeSettings.load(use_cache=False).client_id == "b"


@pytest.mark.django_db
def test_knoten_wird_mit_token_hash_und_cloud_init_erstellt() -> None:
    provider = FakeProvider()
    node = create_node(provider, configured(), timezone.now())
    assert node is not None and node.state == NodeState.PROVISIONING
    spec = provider.created[0]
    assert spec.pool == "GPU Pool" and spec.hostname == node.hostname
    token_line = next(line for line in spec.user_data.splitlines() if "MANDARI_NODE_TOKEN=" in line)
    token = token_line.split("MANDARI_NODE_TOKEN=", 1)[1].split("\\n", 1)[0]
    assert token not in node.token_hash
    assert node.token_matches(token)
    assert "geheim" not in spec.user_data


@pytest.mark.django_db
def test_ohne_verfuegbare_gpu_wird_keine_vm_bezahlt() -> None:
    provider = FakeProvider(gpus=[{"model": "Quadro RTX 6000", "available": 0}])
    assert create_node(provider, configured(), timezone.now()) is None
    assert provider.created == []
    assert GpuNode.objects.count() == 0


@pytest.mark.django_db
def test_fehlgeschlagene_erstellung_wird_als_fehler_markiert() -> None:
    node = create_node(FakeProvider(fail_create=True), configured(), timezone.now())
    assert node is not None and node.state == NodeState.FAILED


@pytest.mark.django_db
def test_bereits_geloeschte_vm_gilt_als_geloescht() -> None:
    provider = FakeProvider()
    provider.delete_error = CentronError("nicht gefunden", 404)
    node = GpuNode.objects.create(hostname="mandari-gpu-x", gpu_model="Quadro RTX 6000", state=NodeState.READY)
    delete_node(provider, node, "Test")
    node.refresh_from_db()
    assert node.state == NodeState.DELETED and node.deleted_at is not None


@pytest.mark.django_db
def test_voruebergehender_loeschfehler_wird_wiederholt() -> None:
    provider = FakeProvider()
    provider.delete_error = CentronError("Wartung", 503)
    node = GpuNode.objects.create(hostname="mandari-gpu-y", gpu_model="Quadro RTX 6000", state=NodeState.READY)
    delete_node(provider, node, "Test")
    node.refresh_from_db()
    assert node.state == NodeState.DELETING

    provider.delete_error = None
    run_once(provider, configured())
    node.refresh_from_db()
    assert node.state == NodeState.DELETED


@pytest.mark.django_db
def test_laufende_vm_geht_in_die_einrichtung() -> None:
    node = GpuNode.objects.create(hostname="mandari-gpu-z", gpu_model="Quadro RTX 6000", state=NodeState.PROVISIONING)
    run_once(FakeProvider(), configured())
    node.refresh_from_db()
    assert node.state == NodeState.BOOTING


@pytest.mark.django_db
def test_ohne_bedarf_entsteht_kein_knoten() -> None:
    provider = FakeProvider()
    run_once(provider, configured(), timezone.now() + timedelta(minutes=1))
    assert provider.created == []
