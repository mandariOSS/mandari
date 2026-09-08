# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einreichungs-Zugänge (API-Tokens) verwalten — Verwaltung stellt Fraktionen
Tokens aus, mit denen Anträge aus dem Work-Portal eingereicht werden (Issue #40).
"""

from datetime import datetime, time

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.session.models import SessionAPIToken, SessionApplication
from apps.session.permissions import SessionViewMixin

SESSION_KEY_NEW_TOKEN = "session_api_token_once"


class APITokenListView(SessionViewMixin, TemplateView):
    """Liste aller Tokens; ein frisch erzeugter Token wird genau einmal angezeigt."""

    template_name = "session/settings/api_tokens.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tokens = list(
            SessionAPIToken.objects.filter(tenant=self.session_tenant)
            .select_related("created_by__user")
            .prefetch_related("tenant__work_connections__organization")
        )
        connections = {
            c.token_hash: c for c in self.session_tenant.work_connections.select_related("organization").all()
        }
        for token in tokens:
            token.connection = connections.get(token.token)
            token.application_count = (
                SessionApplication.objects.filter(
                    tenant=self.session_tenant, submitting_organization=token.connection.organization
                ).count()
                if token.connection
                else 0
            )
        context["tokens"] = tokens
        context["new_token"] = self.request.session.pop(SESSION_KEY_NEW_TOKEN, None)
        context["now"] = timezone.now()
        return context


class APITokenCreateView(SessionViewMixin, View):
    permission_required = "manage_settings"

    def post(self, request, *args, **kwargs):
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Bitte einen Namen angeben, z. B. den Namen der Fraktion.")
            return redirect("session:settings_api_tokens", tenant_slug=self.session_tenant.slug)
        expires_at = None
        raw_expiry = (request.POST.get("expires_at") or "").strip()
        if raw_expiry:
            try:
                expires_at = timezone.make_aware(
                    datetime.combine(datetime.strptime(raw_expiry, "%Y-%m-%d").date(), time(23, 59))
                )
            except ValueError:
                messages.error(request, "Das Ablaufdatum ist ungültig.")
                return redirect("session:settings_api_tokens", tenant_slug=self.session_tenant.slug)

        _token, raw = SessionAPIToken.create_token(
            tenant=self.session_tenant,
            name=name,
            description=(request.POST.get("description") or "").strip(),
            can_submit_applications=True,
            can_read_meetings=request.POST.get("can_read_meetings") == "on",
            can_read_papers=request.POST.get("can_read_papers") == "on",
            expires_at=expires_at,
            created_by=self.session_user,
        )
        request.session[SESSION_KEY_NEW_TOKEN] = {"name": name, "token": raw}
        messages.success(request, f"Einreichungs-Zugang „{name}“ erstellt. Der Token wird nur jetzt angezeigt.")
        return redirect("session:settings_api_tokens", tenant_slug=self.session_tenant.slug)


class APITokenRevokeView(SessionViewMixin, View):
    permission_required = "manage_settings"

    def post(self, request, *args, **kwargs):
        token = get_object_or_404(SessionAPIToken, id=kwargs["token_id"], tenant=self.session_tenant)
        token.is_active = False
        token.save(update_fields=["is_active", "updated_at"])
        messages.success(
            request, f"Zugang „{token.name}“ wurde zurückgezogen. Einreichungen damit sind nicht mehr möglich."
        )
        return redirect(reverse("session:settings_api_tokens", kwargs={"tenant_slug": self.session_tenant.slug}))
