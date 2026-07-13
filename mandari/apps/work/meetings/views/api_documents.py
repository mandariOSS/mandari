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

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlAgendaItem, OParlMeeting

from ..models import (
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
)
from ._helpers import is_pdf_file
from .serializers import serialize_file_annotation


class SupplementaryDocumentAPIView(WorkViewMixin, View):
    """API: Ergänzende Dokumente (Links + Uploads + OParl-Referenzen)."""

    permission_required = "meetings.prepare"

    @staticmethod
    def _preview_info(doc):
        """
        Vorschau-Informationen einer Anlage (Muster RIS-Inline-Vorschau).

        PDF-Uploads werden über ihre Media-URL im iframe angezeigt
        (Anmerkungs-Anker "doc"), OParl-Referenzen über den file_proxy
        (Anmerkungs-Anker "oparl").
        """
        if doc.document_type == "file" and doc.file and is_pdf_file(doc.mime_type, doc.filename, doc.title):
            return {
                "is_pdf": True,
                "preview_kind": "doc",
                "preview_id": str(doc.id),
                "preview_url": doc.display_url,
            }
        if doc.document_type == "oparl" and doc.oparl_file_id:
            f = doc.oparl_file
            if is_pdf_file(f.mime_type, f.file_name, f.name):
                return {
                    "is_pdf": True,
                    "preview_kind": "oparl",
                    "preview_id": str(f.id),
                    "preview_url": reverse("insight_core:insight:file_proxy", args=[f.id]),
                }
        return {"is_pdf": False, "preview_kind": None, "preview_id": None, "preview_url": ""}

    def get(self, request, *args, **kwargs):
        from django.db.models import Count

        item_id = self.kwargs.get("item_id")
        organization = self.organization

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)
        # TOP-Anhänge + über Gremien geteilte Vorlagen-Anhänge der eigenen Org
        docs = list(AgendaSupplementaryDocument.visible_for_item(organization, agenda_item))

        # Anmerkungs-Zähler (beide Anker-Typen)
        doc_counts = {}
        oparl_counts = {}
        if docs:
            doc_counts = {
                row["supplementary_document"]: row["c"]
                for row in FileAnnotation.objects.filter(
                    organization=organization, supplementary_document_id__in=[d.id for d in docs]
                )
                .values("supplementary_document")
                .annotate(c=Count("id"))
            }
            oparl_ids = [d.oparl_file_id for d in docs if d.oparl_file_id]
            if oparl_ids:
                oparl_counts = {
                    row["oparl_file"]: row["c"]
                    for row in FileAnnotation.objects.filter(organization=organization, oparl_file_id__in=oparl_ids)
                    .values("oparl_file")
                    .annotate(c=Count("id"))
                }

        def annotation_count(d):
            if d.document_type == "oparl" and d.oparl_file_id:
                return oparl_counts.get(d.oparl_file_id, 0)
            return doc_counts.get(d.id, 0)

        return JsonResponse(
            {
                "documents": [
                    {
                        "id": str(d.id),
                        "title": d.title,
                        "url": d.display_url,
                        "document_type": d.document_type,
                        "description": d.description,
                        "added_by": d.added_by.user.get_display_name(),
                        "created_at": d.created_at.isoformat(),
                        "paper_id": str(d.paper_id) if d.paper_id else None,
                        "share_across_committees": d.share_across_committees,
                        "is_from_other_item": d.agenda_item_id != agenda_item.id,
                        "annotations": annotation_count(d),
                        **self._preview_info(d),
                    }
                    for d in docs
                ]
            }
        )

    @staticmethod
    def _resolve_paper_anchor(agenda_item, paper_id, share_flag):
        """
        Vorlagen-Anker validieren: Die Vorlage muss tatsächlich von diesem
        TOP beraten werden (sonst könnte man beliebige Papers verknüpfen).

        Returns (paper, share_across_committees) oder (None, False).
        """
        if not paper_id:
            return None, False
        papers = {str(p.id): p for p in agenda_item.get_papers()}
        paper = papers.get(str(paper_id))
        if paper is None:
            return None, False
        return paper, share_flag in [True, "true", "1", "on"]

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists() or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Unterstützt sowohl JSON (Links) als auch Multipart (Uploads)
        if request.content_type and "multipart" in request.content_type:
            return self._handle_file_upload(request, organization, membership, agenda_item)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        doc_type = data.get("document_type", "link")
        title = data.get("title", "").strip()

        if not title:
            return JsonResponse({"error": "Titel erforderlich"}, status=400)

        # Optionaler Anker an der VORLAGE (statt nur am TOP)
        paper, share_across = self._resolve_paper_anchor(
            agenda_item, data.get("paper_id"), data.get("share_across_committees", False)
        )

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            paper=paper,
            share_across_committees=share_across,
            document_type=doc_type,
            title=title,
            url=data.get("url", ""),
            description=data.get("description", ""),
        )

        MeetingPreparation.record_activity(organization, meeting, membership)

        return JsonResponse(
            {
                "success": True,
                "document": {
                    "id": str(doc.id),
                    "title": doc.title,
                    "url": doc.display_url,
                    "document_type": doc.document_type,
                    "paper_id": str(doc.paper_id) if doc.paper_id else None,
                    "share_across_committees": doc.share_across_committees,
                    "annotations": 0,
                    **self._preview_info(doc),
                },
            }
        )

    def _handle_file_upload(self, request, organization, membership, agenda_item):
        """Datei-Upload verarbeiten."""
        uploaded_file = request.FILES.get("file")
        title = request.POST.get("title", "").strip()

        if not uploaded_file:
            return JsonResponse({"error": "Keine Datei"}, status=400)

        if not title:
            title = uploaded_file.name

        # Max 50 MB
        if uploaded_file.size > 50 * 1024 * 1024:
            return JsonResponse({"error": "Datei zu groß (max. 50 MB)"}, status=400)

        # Optionaler Anker an der VORLAGE (statt nur am TOP)
        paper, share_across = self._resolve_paper_anchor(
            agenda_item, request.POST.get("paper_id"), request.POST.get("share_across_committees", False)
        )

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            paper=paper,
            share_across_committees=share_across,
            document_type="file",
            title=title,
            file=uploaded_file,
            filename=uploaded_file.name,
            mime_type=uploaded_file.content_type or "",
            file_size=uploaded_file.size,
            description=request.POST.get("description", ""),
        )

        MeetingPreparation.record_activity(organization, membership=membership, meeting=agenda_item.meeting)

        return JsonResponse(
            {
                "success": True,
                "document": {
                    "id": str(doc.id),
                    "title": doc.title,
                    "url": doc.display_url,
                    "document_type": "file",
                    "paper_id": str(doc.paper_id) if doc.paper_id else None,
                    "share_across_committees": doc.share_across_committees,
                    "annotations": 0,
                    **self._preview_info(doc),
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        doc_id = self.kwargs.get("doc_id") or self.kwargs.get("link_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        doc = get_object_or_404(AgendaSupplementaryDocument, id=doc_id, added_by=membership)
        if doc.file:
            doc.file.delete(save=False)
        doc.delete()
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
        from django.db.models import Q

        from insight_core.models import OParlFile

        anchor_type = self.kwargs.get("anchor_type")
        file_id = self.kwargs.get("file_id")
        organization = self.organization

        if anchor_type == "oparl":
            bodies = organization.get_all_bodies() if organization else None
            if bodies is None or not bodies.exists():
                raise Http404
            file_obj = get_object_or_404(
                OParlFile.objects.filter(
                    Q(body__in=bodies) | Q(paper__body__in=bodies) | Q(meeting__body__in=bodies)
                ).distinct(),
                id=file_id,
            )
            return {"oparl_file": file_obj}
        if anchor_type == "doc":
            doc = get_object_or_404(AgendaSupplementaryDocument, id=file_id, organization=organization)
            return {"supplementary_document": doc}
        raise Http404

    def get(self, request, *args, **kwargs):
        membership = self.membership
        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        anchor = self._resolve_anchor()
        annotations = list(
            FileAnnotation.objects.filter(organization=self.organization, **anchor).select_related(
                "author", "author__user"
            )
        )
        return JsonResponse(
            {
                "annotations": [serialize_file_annotation(a, membership) for a in annotations],
                "count": len(annotations),
            }
        )

    def post(self, request, *args, **kwargs):
        membership = self.membership
        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        anchor = self._resolve_anchor()
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        content = (data.get("content") or "").strip()
        if not content:
            return JsonResponse({"error": "Inhalt erforderlich"}, status=400)

        try:
            page = max(1, int(data.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        annotation = FileAnnotation(
            organization=self.organization,
            author=membership,
            page=page,
            **anchor,
        )
        annotation.set_content_encrypted(content)
        annotation.save()

        count = FileAnnotation.objects.filter(organization=self.organization, **anchor).count()
        return JsonResponse(
            {
                "success": True,
                "annotation": serialize_file_annotation(annotation, membership),
                "count": count,
            }
        )

    def delete(self, request, *args, **kwargs):
        membership = self.membership
        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        annotation_id = self.kwargs.get("annotation_id")
        # Org-Grenze strikt: fremde Organisationen sehen die Anmerkung nicht (404)
        annotation = get_object_or_404(FileAnnotation, id=annotation_id, organization=self.organization)
        if annotation.author_id != membership.id:
            return JsonResponse({"error": "Nur der Autor darf löschen"}, status=403)
        annotation.delete()
        return JsonResponse({"success": True})
