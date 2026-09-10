# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Client für die centron ccloud Customer API.

Grundlage: https://docs.centron.de/api/ — Basis ``https://ccenter.centron.de/api/v1``,
Authentifizierung per OAuth2 Client Credentials.

Nicht öffentlich dokumentiert ist, wie genau eine GPU an eine VM gebunden
wird. Dokumentiert sind der „GPU Pool" und die Verfügbarkeitsabfrage
``GET /ccloud/servers/gpus``. Dieser Client erfindet deshalb keinen
GPU-Parameter: Er prüft vor dem Erstellen die Verfügbarkeit, und der Worker
meldet bei der Registrierung die tatsächlich erkannte GPU. Stimmt sie nicht,
löscht der Orchestrator den Knoten sofort, statt eine VM ohne GPU weiter zu
bezahlen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://ccenter.centron.de/api/v1"
GIB = 1024**3


class CentronError(Exception):
    """Fehler der centron-API. Enthält nie Zugangsdaten."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CentronCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    scope: str = ""


@dataclass(frozen=True)
class ServerSpec:
    """Parameter für ``POST /ccloud/servers``."""

    project_id: int
    pool: str
    hostname: str
    image: str
    cores: int
    memory_gb: int
    disk_gb: int
    user_data: str = field(repr=False)
    ssh_keys: tuple[str, ...] = ()
    tags: tuple[str, ...] = ("mandari", "minutes-gpu")
    server_type: str = "unmanaged"

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "pool": self.pool,
            "hostname": self.hostname,
            "custom_name": self.hostname,
            "description": "mandari Transkriptions-Knoten (automatisch verwaltet)",
            "cores": self.cores,
            "memory": self.memory_gb * GIB,
            "disks": [self.disk_gb * GIB],
            "image": self.image,
            "type": self.server_type,
            "user_data": self.user_data,
            "tags": list(self.tags),
        }
        if self.ssh_keys:
            payload["ssh_keys"] = list(self.ssh_keys)
        return payload


class CentronClient:
    """Schmaler, synchroner Client — der Orchestrator läuft ohnehin sequenziell."""

    TOKEN_REFRESH_MARGIN_SECONDS = 60

    def __init__(
        self,
        credentials: CentronCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._credentials = credentials
        self._http = httpx.Client(base_url=credentials.base_url.rstrip("/"), transport=transport, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CentronClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Authentifizierung --------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = self._http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "scope": self._credentials.scope,
            },
        )
        if response.status_code >= 400:
            # Antworttext bewusst nicht übernehmen: Er könnte Eingaben spiegeln.
            raise CentronError("Anmeldung an der centron-API fehlgeschlagen", response.status_code)
        body = _as_dict(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise CentronError("centron-API lieferte kein Zugriffstoken")
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        self._token = token
        self._token_expires_at = time.monotonic() + max(0.0, lifetime - self.TOKEN_REFRESH_MARGIN_SECONDS)
        return token

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        response = self._http.request(
            method,
            path,
            json=json,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status_code >= 400:
            raise CentronError(f"{method} {path} fehlgeschlagen", response.status_code)
        return response

    # -- Aufrufe -------------------------------------------------------------

    def list_gpus(self) -> list[dict[str, Any]]:
        """Verfügbare GPU-Modelle mit aktueller Verfügbarkeit."""
        return _as_list(self._request("GET", "/ccloud/servers/gpus"))

    def create_server(self, spec: ServerSpec) -> dict[str, Any]:
        return _as_dict(self._request("POST", "/ccloud/servers", json=spec.payload()))

    def server_state(self, hostname: str) -> str:
        body = _as_dict(self._request("GET", f"/ccloud/servers/{hostname}/state"))
        state = body.get("state", body.get("status", ""))
        return str(state)

    def server_details(self, hostname: str) -> dict[str, Any]:
        return _as_dict(self._request("GET", f"/ccloud/servers/{hostname}"))

    def delete_server(self, hostname: str) -> None:
        """VM löschen. Erst damit endet die Abrechnung."""
        self._request("DELETE", f"/ccloud/servers/{hostname}")


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise CentronError("centron-API lieferte keine gültige JSON-Antwort", response.status_code) from exc


def _as_dict(response: httpx.Response) -> dict[str, Any]:
    body = _json(response)
    if not isinstance(body, dict):
        raise CentronError("Unerwartetes Antwortformat der centron-API", response.status_code)
    return body


def _as_list(response: httpx.Response) -> list[dict[str, Any]]:
    body = _json(response)
    if isinstance(body, dict):
        body = body.get("data", body.get("items", body))
    if not isinstance(body, list):
        raise CentronError("Unerwartetes Antwortformat der centron-API", response.status_code)
    return [item for item in body if isinstance(item, dict)]
