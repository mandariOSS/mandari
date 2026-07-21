# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einsichts-View der Änderungshistorie für Fraktionssitzungen (Issue #66).

Zeigt Berechtigten (faction.view_audit) die revisionssichere
Änderungshistorie der eigenen Organisation — mit Filtern nach Objekt-Typ,
Aktion und Objekt-/Sitzungs-ID. Einträge sind rein lesend; es gibt keine
Update-/Delete-Endpunkte.

NÖ-Abschottung (Issue #64): Einträge zu nicht-öffentlichen TOPs werden
Nicht-Vereidigten ausschließlich als "Gesperrte Information" angezeigt —
ohne Objekt-Beschreibung und ohne Änderungs-Diff.
"""

from django.core.paginator import Paginator
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ..models import FactionAuditLog
from ..visibility import LOCKED_PLACEHOLDER, can_view_internal


class FactionAuditLogView(WorkViewMixin, TemplateView):
    """Read-only Liste der Fraktions-Audit-Einträge der eigenen Organisation."""

    template_name = "work/faction/audit_list.html"
    permission_required = "faction.view_audit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        qs = (
            FactionAuditLog.objects.filter(organization=self.organization)
            .select_related("membership__user")
            .order_by("-created_at")
        )

        model_name = self.request.GET.get("model")
        if model_name:
            qs = qs.filter(model_name=model_name)
            context["selected_model"] = model_name

        action = self.request.GET.get("action")
        if action:
            qs = qs.filter(action=action)
            context["selected_action"] = action

        object_id = self.request.GET.get("object")
        if object_id:
            qs = qs.filter(object_id=object_id)
            context["selected_object"] = object_id

        meeting_id = self.request.GET.get("meeting")
        if meeting_id:
            qs = qs.filter(meeting_id_ref=meeting_id)
            context["selected_meeting"] = meeting_id

        paginator = Paginator(qs, 50)
        page = paginator.get_page(self.request.GET.get("page", 1))

        # NÖ-Abschottung (Issue #64): Einträge zu nicht-öffentlichen TOPs
        # für Nicht-Vereidigte vollständig maskieren
        sworn = can_view_internal(self.membership)
        entries = []
        for entry in page:
            locked = entry.is_internal and not sworn
            entries.append(
                {
                    "entry": entry,
                    "locked": locked,
                    "object_repr": LOCKED_PLACEHOLDER if locked else entry.object_repr,
                    "changes": {} if locked else entry.changes,
                }
            )

        context["page_obj"] = page
        context["entries"] = entries
        context["actions"] = FactionAuditLog.ACTION_CHOICES
        context["model_names"] = (
            FactionAuditLog.objects.filter(organization=self.organization)
            .values_list("model_name", flat=True)
            .distinct()
            .order_by("model_name")
        )
        return context
