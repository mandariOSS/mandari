# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommentare zu OParl-Vorlagen (gremienübergreifende Zusammenarbeit).
"""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlPaper

from .. import selectors, services
from ..serializers import decrypted, serialize_paper_comment
from ..services import PreparationError
from ._helpers import error_response, request_payload, unauthorized


class PaperCommentAPIView(WorkViewMixin, View):
    """API endpoint for comments on OParl Papers (cross-committee collaboration)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        paper = get_object_or_404(OParlPaper, id=self.kwargs["paper_id"])
        comments = selectors.visible_paper_comments(paper, self.membership)
        return JsonResponse({"comments": [serialize_paper_comment(c, self.membership) for c in comments]})

    def post(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        paper = get_object_or_404(OParlPaper, id=self.kwargs["paper_id"])
        try:
            comment = services.create_paper_comment(self.organization, paper, self.membership, request_payload(request))
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse(
            {
                "success": True,
                "comment": {
                    "id": str(comment.id),
                    "content": decrypted(comment, "content"),
                    "visibility_display": comment.get_visibility_display(),
                    "is_recommendation": comment.is_recommendation,
                    "author": self.membership.user.get_display_name(),
                    "organization": self.organization.name,
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        if not services.delete_paper_comment(self.membership, self.kwargs["comment_id"]):
            raise Http404
        return JsonResponse({"success": True})
