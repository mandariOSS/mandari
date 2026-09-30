# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eingangsbestätigung einer Einreichung per E-Mail durch die Verwaltung (Issue #580).

Öffentlicher Link aus der Einreichungsmail, ohne Anmeldung. Das Token ist signiert und an den
Empfänger gebunden (``email_submission.check_token``); Aufrufe je IP-Adresse sind begrenzt,
ungültige Tokens zählen strenger. Ein bloßer Aufruf (GET, etwa durch Link-Vorschauen oder
Virenscanner) bestätigt nichts – erst der Klick (POST). Die Seite zeigt nur Titel, einreichende
Organisation und Zeitpunkt, keinen Antragsinhalt.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from .. import email_submission

TEMPLATE = "work/motions/public/submission_confirm.html"


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


class SubmissionConfirmView(View):
    http_method_names = ["get", "post"]

    def _render(self, request: HttpRequest, context: dict[str, Any], status: int = 200) -> HttpResponse:
        return _no_store(render(request, TEMPLATE, context, status=status))

    def _check(self, request: HttpRequest, token: str) -> tuple[email_submission.TokenCheck, HttpResponse | None]:
        if email_submission.rate_limited(request):
            return email_submission.TokenCheck("invalid"), self._render(request, {"state": "rate_limited"}, status=429)
        check = email_submission.check_token(token)
        if check.state != "ok" or check.recipient is None:
            email_submission.record_failure(request)
            return check, self._render(request, {"state": check.state}, status=404)
        return check, None

    def _context(self, recipient: Any, **extra: Any) -> dict[str, Any]:
        submission = recipient.submission
        return {
            "state": "ok",
            "recipient": recipient,
            "submission": submission,
            "motion_title": submission.motion.title,
            "organization_name": submission.motion.organization.name,
            **extra,
        }

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        check, error = self._check(request, token)
        if error is not None:
            return error
        return self._render(request, self._context(check.recipient))

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        check, error = self._check(request, token)
        if error is not None:
            return error
        recipient = check.recipient
        assert recipient is not None
        first = email_submission.confirm_receipt(recipient)
        return self._render(request, self._context(recipient, confirmed_now=first))
