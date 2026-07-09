# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging
import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from .forms import (
    AIAssistantForm,
    MotionCommentForm,
    MotionDocumentForm,
    MotionForm,
    MotionShareForm,
    MotionStatusForm,
    MotionTemplateForm,
)
from .import_service import motion_import_service
from .models import (
    Motion,
    MotionApproval,
    MotionChecklistItem,
    MotionComment,
    MotionRevision,
    MotionShare,
    MotionTemplate,
    MotionType,
    OrganizationLetterhead,
)
from .services import MotionAIService


class MotionListView(WorkViewMixin, TemplateView):
    """List of documents (formerly motions)."""

    template_name = "work/motions/list.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        from apps.tenants.models import Membership, Topic

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"

        # Base queryset - exclude deleted items by default
        motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")

        # Filter by status (Mehrfachauswahl möglich: ?status=a&status=b)
        statuses = [s for s in self.request.GET.getlist("status") if s]
        if statuses:
            motions = motions.filter(status__in=statuses)
            context["selected_status"] = statuses[0]
            context["selected_statuses"] = statuses

        # Filter by type
        motion_type = self.request.GET.get("type")
        if motion_type:
            motions = motions.filter(motion_type=motion_type)
            context["selected_type"] = motion_type

        # Filter by topic
        topic_id = self.request.GET.get("thema")
        if topic_id:
            motions = motions.filter(topics__id=topic_id)
            context["selected_topic"] = topic_id

        # Filter by responsible ("me" oder Membership-ID)
        responsible = self.request.GET.get("verantwortlich")
        if responsible == "me":
            motions = motions.filter(responsible=self.membership)
            context["selected_responsible"] = "me"
        elif responsible:
            motions = motions.filter(responsible__id=responsible)
            context["selected_responsible"] = responsible

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            motions = motions.filter(Q(title__icontains=search) | Q(summary__icontains=search))
            context["search_query"] = search

        # Filter by author (only own motions)
        if self.request.GET.get("mine") == "1":
            motions = motions.filter(author=self.membership)
            context["filter_mine"] = True

        # Order
        order = self.request.GET.get("order", "-updated_at")
        if order in ["-updated_at", "-created_at", "title", "-title", "due_date"]:
            motions = motions.order_by(order)

        motions = motions.distinct()

        # Select/prefetch related for tracker, chips and progress columns
        related = ("author__user", "responsible__user", "document_type", "template")
        prefetches = ("topics", "checklist_items", "approvals")

        # Hauptanträge paginieren, Änderungsanträge (parent_motion) unter
        # ihrem Hauptantrag eingerückt anzeigen
        parents = motions.filter(parent_motion__isnull=True).select_related(*related).prefetch_related(*prefetches)

        paginator = Paginator(parents, 20)
        page = self.request.GET.get("page", 1)
        page_obj = paginator.get_page(page)
        context["motions"] = page_obj
        context["paginator"] = paginator

        # Kinder der Seite laden und gruppiert anhängen
        amendments = (
            Motion.objects.filter(organization=self.organization, parent_motion__in=list(page_obj))
            .exclude(status="deleted")
            .select_related(*related)
            .prefetch_related(*prefetches)
            .order_by("-updated_at")
        )
        amendments_by_parent = {}
        for amendment in amendments:
            amendments_by_parent.setdefault(amendment.parent_motion_id, []).append(amendment)

        motion_rows = []
        for motion in page_obj:
            motion_rows.append({"motion": motion, "is_amendment": False})
            for amendment in amendments_by_parent.get(motion.id, []):
                motion_rows.append({"motion": amendment, "is_amendment": True})
        context["motion_rows"] = motion_rows

        # Statistics (exclude deleted)
        today = timezone.localdate()
        all_motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")
        context["stats"] = {
            "total": all_motions.count(),
            "draft": all_motions.filter(status="draft").count(),
            "submitted": all_motions.filter(status="submitted").count(),
            "completed": all_motions.filter(status="completed").count(),
            "in_consultation": all_motions.filter(status__in=["at_admin", "on_agenda"]).count(),
            "overdue": all_motions.filter(due_date__lt=today)
            .exclude(status__in=["completed", "rejected", "archived"])
            .count(),
        }

        # Filter out 'deleted' from visible status choices
        context["status_choices"] = [(value, label) for value, label in Motion.STATUS_CHOICES if value != "deleted"]
        context["type_choices"] = Motion.LEGACY_TYPE_CHOICES

        # Also get custom document types for this organization
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )

        # Filter-Dropdowns: Themen und Mitglieder der Organisation
        context["org_topics"] = Topic.objects.filter(organization=self.organization)
        context["org_members"] = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )

        return context


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
                    from .export_service import replace_placeholders

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

    def _get_access_level(self, motion):
        """Determine access level: 'view', 'comment', 'edit', or 'admin'."""
        if not motion.can_access(self.membership):
            return "none"

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
        context["ai_available"] = MotionAIService(
            organization=self.organization, user_id=self.request.user.id
        ).is_available()

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


class MotionShareView(WorkViewMixin, TemplateView):
    """Share settings for a motion (legacy - redirects to editor)."""

    template_name = "work/motions/share.html"
    permission_required = "motions.share"

    def get(self, request, *args, **kwargs):
        # Redirect to editor page - sharing is now a modal in the editor
        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=kwargs.get("motion_id"))

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        form = MotionShareForm(request.POST)
        if form.is_valid():
            share = form.save(commit=False)
            share.motion = motion
            share.created_by = request.user

            # Handle user sharing by email
            if share.scope == "user":
                from apps.accounts.models import User

                email = form.cleaned_data.get("email")
                try:
                    user = User.objects.get(email=email)
                    share.user = user
                except User.DoesNotExist:
                    messages.error(request, "Benutzer nicht gefunden.")
                    return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

            share.save()
            messages.success(request, "Freigabe erstellt.")

        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)


class MotionAIAssistantView(WorkViewMixin, View):
    """API endpoint for AI assistant actions."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion_ai_service = MotionAIService(organization=self.organization, user_id=request.user.id)
        if not motion_ai_service.is_available():
            return JsonResponse({"error": "AI-Service nicht verfuegbar"}, status=503)

        form = AIAssistantForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"error": "Ungueltige Anfrage"}, status=400)

        action = form.cleaned_data["action"]
        text = form.cleaned_data.get("text", "")
        instruction = form.cleaned_data.get("instruction", "")
        motion_type = form.cleaned_data.get("motion_type", "motion")
        selected_text = form.cleaned_data.get("selected_text", "")
        history = form.cleaned_data.get("history", [])

        try:
            if action == "improve":
                result = motion_ai_service.improve_text(text, instruction, motion_type)
            elif action == "check":
                result = motion_ai_service.check_formalities(text, motion_type)
            elif action == "suggest":
                result = motion_ai_service.suggest_improvements(text)
            elif action == "title":
                result = motion_ai_service.generate_title(text)
            elif action == "expand":
                result = motion_ai_service.expand_bullet_points(text, motion_type)
            elif action == "summary":
                result = motion_ai_service.generate_summary(text)
            elif action == "chat":
                result = motion_ai_service.chat_with_document(
                    document_html=text,
                    user_message=instruction,
                    selected_text=selected_text,
                    history=history,
                )
            else:
                return JsonResponse({"error": "Unbekannte Aktion"}, status=400)

            if result.success:
                return JsonResponse(
                    {
                        "success": True,
                        "content": result.content,
                        "suggestions": result.suggestions,
                        "tokens_used": result.total_tokens,
                    }
                )
            return JsonResponse({"error": result.error}, status=500)

        except Exception as e:
            logger.exception(f"[MotionAI] Action failed: {e}")
            return JsonResponse({"error": "KI-Aktion fehlgeschlagen."}, status=500)


class MotionCommentView(WorkViewMixin, View):
    """API endpoint for motion comments."""

    permission_required = "motions.comment"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
        if not motion.can_comment(self.membership):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        form = MotionCommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.motion = motion
            comment.author = self.membership
            # Use client-provided mark_id for inline comments, or generate one
            if comment.selected_text:
                client_mark_id = request.POST.get("mark_id")
                if client_mark_id:
                    try:
                        comment.mark_id = uuid.UUID(client_mark_id)
                    except (ValueError, AttributeError):
                        comment.mark_id = uuid.uuid4()
                else:
                    comment.mark_id = uuid.uuid4()
            comment.save()

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": True,
                        "comment": {
                            "id": str(comment.id),
                            "content": comment.content,
                            "author": self.membership.user.get_display_name(),
                            "created_at": comment.created_at.isoformat(),
                            "mark_id": str(comment.mark_id) if comment.mark_id else None,
                            "selected_text": comment.selected_text or None,
                        },
                    }
                )

            messages.success(request, "Kommentar hinzugefügt.")
            return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)

        return JsonResponse({"error": form.errors}, status=400)


class MotionStatusView(WorkViewMixin, View):
    """API endpoint for changing motion status."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        new_status = request.POST.get("status")
        if new_status not in dict(Motion.STATUS_CHOICES):
            return JsonResponse({"error": "Ungültiger Status"}, status=400)

        # Zentrale Übergangsmatrix (Motion.VALID_TRANSITIONS)
        if new_status not in Motion.VALID_TRANSITIONS.get(motion.status, []):
            return JsonResponse(
                {
                    "error": f"Ungültiger Statusübergang von '{motion.get_status_display()}' zu '{dict(Motion.STATUS_CHOICES)[new_status]}'"
                },
                status=400,
            )

        motion.status = new_status
        if new_status == "submitted":
            motion.submitted_at = timezone.now()
        motion.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "status": motion.status,
                    "status_display": motion.get_status_display(),
                }
            )

        messages.success(request, f"Status geändert zu '{motion.get_status_display()}'.")
        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)


class MotionMetaUpdateView(WorkViewMixin, View):
    """API endpoint for updating tracking metadata (Zuständigkeit, Themen, Frist)."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership, Topic
        from apps.work.notifications.services import NotificationHub

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
        action = request.POST.get("action")

        if action == "set_responsible":
            responsible_id = request.POST.get("responsible", "").strip()
            old_responsible_id = motion.responsible_id
            if responsible_id:
                responsible = get_object_or_404(
                    Membership, id=responsible_id, organization=self.organization, is_active=True
                )
                motion.responsible = responsible
            else:
                motion.responsible = None
            motion.save(update_fields=["responsible", "updated_at"])

            # Benachrichtigung an neu zugewiesene Person
            if motion.responsible and motion.responsible_id != old_responsible_id:
                NotificationHub.notify_motion_assigned(motion, motion.responsible, self.membership)

        elif action == "set_contributors":
            contributor_ids = request.POST.getlist("contributors")
            contributors = Membership.objects.filter(
                id__in=contributor_ids, organization=self.organization, is_active=True
            )
            motion.contributors.set(contributors)

        elif action == "set_topics":
            topic_ids = request.POST.getlist("topics")
            topics = Topic.objects.filter(id__in=topic_ids, organization=self.organization)
            motion.topics.set(topics)

        elif action == "set_due_date":
            due_date = request.POST.get("due_date", "").strip()
            if due_date:
                from datetime import date

                try:
                    motion.due_date = date.fromisoformat(due_date)
                except ValueError:
                    return JsonResponse({"error": "Ungültiges Datum"}, status=400)
            else:
                motion.due_date = None
            motion.save(update_fields=["due_date", "updated_at"])

        else:
            return JsonResponse({"error": "Unbekannte Aktion"}, status=400)

        return JsonResponse({"success": True})


class MotionChecklistActionView(WorkViewMixin, View):
    """API endpoint for the document checklist (add, toggle, delete)."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
        action = request.POST.get("action")

        if action == "add":
            title = request.POST.get("title", "").strip()
            if not title:
                return JsonResponse({"error": "Titel ist erforderlich"}, status=400)
            position = motion.checklist_items.count()
            MotionChecklistItem.objects.create(motion=motion, title=title[:300], position=position)

        elif action == "toggle":
            item = get_object_or_404(MotionChecklistItem, id=request.POST.get("item_id"), motion=motion)
            item.is_completed = not item.is_completed
            if item.is_completed:
                item.completed_by = self.membership
                item.completed_at = timezone.now()
            else:
                item.completed_by = None
                item.completed_at = None
            item.save(update_fields=["is_completed", "completed_by", "completed_at"])

        elif action == "delete":
            item = get_object_or_404(MotionChecklistItem, id=request.POST.get("item_id"), motion=motion)
            item.delete()

        else:
            return JsonResponse({"error": "Unbekannte Aktion"}, status=400)

        # Panel neu rendern (Fetch-Swap im Editor)
        from django.template.loader import render_to_string

        html = render_to_string(
            "work/motions/partials/_checklist_panel.html",
            {
                "motion": motion,
                "checklist_items": motion.checklist_items.all(),
                "checklist_progress": motion.checklist_progress,
                "can_edit": True,
                "organization": self.organization,
            },
            request=request,
        )
        progress = motion.checklist_progress
        return JsonResponse({"success": True, "html": html, "progress": progress})


class MotionApprovalRequestView(WorkViewMixin, View):
    """Request an approval from a member (Freigaben-Panel)."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership
        from apps.work.notifications.services import NotificationHub

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        approver = get_object_or_404(
            Membership, id=request.POST.get("approver", ""), organization=self.organization, is_active=True
        )
        approval_type = request.POST.get("approval_type")
        if approval_type not in dict(MotionApproval.APPROVAL_TYPE_CHOICES):
            return JsonResponse({"error": "Ungültiger Genehmigungstyp"}, status=400)

        approval, created = MotionApproval.objects.get_or_create(
            motion=motion,
            approver=approver,
            approval_type=approval_type,
        )
        if not created and approval.approved is not None:
            return JsonResponse({"error": "Diese Freigabe wurde bereits entschieden."}, status=400)

        if created:
            # Angefragte Person braucht Zugriff auf das Dokument,
            # um die Freigabe entscheiden zu können
            if not motion.can_access(approver):
                MotionShare.objects.get_or_create(
                    motion=motion,
                    scope="user",
                    user=approver.user,
                    defaults={"level": "comment", "created_by": request.user},
                )
                if motion.visibility == "private":
                    motion.visibility = "shared"
                    motion.save(update_fields=["visibility"])

            NotificationHub.notify_motion_approval_requested(approval, self.membership)

        return JsonResponse({"success": True, "created": created})


class MotionApprovalDecideView(WorkViewMixin, View):
    """Decide a requested approval (Freigeben/Ablehnen + Kommentar)."""

    permission_required = "motions.view"

    def post(self, request, *args, **kwargs):
        from apps.work.notifications.services import NotificationHub

        approval = get_object_or_404(
            MotionApproval,
            id=kwargs.get("approval_id"),
            motion__id=kwargs.get("motion_id"),
            motion__organization=self.organization,
        )

        # Nur die angefragte Person darf entscheiden
        if approval.approver_id != self.membership.id:
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)
        if approval.approved is not None:
            return JsonResponse({"error": "Diese Freigabe wurde bereits entschieden."}, status=400)

        decision = request.POST.get("decision")
        if decision not in ("approve", "reject"):
            return JsonResponse({"error": "Ungültige Entscheidung"}, status=400)

        approval.approved = decision == "approve"
        approval.comment = request.POST.get("comment", "").strip()
        approval.decided_at = timezone.now()
        approval.save(update_fields=["approved", "comment", "decided_at"])

        # Autor und Federführung informieren
        motion = approval.motion
        recipients = {motion.author_id: motion.author}
        if motion.responsible:
            recipients[motion.responsible_id] = motion.responsible
        for recipient in recipients.values():
            NotificationHub.notify_motion_approval_decided(approval, self.membership, recipient)

        return JsonResponse({"success": True, "approved": approval.approved})


class MotionDocumentUploadView(WorkViewMixin, View):
    """API endpoint for uploading documents."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        form = MotionDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            document = form.save(commit=False)
            document.motion = motion
            document.uploaded_by = self.membership

            file = request.FILES["file"]
            document.filename = file.name
            document.mime_type = file.content_type
            document.file_size = file.size

            document.save()

            return JsonResponse(
                {
                    "success": True,
                    "document": {
                        "id": str(document.id),
                        "filename": document.filename,
                        "size": document.file_size,
                    },
                }
            )

        return JsonResponse({"error": form.errors}, status=400)


class MotionCommentResolveView(WorkViewMixin, View):
    """Mark a comment as resolved."""

    permission_required = "motions.comment"

    def post(self, request, *args, **kwargs):
        comment = get_object_or_404(
            MotionComment,
            id=kwargs.get("comment_id"),
            motion__id=kwargs.get("motion_id"),
            motion__organization=self.organization,
        )

        if not comment.motion.can_access(self.membership):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        # Only author or someone with edit permission can resolve
        if comment.author != self.membership:
            if not self.membership.has_permission("motions.edit_all"):
                return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        comment.is_resolved = True
        comment.resolved_by = self.membership
        comment.resolved_at = timezone.now()
        comment.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "mark_id": str(comment.mark_id) if comment.mark_id else None,
                }
            )

        messages.success(request, "Kommentar als erledigt markiert.")
        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=comment.motion.id)


class MotionExportView(WorkViewMixin, View):
    """Export motion as PDF or DOCX."""

    permission_required = "motions.view"

    def get(self, request, *args, **kwargs):
        from django.http import HttpResponse

        from .export_service import motion_export_service

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        export_format = request.GET.get("format", "pdf")

        if export_format == "pdf":
            try:
                pdf_content = motion_export_service.export_to_pdf(motion)

                # Create filename
                safe_title = "".join(c for c in motion.title if c.isalnum() or c in " -_").strip()
                filename = f"{safe_title[:50]}.pdf"

                response = HttpResponse(pdf_content, content_type="application/pdf")
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response

            except Exception as e:
                logger.exception(f"[MotionExport] PDF export failed: {e}")
                return JsonResponse({"error": "PDF-Export fehlgeschlagen."}, status=500)

        elif export_format == "docx":
            try:
                docx_content = motion_export_service.export_to_docx(motion)

                safe_title = "".join(c for c in motion.title if c.isalnum() or c in " -_").strip()
                filename = f"{safe_title[:50]}.docx"

                response = HttpResponse(
                    docx_content,
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response

            except Exception as e:
                logger.exception(f"[MotionExport] DOCX export failed: {e}")
                return JsonResponse({"error": "DOCX-Export fehlgeschlagen."}, status=500)

        else:
            return JsonResponse({"error": f"Unbekanntes Export-Format: {export_format}"}, status=400)


class MotionImportView(WorkViewMixin, TemplateView):
    """Import PDF/DOCX files as documents."""

    template_name = "work/motions/import.html"
    permission_required = "motions.create"

    ALLOWED_TYPES = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    ALLOWED_EXTENSIONS = {".pdf", ".docx"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )
        return context

    def post(self, request, *args, **kwargs):
        uploaded_files = request.FILES.getlist("import_files")

        if not uploaded_files:
            messages.error(request, "Bitte wählen Sie mindestens eine Datei aus.")
            return redirect("work:document_import", org_slug=self.organization.slug)

        # Filter to allowed file types
        valid_files = []
        for f in uploaded_files:
            name_lower = (f.name or "").lower()
            if any(name_lower.endswith(ext) for ext in self.ALLOWED_EXTENSIONS):
                valid_files.append(f)
            else:
                messages.warning(request, f"'{f.name}' übersprungen — nur PDF und DOCX werden unterstützt.")

        if not valid_files:
            messages.error(request, "Keine gültigen Dateien (PDF/DOCX) ausgewählt.")
            return redirect("work:document_import", org_slug=self.organization.slug)

        # Get optional document type
        motion_type = None
        motion_type_id = request.POST.get("document_type")
        if motion_type_id:
            try:
                motion_type = MotionType.objects.get(id=motion_type_id, organization=self.organization)
            except MotionType.DoesNotExist:
                pass

        # Get visibility
        visibility = request.POST.get("visibility", "private")
        if visibility not in ["private", "shared", "organization"]:
            visibility = "private"

        # Import files (PDF + DOCX)
        results = motion_import_service.import_multiple_files(
            files=valid_files,
            organization=self.organization,
            author=self.membership,
            motion_type=motion_type,
            visibility=visibility,
        )

        # Count successes and failures
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        if successes:
            if len(successes) == 1:
                motion = successes[0].motion
                messages.success(request, f"Dokument '{motion.title}' erfolgreich importiert.")
                return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=motion.id)
            else:
                messages.success(request, f"{len(successes)} Dokumente erfolgreich importiert.")

        if failures:
            for failure in failures:
                messages.error(request, f"Import fehlgeschlagen: {failure.error}")

        return redirect("work:documents", org_slug=self.organization.slug)


class MotionShareUpdateView(WorkViewMixin, View):
    """HTMX endpoint for updating share settings via modal."""

    permission_required = "motions.share"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        # Check if user can share this motion
        if motion.author != self.membership and not self.membership.has_permission("motions.edit_all"):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        # Update visibility
        new_visibility = request.POST.get("visibility")
        if new_visibility in ["private", "shared", "organization"]:
            motion.visibility = new_visibility
            motion.save()

        # Handle adding users for shared visibility
        if new_visibility == "shared":
            add_user_email = request.POST.get("add_user_email", "").strip()
            if add_user_email:
                from apps.accounts.models import User

                try:
                    user = User.objects.get(email=add_user_email)
                    # Create share if doesn't exist
                    MotionShare.objects.get_or_create(
                        motion=motion,
                        scope="user",
                        user=user,
                        defaults={
                            "level": "edit",
                            "created_by": request.user,
                        },
                    )
                except User.DoesNotExist:
                    return JsonResponse({"error": f"Benutzer '{add_user_email}' nicht gefunden."}, status=400)

        # Return success for HTMX
        from django.http import HttpResponse

        return HttpResponse(status=204, headers={"HX-Refresh": "true"})


class MotionShareRemoveView(WorkViewMixin, View):
    """Remove a share entry."""

    permission_required = "motions.share"

    def post(self, request, *args, **kwargs):
        share = get_object_or_404(MotionShare, id=kwargs.get("share_id"), motion__organization=self.organization)

        # Check if user can manage this share
        motion = share.motion
        if motion.author != self.membership and not self.membership.has_permission("motions.edit_all"):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        share.delete()

        from django.http import HttpResponse

        return HttpResponse(status=204, headers={"HX-Refresh": "true"})


# =============================================================================
# Settings Views for Motion Types, Templates, and Letterheads
# =============================================================================


class MotionSettingsView(WorkViewMixin, TemplateView):
    """Overview of motion/document settings."""

    template_name = "work/motions/settings/index.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "documents"

        # Get counts
        from apps.tenants.models import Topic

        context["type_count"] = MotionType.objects.filter(organization=self.organization).count()
        context["template_count"] = MotionTemplate.objects.filter(organization=self.organization).count()
        context["letterhead_count"] = OrganizationLetterhead.objects.filter(organization=self.organization).count()
        context["topic_count"] = Topic.objects.filter(organization=self.organization).count()

        # Branding-Kachel: Logo/Farben aus tenants + Briefkopf-Status
        primary_letterhead = (
            OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
            .order_by("-is_default", "name")
            .first()
        )
        if primary_letterhead is None:
            letterhead_status = "keiner"
        elif primary_letterhead.is_generated:
            letterhead_status = "generiert"
        else:
            letterhead_status = "PDF"
        context["primary_letterhead"] = primary_letterhead
        context["letterhead_status"] = letterhead_status

        return context


class MotionTypeListView(WorkViewMixin, TemplateView):
    """List and manage document types."""

    template_name = "work/motions/settings/types.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"

        context["types"] = MotionType.objects.filter(organization=self.organization).order_by("sort_order", "name")

        return context


class MotionTypeCreateView(WorkViewMixin, TemplateView):
    """Create a new document type."""

    template_name = "work/motions/settings/type_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"
        context["is_new"] = True
        return context

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        description = request.POST.get("description", "").strip()
        icon = request.POST.get("icon", "file-text").strip()
        color = request.POST.get("color", "blue").strip()
        requires_approval = request.POST.get("requires_approval") == "on"
        is_submittable = request.POST.get("is_submittable") == "on"
        is_default = request.POST.get("is_default") == "on"
        default_checklist = [
            line.strip() for line in request.POST.get("default_checklist", "").splitlines() if line.strip()
        ]

        if not name or not slug:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Check uniqueness
        if MotionType.objects.filter(organization=self.organization, slug=slug).exists():
            messages.error(request, "Ein Typ mit diesem Kurznamen existiert bereits.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # If setting as default, unset others
        if is_default:
            MotionType.objects.filter(organization=self.organization, is_default=True).update(is_default=False)

        MotionType.objects.create(
            organization=self.organization,
            name=name,
            slug=slug,
            description=description,
            icon=icon,
            color=color,
            requires_approval=requires_approval,
            is_submittable=is_submittable,
            is_default=is_default,
            default_checklist=default_checklist,
        )

        messages.success(request, f"Dokumenttyp '{name}' erstellt.")
        return redirect("work:document_type_list", org_slug=self.organization.slug)


class MotionTypeEditView(WorkViewMixin, TemplateView):
    """Edit a document type."""

    template_name = "work/motions/settings/type_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"
        context["is_new"] = False

        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)
        context["motion_type"] = motion_type
        context["default_checklist_text"] = "\n".join(motion_type.default_checklist or [])
        return context

    def post(self, request, *args, **kwargs):
        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)

        motion_type.name = request.POST.get("name", "").strip()
        motion_type.slug = request.POST.get("slug", "").strip()
        motion_type.description = request.POST.get("description", "").strip()
        motion_type.icon = request.POST.get("icon", "file-text").strip()
        motion_type.color = request.POST.get("color", "blue").strip()
        motion_type.requires_approval = request.POST.get("requires_approval") == "on"
        motion_type.is_submittable = request.POST.get("is_submittable") == "on"
        motion_type.default_checklist = [
            line.strip() for line in request.POST.get("default_checklist", "").splitlines() if line.strip()
        ]
        is_default = request.POST.get("is_default") == "on"

        if is_default and not motion_type.is_default:
            MotionType.objects.filter(organization=self.organization, is_default=True).update(is_default=False)
        motion_type.is_default = is_default

        motion_type.save()

        messages.success(request, f"Dokumenttyp '{motion_type.name}' aktualisiert.")
        return redirect("work:document_type_list", org_slug=self.organization.slug)


class MotionTypeDeleteView(WorkViewMixin, View):
    """Delete a document type."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)

        # Check if type is in use
        if Motion.objects.filter(document_type=motion_type).exists():
            messages.error(
                request,
                f"Dokumenttyp '{motion_type.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = motion_type.name
            motion_type.delete()
            messages.success(request, f"Dokumenttyp '{name}' gelöscht.")

        return redirect("work:document_type_list", org_slug=self.organization.slug)


class TopicListView(WorkViewMixin, TemplateView):
    """List and manage the organization's topic catalog (Themenkatalog)."""

    template_name = "work/motions/settings/topics.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        from apps.tenants.models import Topic

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "topics"
        context["topics"] = Topic.objects.filter(organization=self.organization)
        context["color_choices"] = Topic.COLOR_CHOICES
        return context

    def post(self, request, *args, **kwargs):
        """Create a new topic (inline form on the list page)."""
        from apps.tenants.models import Topic

        name = request.POST.get("name", "").strip()
        color = request.POST.get("color", "blue").strip()
        if color not in dict(Topic.COLOR_CHOICES):
            color = "blue"

        if not name:
            messages.error(request, "Name ist erforderlich.")
        elif Topic.objects.filter(organization=self.organization, name=name).exists():
            messages.error(request, f"Das Thema '{name}' existiert bereits.")
        else:
            sort_order = Topic.objects.filter(organization=self.organization).count()
            Topic.objects.create(organization=self.organization, name=name, color=color, sort_order=sort_order)
            messages.success(request, f"Thema '{name}' erstellt.")

        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class TopicUpdateView(WorkViewMixin, View):
    """Update a topic (name, color, sort order)."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Topic

        topic = get_object_or_404(Topic, id=kwargs.get("topic_id"), organization=self.organization)

        name = request.POST.get("name", "").strip()
        color = request.POST.get("color", topic.color).strip()

        if not name:
            messages.error(request, "Name ist erforderlich.")
            return redirect("work:document_topic_list", org_slug=self.organization.slug)

        if Topic.objects.filter(organization=self.organization, name=name).exclude(id=topic.id).exists():
            messages.error(request, f"Das Thema '{name}' existiert bereits.")
            return redirect("work:document_topic_list", org_slug=self.organization.slug)

        topic.name = name
        if color in dict(Topic.COLOR_CHOICES):
            topic.color = color
        try:
            topic.sort_order = int(request.POST.get("sort_order", topic.sort_order))
        except (TypeError, ValueError):
            pass
        topic.save()

        messages.success(request, f"Thema '{topic.name}' aktualisiert.")
        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class TopicDeleteView(WorkViewMixin, View):
    """Delete a topic."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Topic

        topic = get_object_or_404(Topic, id=kwargs.get("topic_id"), organization=self.organization)
        name = topic.name
        topic.delete()
        messages.success(request, f"Thema '{name}' gelöscht.")
        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class MotionTemplateListView(WorkViewMixin, TemplateView):
    """List and manage document templates."""

    template_name = "work/motions/settings/templates.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"

        context["templates"] = (
            MotionTemplate.objects.filter(organization=self.organization)
            .select_related("motion_type", "letterhead")
            .order_by("-is_default", "name")
        )

        return context


class MotionTemplateCreateView(WorkViewMixin, TemplateView):
    """Create a new document template."""

    template_name = "work/motions/settings/template_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"
        context["is_new"] = True
        context["form"] = MotionTemplateForm(organization=self.organization)

        context["types"] = MotionType.objects.filter(organization=self.organization, is_active=True)
        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        form = MotionTemplateForm(request.POST, organization=self.organization)

        if form.is_valid():
            template = form.save(commit=False)
            template.organization = self.organization

            # Ein Default je Typ: beim Setzen andere Vorlagen desselben Typs zurücksetzen
            if template.is_default:
                MotionTemplate.objects.filter(
                    organization=self.organization, is_default=True, motion_type=template.motion_type
                ).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' erstellt.")
            return redirect("work:document_template_list", org_slug=self.organization.slug)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class MotionTemplateEditView(WorkViewMixin, TemplateView):
    """Edit a document template."""

    template_name = "work/motions/settings/template_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"
        context["is_new"] = False

        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)
        context["template"] = template
        context["form"] = MotionTemplateForm(instance=template, organization=self.organization)

        context["types"] = MotionType.objects.filter(organization=self.organization, is_active=True)
        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)

        form = MotionTemplateForm(request.POST, instance=template, organization=self.organization)

        if form.is_valid():
            template = form.save(commit=False)

            # Ein Default je Typ: beim Setzen andere Vorlagen desselben Typs zurücksetzen
            if template.is_default:
                MotionTemplate.objects.filter(
                    organization=self.organization, is_default=True, motion_type=template.motion_type
                ).exclude(id=template.id).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' aktualisiert.")
            return redirect("work:document_template_list", org_slug=self.organization.slug)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class MotionTemplateDeleteView(WorkViewMixin, View):
    """Delete a document template."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)

        # Check if template is in use
        if Motion.objects.filter(template=template).exists():
            messages.error(
                request,
                f"Vorlage '{template.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = template.name
            template.delete()
            messages.success(request, f"Vorlage '{name}' gelöscht.")

        return redirect("work:document_template_list", org_slug=self.organization.slug)


class LetterheadListView(WorkViewMixin, TemplateView):
    """List and manage letterheads."""

    template_name = "work/motions/settings/letterheads.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"

        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization).order_by(
            "-is_default", "name"
        )

        return context


def _generated_letterhead_defaults(organization) -> dict:
    """
    Sinnvolle Vorbelegung für einen generierten Briefkopf aus den Org-Daten
    (Name, Adresse, Kontakt — falls vorhanden).
    """
    address_lines = [line.strip() for line in (organization.address or "").splitlines() if line.strip()]

    contact_parts = []
    if organization.contact_email:
        contact_parts.append(organization.contact_email)
    if organization.contact_phone:
        contact_parts.append(f"Tel. {organization.contact_phone}")
    if organization.website:
        contact_parts.append(organization.website)

    return {
        "sender_line": " · ".join([organization.name] + address_lines),
        "address_block": "\n".join([organization.name] + address_lines),
        "footer_text": " · ".join(contact_parts),
    }


class LetterheadCreateView(WorkViewMixin, TemplateView):
    """Create a new letterhead."""

    template_name = "work/motions/settings/letterhead_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"
        context["is_new"] = True
        context["generated_defaults"] = _generated_letterhead_defaults(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        kind = request.POST.get("kind", "pdf")
        if kind not in dict(OrganizationLetterhead.KIND_CHOICES):
            kind = "pdf"
        pdf_file = request.FILES.get("pdf_file")

        if not name:
            messages.error(request, "Name ist erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        if kind == "pdf":
            if not pdf_file:
                messages.error(request, "Für einen PDF-Briefkopf ist eine PDF-Datei erforderlich.")
                return self.render_to_response(self.get_context_data(**kwargs))
            if not pdf_file.name.lower().endswith(".pdf"):
                messages.error(request, "Nur PDF-Dateien sind erlaubt.")
                return self.render_to_response(self.get_context_data(**kwargs))
        else:
            pdf_file = None

        is_default = request.POST.get("is_default") == "on"
        if is_default:
            OrganizationLetterhead.objects.filter(organization=self.organization, is_default=True).update(
                is_default=False
            )

        OrganizationLetterhead.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            kind=kind,
            pdf_file=pdf_file,
            header_logo_enabled=request.POST.get("header_logo_enabled") == "on",
            sender_line=request.POST.get("sender_line", "").strip(),
            address_block=request.POST.get("address_block", "").strip(),
            footer_text=request.POST.get("footer_text", "").strip(),
            accent_color_enabled=request.POST.get("accent_color_enabled") == "on",
            content_margin_top=int(request.POST.get("content_margin_top", 60)),
            content_margin_left=int(request.POST.get("content_margin_left", 25)),
            content_margin_right=int(request.POST.get("content_margin_right", 20)),
            content_margin_bottom=int(request.POST.get("content_margin_bottom", 30)),
            font_family=request.POST.get("font_family", "Arial").strip(),
            font_size=int(request.POST.get("font_size", 11)),
            is_default=is_default,
        )

        messages.success(request, f"Briefkopf '{name}' erstellt.")
        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadEditView(WorkViewMixin, TemplateView):
    """Edit a letterhead."""

    template_name = "work/motions/settings/letterhead_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"
        context["is_new"] = False

        context["letterhead"] = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )
        return context

    def post(self, request, *args, **kwargs):
        letterhead = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )

        letterhead.name = request.POST.get("name", "").strip()
        letterhead.description = request.POST.get("description", "").strip()

        kind = request.POST.get("kind", letterhead.kind)
        if kind in dict(OrganizationLetterhead.KIND_CHOICES):
            letterhead.kind = kind

        # Handle file upload (optional for edit)
        new_file = request.FILES.get("pdf_file")
        if new_file:
            if not new_file.name.lower().endswith(".pdf"):
                messages.error(request, "Nur PDF-Dateien sind erlaubt.")
                return self.render_to_response(self.get_context_data(**kwargs))
            letterhead.pdf_file = new_file

        if letterhead.kind == "pdf" and not letterhead.pdf_file:
            messages.error(request, "Für einen PDF-Briefkopf ist eine PDF-Datei erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Felder für generierten Briefkopf
        letterhead.header_logo_enabled = request.POST.get("header_logo_enabled") == "on"
        letterhead.sender_line = request.POST.get("sender_line", "").strip()
        letterhead.address_block = request.POST.get("address_block", "").strip()
        letterhead.footer_text = request.POST.get("footer_text", "").strip()
        letterhead.accent_color_enabled = request.POST.get("accent_color_enabled") == "on"

        letterhead.content_margin_top = int(request.POST.get("content_margin_top", 60))
        letterhead.content_margin_left = int(request.POST.get("content_margin_left", 25))
        letterhead.content_margin_right = int(request.POST.get("content_margin_right", 20))
        letterhead.content_margin_bottom = int(request.POST.get("content_margin_bottom", 30))
        letterhead.font_family = request.POST.get("font_family", "Arial").strip()
        letterhead.font_size = int(request.POST.get("font_size", 11))

        is_default = request.POST.get("is_default") == "on"
        if is_default and not letterhead.is_default:
            OrganizationLetterhead.objects.filter(organization=self.organization, is_default=True).update(
                is_default=False
            )
        letterhead.is_default = is_default

        letterhead.save()

        messages.success(request, f"Briefkopf '{letterhead.name}' aktualisiert.")
        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadDeleteView(WorkViewMixin, View):
    """Delete a letterhead."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        letterhead = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )

        # Check if letterhead is in use
        in_use = (
            Motion.objects.filter(letterhead=letterhead).exists()
            or MotionTemplate.objects.filter(letterhead=letterhead).exists()
        )

        if in_use:
            messages.error(
                request,
                f"Briefkopf '{letterhead.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = letterhead.name
            letterhead.delete()
            messages.success(request, f"Briefkopf '{name}' gelöscht.")

        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadPreviewView(WorkViewMixin, View):
    """
    Live-Vorschau des generierten Briefkopfs im Briefkopf-Formular.

    Rendert das gemeinsame Partial mit Beispieltext aus den (ungespeicherten)
    Formularwerten (per GET-Parametern).
    """

    permission_required = "organization.edit"

    def get(self, request, *args, **kwargs):
        org = self.organization
        params = request.GET

        logo_url = ""
        if params.get("header_logo_enabled") in ("on", "true", "1"):
            logo = org.effective_logo
            if logo:
                try:
                    logo_url = logo.url
                except ValueError:
                    logo_url = ""

        context = {
            "org_name": org.name,
            "primary_color": org.effective_primary_color,
            "accent_enabled": params.get("accent_color_enabled") in ("on", "true", "1"),
            "logo_url": logo_url,
            "sender_line": params.get("sender_line", "").strip(),
            "address_lines": [line for line in params.get("address_block", "").splitlines() if line.strip()],
            "footer_lines": [line for line in params.get("footer_text", "").splitlines() if line.strip()],
            "show_sample": True,
            "show_footer": True,
        }
        return render(request, "work/motions/_generated_letterhead.html", context)


class LetterheadEditorPreviewView(WorkViewMixin, View):
    """
    HTML-Briefkopf-Vorschau für den Dokument-Editor (kind=generated).

    Der Editor rendert dieses Partial über dem Inhalt statt des
    pdfjs-Overlays.
    """

    permission_required = "motions.view"

    def get(self, request, *args, **kwargs):
        from .export_service import generated_letterhead_context

        letterhead = get_object_or_404(
            OrganizationLetterhead,
            id=kwargs.get("letterhead_id"),
            organization=self.organization,
            kind="generated",
        )
        context = generated_letterhead_context(letterhead)
        context.update({"show_sample": False, "show_footer": False})
        return render(request, "work/motions/_generated_letterhead.html", context)


class MotionTemplatePreviewView(WorkViewMixin, TemplateView):
    """
    Vorschau einer Dokumentvorlage: Inhaltsvorlage + gewählter Briefkopf
    als HTML-Seite (Platzhalter mit Beispielwerten ersetzt).
    """

    template_name = "work/motions/settings/template_preview.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        from django.template.loader import render_to_string

        from .export_service import apply_placeholders, build_placeholder_values, generated_letterhead_context

        context = super().get_context_data(**kwargs)
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)
        context["template"] = template

        values = build_placeholder_values(self.organization, responsible_name="Erika Musterfrau")
        context["content_html"] = apply_placeholders(template.content_template or "", values)
        context["signature_text"] = apply_placeholders(template.signature_block or "", values)

        letterhead = template.letterhead
        context["letterhead_obj"] = letterhead
        context["letterhead_html"] = ""
        if letterhead and letterhead.is_generated:
            lh_context = generated_letterhead_context(letterhead)
            lh_context.update({"show_sample": False, "show_footer": True})
            context["letterhead_html"] = render_to_string("work/motions/_generated_letterhead.html", lh_context)

        return context


# =============================================================================
# Trash (Papierkorb) Views
# =============================================================================


class MotionTrashView(WorkViewMixin, TemplateView):
    """View deleted documents (Papierkorb)."""

    template_name = "work/motions/trash.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"

        # Get only deleted motions
        deleted_motions = (
            Motion.objects.filter(organization=self.organization, status="deleted")
            .select_related("author__user")
            .order_by("-deleted_at")
        )

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            deleted_motions = deleted_motions.filter(Q(title__icontains=search) | Q(summary__icontains=search))
            context["search_query"] = search

        # Pagination
        paginator = Paginator(deleted_motions, 20)
        page = self.request.GET.get("page", 1)
        context["motions"] = paginator.get_page(page)
        context["paginator"] = paginator
        context["trash_count"] = Motion.objects.filter(organization=self.organization, status="deleted").count()

        return context


class MotionRestoreView(WorkViewMixin, View):
    """Restore a motion from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization, status="deleted")

        # Restore to draft
        motion.status = "draft"
        motion.deleted_at = None
        motion.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        messages.success(request, f"'{motion.title}' wurde wiederhergestellt.")
        return redirect("work:document_trash", org_slug=self.organization.slug)


class MotionPermanentDeleteView(WorkViewMixin, View):
    """Permanently delete a motion from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization, status="deleted")

        title = motion.title
        motion.delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        messages.success(request, f"'{title}' wurde endgültig gelöscht.")
        return redirect("work:document_trash", org_slug=self.organization.slug)


class MotionEmptyTrashView(WorkViewMixin, View):
    """Empty all items from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        count = Motion.objects.filter(organization=self.organization, status="deleted").count()

        Motion.objects.filter(organization=self.organization, status="deleted").delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "count": count})

        messages.success(request, f"Papierkorb geleert ({count} Dokumente gelöscht).")
        return redirect("work:document_trash", org_slug=self.organization.slug)


# =============================================================================
# Revision API Views (Version History)
# =============================================================================


class DocumentRevisionsAPIView(WorkViewMixin, View):
    """API endpoint listing all revisions for a document."""

    permission_required = "motions.view"

    def get(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
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

    def get(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)
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

        return JsonResponse({"success": True, "version": restore_version})


# =============================================================================
# Legacy /motions/ → /documents/ Redirect Views
# =============================================================================


class MotionRedirectView(View):
    """Redirect /motions/ → /documents/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:documents", org_slug=org_slug, permanent=True)


class MotionDetailRedirectView(View):
    """Redirect /motions/<id>/ and /motions/<id>/edit/ → /documents/<id>/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        motion_id = kwargs.get("motion_id")
        return redirect("work:document_editor", org_slug=org_slug, motion_id=motion_id, permanent=True)


class MotionRedirectCreateView(View):
    """Redirect /motions/create/ → /documents/create/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_create", org_slug=org_slug, permanent=True)


class MotionRedirectTrashView(View):
    """Redirect /motions/trash/ → /documents/trash/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_trash", org_slug=org_slug, permanent=True)


class MotionRedirectImportView(View):
    """Redirect /motions/import/ → /documents/import/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_import", org_slug=org_slug, permanent=True)
