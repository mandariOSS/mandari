# SPDX-License-Identifier: AGPL-3.0-or-later
"""
S3-kompatibler Objektspeicher für die Dokumentablage (Issue #788). Standard: aus.

Eingeschaltet mit ``OBJ_ENABLED=true`` und den Zugangsdaten ``OBJ_ENDPOINT``, ``OBJ_BUCKET``, ``OBJ_KEY``
und ``OBJ_SECRET`` (nie im Repo, nur in der Umgebung). Inhalte liegen dort unter demselben Schlüssel wie
lokal (``sha256/<ab>/<sha256>``); die lokale Ablage wird zum Zwischenspeicher (``services/file_store.py``).

Die Bibliothek (boto3) wird erst geladen, wenn der Objektspeicher eingeschaltet ist.

Prüfsummen: boto3 berechnet seit 1.36 standardmäßig Prüfsummen für jeden Upload und sendet sie im
``aws-chunked``-Verfahren. Manche S3-kompatiblen Anbieter lehnen das ab. ``OBJ_CHECKSUMS=when_required``
(Standard) verhält sich wie frühere Versionen: Prüfsummen nur, wo die Schnittstelle sie verlangt.
Unsere eigene Prüfung bleibt davon unberührt: Jeder geholte Inhalt wird gegen seinen SHA-256 geprüft.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from django.conf import settings

DEFAULT_REGION = "us-east-1"
CHECKSUM_MODES = ("when_required", "when_supported")


class DeadlineExceededError(TimeoutError):
    """Der Abruf aus dem Objektspeicher dauert länger als erlaubt."""


def enabled() -> bool:
    """Eingeschaltet und vollständig konfiguriert?"""
    if not getattr(settings, "OBJ_ENABLED", False):
        return False
    return all(getattr(settings, name, "") for name in ("OBJ_ENDPOINT", "OBJ_BUCKET", "OBJ_KEY", "OBJ_SECRET"))


def key_for(sha256: str) -> str:
    return f"sha256/{sha256[:2]}/{sha256}"


def region() -> str:
    """``OBJ_REGION``, sonst der Standort aus dem Endpunkt (``<standort>.<anbieter>``), sonst us-east-1."""
    configured = str(getattr(settings, "OBJ_REGION", "") or "").strip()
    if configured:
        return configured
    host = urlsplit(str(getattr(settings, "OBJ_ENDPOINT", "") or "")).hostname or ""
    label = host.split(".", 1)[0]
    return label if re.fullmatch(r"[a-z]{2,4}[0-9]{1,2}", label) else DEFAULT_REGION


def checksum_mode() -> str:
    """``OBJ_CHECKSUMS``: ``when_required`` (Standard, verträglich) oder ``when_supported`` (boto3-Standard)."""
    value = str(getattr(settings, "OBJ_CHECKSUMS", "") or "").strip().lower()
    return value if value in CHECKSUM_MODES else CHECKSUM_MODES[0]


@lru_cache(maxsize=1)
def _client(
    endpoint: str, key: str, secret: str, region_name: str, addressing: str, timeout: float, checksums: str
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=region_name,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing},
            connect_timeout=5,
            read_timeout=timeout,
            retries={"max_attempts": 3, "mode": "standard"},
            request_checksum_calculation=checksums,
            response_checksum_validation=checksums,
        ),
    )


def client() -> Any:
    return _client(
        str(settings.OBJ_ENDPOINT),
        str(settings.OBJ_KEY),
        str(settings.OBJ_SECRET),
        region(),
        str(getattr(settings, "OBJ_ADDRESSING_STYLE", "auto") or "auto"),
        float(getattr(settings, "OBJ_TIMEOUT_SECONDS", 30)),
        checksum_mode(),
    )


def _transfer_config() -> Any:
    from boto3.s3.transfer import TransferConfig

    # Ein Thread, kleine Teile: der Anwendungsprozess hat wenig Speicher
    return TransferConfig(use_threads=False, multipart_chunksize=8 * 1024 * 1024)


def upload(sha256: str, path: Path) -> None:
    client().upload_file(
        str(path),
        settings.OBJ_BUCKET,
        key_for(sha256),
        ExtraArgs={"ContentType": "application/octet-stream"},
        Config=_transfer_config(),
    )


def download(sha256: str, target: IO[bytes] | Any, *, deadline: float | None = None) -> None:
    """
    Inhalt gestreamt nach ``target`` schreiben (Datei oder ``file_store.Spool``).

    ``deadline`` (``time.monotonic()``) begrenzt die Gesamtdauer; ``OBJ_TIMEOUT_SECONDS`` gilt nur je Lesevorgang.
    """
    response = client().get_object(Bucket=settings.OBJ_BUCKET, Key=key_for(sha256))
    body = response["Body"]
    try:
        for chunk in body.iter_chunks(1024 * 1024):
            if deadline is not None and time.monotonic() > deadline:
                raise DeadlineExceededError
            target.write(chunk)
    finally:
        body.close()


def delete(sha256: str) -> None:
    client().delete_object(Bucket=settings.OBJ_BUCKET, Key=key_for(sha256))


def exists(sha256: str) -> bool:
    return remote_size(sha256) is not None


def remote_size(sha256: str) -> int | None:
    """Größe des Inhalts im Objektspeicher (``HEAD``), ``None``, wenn er dort fehlt; andere Fehler gehen durch."""
    from botocore.exceptions import ClientError

    try:
        response = client().head_object(Bucket=settings.OBJ_BUCKET, Key=key_for(sha256))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return int(response.get("ContentLength") or 0)
