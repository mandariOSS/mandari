# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# PROFILE: VISIBILITY & CONTACT
# =============================================================================


class ProfileVisibilityView(WorkViewMixin, TemplateView):
    """Visibility settings, bio, and contact preferences."""

    template_name = "work/profile/visibility.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "visibility"

        # Load profile settings from User.settings JSON
        user_settings = self.request.user.settings or {}
        profile = user_settings.get("profile", {})

        context["bio"] = profile.get("bio", "")
        context["show_email"] = profile.get("show_email", True)
        context["show_phone"] = profile.get("show_phone", False)
        context["preferred_contact"] = profile.get("preferred_contact", "email")
        context["contact_signal"] = profile.get("contact_signal", "")
        context["oparl_person"] = self.membership.oparl_person

        return context

    def post(self, request, *args, **kwargs):
        user = request.user
        settings = user.settings or {}
        profile = settings.get("profile", {})

        profile["bio"] = request.POST.get("bio", "").strip()[:500]
        profile["show_email"] = request.POST.get("show_email") == "on"
        profile["show_phone"] = request.POST.get("show_phone") == "on"
        profile["preferred_contact"] = request.POST.get("preferred_contact", "email")
        profile["contact_signal"] = request.POST.get("contact_signal", "").strip()[:100]

        settings["profile"] = profile
        user.settings = settings
        user.save(update_fields=["settings"])

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

        from django.db.models import Q

        from insight_core.models import OParlOrganization

        bodies = self.organization.get_all_bodies()
        if bodies.exists():
            today = timezone.now().date()
            context["available_committees"] = (
                OParlOrganization.objects.filter(body__in=bodies)
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .select_related("body")
                .order_by("name")
            )
        else:
            context["available_committees"] = OParlOrganization.objects.none()

        context["followed_ids"] = set(self.membership.followed_organizations.values_list("id", flat=True))
        context["assigned_committees"] = self.membership.oparl_committees.all().order_by("name")
        context["show_body_names"] = self.organization.has_multiple_bodies

        # Fachgebiete (Themenkatalog der Organisation)
        from apps.tenants.models import Topic

        context["org_topics"] = Topic.objects.filter(organization=self.organization)
        context["expertise_ids"] = set(self.membership.expertise_topics.values_list("id", flat=True))

        return context

    def post(self, request, *args, **kwargs):
        from insight_core.models import OParlOrganization

        # Fachgebiete speichern (eigenes Formular auf derselben Seite)
        if request.POST.get("action") == "save_expertise":
            from apps.tenants.models import Topic

            topic_ids = request.POST.getlist("expertise_topics")
            topics = Topic.objects.filter(id__in=topic_ids, organization=self.organization)
            self.membership.expertise_topics.set(topics)
            messages.success(request, "Fachgebiete gespeichert.")
            return redirect("work:profile_committees", org_slug=self.organization.slug)

        committee_ids = request.POST.getlist("committees")
        bodies = self.organization.get_all_bodies()
        committees = OParlOrganization.objects.filter(id__in=committee_ids, body__in=bodies)
        self.membership.followed_organizations.set(committees)

        messages.success(request, "Meine Gremien gespeichert.")
        return redirect("work:profile_committees", org_slug=self.organization.slug)
