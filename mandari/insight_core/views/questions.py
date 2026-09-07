# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Ratsfragen (Abgeordnetenwatch-Stil) für Mandari Insight.

- Portal: alle Fragen einer Kommune mit Filtern, Statistik und Fraktions-Ranking
- Detailseite je Frage (teilbar, SEO)
- Einstieg „Frage stellen“ mit Auswahl der Mandatsträger:in
- Formular, E-Mail-Verifizierung, Antwort per Token-Link
"""

from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, FormView, ListView, TemplateView, View

from ..models import OParlOrganization, OParlPerson, PublicQuestion
from ..services import question_service
from ._helpers import ActiveBodyRequiredMixin, get_active_body

SORT_OPTIONS = [
    ("neu", "Neueste zuerst"),
    ("offen", "Am längsten offen"),
    ("beantwortet", "Zuletzt beantwortet"),
]


def _filter_querystring(request, drop=("page",)) -> str:
    params = request.GET.copy()
    for key in drop:
        params.pop(key, None)
    encoded = params.urlencode()
    return f"&{encoded}" if encoded else ""


class QuestionPortalView(ActiveBodyRequiredMixin, ListView):
    """Alle veröffentlichten Ratsfragen der aktiven Kommune."""

    template_name = "pages/questions/portal.html"
    context_object_name = "questions"
    paginate_by = 20

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return PublicQuestion.objects.none()
        qs = PublicQuestion.objects.filter(body=body, status="published").select_related("recipient")
        get = self.request.GET

        status = get.get("status", "")
        if status == "offen":
            qs = qs.exclude(answer_status="published")
        elif status == "beantwortet":
            qs = qs.filter(answer_status="published")

        topic = get.get("thema", "")
        if topic in dict(PublicQuestion.TOPIC_CHOICES):
            qs = qs.filter(topic=topic)

        person = get.get("person", "")
        if person:
            qs = qs.filter(recipient_id=person)

        faction = get.get("fraktion", "")
        if faction:
            today = timezone.now().date()
            qs = qs.filter(
                Q(recipient__memberships__organization_id=faction)
                & (Q(recipient__memberships__end_date__isnull=True) | Q(recipient__memberships__end_date__gte=today))
            ).distinct()

        q = get.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(subject__icontains=q)
                | Q(question_text__icontains=q)
                | Q(answer_text__icontains=q)
                | Q(recipient__name__icontains=q)
                | Q(recipient__family_name__icontains=q)
            )

        sort = get.get("sort", "neu")
        if sort == "offen":
            qs = qs.order_by("answer_status", "published_at")
        elif sort == "beantwortet":
            qs = qs.order_by("-answered_at", "-published_at")
        else:
            qs = qs.order_by("-published_at", "-created_at")
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        get = self.request.GET

        questions = list(context["questions"])
        faction_map = question_service.get_faction_map([q.recipient for q in questions])
        for q in questions:
            q.recipient_faction = faction_map.get(q.recipient_id)

        ranking = question_service.get_faction_ranking(body) if body else []
        context.update(
            {
                "questions": questions,
                "stats": question_service.get_body_stats(body) if body else {},
                "faction_ranking": ranking,
                "topic_counts": question_service.get_topic_counts(body) if body else [],
                "topic_choices": PublicQuestion.TOPIC_CHOICES,
                "sort_options": SORT_OPTIONS,
                "filter_status": get.get("status", ""),
                "filter_topic": get.get("thema", ""),
                "filter_faction": get.get("fraktion", ""),
                "filter_person": get.get("person", ""),
                "filter_q": get.get("q", "").strip(),
                "filter_sort": get.get("sort", "neu"),
                "has_filters": any(get.get(k) for k in ("status", "thema", "fraktion", "person", "q")),
                "filter_querystring": _filter_querystring(self.request),
                "top_recipients": question_service.get_top_recipients(body) if body else [],
            }
        )
        if get.get("person"):
            context["filter_person_obj"] = OParlPerson.objects.filter(id=get.get("person")).first()

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Ratsfragen",
            description=(
                "Öffentliche Fragen an Ratsmitglieder und ihre Antworten – "
                "transparent, nachvollziehbar und für alle sichtbar."
            ),
            body=body,
            keywords=["Ratsfragen", "Fragen an Ratsmitglieder", "Kommunalpolitik", "Transparenz"],
        ).to_dict()
        return context


class QuestionDetailView(DetailView):
    """Einzelne veröffentlichte Frage mit Antwort — eigene, teilbare URL."""

    model = PublicQuestion
    template_name = "pages/questions/detail.html"
    context_object_name = "question"

    def get_queryset(self):
        return PublicQuestion.objects.filter(status="published").select_related("recipient", "body")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        question = self.object
        person = question.recipient
        related = (
            PublicQuestion.objects.filter(recipient=person, status="published")
            .exclude(id=question.id)
            .order_by("-published_at")[:5]
        )
        context.update(
            {
                "person": person,
                "faction": question_service.get_faction(person),
                "council_role": question_service.get_council_role(person),
                "answer_stats": question_service.get_answer_stats(person),
                "related_questions": related,
                "can_ask": question_service.is_mandate_holder(person),
            }
        )

        from ..seo import get_question_seo

        context["seo"] = get_question_seo(question, self.request).to_dict()
        return context


class AskQuestionStartView(ActiveBodyRequiredMixin, TemplateView):
    """Einstieg: Mandatsträger:in auswählen, an die eine Frage gehen soll."""

    template_name = "pages/questions/start.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        q = self.request.GET.get("q", "").strip()

        persons = question_service.mandate_holders_queryset(body).annotate(
            question_count=Count("public_questions", filter=Q(public_questions__status="published"), distinct=True),
            answered_count=Count(
                "public_questions",
                filter=Q(public_questions__status="published", public_questions__answer_status="published"),
                distinct=True,
            ),
        )
        if q:
            persons = persons.filter(
                Q(name__icontains=q)
                | Q(family_name__icontains=q)
                | Q(given_name__icontains=q)
                | Q(memberships__organization__name__icontains=q)
            ).distinct()
        persons = list(persons.order_by("family_name", "given_name")[:300])
        faction_map = question_service.get_faction_map(persons)
        for person in persons:
            person.faction = faction_map.get(person.id)

        factions = OParlOrganization.objects.filter(id__in={org.id for org in faction_map.values()}).order_by("name")

        context.update({"persons": persons, "filter_q": q, "factions": factions})

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Frage stellen",
            description="Wählen Sie ein Ratsmitglied aus und stellen Sie Ihre öffentliche Frage.",
            body=body,
            robots="noindex, follow",
        ).to_dict()
        return context


class AskQuestionView(FormView):
    """Formular zum Stellen einer öffentlichen Frage an ein Ratsmitglied."""

    template_name = "pages/persons/ask_question.html"

    def get_form_class(self):
        from ..forms import PublicQuestionForm

        return PublicQuestionForm

    def dispatch(self, request, *args, **kwargs):
        self.active_body = get_active_body(request)
        self.person = get_object_or_404(OParlPerson, pk=kwargs["pk"], deleted=False)
        if self.active_body and self.person.body_id != self.active_body.id:
            raise Http404
        if not question_service.is_mandate_holder(self.person):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["person"] = self.person
        context["faction"] = question_service.get_faction(self.person)
        context["answer_stats"] = question_service.get_answer_stats(self.person)
        context["topic_choices"] = PublicQuestion.TOPIC_CHOICES

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title=f"Frage an {self.person.display_name}",
            description=f"Stellen Sie {self.person.display_name} eine öffentliche Frage.",
            body=self.person.body,
            robots="noindex, follow",
        ).to_dict()
        return context

    def form_valid(self, form):
        email = form.cleaned_data["questioner_email"]
        if not question_service.check_rate_limit(email):
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

        question_service.send_verification_email(question)
        return redirect("insight_core:insight:question_submitted")


class VerifyQuestionView(View):
    """E-Mail-Verifizierung einer eingereichten Frage."""

    def get(self, request, token):
        question = get_object_or_404(PublicQuestion, verification_token=token, status="unverified")
        question.status = "pending"
        question.save(update_fields=["status", "updated_at"])
        question_service.send_moderation_notification(question, kind="question")
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
        self.question.save(update_fields=["answer_text", "answered_at", "answer_status", "updated_at"])
        question_service.send_moderation_notification(self.question, kind="answer")
        return render(self.request, "pages/questions/answer_submitted.html", {"question": self.question})


class QuestionSubmittedView(TemplateView):
    """Bestätigungsseite nach Absenden einer Frage."""

    template_name = "pages/questions/submitted.html"
