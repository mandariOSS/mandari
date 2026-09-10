# SPDX-License-Identifier: AGPL-3.0-or-later
"""centron-API-Client gegen einen simulierten Server."""

import json

import httpx
import pytest

from apps.minutes.provisioning.centron import (
    GIB,
    CentronClient,
    CentronCredentials,
    CentronError,
    ServerSpec,
)

SECRET = "sehr-geheimes-client-secret"


class FakeCentron:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth/token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401, json={"error": "unauthorized"})
        if path.endswith("/ccloud/servers/gpus"):
            return httpx.Response(200, json={"data": [{"model": "Quadro RTX 6000", "available": 3}]})
        if path.endswith("/ccloud/servers") and request.method == "POST":
            return httpx.Response(201, json={"id": 1, "job_id": "j1", "action": "create"})
        if path.endswith("/state"):
            return httpx.Response(200, json={"state": "Running"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": 2, "job_id": "j2", "action": "delete"})
        return httpx.Response(404)


def client_for(fake: FakeCentron) -> CentronClient:
    credentials = CentronCredentials(client_id="mandari", client_secret=SECRET, base_url="https://api.test/api/v1")
    return CentronClient(credentials, transport=httpx.MockTransport(fake))


def spec() -> ServerSpec:
    return ServerSpec(
        project_id=42,
        pool="GPU Pool",
        hostname="mandari-gpu-01",
        image="ubuntu-24.04",
        cores=8,
        memory_gb=32,
        disk_gb=100,
        user_data="#cloud-config\n{}",
    )


def test_token_wird_einmal_geholt_und_wiederverwendet() -> None:
    fake = FakeCentron()
    with client_for(fake) as client:
        client.list_gpus()
        client.server_state("mandari-gpu-01")
    assert fake.token_calls == 1


def test_gpu_liste_wird_aus_data_gelesen() -> None:
    with client_for(FakeCentron()) as client:
        assert client.list_gpus() == [{"model": "Quadro RTX 6000", "available": 3}]


def test_server_wird_mit_bytes_und_cloud_init_angelegt() -> None:
    fake = FakeCentron()
    with client_for(fake) as client:
        client.create_server(spec())
    create = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/ccloud/servers"))
    body = json.loads(create.content)
    assert body["memory"] == 32 * GIB
    assert body["disks"] == [100 * GIB]
    assert body["pool"] == "GPU Pool"
    assert body["user_data"].startswith("#cloud-config")
    assert body["type"] == "unmanaged"


def test_loeschen_trifft_den_richtigen_server() -> None:
    fake = FakeCentron()
    with client_for(fake) as client:
        client.delete_server("mandari-gpu-01")
    delete = next(r for r in fake.requests if r.method == "DELETE")
    assert delete.url.path == "/api/v1/ccloud/servers/mandari-gpu-01"


def test_fehlerhafte_anmeldung_verraet_kein_secret() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid client {SECRET}")

    credentials = CentronCredentials(client_id="mandari", client_secret=SECRET, base_url="https://api.test/api/v1")
    with (
        CentronClient(credentials, transport=httpx.MockTransport(reject)) as client,
        pytest.raises(CentronError) as error,
    ):
        client.list_gpus()
    assert SECRET not in str(error.value)
    assert error.value.status_code == 401


def test_secret_erscheint_nicht_in_der_darstellung() -> None:
    credentials = CentronCredentials(client_id="mandari", client_secret=SECRET)
    assert SECRET not in repr(credentials)
    assert "#cloud-config" not in repr(spec())
