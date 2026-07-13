"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import FormView, TemplateView, View

from ..models import (
    OParlMembership,
    OParlPerson,
    PublicQuestion,
)
from ._helpers import get_active_body
from .persons import COUNCIL_ROLES

# =============================================================================
# Öffentliche Fragen (Ratsfragen)
# =============================================================================


class AskQuestionView(FormView):
    """Formular zum Stellen einer öffentlichen Frage an ein Ratsmitglied."""

    template_name = "pages/persons/ask_question.html"

    def get_form_class(self):
        from ..forms import PublicQuestionForm

        return PublicQuestionForm

    def dispatch(self, request, *args, **kwargs):
        self.active_body = get_active_body(request)
        self.person = get_object_or_404(OParlPerson, pk=kwargs["pk"])
        if self.active_body and self.person.body_id != self.active_body.id:
            raise Http404
        if not self._is_council_member(self.person):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["person"] = self.person
        return context

    def form_valid(self, form):
        from ..services.question_service import check_rate_limit, send_verification_email

        # Rate Limiting
        email = form.cleaned_data["questioner_email"]
        if not check_rate_limit(email):
            form.add_error(
                None,
                "Sie haben heute bereits zu viele Fragen eingereicht. Bitte versuchen Sie es morgen erneut.",
            )
            return self.form_invalid(form)

        question = form.save(commit=False)
        question.recipient = self.person
        question.body = self.person.body
        question.status = "unverified"
        question.save()

        send_verification_email(question)

        return redirect("insight_core:insight:question_submitted")

    def _is_council_member(self, person):
        today = timezone.now().date()
        return (
            OParlMembership.objects.filter(
                person=person,
                organization__name="Rat",
                role__in=COUNCIL_ROLES,
            )
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .exists()
        )


class VerifyQuestionView(View):
    """E-Mail-Verifizierung einer eingereichten Frage."""

    def get(self, request, token):
        question = get_object_or_404(PublicQuestion, verification_token=token, status="unverified")
        question.status = "pending"
        question.save(update_fields=["status", "updated_at"])
        return render(request, "pages/questions/verified.html", {"question": question})


class AnswerQuestionView(FormView):
    """Antwort-Formular für Ratsmitglieder (Token-basiert, kein Login nötig)."""

    template_name = "pages/questions/answer_form.html"

    def get_form_class(self):
        from ..forms import PublicAnswerForm

        return PublicAnswerForm

    def dispatch(self, request, *args, **kwargs):
        self.question = get_object_or_404(
            PublicQuestion,
            answer_token=kwargs["token"],
            status="published",
            answer_status="none",
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["question"] = self.question
        return context

    def form_valid(self, form):
        self.question.answer_text = form.cleaned_data["answer_text"]
        self.question.answered_at = timezone.now()
        self.question.answer_status = "pending"
        self.question.save(
            update_fields=[
                "answer_text",
                "answered_at",
                "answer_status",
                "updated_at",
            ]
        )
        return render(
            self.request,
            "pages/questions/answer_submitted.html",
            {"question": self.question},
        )


class QuestionSubmittedView(TemplateView):
    """Bestätigungsseite nach Absenden einer Frage."""

    template_name = "pages/questions/submitted.html"
