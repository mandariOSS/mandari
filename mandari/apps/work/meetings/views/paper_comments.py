# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.common.mixins import WorkViewMixin

from .. import consumers
from ..models import (
    PaperComment,
)
from .serializers import serialize_paper_comment_as_note

# =============================================================================
# PAPER COMMENTS (unverändert, gremienübergreifend)
# =============================================================================


class PaperCommentAPIView(WorkViewMixin, View):
    """API endpoint for comments on OParl Papers (cross-committee collaboration)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        from insight_core.models import OParlPaper

        paper_id = self.kwargs.get("paper_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        paper = get_object_or_404(OParlPaper, id=paper_id)
        visible_comments = PaperComment.get_visible_comments_for_paper(paper, membership)

        return JsonResponse(
            {
                "comments": [
                    {
                        "id": str(c.id),
                        "content": c.get_content_decrypted(),
                        "visibility": c.visibility,
                        "visibility_display": c.get_visibility_display(),
                        "is_recommendation": c.is_recommendation,
                        "author": c.author.user.get_display_name(),
                        "organization": c.organization.name,
                        "is_own": c.author == membership,
                        "is_own_org": c.organization == membership.organization,
                        "created_at": c.created_at.isoformat(),
                    }
                    for c in visible_comments
                ]
            }
        )

    def post(self, request, *args, **kwargs):
        from insight_core.models import OParlPaper

        paper_id = self.kwargs.get("paper_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        paper = get_object_or_404(OParlPaper, id=paper_id)
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        content = data.get("content", "").strip()
        if not content:
            return JsonResponse({"error": "Content required"}, status=400)

        visibility = data.get("visibility", "organization")
        if visibility not in dict(PaperComment.VISIBILITY_CHOICES):
            visibility = "organization"
        is_recommendation = data.get("is_recommendation", False) in [True, "true", "1", "on"]

        comment = PaperComment(
            paper=paper,
            organization=organization,
            author=membership,
            visibility=visibility,
            is_recommendation=is_recommendation,
        )
        comment.set_content_encrypted(content)
        comment.save()

        # Echtzeit-Broadcast in die (org, paper)-Gruppe; Polling bleibt Fallback.
        # Private Kommentare NICHT broadcasten (nur für den Autor sichtbar).
        if visibility != "private":
            consumers.broadcast_preparation_event(
                organization.id,
                {
                    "type": "comment",
                    "event": "created",
                    "comment": serialize_paper_comment_as_note(comment, membership),
                },
                paper_id=paper.id,
            )

        return JsonResponse(
            {
                "success": True,
                "comment": {
                    "id": str(comment.id),
                    "content": content,
                    "visibility_display": comment.get_visibility_display(),
                    "is_recommendation": comment.is_recommendation,
                    "author": membership.user.get_display_name(),
                    "organization": organization.name,
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        comment_id = self.kwargs.get("comment_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        comment = get_object_or_404(PaperComment, id=comment_id, author=membership)
        paper_id = comment.paper_id
        organization_id = comment.organization_id
        comment.delete()
        consumers.broadcast_preparation_event(
            organization_id,
            {"type": "comment", "event": "deleted", "comment_id": str(comment_id)},
            paper_id=paper_id,
        )
        return JsonResponse({"success": True})
