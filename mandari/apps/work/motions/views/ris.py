# SPDX-License-Identifier: AGPL-3.0-or-later
"""Antrag bei der Verwaltung einreichen — Vorschau, Formular und Statusseite (Issue #40)."""

from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.session.services.application_service import ApplicationService

from .. import ris_submission
from ..models import Motion

APPLICATION_TYPE_CHOICES = [
    ("motion", "Antrag"),
    ("inquiry", "Anfrage"),
    ("resolution", "Resolution"),
    ("urgent", "Dringlichkeitsantrag"),
    ("amendment", "Änderungsantrag"),
    ("other", "Sonstiges"),
]


class MotionSubmitToAdministrationView(WorkViewMixin, TemplateView):
    """GET: Vorschau + Formular (oder Statusseite), POST: einreichen."""

    template_name = "work/motions/submit_ris.html"
    permission_required = "motions.submit_to_ris"

    def _get_motion(self):
        motion = get_object_or_404(
            Motion.objects.select_related("document_type", "session_application__tenant"),
            id=self.kwargs["motion_id"],
            organization=self.organization,
        )
        if not motion.can_edit(self.membership):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Kein Zugriff auf dieses Dokument.")
        return motion

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        motion = kwargs.get("motion") or self._get_motion()
        connection = ris_submission.get_connection(self.organization)
        usable, reason = ris_submission.connection_state(connection)
        allowed, block_reason = ris_submission.can_submit(motion, self.membership)

        context.update(
            {
                "active_nav": "documents",
                "motion": motion,
                "motion_content": motion.get_content_decrypted(),
                "connection": connection,
                "connection_usable": usable,
                "connection_reason": reason,
                "can_submit_now": usable and allowed,
                "block_reason": block_reason,
                "can_manage_connection": self.membership.has_permission("faction.manage"),
                "application": motion.session_application,
                "timeline": ris_submission.consultation_timeline(motion.session_application),
                "application_type_choices": APPLICATION_TYPE_CHOICES,
                "target_organizations": (
                    ApplicationService.get_target_organizations(connection.tenant) if connection else []
                ),
            }
        )
        if "form" not in context:
            context["form"] = kwargs.get("form") or ris_submission.build_prefill(motion)
        return context

    def post(self, request, *args, **kwargs):
        motion = self._get_motion()
        form = {
            "title": (request.POST.get("title") or "").strip(),
            "application_type": request.POST.get("application_type") or "motion",
            "resolution_proposal": (request.POST.get("resolution_proposal") or "").strip(),
            "justification": (request.POST.get("justification") or "").strip(),
            "financial_impact": (request.POST.get("financial_impact") or "").strip(),
            "co_signers": (request.POST.get("co_signers") or "").strip(),
            "is_urgent": request.POST.get("is_urgent") == "on",
            "urgency_reason": (request.POST.get("urgency_reason") or "").strip(),
            "target_organization_id": request.POST.get("target_organization") or "",
            "deadline": None,
            "deadline_raw": (request.POST.get("deadline") or "").strip(),
        }
        errors = []
        if form["deadline_raw"]:
            try:
                form["deadline"] = date.fromisoformat(form["deadline_raw"])
            except ValueError:
                errors.append("Der gewünschte Beratungstermin ist kein gültiges Datum.")
        if form["application_type"] not in dict(APPLICATION_TYPE_CHOICES):
            errors.append("Ungültige Antragsart.")
        if form["is_urgent"] and not form["urgency_reason"]:
            errors.append("Bitte die Dringlichkeit kurz begründen.")
        if not form["resolution_proposal"]:
            errors.append("Der Beschlussvorschlag darf nicht leer sein.")
        if not form["justification"]:
            errors.append("Die Begründung darf nicht leer sein.")
        if request.POST.get("confirm") != "on":
            errors.append("Bitte bestätigen, dass der Antrag verbindlich eingereicht werden soll.")

        if not errors:
            try:
                application = ris_submission.submit_motion(motion, self.membership, form)
            except ris_submission.SubmissionError as exc:
                errors.append(str(exc))
            else:
                messages.success(
                    request,
                    f"Antrag eingereicht. Eingangsnummer bei {application.tenant.name}: {application.reference}.",
                )
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        for error in errors:
            messages.error(request, error)
        context = self.get_context_data(motion=motion, form=form)
        return self.render_to_response(context)
