# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anonyme Rückmeldung am Seitenende: „War diese Seite hilfreich? Ja / Nein“, optional ein Satz.

Ein Endpunkt für beide Schritte (POST): Antwort speichern, danach optional den Satz an die Antwort
hängen. Mit HTMX kommt nur der Teil für ``#rueckmeldung-inhalt`` zurück (immer Status 200, HTMX tauscht
nur 2xx ein); ohne JavaScript eine eigene Seite mit Rückweg. Was gespeichert wird und was nicht:
``services/page_feedback.py``.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ..models import PageFeedback
from ..services import page_feedback as service

TEMPLATE = "partials/page_feedback.html"

MESSAGE_INVALID = "Die Rückmeldung konnte nicht gespeichert werden. Bitte laden Sie die Seite neu."
MESSAGE_LIMIT = "Gerade kommen sehr viele Rückmeldungen an. Bitte versuchen Sie es später noch einmal."
MESSAGE_TOO_LONG = f"Bitte höchstens {PageFeedback.COMMENT_MAX_LENGTH} Zeichen."


class FeedbackForm(forms.Form):
    page_type = forms.CharField(max_length=50)
    path = forms.CharField(max_length=255, required=False)
    body = forms.CharField(max_length=64, required=False)
    helpful = forms.ChoiceField(choices=[("ja", "Ja"), ("nein", "Nein")])
    website = forms.CharField(required=False)  # Honeypot: Menschen sehen das Feld nicht


class FeedbackCommentForm(forms.Form):
    token = forms.CharField(max_length=200)
    path = forms.CharField(max_length=255, required=False)
    helpful = forms.ChoiceField(choices=[("ja", "Ja"), ("nein", "Nein")], required=False)
    comment = forms.CharField(max_length=PageFeedback.COMMENT_MAX_LENGTH, required=False)
    website = forms.CharField(required=False)


def _respond(request: HttpRequest, partial: str, context: dict[str, Any], status: int = 200) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        return render(request, f"{TEMPLATE}#{partial}", context)
    page = {
        **context,
        "feedback_partial": f"{TEMPLATE}#{partial}",
        "back_url": service.clean_path(str(context.get("path") or "")),
        "seo": {"robots": "noindex"},
    }
    return render(request, "pages/feedback.html", page, status=status)


@require_POST
def page_feedback(request: HttpRequest) -> HttpResponse:
    """Antwort (Ja/Nein) bzw. Ergänzung entgegennehmen."""
    if "token" in request.POST:
        return _comment(request)
    form = FeedbackForm(request.POST)
    if not form.is_valid() or form.cleaned_data["page_type"] not in service.PAGE_TYPES:
        return _respond(request, "hinweis", {"message": MESSAGE_INVALID}, status=400)
    data = form.cleaned_data
    helpful = data["helpful"] == "ja"
    context: dict[str, Any] = {"helpful": helpful, "path": data["path"], "token": ""}
    if data["website"]:
        # Bot: Antwort wie bei Menschen, gespeichert wird nichts
        return _respond(request, "danke", context)
    if service.rate_limited(request):
        return _respond(request, "hinweis", {"message": MESSAGE_LIMIT, "path": data["path"]}, status=429)
    feedback = service.record(page_type=data["page_type"], path=data["path"], body_id=data["body"], helpful=helpful)
    context["token"] = service.token_for(feedback)
    return _respond(request, "danke", context)


def _comment(request: HttpRequest) -> HttpResponse:
    form = FeedbackCommentForm(request.POST)
    if not form.is_valid():
        if "comment" in form.errors and "token" not in form.errors:
            # Zu lang: Formular mit Hinweis erneut zeigen, der Text bleibt erhalten
            retry = {
                "token": request.POST.get("token", ""),
                "path": request.POST.get("path", ""),
                "helpful": request.POST.get("helpful") == "ja",
                "comment": request.POST.get("comment", ""),
                "error": MESSAGE_TOO_LONG,
            }
            return _respond(request, "danke", retry, status=400)
        return _respond(request, "hinweis", {"message": MESSAGE_INVALID}, status=400)
    data = form.cleaned_data
    context: dict[str, Any] = {"path": data["path"]}
    if data["website"] or not service.clean_comment(data["comment"]):
        return _respond(request, "ergaenzt", context)
    if service.rate_limited(request):
        return _respond(request, "hinweis", {**context, "message": MESSAGE_LIMIT}, status=429)
    if not service.add_comment(data["token"], data["comment"]):
        return _respond(request, "hinweis", {**context, "message": MESSAGE_INVALID}, status=400)
    return _respond(request, "ergaenzt", context)
