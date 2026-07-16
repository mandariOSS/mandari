# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..forms import (
    MotionCommentForm,
    MotionForm,
    MotionStatusForm,
)
from ..models import (
    DocumentFolder,
    Motion,
    MotionApproval,
    MotionRevision,
    MotionShare,
    MotionTemplate,
    MotionType,
    OrganizationLetterhead,
)
from ..services import MotionAIService
from ._helpers import _flatten_folder_tree, _get_org_folder_or_404


class MotionCreateView(WorkViewMixin, TemplateView):
    """Create a new document (Step 1: Basic data)."""

    template_name = "work/motions/create.html"
    permission_required = "motions.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"
        context["form"] = MotionForm(organization=self.organization)
        context["ai_available"] = MotionAIService(
            organization=self.organization, user_id=self.request.user.id
        ).is_available()

        # Get custom document types for this organization
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )

        # Get templates
        context["templates"] = (
            MotionTemplate.objects.filter(organization=self.organization, is_active=True)
            .select_related("motion_type", "letterhead")
            .order_by("-is_default", "name")
        )

        # Get letterheads
        context["letterheads"] = OrganizationLetterhead.objects.filter(
            organization=self.organization, is_active=True
        ).order_by("-is_default", "name")

        # Aktuell gewählter Ordner aus der Liste (?ordner=<id>):
        # neues Dokument landet dort
        folder_param = self.request.GET.get("ordner")
        context["current_folder"] = _get_org_folder_or_404(self.organization, folder_param) if folder_param else None

        return context

    def post(self, request, *args, **kwargs):
        title = request.POST.get("title", "").strip()
        summary = request.POST.get("summary", "").strip()

        if not title:
            messages.error(request, "Titel ist erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Create the motion
        motion = Motion(
            organization=self.organization,
            author=self.membership,
            title=title,
            summary=summary,
            status="draft",
            responsible=self.membership,  # Standard: Federführung = Autor
        )

        # Ordner-Ablage: Dokument im aktuell gewählten Ordner anlegen
        folder_id = request.POST.get("folder", "").strip()
        if folder_id:
            motion.folder = _get_org_folder_or_404(self.organization, folder_id)

        # Handle document type (new system)
        document_type_id = request.POST.get("document_type")
        if document_type_id:
            try:
                motion.document_type = MotionType.objects.get(id=document_type_id, organization=self.organization)
            except MotionType.DoesNotExist:
                pass

        # Handle legacy type (fallback)
        motion_type = request.POST.get("motion_type", "motion")
        if motion_type in dict(Motion.LEGACY_TYPE_CHOICES):
            motion.motion_type = motion_type

        # Handle template
        template_id = request.POST.get("template")
        if template_id:
            try:
                template = MotionTemplate.objects.get(id=template_id, organization=self.organization)
                motion.template = template

                # Use template's letterhead if set
                if template.letterhead:
                    motion.letterhead = template.letterhead

                # Pre-fill content from template (placeholders replaced)
                if template.content_template:
                    from ..export_service import replace_placeholders

                    motion.set_content_encrypted(replace_placeholders(template.content_template, motion))
            except MotionTemplate.DoesNotExist:
                pass

        # Handle letterhead (overrides template letterhead)
        letterhead_id = request.POST.get("letterhead")
        if letterhead_id:
            try:
                motion.letterhead = OrganizationLetterhead.objects.get(id=letterhead_id, organization=self.organization)
            except OrganizationLetterhead.DoesNotExist:
                pass

        motion.save()

        # Standard-Checkliste des Dokumenttyps anlegen
        motion.apply_default_checklist()

        messages.success(request, "Dokument erstellt. Sie können jetzt den Inhalt bearbeiten.")
        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)


class DocumentEditorView(WorkViewMixin, TemplateView):
    """Merged document editor view (combines former detail + edit pages, Google Docs style)."""

    template_name = "work/motions/editor.html"
    permission_required = "motions.view"
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_access)

    def _get_access_level(self, motion):
        """Determine access level: 'view', 'comment', 'edit', or 'admin'."""
        if not motion.can_access(self.membership):
            return "none"

        # Gäste: Level ergibt sich ausschließlich aus der persönlichen Freigabe
        if getattr(self.membership, "is_guest", False):
            level = motion.get_guest_share_level(self.membership)
            if level is None:
                return "none"
            if level == "admin":
                level = "edit"  # Gäste erhalten nie Verwaltungsrechte
            if level == "edit" and motion.status not in ["draft", "review", "internal_review", "external_review"]:
                level = "comment"
            return level

        is_author = motion.author == self.membership
        has_edit_all = self.membership.has_permission("motions.edit_all")
        has_edit = self.membership.has_permission("motions.edit")
        has_comment = self.membership.has_permission("motions.comment")
        editable_status = motion.status in ["draft", "review", "internal_review", "external_review"]

        if is_author and editable_status:
            return "admin"
        if has_edit_all and editable_status:
            return "edit"
        if has_edit and is_author and editable_status:
            return "edit"
        if has_comment:
            return "comment"
        return "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        access_level = self._get_access_level(motion)
        if access_level == "none":
            raise PermissionDenied("Keine Berechtigung für dieses Dokument.")

        context["motion"] = motion
        context["motion_content"] = motion.get_content_decrypted()
        context["access_level"] = access_level
        context["is_author"] = motion.author == self.membership
        context["can_edit"] = access_level in ("edit", "admin")
        context["can_comment"] = access_level in ("comment", "edit", "admin")

        # Comments
        comments = (
            motion.comments.filter(parent__isnull=True)
            .select_related("author__user")
            .prefetch_related("replies__author__user")
            .order_by("created_at")
        )
        context["comments"] = comments

        # Serialize inline comments (with mark_id) as JSON for click-on-mark popup
        inline_comments_data = []
        for comment in comments:
            if comment.mark_id:
                inline_comments_data.append(
                    {
                        "id": str(comment.id),
                        "mark_id": str(comment.mark_id),
                        "content": comment.content,
                        "selected_text": comment.selected_text or "",
                        "author_name": comment.author.user.get_display_name(),
                        "author_initials": comment.author.user.get_initials(),
                        "created_at": comment.created_at.isoformat(),
                        "is_resolved": comment.is_resolved,
                        "replies": [
                            {
                                "id": str(reply.id),
                                "content": reply.content,
                                "author_name": reply.author.user.get_display_name(),
                                "author_initials": reply.author.user.get_initials(),
                                "created_at": reply.created_at.isoformat(),
                            }
                            for reply in comment.replies.all()
                        ],
                    }
                )
        context["inline_comments_data"] = inline_comments_data

        # Documents (attachments)
        context["documents"] = motion.documents.all()

        # Revisions
        context["revisions"] = motion.revisions.all()[:10]

        # Shares (for share modal)
        if motion.author == self.membership or self.membership.has_permission("motions.share"):
            context["shares"] = motion.shares.select_related("user", "role", "organization").order_by("-created_at")
            context["can_share"] = True
        else:
            context["can_share"] = False

        context["comment_form"] = MotionCommentForm()
        context["status_form"] = MotionStatusForm(initial={"status": motion.status})

        # Zuständigkeit, Themen, Checkliste, Aufgaben und Freigaben (Sidebar)
        from apps.tenants.models import Membership, Topic
        from apps.work.tasks.models import Task

        context["org_members"] = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )
        context["org_topics"] = Topic.objects.filter(organization=self.organization)
        context["motion_topic_ids"] = set(motion.topics.values_list("id", flat=True))
        context["contributor_ids"] = set(motion.contributors.values_list("id", flat=True))

        # Kompetenz im Thema: Mitglieder, deren Fachgebiete sich mit den
        # Dokument-Themen schneiden (eine Query)
        context["competent_members"] = (
            Membership.objects.filter(
                organization=self.organization,
                is_active=True,
                expertise_topics__in=motion.topics.all(),
            )
            .select_related("user")
            .distinct()
            .order_by("user__first_name", "user__last_name")
        )

        # Ordner-Feld (Details-Sidebar): alle Ordner der Org, eingerückt
        context["org_folders"] = _flatten_folder_tree(self.organization)

        context["checklist_items"] = motion.checklist_items.all()
        context["checklist_progress"] = motion.checklist_progress
        context["linked_tasks"] = (
            Task.objects.filter(organization=self.organization, related_motion=motion)
            .select_related("assigned_to__user")
            .order_by("is_completed", "due_date", "-created_at")
        )

        approvals = motion.approvals.select_related("approver__user").order_by("created_at")
        context["approvals"] = approvals
        context["approval_summary"] = motion.approval_summary
        context["my_pending_approval"] = next(
            (a for a in approvals if a.approver_id == self.membership.id and a.approved is None), None
        )
        context["approval_type_choices"] = MotionApproval.APPROVAL_TYPE_CHOICES
        ai_service = MotionAIService(organization=self.organization, user_id=self.request.user.id)
        context["ai_available"] = ai_service.is_available()
        context["ai_quota"] = ai_service.get_quota_status()

        # Document types for dropdown
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True)

        # Letterheads for dropdown
        letterheads = OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
        context["letterheads"] = letterheads

        # Current letterhead for editor background preview
        if motion.letterhead and (motion.letterhead.is_generated or motion.letterhead.pdf_file):
            context["letterhead"] = motion.letterhead

        # Serialize letterheads as JSON for dynamic JS-side rendering
        letterheads_json = []
        for lh in letterheads:
            if not lh.is_generated and not lh.pdf_file:
                continue
            letterheads_json.append(
                {
                    "id": str(lh.id),
                    "name": lh.name,
                    "kind": lh.kind,
                    "pdf_url": lh.pdf_file.url if (not lh.is_generated and lh.pdf_file) else "",
                    "preview_url": (
                        reverse(
                            "work:document_letterhead_editor_preview",
                            kwargs={"org_slug": self.organization.slug, "letterhead_id": lh.id},
                        )
                        if lh.is_generated
                        else ""
                    ),
                    "margin_top": lh.content_margin_top,
                    "margin_right": lh.content_margin_right,
                    "margin_bottom": lh.content_margin_bottom,
                    "margin_left": lh.content_margin_left,
                    "font_family": lh.font_family,
                    "font_size": lh.font_size,
                }
            )
        context["letterheads_json"] = letterheads_json

        # Collaboration cursor color (deterministic from user ID)
        cursor_colors = ["#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]
        context["collab_color"] = cursor_colors[hash(str(self.request.user.id)) % len(cursor_colors)]

        return context

    def post(self, request, *args, **kwargs):
        motion_id = kwargs.get("motion_id")
        motion = get_object_or_404(Motion, id=motion_id, organization=self.organization)

        # Check edit permission
        access_level = self._get_access_level(motion)
        if access_level not in ("edit", "admin"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"error": "Keine Berechtigung"}, status=403)
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:documents", org_slug=self.organization.slug)

        action = request.POST.get("action", "save")

        # Handle delete action (soft delete - move to trash)
        if action == "delete":
            try:
                motion.status = "deleted"
                motion.deleted_at = timezone.now()
                motion.save()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True, "redirect": True})

                messages.success(request, "Dokument wurde in den Papierkorb verschoben.")
                return redirect("work:documents", org_slug=self.organization.slug)
            except Exception as e:
                logger.exception(f"[DocumentEditor] DELETE FAILED: {e}")
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Löschen fehlgeschlagen."}, status=500)
                raise

        # Handle save action
        if action == "save":
            try:
                title = request.POST.get("title", "").strip()
                if title:
                    motion.title = title

                summary = request.POST.get("summary", "").strip()
                motion.summary = summary

                # Update document type if provided
                document_type_id = request.POST.get("document_type_id", "").strip()
                if document_type_id:
                    motion.document_type_id = document_type_id
                elif document_type_id == "":
                    motion.document_type = None

                # Update letterhead if provided
                letterhead_id = request.POST.get("letterhead_id", "").strip()
                if letterhead_id:
                    motion.letterhead_id = letterhead_id
                elif letterhead_id == "":
                    motion.letterhead = None

                try:
                    old_content = motion.get_content_decrypted()
                except Exception:
                    old_content = ""

                new_content = request.POST.get("content", "")

                # Create revision if content changed
                if old_content != new_content and old_content:
                    try:
                        version = motion.revisions.count() + 1
                        revision = MotionRevision(
                            motion=motion,
                            version=version,
                            changed_by=self.membership,
                            change_summary=request.POST.get("change_summary", "Automatische Speicherung"),
                        )
                        revision.set_content_encrypted(old_content)
                        revision.save()
                    except Exception as e:
                        logger.exception(f"[DocumentEditor] REVISION CREATION FAILED: {e}")

                motion.set_content_encrypted(new_content)
                motion.save()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True, "saved_at": timezone.now().isoformat()})

                messages.success(request, "Änderungen gespeichert.")
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

            except Exception as e:
                logger.exception(f"[DocumentEditor] SAVE FAILED: {e}")
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Speichern fehlgeschlagen."}, status=500)
                messages.error(request, "Speichern fehlgeschlagen.")
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        # Default: use form for full updates
        form = MotionForm(request.POST, instance=motion, organization=self.organization)

        if form.is_valid():
            motion = form.save(commit=False)
            new_content = request.POST.get("content", "")
            if new_content:
                motion.set_content_encrypted(new_content)
            motion.save()

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"success": True})

            messages.success(request, "Änderungen gespeichert.")
            return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": form.errors}, status=400)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class GuestSharedDocumentsView(WorkViewMixin, TemplateView):
    """
    Gast-Übersicht: "Freigegebene Dokumente".

    Landing-Page für Gast-Zugänge — listet alle Dokumente, die per
    persönlicher Freigabe (MotionShare, scope=user) geteilt wurden, sowie
    freigegebene Ordner (FolderGuestShare) als navigierbaren Baum.
    Auch für reguläre Mitglieder aufrufbar (zeigt deren persönliche Freigaben).
    """

    template_name = "work/motions/guest_documents.html"
    permission_required = None  # Gäste haben keine Berechtigungen
    guest_allowed = True

    def get_context_data(self, **kwargs):
        from django.http import Http404

        from ..models import FolderGuestShare

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "guest_documents"

        level_rank = {"view": 0, "comment": 1, "edit": 2, "admin": 3}
        level_labels = dict(MotionShare.LEVEL_CHOICES)

        # === Ordner-Freigaben (rekursiv inkl. Unterordner) ===
        folder_levels = FolderGuestShare.shared_folder_levels(self.request.user, self.organization)

        current_folder = None
        folder_param = self.request.GET.get("ordner")
        if folder_param:
            current_folder = _get_org_folder_or_404(self.organization, folder_param)
            # Nur innerhalb des freigegebenen Teilbaums navigierbar
            if current_folder.id not in folder_levels:
                raise Http404("Ordner nicht freigegeben")

        context["current_folder"] = current_folder
        context["folder_level_label"] = (
            level_labels.get(folder_levels.get(current_folder.id)) if current_folder else None
        )
        context["folder_breadcrumbs"] = (
            [a for a in current_folder.get_ancestors() if a.id in folder_levels] if current_folder else []
        )

        if current_folder is not None:
            # Innerhalb eines Ordners: Unterordner + enthaltene Dokumente
            subfolders = current_folder.children.all()
            folder_documents = (
                Motion.objects.filter(organization=self.organization, folder=current_folder)
                .exclude(status="deleted")
                .select_related("author__user")
                .order_by("-updated_at")
            )
        else:
            # Übersicht: nur direkt freigegebene Wurzeln des Teilbaums
            # (Ordner, deren Parent nicht ebenfalls freigegeben ist)
            subfolders = [
                folder
                for folder in DocumentFolder.objects.filter(organization=self.organization, id__in=folder_levels)
                if folder.parent_id not in folder_levels
            ]
            folder_documents = Motion.objects.none()

        context["shared_folders"] = [
            {
                "folder": folder,
                "level": folder_levels.get(folder.id),
                "level_label": level_labels.get(folder_levels.get(folder.id), ""),
            }
            for folder in subfolders
        ]
        context["folder_documents"] = folder_documents

        # === Dokument-Freigaben (nur auf der Übersichtsseite) ===
        entries = {}
        if current_folder is None:
            shares = (
                MotionShare.objects.filter(
                    scope="user",
                    user=self.request.user,
                    motion__organization=self.organization,
                )
                .exclude(motion__status="deleted")
                .select_related("motion", "motion__author__user", "created_by")
                .order_by("-created_at")
            )

            # Ein Eintrag je Dokument mit dem höchsten Freigabe-Level
            for share in shares:
                entry = entries.get(share.motion_id)
                if entry is None:
                    entries[share.motion_id] = {
                        "motion": share.motion,
                        "level": share.level,
                        "level_label": level_labels.get(share.level, share.level),
                        "shared_at": share.created_at,
                        "shared_by": share.created_by,
                    }
                elif level_rank.get(share.level, 0) > level_rank.get(entry["level"], 0):
                    entry["level"] = share.level
                    entry["level_label"] = level_labels.get(share.level, share.level)

        context["shared_entries"] = list(entries.values())
        return context
