# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antrag bei der Verwaltung einreichen — Vorschau, Formular und Statusseite (Issue #40).

Mit nutzbarer Einreichungsverbindung zu mandari Session geht der Antrag dorthin; sonst per E-Mail
an die gepflegten Verwaltungskontakte (Issue #580, ``email_submission``).
"""

from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.session.services.application_service import ApplicationService
from apps.work.sanitize import safe_editor_html

from .. import administration_feedback, email_submission, ris_submission
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
        # E-Mail-Weg (#580): nur ohne nutzbare Session-Verbindung und mit gepflegten Kontakten
        contacts = email_submission.contacts_for(self.organization)

        context.update(
            {
                "active_nav": "documents",
                "motion": motion,
                "motion_content": safe_editor_html(motion.get_content_decrypted()),
                "connection": connection,
                "connection_usable": usable,
                "connection_reason": reason,
                "can_submit_now": usable and allowed,
                "block_reason": block_reason,
                "can_manage_connection": self.membership.has_permission("faction.manage"),
                "application": motion.session_application,
                # Öffentlich zulässiger Rückmeldestand der Verwaltung (Issue #316)
                "feedback": administration_feedback.feedback_for(motion),
                "application_type_choices": APPLICATION_TYPE_CHOICES,
                "target_organizations": (
                    ApplicationService.get_target_organizations(connection.tenant) if connection else []
                ),
                "email_submission": email_submission.latest_submission(motion),
                "email_mode": not usable and bool(contacts),
                "email_contacts": contacts,
                # Dieselben Regeln wie beim Session-Weg (can_submit berücksichtigt Einreichungen per E-Mail)
                "email_can_submit": allowed,
                "email_block_reason": block_reason,
                "email_attachments": email_submission.planned_attachments(motion),
                "email_pdf_name": email_submission.pdf_filename(motion),
                "email_max_mb": email_submission.EMAIL_ATTACHMENTS_MAX_BYTES // (1024 * 1024),
                "can_manage_contacts": self.membership.has_permission("organization.edit"),
            }
        )
        if "email_form" not in context:
            # Nach einem abgelehnten POST steht das eingegebene Formular schon im Kontext (kwargs)
            context["email_form"] = email_submission.initial_form(motion, contacts)
        if "form" not in context:
            context["form"] = kwargs.get("form") or ris_submission.build_prefill(motion)
        # Anhänge, die mitgehen, und solche, die die Verwaltung nicht annimmt (#584)
        context["attachments_accepted"], context["attachments_rejected"] = ris_submission.attachment_preview(motion)
        return context

    def _post_email(self, request, motion, email_form):
        errors = []
        connection = ris_submission.get_connection(self.organization)
        usable, _reason = ris_submission.connection_state(connection)
        if usable:
            # Die Session-Verbindung hat Vorrang
            errors.append("Die Organisation ist mit mandari Session verbunden – bitte darüber einreichen.")
        if not email_form["confirmed"]:
            errors.append("Bitte bestätigen, dass der Antrag verbindlich eingereicht werden soll.")
        if not errors:
            try:
                submission = email_submission.submit_by_email(
                    motion,
                    self.membership,
                    contact_ids=email_form["contact_ids"],
                    subject=email_form["subject"],
                    message=email_form["message"],
                )
            except ris_submission.SubmissionError as exc:
                errors.append(str(exc))
            else:
                recipients = list(submission.recipients.all())
                delivered = [r for r in recipients if r.delivered]
                messages.success(
                    request,
                    "Antrag per E-Mail eingereicht an " + ", ".join(r.label for r in delivered) + ".",
                )
                missing = [r for r in recipients if not r.delivered]
                if missing:
                    messages.warning(
                        request,
                        "Nicht zugestellt an: "
                        + ", ".join(r.label for r in missing)
                        + ". Bitte dort direkt nachreichen.",
                    )
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        for error in errors:
            messages.error(request, error)
        context = self.get_context_data(motion=motion, email_form=email_form)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        motion = self._get_motion()
        email_form = email_submission.form_from_post(request.POST)
        if email_form is not None:
            return self._post_email(request, motion, email_form)
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
            skipped: list[str] = []
            try:
                application = ris_submission.submit_motion(motion, self.membership, form, skipped=skipped)
            except ris_submission.SubmissionError as exc:
                errors.append(str(exc))
            else:
                anhaenge = application.files.count()
                hinweis = (
                    f" Mit {anhaenge} Anhang." if anhaenge == 1 else (f" Mit {anhaenge} Anhängen." if anhaenge else "")
                )
                messages.success(
                    request,
                    f"Antrag eingereicht. Eingangsnummer bei {application.tenant.name}: {application.reference}.{hinweis}",
                )
                if skipped:
                    # Namen und Gründe stammen aus festen Texten der Prüfung, nicht aus Ausnahmen
                    messages.warning(
                        request,
                        "Nicht übermittelt: " + "; ".join(skipped) + ". Bitte bei Bedarf direkt nachreichen.",
                    )
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        for error in errors:
            messages.error(request, error)
        context = self.get_context_data(motion=motion, form=form)
        return self.render_to_response(context)
