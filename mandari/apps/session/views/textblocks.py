# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaltung von Textbausteinen und Standard-Tagesordnungspunkten (Issue #85).
"""

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionOrganization, SessionStandardAgendaItem, SessionTextBlock
from ..permissions import SessionViewMixin


class TextblockSettingsView(SessionViewMixin, TemplateView):
    """Übersicht: Standard-TOPs und Textbausteine pflegen."""

    template_name = "session/settings/textblocks.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "standard_items": SessionStandardAgendaItem.objects.filter(tenant=self.session_tenant).select_related(
                    "organization"
                ),
                "text_blocks": SessionTextBlock.objects.filter(tenant=self.session_tenant),
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                "placement_choices": SessionStandardAgendaItem.PLACEMENT_CHOICES,
                "category_choices": SessionTextBlock.CATEGORY_CHOICES,
            }
        )
        return context


class _ManageBase(SessionViewMixin, View):
    permission_required = "manage_settings"
    http_method_names = ["post"]

    def _redirect(self):
        return redirect("session:settings_textblocks", tenant_slug=self.session_tenant.slug)

    def _get_instance(self, model, pk):
        if not pk:
            return None
        try:
            return model.objects.filter(tenant=self.session_tenant, pk=pk).first()
        except (ValueError, DjangoValidationError):
            return None

    @staticmethod
    def _parse_order(raw):
        try:
            return max(0, min(9999, int(raw)))
        except (TypeError, ValueError):
            return 0


class StandardItemManageView(_ManageBase):
    """Standard-TOP anlegen, ändern oder löschen."""

    def post(self, request, tenant_slug):
        instance = self._get_instance(SessionStandardAgendaItem, request.POST.get("item_id"))

        if request.POST.get("action") == "delete":
            if instance is None:
                messages.error(request, "Standard-TOP nicht gefunden.")
            else:
                audit.log_event(
                    "delete",
                    instance,
                    tenant=self.session_tenant,
                    user=self.session_user,
                    request=request,
                )
                instance.delete()
                messages.success(request, "Standard-TOP gelöscht.")
            return self._redirect()

        name = request.POST.get("name", "").strip()[:500]
        if not name:
            messages.error(request, "Bitte einen Betreff angeben.")
            return self._redirect()

        organization = None
        org_id = request.POST.get("organization", "").strip()
        if org_id:
            organization = self._get_instance(SessionOrganization, org_id)

        placement = request.POST.get("placement", "start")
        if placement not in {value for value, _ in SessionStandardAgendaItem.PLACEMENT_CHOICES}:
            placement = "start"

        values = {
            "name": name,
            "organization": organization,
            "placement": placement,
            "order": self._parse_order(request.POST.get("order")),
            "is_public": request.POST.get("is_public") == "1",
            "is_active": request.POST.get("is_active", "1") == "1",
        }
        if instance is None:
            instance = SessionStandardAgendaItem.objects.create(tenant=self.session_tenant, **values)
            action = "create"
            messages.success(request, f"Standard-TOP „{name}“ angelegt.")
        else:
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            action = "update"
            messages.success(request, f"Standard-TOP „{name}“ gespeichert.")

        audit.log_event(
            action,
            instance,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"betreff": name, "position": placement},
        )
        return self._redirect()


class TextblockManageView(_ManageBase):
    """Textbaustein anlegen, ändern oder löschen."""

    def post(self, request, tenant_slug):
        instance = self._get_instance(SessionTextBlock, request.POST.get("block_id"))

        if request.POST.get("action") == "delete":
            if instance is None:
                messages.error(request, "Textbaustein nicht gefunden.")
            else:
                audit.log_event(
                    "delete",
                    instance,
                    tenant=self.session_tenant,
                    user=self.session_user,
                    request=request,
                )
                instance.delete()
                messages.success(request, "Textbaustein gelöscht.")
            return self._redirect()

        title = request.POST.get("title", "").strip()[:200]
        content = request.POST.get("content", "").strip()
        if not title or not content:
            messages.error(request, "Bitte Titel und Text angeben.")
            return self._redirect()

        category = request.POST.get("category", "general")
        if category not in {value for value, _ in SessionTextBlock.CATEGORY_CHOICES}:
            category = "general"

        values = {
            "title": title,
            "content": content,
            "category": category,
            "order": self._parse_order(request.POST.get("order")),
            "is_active": request.POST.get("is_active", "1") == "1",
        }
        if instance is None:
            instance = SessionTextBlock.objects.create(tenant=self.session_tenant, **values)
            action = "create"
            messages.success(request, f"Textbaustein „{title}“ angelegt.")
        else:
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            action = "update"
            messages.success(request, f"Textbaustein „{title}“ gespeichert.")

        audit.log_event(
            action,
            instance,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"titel": title, "kategorie": category},
        )
        return self._redirect()
