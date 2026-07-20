# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Audit-Log-Ansicht für das Session RIS (Issue #23).

Zeigt Berechtigten (can_view_audit_log) die revisionssichere
Änderungshistorie des eigenen Mandanten — mit Filtern nach Objekt-Typ,
Aktion, Nutzer und Zeitraum. Einträge sind rein lesend; es gibt keine
Update-/Delete-Endpunkte.
"""

from django.views.generic import ListView

from ..models import SessionAuditLog, SessionUser
from ..permissions import SessionViewMixin

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

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("user__user").order_by("-created_at")

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
            qs = qs.filter(user_id=user_id, user__tenant=self.session_tenant)

        # Filter: Objekt-ID (Deep-Link aus Detailansichten)
        object_id = self.request.GET.get("object")
        if object_id:
            qs = qs.filter(object_id=object_id)

        # Filter: Zeitraum
        date_from = self.request.GET.get("from")
        date_to = self.request.GET.get("to")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["actions"] = SessionAuditLog._meta.get_field("action").choices
        context["model_names"] = (
            SessionAuditLog.objects.filter(tenant=self.session_tenant)
            .values_list("model_name", flat=True)
            .distinct()
            .order_by("model_name")
        )
        context["tenant_users"] = (
            SessionUser.objects.filter(tenant=self.session_tenant).select_related("user").order_by("user__email")
        )
        return context
