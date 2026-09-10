# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin: zentrale Rechenknoten-Konfiguration und Knotenübersicht."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from unfold.admin import ModelAdmin

from .models_compute import ComputeSettings, GpuNode


class ComputeSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Secrets sind Schreibfelder: Sie werden nie wieder angezeigt."""

    client_secret = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="Client-Secret",
        help_text="Wird mit dem Master-Key verschlüsselt gespeichert.",
    )
    s3_secret_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="S3-Secret-Key",
        help_text="Wird mit dem Master-Key verschlüsselt gespeichert.",
    )

    class Meta:
        model = ComputeSettings
        fields = "__all__"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        instance = self.instance
        if instance.pk and instance.client_secret_encrypted:
            self.fields["client_secret"].help_text = "Ist gesetzt. Nur für eine Rotation neu eintragen."
        if instance.pk and instance.s3_secret_key_encrypted:
            self.fields["s3_secret_key"].help_text = "Ist gesetzt. Nur für eine Rotation neu eintragen."

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        # Läuft vor der Modellvalidierung: So sieht ComputeSettings.clean() ein
        # neu eingetragenes Secret bereits beim Einschalten.
        client_secret = str(cleaned.get("client_secret", "")).strip()
        if client_secret:
            self.instance.set_client_secret(client_secret)
        s3_secret_key = str(cleaned.get("s3_secret_key", "")).strip()
        if s3_secret_key:
            self.instance.set_s3_secret_key(s3_secret_key)
        return cleaned


@admin.register(ComputeSettings)
class ComputeSettingsAdmin(ModelAdmin):  # type: ignore[misc]
    form = ComputeSettingsForm
    fieldsets = (
        (
            "Betrieb",
            {
                "fields": ("enabled",),
                "description": (
                    "Gilt plattformweit für alle Organisationen und Kommunen. Solange ausgeschaltet, "
                    "erstellt mandari keine VM und verursacht keine Kosten."
                ),
            },
        ),
        (
            "centron API",
            {"fields": ("api_base_url", "client_id", "client_secret", "scope", "project_id")},
        ),
        (
            "GPU-vServer",
            {
                "fields": (
                    "pool",
                    "image",
                    "prepared_image",
                    "gpu_model",
                    ("cores", "memory_gb", "disk_gb"),
                    "ssh_public_key",
                ),
            },
        ),
        ("Worker", {"fields": ("worker_image", "mandari_url")}),
        (
            "centron Object Storage",
            {
                "fields": ("s3_endpoint", "s3_bucket", "s3_access_key", "s3_secret_key"),
                "description": "Modellgewichte und Audio. Datenverkehr zwischen Object Storage und VMs ist bei centron kostenfrei.",
            },
        ),
        (
            "Grenzen und Abrechnung",
            {
                "fields": (
                    ("max_nodes", "jobs_per_node"),
                    ("idle_minutes", "billing_boundary_minute"),
                    ("provisioning_timeout_minutes", "max_lifetime_hours"),
                ),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not ComputeSettings.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> HttpResponse:
        # Singleton: direkt zur Bearbeitung.
        instance = ComputeSettings.load(use_cache=False)
        return HttpResponseRedirect(f"../computesettings/{instance.pk}/change/")


@admin.register(GpuNode)
class GpuNodeAdmin(ModelAdmin):  # type: ignore[misc]
    """Nur Ansicht: Knoten entstehen und verschwinden ausschließlich über den Orchestrator."""

    list_display = ("hostname", "state", "gpu_model", "detected_gpu", "created_at", "ready_at", "deleted_at")
    list_filter = ("state",)
    search_fields = ("hostname",)
    readonly_fields = (
        "hostname",
        "state",
        "gpu_model",
        "detected_gpu",
        "ip_address",
        "created_at",
        "ready_at",
        "last_heartbeat_at",
        "last_activity_at",
        "deleted_at",
        "error",
    )
    exclude = ("token_hash",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
