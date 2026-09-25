# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Audit-Log-Ansicht, Export und Kettenprüfung für das Session RIS (Issues #23, #221).

- :class:`AuditLogListView`: revisionssichere Historie des eigenen Mandanten mit Filtern nach
  Objekt-Typ, Aktion, Nutzer und Zeitraum (Recht ``view_audit_log``). Jede Einsicht wird selbst
  protokolliert (``audit_view``, zusammengefasst je Filter und zehn Minuten).
- :class:`AuditLogExportView`: Zeitraum als CSV, JSON oder ZIP mit SHA-256-Prüfsumme im Kopf
  ``X-Checksum-SHA256`` (Recht ``export_audit_log``); jeder Export ist ein Protokolleintrag mit
  Prüfsumme.
- :class:`AuditLogVerifyView`: Hash-Kette prüfen und das Ergebnis protokollieren.

Beide Rechte sind Kontrollrechte: nicht in der Administrator-Vollmacht enthalten (Rollen
„Revision“ und „Datenschutz“). Es gibt keine Update- oder Delete-Endpunkte.
"""

import datetime as dt
import uuid

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import ListView

from apps.common import audit_chain

from .. import audit
from ..models import SessionAuditLog, SessionUser
from ..permissions import SessionPermissionChecker, SessionViewMixin
from ..services import audit_log_service

#: Größere Ketten prüft die Oberfläche nicht (Laufzeit im Request), sondern verify_audit_chain
UI_VERIFY_MAX_ENTRIES = 300_000


def _parse_date(raw):
    try:
        return dt.date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _parse_uuid(raw):
    try:
        return uuid.UUID(str(raw).strip())
    except (TypeError, ValueError):
        return None


def change_rows(changes) -> list[dict]:
    """Änderungen für die Anzeige: Feld-Diffs (alt → neu) oder einfache Angaben."""
    rows = []
    if not isinstance(changes, dict):
        return rows
    for field, value in changes.items():
        if isinstance(value, dict) and set(value) <= {"alt", "neu"} and value:
            rows.append({"field": field, "is_diff": True, "alt": value.get("alt"), "neu": value.get("neu")})
        else:
            text = audit_chain.canonical_json(value) if isinstance(value, dict | list) else value
            rows.append({"field": field, "is_diff": False, "value": text})
    return rows


# =============================================================================
# AUDIT LOG
# =============================================================================


class AuditLogListView(SessionViewMixin, ListView):
    """Read-only Liste der Audit-Einträge des eigenen Mandanten."""

    model = SessionAuditLog
    template_name = "session/audit/list.html"
    context_object_name = "entries"
    paginate_by = 50
    permission_required = "view_audit_log"

    FILTER_KEYS = ("model", "action", "user", "object", "from", "to")

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("user__user", "on_behalf_of__user").order_by("-created_at")

        # Filter: Objekt-Typ
        model_name = self.request.GET.get("model")
        if model_name:
            qs = qs.filter(model_name=model_name)

        # Filter: Aktion
        action = self.request.GET.get("action")
        if action:
            qs = qs.filter(action=action)

        # Filter: Nutzer (nur Nutzer des eigenen Tenants wählbar)
        user_id = self.request.GET.get("user")
        if user_id:
            user_uuid = _parse_uuid(user_id)
            qs = qs.filter(user_id=user_uuid, user__tenant=self.session_tenant) if user_uuid else qs.none()

        # Filter: Objekt-ID (Deep-Link aus Detailansichten)
        object_id = self.request.GET.get("object")
        if object_id:
            object_uuid = _parse_uuid(object_id)
            qs = qs.filter(object_id=object_uuid) if object_uuid else qs.none()

        # Filter: Zeitraum
        date_from = _parse_date(self.request.GET.get("from"))
        date_to = _parse_date(self.request.GET.get("to"))
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def get(self, request, *args, **kwargs):
        # Einsicht in das Protokoll selbst protokollieren – immer, unabhängig vom Lese-Schalter
        filters = {key: request.GET[key][:100] for key in self.FILTER_KEYS if request.GET.get(key)}
        audit.log_read(
            request,
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            action="audit_view",
            changes={"filter": filters} if filters else {},
            respect_setting=False,
            dedup_suffix=audit_chain.canonical_json(filters),
        )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for entry in context["entries"]:
            entry.change_rows = change_rows(entry.changes)
        context["actions"] = SessionAuditLog.ACTION_CHOICES
        context["model_names"] = (
            SessionAuditLog.objects.filter(tenant=self.session_tenant)
            .values_list("model_name", flat=True)
            .distinct()
            .order_by("model_name")
        )
        context["tenant_users"] = (
            SessionUser.objects.filter(tenant=self.session_tenant).select_related("user").order_by("user__email")
        )
        can_export = SessionPermissionChecker(self.session_user).has_permission("export_audit_log")
        context["can_export"] = can_export
        if can_export:
            context["chain"] = audit_log_service.chain_status(self.session_tenant)
            context["last_verify"] = (
                SessionAuditLog.objects.filter(tenant=self.session_tenant, action="audit_verify")
                .order_by("-created_at")
                .first()
            )
            context["export_formats"] = [("csv", "CSV"), ("json", "JSON"), ("zip", "ZIP (CSV, JSON, Prüfsummen)")]
            context["export_max_rows"] = settings.AUDIT_EXPORT_MAX_ROWS
        return context


class AuditLogExportView(SessionViewMixin, View):
    """Zeitraum exportieren (CSV, JSON, ZIP) mit SHA-256-Prüfsumme; der Export wird protokolliert."""

    permission_required = "export_audit_log"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        fmt = request.POST.get("format", "csv")
        date_from = _parse_date(request.POST.get("from"))
        date_to = _parse_date(request.POST.get("to"))
        if fmt not in audit_log_service.EXPORT_FORMATS:
            messages.error(request, "Unbekanntes Exportformat.")
            return redirect("session:audit_log", tenant_slug=tenant_slug)
        if date_from and date_to and date_from > date_to:
            messages.error(request, "Der Beginn des Zeitraums liegt nach dessen Ende.")
            return redirect("session:audit_log", tenant_slug=tenant_slug)

        count = audit_log_service.period_queryset(self.session_tenant, date_from, date_to).count()
        if count > settings.AUDIT_EXPORT_MAX_ROWS:
            messages.error(
                request,
                f"Der Zeitraum umfasst {count} Einträge – mehr als {settings.AUDIT_EXPORT_MAX_ROWS} lassen sich hier "
                "nicht exportieren. Bitte einen kürzeren Zeitraum wählen; der Betrieb kann größere Exporte mit dem "
                "Befehl export_audit_log erstellen.",
            )
            return redirect("session:audit_log", tenant_slug=tenant_slug)

        export = audit_log_service.build_export(
            self.session_tenant,
            fmt=fmt,
            date_from=date_from,
            date_to=date_to,
            requested_by=self.session_user.user.email,
        )
        # Der Export ist selbst ein Protokolleintrag – mit der Prüfsumme der ausgelieferten Datei.
        # Scheitert dieser Eintrag, wird nichts ausgeliefert.
        audit.log_event(
            "audit_export",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "format": fmt,
                "von": date_from.isoformat() if date_from else None,
                "bis": date_to.isoformat() if date_to else None,
                "anzahl": export.count,
                "datei": export.name,
                "sha256": export.sha256,
            },
        )
        response = FileResponse(export.file, as_attachment=True, filename=export.name, content_type=export.content_type)
        response["X-Checksum-SHA256"] = export.sha256
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class AuditLogVerifyView(SessionViewMixin, View):
    """Hash-Kette des Mandanten prüfen; das Ergebnis ist ein Protokolleintrag."""

    permission_required = "export_audit_log"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        tenant = self.session_tenant
        if SessionAuditLog.objects.filter(tenant=tenant).count() > UI_VERIFY_MAX_ENTRIES:
            messages.warning(
                request,
                "Das Protokoll ist zu groß für eine Prüfung in der Oberfläche. Der Betrieb prüft es mit dem Befehl "
                "verify_audit_chain.",
            )
            return redirect("session:audit_log", tenant_slug=tenant_slug)

        started = timezone.now()
        result = audit_log_service.verify_tenant(tenant)
        summary = result.as_dict()
        summary["dauer_ms"] = int((timezone.now() - started).total_seconds() * 1000)
        if result.errors:
            summary["erste_befunde"] = result.errors[:5]
        audit.log_event("audit_verify", tenant, tenant=tenant, user=self.session_user, request=request, changes=summary)

        if result.ok:
            text = f"Die Hash-Kette ist intakt ({result.checked} Einträge geprüft)."
            for warning in result.warnings:
                messages.warning(request, warning)
            messages.success(request, text)
        else:
            messages.error(
                request,
                f"Die Prüfung hat {result.error_count} Befund(e) ergeben: " + " ".join(result.errors[:3]),
            )
        return redirect("session:audit_log", tenant_slug=tenant_slug)
