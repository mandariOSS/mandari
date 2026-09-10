# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-Formular der zentralen Rechenknoten-Konfiguration."""

from __future__ import annotations

from typing import Any

import pytest

from apps.minutes.admin import ComputeSettingsForm
from apps.minutes.models_compute import ComputeSettings


def form_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": "on",
        "api_base_url": "https://ccenter.centron.de/api/v1",
        "client_id": "mandari",
        "client_secret": "",
        "scope": "",
        "project_id": "42",
        "pool": "GPU Pool",
        "image": "ubuntu-24.04",
        "gpu_model": "Quadro RTX 6000",
        "cores": "8",
        "memory_gb": "32",
        "disk_gb": "120",
        "ssh_public_key": "",
        "worker_image": "ghcr.io/mandarioss/transcriber:latest",
        "mandari_url": "",
        "s3_endpoint": "",
        "s3_bucket": "",
        "s3_access_key": "",
        "s3_secret_key": "",
        "max_nodes": "1",
        "jobs_per_node": "30",
        "idle_minutes": "10",
        "provisioning_timeout_minutes": "30",
        "max_lifetime_hours": "12",
        "billing_boundary_minute": "55",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_einschalten_mit_neuem_secret_ist_gueltig() -> None:
    form = ComputeSettingsForm(data=form_data(client_secret="neu"), instance=ComputeSettings.load(use_cache=False))
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.get_client_secret() == "neu"


@pytest.mark.django_db
def test_einschalten_ohne_secret_wird_abgelehnt() -> None:
    form = ComputeSettingsForm(data=form_data(), instance=ComputeSettings.load(use_cache=False))
    assert not form.is_valid()
    assert "Client-Secret" in str(form.errors)


@pytest.mark.django_db
def test_leeres_feld_behaelt_vorhandenes_secret() -> None:
    config = ComputeSettings.load(use_cache=False)
    config.set_client_secret("bestehend")
    config.save()

    form = ComputeSettingsForm(data=form_data(client_id="geaendert"), instance=ComputeSettings.load(use_cache=False))
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.client_id == "geaendert"
    assert saved.get_client_secret() == "bestehend"
