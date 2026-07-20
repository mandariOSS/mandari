# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Einstellungen und Benutzerverwaltung (Issue #27): Benutzer per E-Mail
einladen (Vorbild: Work-Einladungsflow), Rollen zuweisen/entziehen,
Deaktivieren — ohne Django-Admin.
"""

import logging

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import (
    ListView,
    TemplateView,
)

from ..models import (
    SessionInvitation,
    SessionRole,
    SessionUser,
)
from ..permissions import SessionViewMixin

logger = logging.getLogger(__name__)

# =============================================================================
# SETTINGS
# =============================================================================


class SettingsView(SessionViewMixin, TemplateView):
    """Tenant settings view."""

    template_name = "session/settings/index.html"
    permission_required = "manage_settings"


class UserListView(SessionViewMixin, ListView):
    """List of session users (mit Rollen-Verwaltung und Einladungen)."""

    model = SessionUser
    template_name = "session/settings/users.html"
    context_object_name = "session_users"
    paginate_by = 50
    permission_required = "manage_users"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related("user").prefetch_related("roles").order_by("user__email")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["available_roles"] = SessionRole.objects.filter(tenant=self.session_tenant).order_by("-priority")
        context["pending_invitations"] = SessionInvitation.objects.filter(
            tenant=self.session_tenant,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).order_by("-created_at")
        return context


class UserInviteView(SessionViewMixin, TemplateView):
    """Benutzer per E-Mail einladen (mit vorbelegten Rollen)."""

    template_name = "session/settings/invite.html"
    permission_required = "manage_users"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["available_roles"] = SessionRole.objects.filter(tenant=self.session_tenant).order_by("-priority")
        return context

    def post(self, request, *args, **kwargs):
        from apps.accounts.models import User

        email = request.POST.get("email", "").strip().lower()
        role_ids = request.POST.getlist("roles")

        if not email or "@" not in email:
            messages.error(request, "Bitte eine gültige E-Mail-Adresse angeben.")
            return redirect("session:user_invite", tenant_slug=self.session_tenant.slug)

        roles = list(SessionRole.objects.filter(id__in=role_ids, tenant=self.session_tenant))

        # Existiert bereits ein Konto? Dann direkt Mitglied machen.
        existing_user = User.objects.filter(email=email).first()
        if existing_user:
            session_user, created = SessionUser.objects.get_or_create(
                user=existing_user,
                tenant=self.session_tenant,
            )
            if not created and session_user.is_active:
                messages.warning(request, f"{email} ist bereits Mitglied dieses Mandanten.")
                return redirect("session:users", tenant_slug=self.session_tenant.slug)
            session_user.is_active = True
            session_user.save()
            if roles:
                session_user.roles.set(roles)
            messages.success(request, f"{email} wurde als Benutzer hinzugefügt.")
            return redirect("session:users", tenant_slug=self.session_tenant.slug)

        # Offene Einladung vorhanden?
        pending = SessionInvitation.objects.filter(
            tenant=self.session_tenant,
            email=email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists()
        if pending:
            messages.warning(request, f"Für {email} ist bereits eine Einladung offen.")
            return redirect("session:users", tenant_slug=self.session_tenant.slug)

        invitation = SessionInvitation.create_for_tenant(
            tenant=self.session_tenant,
            email=email,
            invited_by=self.session_user,
            roles=roles,
        )
        self._send_invitation_email(invitation)
        messages.success(request, f"Einladung an {email} wurde versendet.")
        return redirect("session:users", tenant_slug=self.session_tenant.slug)

    def _send_invitation_email(self, invitation):
        from apps.common.email import send_email

        base_url = getattr(django_settings, "SITE_URL", "https://mandari.de").rstrip("/")
        accept_path = reverse("session:invitation_accept", kwargs={"token": invitation.token})
        accept_url = f"{base_url}{accept_path}"

        tenant_name = invitation.tenant.name
        body = (
            f"Guten Tag,\n\n"
            f"Sie wurden eingeladen, dem Sitzungsdienst von {tenant_name} beizutreten.\n\n"
            f"Einladung annehmen: {accept_url}\n\n"
            f"Der Link ist 7 Tage gültig.\n\n"
            f"Mit freundlichen Grüßen\n{tenant_name}"
        )
        try:
            send_email(
                subject=f"Einladung zum Sitzungsdienst {tenant_name}",
                body=body,
                to=[invitation.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Einladungs-E-Mail an %s konnte nicht versendet werden.", invitation.email)


class UserRolesUpdateView(SessionViewMixin, View):
    """Rollen eines Benutzers zuweisen/entziehen."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, session_user_id):
        target = get_object_or_404(SessionUser, pk=session_user_id, tenant=self.session_tenant)
        role_ids = request.POST.getlist("roles")
        roles = SessionRole.objects.filter(id__in=role_ids, tenant=self.session_tenant)

        # Schutz: Der letzte Administrator darf sich nicht selbst entmachten
        if target.is_admin() and not any(r.is_admin for r in roles):
            other_admins = (
                SessionUser.objects.filter(tenant=self.session_tenant, is_active=True, roles__is_admin=True)
                .exclude(pk=target.pk)
                .exists()
            )
            if not other_admins:
                messages.error(request, "Der letzte Administrator kann nicht entfernt werden.")
                return redirect("session:users", tenant_slug=tenant_slug)

        target.roles.set(roles)
        messages.success(request, f"Rollen von {target.user.email} wurden aktualisiert.")
        return redirect("session:users", tenant_slug=tenant_slug)


class UserDeactivateView(SessionViewMixin, View):
    """Benutzer deaktivieren/reaktivieren."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, session_user_id):
        target = get_object_or_404(SessionUser, pk=session_user_id, tenant=self.session_tenant)

        if target.pk == self.session_user.pk:
            messages.error(request, "Sie können sich nicht selbst deaktivieren.")
            return redirect("session:users", tenant_slug=tenant_slug)

        # Schutz: letzter aktiver Admin bleibt
        if target.is_active and target.is_admin():
            other_admins = (
                SessionUser.objects.filter(tenant=self.session_tenant, is_active=True, roles__is_admin=True)
                .exclude(pk=target.pk)
                .exists()
            )
            if not other_admins:
                messages.error(request, "Der letzte aktive Administrator kann nicht deaktiviert werden.")
                return redirect("session:users", tenant_slug=tenant_slug)

        target.is_active = not target.is_active
        target.save()
        state = "reaktiviert" if target.is_active else "deaktiviert"
        messages.success(request, f"{target.user.email} wurde {state}.")
        return redirect("session:users", tenant_slug=tenant_slug)


class InvitationCancelView(SessionViewMixin, View):
    """Offene Einladung zurückziehen."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, invitation_id):
        invitation = get_object_or_404(
            SessionInvitation,
            pk=invitation_id,
            tenant=self.session_tenant,
            accepted_at__isnull=True,
        )
        email = invitation.email
        invitation.delete()
        messages.success(request, f"Einladung an {email} wurde zurückgezogen.")
        return redirect("session:users", tenant_slug=tenant_slug)


class InvitationAcceptView(View):
    """
    Einladung annehmen (öffentliche, token-basierte URL).

    - Angemeldete Nutzer treten direkt bei (E-Mail muss zur Einladung passen).
    - Nutzer ohne Konto registrieren sich über die Einladung (Passwort setzen).
    """

    template_name = "session/settings/invitation_accept.html"

    def _get_invitation(self, token):
        try:
            invitation = SessionInvitation.objects.select_related("tenant").get(token=token)
        except SessionInvitation.DoesNotExist:
            return None
        if not invitation.is_valid:
            return None
        return invitation

    def get(self, request, token):
        invitation = self._get_invitation(token)
        if invitation is None:
            return render(request, self.template_name, {"invalid": True}, status=404)

        email_matches = request.user.is_authenticated and request.user.email.lower() == invitation.email
        return render(
            request,
            self.template_name,
            {
                "invitation": invitation,
                "email_matches": email_matches,
                "needs_account": not request.user.is_authenticated,
            },
        )

    def post(self, request, token):
        from apps.accounts.models import User

        invitation = self._get_invitation(token)
        if invitation is None:
            return render(request, self.template_name, {"invalid": True}, status=404)

        if request.user.is_authenticated:
            if request.user.email.lower() != invitation.email:
                messages.error(request, "Diese Einladung ist für eine andere E-Mail-Adresse bestimmt.")
                return redirect("session:invitation_accept", token=token)
            user = request.user
        else:
            existing = User.objects.filter(email=invitation.email).first()
            if existing:
                messages.info(request, "Für diese E-Mail existiert bereits ein Konto. Bitte zuerst anmelden.")
                return redirect("session:invitation_accept", token=token)

            password = request.POST.get("password", "")
            password_confirm = request.POST.get("password_confirm", "")
            if password != password_confirm:
                messages.error(request, "Die Passwörter stimmen nicht überein.")
                return redirect("session:invitation_accept", token=token)

            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError

            try:
                validate_password(password)
            except ValidationError as exc:
                for error in exc.messages:
                    messages.error(request, error)
                return redirect("session:invitation_accept", token=token)

            user = User.objects.create_user(
                email=invitation.email,
                password=password,
                first_name=request.POST.get("first_name", "").strip()[:150],
                last_name=request.POST.get("last_name", "").strip()[:150],
            )
            login(request, user)

        session_user, _created = SessionUser.objects.get_or_create(user=user, tenant=invitation.tenant)
        session_user.is_active = True
        session_user.save()
        session_user.roles.set(invitation.roles.all())

        invitation.accepted_at = timezone.now()
        invitation.accepted_by = user
        invitation.save(update_fields=["accepted_at", "accepted_by"])

        messages.success(request, f"Willkommen im Sitzungsdienst von {invitation.tenant.name}.")
        return redirect("session:dashboard", tenant_slug=invitation.tenant.slug)
