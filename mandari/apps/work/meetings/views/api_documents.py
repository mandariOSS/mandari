# SPDX-License-Identifier: AGPL-3.0-or-later
"""
JSON-APIs für ergänzende Dokumente (Links, Uploads, OParl-Referenzen) und
seitenbezogene Datei-Anmerkungen der Sitzungsvorbereitung.
"""

from django.http import Http404, JsonResponse
from django.views import View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..serializers import serialize_document, serialize_file_annotation, serialize_new_document
from ..services import PreparationError
from ._helpers import error_response, request_payload, unauthorized


class SupplementaryDocumentAPIView(WorkViewMixin, View):
    """API: Ergänzende Dokumente (Links + Uploads + OParl-Referenzen)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"])
        # TOP-Anhänge + über Gremien geteilte Vorlagen-Anhänge der eigenen Org
        docs = selectors.documents_with_annotation_counts(self.organization, agenda_item)
        return JsonResponse({"documents": [serialize_document(d, agenda_item.id, count) for d, count in docs]})

    def post(self, request, *args, **kwargs):
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None or not self.membership:
            return unauthorized()
        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"], meeting)

        try:
            # Unterstützt sowohl JSON (Links) als auch Multipart (Uploads)
            if request.content_type and "multipart" in request.content_type:
                doc = services.add_document_upload(
                    self.organization,
                    agenda_item,
                    self.membership,
                    request.FILES.get("file"),
                    title=request.POST.get("title", "").strip(),
                    paper_id=request.POST.get("paper_id"),
                    share_flag=request.POST.get("share_across_committees", False),
                    description=request.POST.get("description", ""),
                )
            else:
                doc = services.add_document_link(
                    self.organization, meeting, agenda_item, self.membership, request_payload(request)
                )
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse({"success": True, "document": serialize_new_document(doc)})

    def delete(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        doc_id = self.kwargs.get("doc_id") or self.kwargs.get("link_id")
        if not services.delete_document(self.membership, doc_id):
            raise Http404
        return JsonResponse({"success": True})


class FileAnnotationAPIView(WorkViewMixin, View):
    """
    API: Seitenbezogene Anmerkungen direkt an PDF-Dateien.

    Anker-Typen (anchor_type in der URL):
    - "oparl": OParlFile aus dem Ratsinformationssystem — Org-Grenze über die
      Körperschaften der Organisation (Datei muss zu einem verknüpften Body
      gehören, sonst 404)
    - "doc": eigene Anlage (AgendaSupplementaryDocument, organization=eigene
      Org, sonst 404)

    Anmerkungen sind org-weit sichtbar (wie Fraktionskommentare);
    DELETE darf nur der Autor (sonst 403).
    """

    permission_required = "meetings.prepare"

    def _resolve_anchor(self):
        """Anker auflösen; außerhalb der Org-Grenze wird 404 geworfen."""
        return selectors.resolve_file_anchor(self.organization, self.kwargs.get("anchor_type"), self.kwargs["file_id"])

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        annotations = selectors.file_annotations(self.organization, self._resolve_anchor())
        return JsonResponse(
            {
                "annotations": [serialize_file_annotation(a, self.membership) for a in annotations],
                "count": len(annotations),
            }
        )

    def post(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        anchor = self._resolve_anchor()
        try:
            annotation, count = services.add_file_annotation(
                self.organization, self.membership, anchor, request_payload(request)
            )
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse({"success": True, "annotation": annotation, "count": count})

    def delete(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        try:
            # Org-Grenze strikt: fremde Organisationen sehen die Anmerkung nicht (404)
            found = services.delete_file_annotation(self.organization, self.membership, self.kwargs["annotation_id"])
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        if not found:
            raise Http404
        return JsonResponse({"success": True})
