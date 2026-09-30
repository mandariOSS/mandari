# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Einstellungen und Benutzerverwaltung (Issue #27): Benutzer per E-Mail
einladen (Vorbild: Work-Einladungsflow), Rollen zuweisen/entziehen,
Deaktivieren — ohne Django-Admin.
"""

import logging

from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import get_object_or_404, redirect, render
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
from ..permissions import SessionViewMixin, grantable_permissions, is_admin_user, role_within_scope
from ..services.user_invitations import resend_user_invitation, send_user_invitation

logger = logging.getLogger(__name__)

#: Meldung, wenn eine Rolle außerhalb des eigenen Umfangs zugewiesen oder entzogen werden soll
OUTSIDE_SCOPE = (
    "Rollen mit Rechten, die Sie selbst nicht haben – darunter die Administrator-Rolle und die "
    "Kontrollrechte –, weist nur ein Administrator zu oder entzieht sie."
)


def roles_within_scope(session_user, roles) -> bool:
    """Darf diese Person alle genannten Rollen zuweisen bzw. entziehen? (keine Rechteausweitung)"""
    grantable = grantable_permissions(session_user)
    admin = is_admin_user(session_user)
    return all(role_within_scope(role, grantable, admin=admin) for role in roles)


# =============================================================================
# SETTINGS
# =============================================================================


class SettingsView(SessionViewMixin, TemplateView):
    """Tenant settings view."""

    template_name = "session/settings/index.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        from ..models import SessionTenant
        from ..services import oparl_access, portal_publication

        context = super().get_context_data(**kwargs)
        context["reminder_config"] = self.session_tenant.reminder_config()
        context["rsvp_reason_choices"] = SessionTenant.RSVP_REASON_CHOICES
        context["rsvp_audience_choices"] = SessionTenant.RSVP_AUDIENCE_CHOICES
        # Vor Issue #618 beendet: Bestand ohne Hinweis öffentlich, Auswahl anbieten
        context["portal_legacy_stock"] = portal_publication.legacy_stock(self.session_tenant)
        context["oparl_licenses"] = oparl_access.LICENSES
        return context


class ReminderSettingsView(SessionViewMixin, View):
    """Fristen-Erinnerungen konfigurieren (Issue #83), Erinnerung zur Ladung mit Anlass, Kreis, Zeitpunkten (#619)."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from .. import audit
        from ..models import SessionTenant

        tenant = self.session_tenant
        old_config = tenant.reminder_config()

        settings_dict = {}
        for key, default in SessionTenant.REMINDER_DEFAULTS.items():
            if key.endswith("_enabled"):
                settings_dict[key] = key in request.POST
            else:
                raw = request.POST.get(key, "").strip()
                try:
                    settings_dict[key] = max(0, min(60, int(raw)))
                except (TypeError, ValueError):
                    settings_dict[key] = default

        # Erinnerung zur Ladung (Issue #619): Unbekanntes fällt auf den Standard zurück
        reasons = {key for key, _ in SessionTenant.RSVP_REASON_CHOICES}
        audiences = {key for key, _ in SessionTenant.RSVP_AUDIENCE_CHOICES}
        reason = request.POST.get("rsvp_reason", "")
        audience = request.POST.get("rsvp_audience", "")
        settings_dict["rsvp_reason"] = reason if reason in reasons else SessionTenant.RSVP_REASON_RESPONSE
        settings_dict["rsvp_audience"] = audience if audience in audiences else SessionTenant.RSVP_AUDIENCE_ALL
        if "rsvp_days" in request.POST:
            days = SessionTenant.parse_rsvp_days(request.POST.get("rsvp_days", ""))
            if days:
                settings_dict["rsvp_days"] = days
                # Früheste Erinnerung auch im bisherigen Feld (ältere Auswertungen lesen nur dieses)
                settings_dict["rsvp_days_before"] = days[0]
            else:
                settings_dict["rsvp_days"] = old_config["rsvp_days"]
                settings_dict["rsvp_days_before"] = old_config["rsvp_days"][0]
                messages.warning(
                    request, "Keine gültigen Zeitpunkte angegeben – die bisherigen Zeitpunkte bleiben bestehen."
                )

        tenant.reminder_settings = settings_dict
        tenant.save(update_fields=["reminder_settings", "updated_at"])

        audit.log_event(
            "update",
            tenant,
            tenant=tenant,
            user=self.session_user,
            request=request,
            changes={"erinnerungen": {"alt": old_config, "neu": tenant.reminder_config()}},
        )
        messages.success(request, "Erinnerungs-Einstellungen gespeichert.")
        return redirect("session:settings", tenant_slug=tenant_slug)


class TwoFactorPolicyView(SessionViewMixin, View):
    """Zwei-Faktor-Pflicht für alle Nutzer des Mandanten ein- oder ausschalten.

    Administratoren sowie Nutzer mit Benutzer- oder Einstellungsrechten sind
    unabhängig davon immer verpflichtet (apps/accounts/two_factor_policy.py).
    """

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from .. import audit

        required = request.POST.get("require_2fa") == "1"
        tenant = self.session_tenant
        if tenant.require_2fa != required:
            old_value = tenant.require_2fa
            tenant.require_2fa = required
            tenant.save(update_fields=["require_2fa", "updated_at"])
            audit.log_event(
                "update",
                tenant,
                tenant=tenant,
                user=self.session_user,
                request=request,
                changes={"require_2fa": {"alt": old_value, "neu": required}},
            )
        if required:
            messages.success(
                request,
                "Zwei-Faktor-Authentifizierung ist jetzt für alle Nutzer verpflichtend. Wer noch keinen "
                "zweiten Faktor hat, richtet ihn bei der nächsten Anmeldung ein.",
            )
        else:
            messages.success(
                request,
                "Die Pflicht für alle Nutzer ist aufgehoben. Für Administratoren und Nutzer mit "
                "Verwaltungsrechten bleibt sie bestehen.",
            )
        return redirect("session:settings", tenant_slug=tenant_slug)


class ImplementationPublishView(SessionViewMixin, View):
    """Öffentliches Beschluss-Tracking ein-/ausschalten (Issue #48)."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from .. import audit

        publish = request.POST.get("publish") == "1"
        tenant = self.session_tenant
        if tenant.implementation_publish != publish:
            old_value = tenant.implementation_publish
            tenant.implementation_publish = publish
            tenant.save(update_fields=["implementation_publish", "updated_at"])
            audit.log_event(
                "publish" if publish else "update",
                tenant,
                tenant=tenant,
                user=self.session_user,
                request=request,
                changes={"implementation_publish": {"alt": old_value, "neu": publish}},
            )
            if publish:
                messages.success(
                    request,
                    "Umsetzungsstand wird veröffentlicht: Angenommene öffentliche Beschlüsse erscheinen mit "
                    "Status-Zeitleiste und öffentlicher Statusmeldung im Bürgerportal.",
                )
            else:
                messages.success(request, "Der Umsetzungsstand wird nicht mehr im Bürgerportal gezeigt.")
        return redirect("session:settings", tenant_slug=tenant_slug)


class InsightPublishView(SessionViewMixin, View):
    """
    Im Bürgerportal (wieder) veröffentlichen (Issue #36).

    Der Mandant entscheidet, ab wann seine öffentlichen Daten über die OParl-API ins Bürgerportal
    fließen. Das Einschalten registriert die OParl-Quelle (Signal in signals.py), hebt eine beim
    Beenden gewählte Möglichkeit auf und wird im Audit-Log protokolliert. Beenden geht nur über
    :class:`PortalPublicationEndView` mit Auswahl, was mit dem Bestand geschieht (Issue #618).
    """

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from ..services import portal_publication
        from ..services.oparl_access import RELEASE_REQUIRED, OParlAccessError

        tenant = self.session_tenant
        if request.POST.get("publish") != "1":
            # Alte Formulare ohne Auswahl: erst entscheiden, was mit dem Bestand geschieht
            if tenant.insight_publish:
                messages.info(request, "Bitte wählen Sie, was mit den bereits veröffentlichten Daten geschehen soll.")
                return redirect("session:portal_publication_end", tenant_slug=tenant_slug)
            messages.info(request, "Der Veröffentlichungs-Status ist bereits gesetzt.")
            return redirect("session:settings", tenant_slug=tenant_slug)

        try:
            change = portal_publication.resume_publication(tenant, user=self.session_user, request=request)
        except OParlAccessError:
            # Erst die OParl-Schnittstelle freischalten (Issue #319); feste Meldung, nie Ausnahmetexte
            messages.error(request, RELEASE_REQUIRED)
            return redirect("session:settings", tenant_slug=tenant_slug)
        if change is None:
            messages.info(request, "Der Veröffentlichungs-Status ist bereits gesetzt.")
        elif change.entries:
            messages.success(
                request,
                f"Veröffentlichung aktiviert — {change.entries} zurückgenommene Einträge sind wieder im "
                "Bürgerportal; Änderungen folgen mit dem nächsten Sync-Zyklus.",
            )
        else:
            messages.success(
                request,
                "Veröffentlichung aktiviert — die öffentlichen Daten dieses Mandanten "
                "erscheinen mit dem nächsten Sync-Zyklus im Bürgerportal.",
            )
        return redirect("session:settings", tenant_slug=tenant_slug)


class OParlAccessView(SessionViewMixin, View):
    """
    OParl-Schnittstelle freischalten bzw. die Freischaltung zurücknehmen (Issue #319).

    Bis zur Freischaltung antwortet die Schnittstelle mit 404 und das Bürgerportal registriert keine
    Quelle. Zurücknehmen geht nur, solange der Mandant nicht im Bürgerportal veröffentlicht. Beides steht
    mit Datum am Mandanten und im Audit-Log.
    """

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from ..services import oparl_access

        tenant = self.session_tenant
        if request.POST.get("public") == "1":
            if oparl_access.release(tenant, user=self.session_user, request=request):
                messages.success(
                    request,
                    "OParl-Schnittstelle freigeschaltet – öffentliche Daten sind ab sofort abrufbar.",
                )
            else:
                messages.info(request, "Die OParl-Schnittstelle ist bereits freigeschaltet.")
            return redirect("session:settings", tenant_slug=tenant_slug)
        try:
            gesperrt = oparl_access.lock(tenant, user=self.session_user, request=request)
        except oparl_access.OParlAccessError:
            messages.error(request, oparl_access.LOCK_BLOCKED)
            return redirect("session:settings", tenant_slug=tenant_slug)
        if gesperrt:
            messages.success(request, "Freischaltung zurückgenommen – die OParl-Schnittstelle antwortet mit 404.")
        else:
            messages.info(request, "Die OParl-Schnittstelle ist nicht freigeschaltet.")
        return redirect("session:settings", tenant_slug=tenant_slug)


class OParlLicenseView(SessionViewMixin, View):
    """Lizenz der offenen Daten festlegen (OParl ``license`` an System und Body), mit Audit-Eintrag."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        from ..services import oparl_access

        try:
            changed = oparl_access.set_license(
                self.session_tenant, request.POST.get("license", ""), user=self.session_user, request=request
            )
        except oparl_access.OParlAccessError:
            messages.error(request, oparl_access.UNKNOWN_LICENSE)
            return redirect("session:settings", tenant_slug=tenant_slug)
        if changed:
            messages.success(request, "Lizenz der offenen Daten gespeichert.")
        else:
            messages.info(request, "Die Lizenz der offenen Daten ist unverändert.")
        return redirect("session:settings", tenant_slug=tenant_slug)


class PortalPublicationEndView(SessionViewMixin, TemplateView):
    """
    Veröffentlichung im Bürgerportal beenden (Issue #618).

    Schritt 1 (GET): drei Möglichkeiten mit Hilfetext. Schritt 2 (POST ohne ``confirm``):
    Zusammenfassung der Folgen für die gewählte Möglichkeit und den heutigen Bestand. Erst die
    Bestätigung (POST mit ``confirm=1``) beendet bzw. wechselt die Möglichkeit, mit Audit-Eintrag.
    """

    template_name = "session/settings/portal_publication.html"
    permission_required = "manage_settings"
    http_method_names = ["get", "post"]

    def get_context_data(self, **kwargs):
        from ..services import portal_publication

        context = super().get_context_data(**kwargs)
        tenant = self.session_tenant
        context.update(
            {
                "options": portal_publication.OPTIONS,
                "current": None if tenant.insight_publish else portal_publication.option(tenant.insight_end_mode),
                "state_label": portal_publication.state_label(tenant),
                "inventory": portal_publication.inventory(tenant),
                "implementation_publish": tenant.implementation_publish,
            }
        )
        return context

    def post(self, request, tenant_slug):
        from ..services import portal_publication

        chosen = portal_publication.option(request.POST.get("mode"))
        if chosen is None:
            messages.error(request, "Bitte wählen Sie eine der drei Möglichkeiten.")
            return redirect("session:portal_publication_end", tenant_slug=tenant_slug)
        if request.POST.get("confirm") != "1":
            context = self.get_context_data(chosen=chosen, step="confirm")
            return self.render_to_response(context)
        change = portal_publication.end_publication(
            self.session_tenant, chosen.key, user=self.session_user, request=request
        )
        if change is None:
            messages.info(request, f"„{chosen.title}“ ist bereits eingestellt.")
        elif chosen.key == self.session_tenant.PORTAL_END_WITHDRAWN:
            messages.success(
                request,
                f"Veröffentlichung dauerhaft zurückgenommen: {change.entries} Einträge sind nicht mehr im Bürgerportal.",
            )
        elif chosen.key == self.session_tenant.PORTAL_END_ARCHIVED:
            messages.success(request, "Veröffentlichung beendet. Der Bestand bleibt als Archiv mit Hinweis lesbar.")
        else:
            messages.success(
                request, "Veröffentlichung vorübergehend abgeschaltet. Die Seiten im Bürgerportal zeigen einen Hinweis."
            )
        return redirect("session:settings", tenant_slug=tenant_slug)


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
        email = request.POST.get("email", "").strip().lower()
        role_ids = request.POST.getlist("roles")

        if not email or "@" not in email:
            messages.error(request, "Bitte eine gültige E-Mail-Adresse angeben.")
            return redirect("session:user_invite", tenant_slug=self.session_tenant.slug)

        roles = list(SessionRole.objects.filter(id__in=role_ids, tenant=self.session_tenant))
        if not roles_within_scope(self.session_user, roles):
            messages.error(request, OUTSIDE_SCOPE)
            return redirect("session:user_invite", tenant_slug=self.session_tenant.slug)

        # Bereits aktives Mitglied dieses Mandanten (steht ohnehin in der Benutzerliste)?
        if SessionUser.objects.filter(tenant=self.session_tenant, is_active=True, user__email__iexact=email).exists():
            messages.warning(request, f"{email} ist bereits Mitglied dieses Mandanten.")
            return redirect("session:users", tenant_slug=self.session_tenant.slug)

        # Alle anderen – mit oder ohne Konto – erhalten eine Einladung und treten erst mit der Annahme bei
        # (angemeldet mit genau dieser Adresse). Niemand wird ungefragt aufgenommen, und die Meldung verrät
        # nicht, ob es zu der Adresse schon ein Konto gibt.

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
        # Gemeinsames Mail-Layout, Standardversand, Absendername nennt den Mandanten (#239)
        send_user_invitation(invitation)


class UserRolesUpdateView(SessionViewMixin, View):
    """Rollen eines Benutzers zuweisen/entziehen."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, session_user_id):
        target = get_object_or_404(SessionUser, pk=session_user_id, tenant=self.session_tenant)
        role_ids = request.POST.getlist("roles")
        roles = list(SessionRole.objects.filter(id__in=role_ids, tenant=self.session_tenant))

        # Keine Rechteausweitung: Jede hinzugefügte oder entzogene Rolle muss im eigenen Umfang liegen
        if not roles_within_scope(self.session_user, set(target.roles.all()) ^ set(roles)):
            messages.error(request, OUTSIDE_SCOPE)
            return redirect("session:users", tenant_slug=tenant_slug)

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

        from .. import audit

        old_roles = list(target.roles.all())
        target.roles.set(roles)
        # Rollen- und Rechteänderung direkt protokollieren (Issue #221)
        audit.log_role_assignment(target, old_roles, roles, request=request, user=self.session_user)
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

        # Keine Rechteausweitung: Reaktivieren gibt die Rollen des Kontos zurück
        if not roles_within_scope(self.session_user, target.roles.all()):
            messages.error(
                request,
                "Konten mit Rollen, deren Rechte Sie selbst nicht haben, aktiviert oder deaktiviert nur ein Administrator.",
            )
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


class InvitationResendView(SessionViewMixin, View):
    """Offene Einladung erneut senden; die Gültigkeit läuft ab jetzt neu (#239)."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, invitation_id):
        invitation = get_object_or_404(
            SessionInvitation,
            pk=invitation_id,
            tenant=self.session_tenant,
            accepted_at__isnull=True,
        )
        if resend_user_invitation(invitation):
            messages.success(request, f"Einladung an {invitation.email} wurde erneut versendet.")
        else:
            messages.error(request, f"Die Einladung an {invitation.email} konnte nicht versendet werden.")
        return redirect("session:users", tenant_slug=self.session_tenant.slug)


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
        invitation = SessionInvitation.find_by_token(token, SessionInvitation.objects.select_related("tenant"))
        if invitation is None or not invitation.is_valid:
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
            if not user.email_verified:
                # Der Einladungslink kam per E-Mail – die Adresse ist damit bestätigt
                user.email_verified = True
                user.save(update_fields=["email_verified"])
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
                # Der Einladungslink kam per E-Mail – die Adresse ist damit bestätigt
                email_verified=True,
            )
            login(request, user)

        from .. import audit

        session_user, created = SessionUser.objects.get_or_create(user=user, tenant=invitation.tenant)
        session_user.is_active = True
        session_user.save()
        old_roles = [] if created else list(session_user.roles.all())
        new_roles = list(invitation.roles.all())
        session_user.roles.set(new_roles)
        # Rollen aus der Einladung (Issue #221); vergeben hat sie die einladende Person
        audit.log_role_assignment(
            session_user,
            old_roles,
            new_roles,
            request=request,
            user=session_user,
            reason="Einladung angenommen",
        )

        invitation.accepted_at = timezone.now()
        invitation.accepted_by = user
        invitation.save(update_fields=["accepted_at", "accepted_by"])

        messages.success(request, f"Willkommen im Sitzungsdienst von {invitation.tenant.name}.")
        return redirect("session:dashboard", tenant_slug=invitation.tenant.slug)
