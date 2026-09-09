# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil: Sichtbarkeit/Kontakt und „Meine Gremien“ (inkl. Fachgebiete).
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services


class ProfileVisibilityView(WorkViewMixin, TemplateView):
    """Visibility settings, bio, and contact preferences."""

    template_name = "work/profile/visibility.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "visibility"
        context.update(selectors.profile_settings(self.request.user))
        context["oparl_person"] = self.membership.oparl_person
        return context

    def post(self, request, *args, **kwargs):
        services.save_profile_visibility(
            request.user,
            bio=request.POST.get("bio", "").strip(),
            show_email=request.POST.get("show_email") == "on",
            show_phone=request.POST.get("show_phone") == "on",
            preferred_contact=request.POST.get("preferred_contact", "email"),
            contact_signal=request.POST.get("contact_signal", "").strip(),
        )
        messages.success(request, "Sichtbarkeitseinstellungen gespeichert.")
        return redirect("work:profile_visibility", org_slug=self.organization.slug)


class ProfileCommitteesView(WorkViewMixin, TemplateView):
    """Personal selection of followed committees ("Meine Gremien").

    Unlike the admin-assigned ``Membership.oparl_committees`` this list is
    freely editable by the member and only personalizes dashboard views.
    """

    template_name = "work/profile/committees.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "committees"
        context.update(selectors.profile_committee_context(self.organization, self.membership))
        # Fachgebiete (Themenkatalog der Organisation)
        context["org_topics"] = selectors.org_topics(self.organization)
        context["expertise_ids"] = selectors.expertise_ids(self.membership)
        return context

    def post(self, request, *args, **kwargs):
        # Fachgebiete speichern (eigenes Formular auf derselben Seite)
        if request.POST.get("action") == "save_expertise":
            services.update_member_expertise(
                self.organization, self.membership, request.POST.getlist("expertise_topics")
            )
            messages.success(request, "Fachgebiete gespeichert.")
            return redirect("work:profile_committees", org_slug=self.organization.slug)

        services.save_followed_committees(self.organization, self.membership, request.POST.getlist("committees"))
        messages.success(request, "Meine Gremien gespeichert.")
        return redirect("work:profile_committees", org_slug=self.organization.slug)
