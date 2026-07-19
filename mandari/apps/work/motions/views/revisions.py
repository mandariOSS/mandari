# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..models import (
    Motion,
    MotionRevision,
)
from ._helpers import _broadcast_doc_reload

# =============================================================================
# Revision API Views (Version History)
# =============================================================================


class DocumentRevisionsAPIView(WorkViewMixin, View):
    """API endpoint listing all revisions for a document."""

    permission_required = "motions.view"
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_access)

    def get(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        if not motion.can_access(self.membership):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        revisions = motion.revisions.select_related("changed_by__user").order_by("-version")

        data = []
        for rev in revisions:
            data.append(
                {
                    "id": str(rev.id),
                    "version": rev.version,
                    "change_summary": rev.change_summary,
                    "changed_by": rev.changed_by.user.get_display_name(),
                    "created_at": rev.created_at.isoformat(),
                }
            )

        return JsonResponse({"success": True, "revisions": data})


class DocumentRevisionDetailAPIView(WorkViewMixin, View):
    """API endpoint returning the content of a specific revision."""

    permission_required = "motions.view"
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_access)

    def get(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        if not motion.can_access(self.membership):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        revision = get_object_or_404(MotionRevision, id=kwargs.get("revision_id"), motion=motion)

        return JsonResponse(
            {
                "success": True,
                "revision": {
                    "id": str(revision.id),
                    "version": revision.version,
                    "content": revision.get_content_decrypted(),
                    "change_summary": revision.change_summary,
                    "changed_by": revision.changed_by.user.get_display_name(),
                    "created_at": revision.created_at.isoformat(),
                },
            }
        )


class DocumentRevisionRestoreView(WorkViewMixin, View):
    """Restore a document to a specific revision."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
        revision = get_object_or_404(MotionRevision, id=kwargs.get("revision_id"), motion=motion)

        # Check edit permission
        is_author = motion.author == self.membership
        has_edit_all = self.membership.has_permission("motions.edit_all")
        if not is_author and not has_edit_all:
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        # Save current content as a new revision (safety net)
        current_version = motion.revisions.count() + 1
        safety_revision = MotionRevision(
            motion=motion,
            version=current_version,
            changed_by=self.membership,
            change_summary=f"Automatische Sicherung vor Wiederherstellung von v{revision.version}",
        )
        safety_revision.set_content_encrypted(motion.get_content_decrypted())
        safety_revision.save()

        # Restore the revision content
        restored_content = revision.get_content_decrypted()
        motion.set_content_encrypted(restored_content)
        # Kollaboration: Yjs-Zustand verwerfen, damit alle Clients nach dem
        # Reload frisch aus dem wiederhergestellten HTML seeden.
        motion.yjs_document = None
        motion.save()

        # Create another revision marking the restore
        restore_version = current_version + 1
        restore_revision = MotionRevision(
            motion=motion,
            version=restore_version,
            changed_by=self.membership,
            change_summary=f"Wiederhergestellt von Version {revision.version}",
        )
        restore_revision.set_content_encrypted(restored_content)
        restore_revision.save()

        # Alle verbundenen Kollaborations-Clients zum Neuladen auffordern
        # (Best-Effort — Restore selbst ist bereits erfolgt).
        _broadcast_doc_reload(motion, version=restore_version)

        return JsonResponse({"success": True, "version": restore_version})
