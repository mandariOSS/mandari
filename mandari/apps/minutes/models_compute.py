# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zentrale Rechenknoten-Konfiguration und GPU-Knoten.

Die centron-Anbindung ist Plattformbetrieb, keine Mandanteneinstellung: Sie
wird genau einmal im mandari-Admin gepflegt und gilt für alle Organisationen
und Kommunen. Zugangsdaten liegen mit dem Master-Key verschlüsselt in der
Datenbank, nie im Klartext und nie auf einem Knoten.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models

from .orchestrator import Limits, NodeState

NODE_STATE_LABELS: dict[str, str] = {
    NodeState.REQUESTED: "Angefordert",
    NodeState.PROVISIONING: "Wird bei centron erstellt",
    NodeState.BOOTING: "Wird eingerichtet",
    NodeState.READY: "Bereit",
    NodeState.BUSY: "Arbeitet",
    NodeState.DELETING: "Wird gelöscht",
    NodeState.DELETED: "Gelöscht",
    NodeState.FAILED: "Fehlgeschlagen",
}


def _encrypt(value: str) -> bytes | None:
    from apps.common.encryption import encrypt_key

    return encrypt_key(value.encode("utf-8")) if value else None


def _decrypt(value: bytes | memoryview | None) -> str:
    from apps.common.encryption import decrypt_key

    if not value:
        return ""
    return decrypt_key(bytes(value)).decode("utf-8")


class ComputeSettings(models.Model):
    """Zentrale Konfiguration der GPU-Rechenknoten (Singleton)."""

    CACHE_KEY = "minutes_compute_settings"
    CACHE_TIMEOUT = 300

    enabled = models.BooleanField(
        default=False,
        verbose_name="Rechenknoten aktiv",
        help_text="Erst einschalten, wenn Zugangsdaten, Image und Worker bereitstehen. Aus = es wird nie eine VM erstellt.",
    )

    # -- centron API ------------------------------------------------------------
    api_base_url = models.URLField(default="https://ccenter.centron.de/api/v1", verbose_name="API-Basis-URL")
    client_id = models.CharField(max_length=200, blank=True, verbose_name="Client-ID")
    client_secret_encrypted = models.BinaryField(blank=True, null=True, editable=False, verbose_name="Client-Secret")
    scope = models.CharField(max_length=200, blank=True, verbose_name="OAuth-Scope")
    project_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="Projekt-ID")

    # -- vServer ------------------------------------------------------------------
    pool = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Pool",
        help_text="Kennung des GPU-Pools, in dem die VM entsteht.",
    )
    image = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Image",
        help_text="Standard: Ubuntu 24.04 (GPU-fähig). Alternativ ein eigenes, vorbereitetes Image.",
    )
    prepared_image = models.BooleanField(
        default=False,
        verbose_name="Vorbereitetes Image",
        help_text="Image enthält NVIDIA-Treiber, Docker und Container Toolkit. Verkürzt den Start von ~15 auf ~3 Minuten.",
    )
    gpu_model = models.CharField(max_length=100, default="Quadro RTX 6000", verbose_name="GPU-Modell")
    cores = models.PositiveSmallIntegerField(default=8, verbose_name="CPU-Kerne")
    memory_gb = models.PositiveSmallIntegerField(default=32, verbose_name="Arbeitsspeicher (GB)")
    disk_gb = models.PositiveSmallIntegerField(
        default=120,
        verbose_name="Festplatte (GB)",
        help_text="Nur für Modellgewichte. Audio und Transkripte liegen ausschließlich im RAM.",
    )
    ssh_public_key = models.TextField(
        blank=True,
        verbose_name="SSH-Schlüssel (öffentlich)",
        help_text="Optional, nur zur Fehlersuche. Leer = kein SSH-Zugang hinterlegt.",
    )

    # -- Worker -------------------------------------------------------------------
    worker_image = models.CharField(
        max_length=300,
        default="ghcr.io/mandarioss/transcriber:latest",
        verbose_name="Worker-Container",
    )
    mandari_url = models.URLField(
        blank=True,
        verbose_name="mandari-Adresse für Knoten",
        help_text="Unter dieser Adresse holen sich Knoten ihre Aufträge. Leer = SITE_URL.",
    )

    # -- centron Object Storage (Modelle, Audio) --------------------------------
    s3_endpoint = models.URLField(blank=True, verbose_name="S3-Endpunkt")
    s3_bucket = models.CharField(max_length=200, blank=True, verbose_name="S3-Bucket")
    s3_access_key = models.CharField(max_length=200, blank=True, verbose_name="S3-Access-Key")
    s3_secret_key_encrypted = models.BinaryField(blank=True, null=True, editable=False, verbose_name="S3-Secret-Key")

    # -- Betrieb ------------------------------------------------------------------
    max_nodes = models.PositiveSmallIntegerField(default=1, verbose_name="Höchstzahl gleichzeitiger Knoten")
    jobs_per_node = models.PositiveSmallIntegerField(default=30, verbose_name="Aufträge je Knoten")
    idle_minutes = models.PositiveSmallIntegerField(default=10, verbose_name="Leerlauf bis Abbau (Minuten)")
    provisioning_timeout_minutes = models.PositiveSmallIntegerField(
        default=30,
        verbose_name="Höchstdauer Bereitstellung (Minuten)",
    )
    max_lifetime_hours = models.PositiveSmallIntegerField(default=12, verbose_name="Höchstlaufzeit je Knoten (Stunden)")
    billing_boundary_minute = models.PositiveSmallIntegerField(
        default=55,
        verbose_name="Löschfenster ab Minute",
        help_text="centron rechnet stundenweise ab. Leerlaufende Knoten werden erst ab dieser Minute der laufenden Stunde gelöscht.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "minutes_compute_settings"
        verbose_name = "GPU-Rechenknoten"
        verbose_name_plural = "GPU-Rechenknoten"

    def __str__(self) -> str:
        return "GPU-Rechenknoten"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        return (0, {})

    @classmethod
    def load(cls, *, use_cache: bool = True) -> ComputeSettings:
        if use_cache:
            cached = cache.get(cls.CACHE_KEY)
            if isinstance(cached, cls):
                return cached
        instance, _ = cls.objects.get_or_create(pk=1)
        cache.set(cls.CACHE_KEY, instance, cls.CACHE_TIMEOUT)
        return instance

    def clean(self) -> None:
        if not self.enabled:
            return
        missing = [
            label
            for label, value in (
                ("Client-ID", self.client_id),
                ("Client-Secret", self.client_secret_encrypted),
                ("Projekt-ID", self.project_id),
                ("Pool", self.pool),
                ("Image", self.image),
                ("Worker-Container", self.worker_image),
            )
            if not value
        ]
        if missing:
            raise ValidationError(f"Zum Einschalten fehlen: {', '.join(missing)}.")

    def set_client_secret(self, value: str) -> None:
        self.client_secret_encrypted = _encrypt(value)

    def get_client_secret(self) -> str:
        return _decrypt(self.client_secret_encrypted)

    def set_s3_secret_key(self, value: str) -> None:
        self.s3_secret_key_encrypted = _encrypt(value)

    def get_s3_secret_key(self) -> str:
        return _decrypt(self.s3_secret_key_encrypted)

    def effective_mandari_url(self) -> str:
        return (self.mandari_url or str(getattr(settings, "SITE_URL", ""))).rstrip("/")

    def limits(self) -> Limits:
        return Limits(
            max_nodes=self.max_nodes,
            jobs_per_node=self.jobs_per_node,
            idle_minutes=self.idle_minutes,
            provisioning_timeout_minutes=self.provisioning_timeout_minutes,
            max_lifetime_hours=self.max_lifetime_hours,
            billing_boundary_minute=self.billing_boundary_minute,
        )


def hash_node_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class GpuNode(models.Model):
    """Ein GPU-vServer bei centron, verwaltet vom Orchestrator."""

    hostname = models.CharField(max_length=100, unique=True, verbose_name="Hostname")
    state = models.CharField(
        max_length=20,
        choices=list(NODE_STATE_LABELS.items()),
        default=NodeState.REQUESTED,
        verbose_name="Zustand",
    )
    gpu_model = models.CharField(max_length=100, verbose_name="Angefordertes GPU-Modell")
    detected_gpu = models.CharField(max_length=200, blank=True, verbose_name="Erkannte GPU")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP-Adresse")

    # Nur der Hash wird gespeichert; das Token selbst kennt ausschließlich der Knoten.
    token_hash = models.CharField(max_length=64, editable=False)

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Angefordert am")
    ready_at = models.DateTimeField(null=True, blank=True, verbose_name="Bereit seit")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True, verbose_name="Letztes Lebenszeichen")
    last_activity_at = models.DateTimeField(null=True, blank=True, verbose_name="Letzte Arbeit")
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name="Gelöscht am")
    error = models.TextField(blank=True, verbose_name="Fehler")

    class Meta:
        db_table = "minutes_gpu_nodes"
        verbose_name = "GPU-Knoten"
        verbose_name_plural = "GPU-Knoten"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["state"])]

    def __str__(self) -> str:
        return f"{self.hostname} ({self.get_state_display()})"

    def token_matches(self, token: str) -> bool:
        import hmac

        return bool(self.token_hash) and hmac.compare_digest(self.token_hash, hash_node_token(token))
