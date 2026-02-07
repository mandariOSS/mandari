# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .forms import (
    AIAssistantForm,
    MotionCommentForm,
    MotionContentForm,
    MotionDocumentForm,
    MotionForm,
    MotionShareForm,
    MotionStatusForm,
    MotionTemplateForm,
)
from .import_service import motion_import_service
from .models import (
    Motion,
    MotionComment,
    MotionRevision,
    MotionShare,
    MotionTemplate,
    MotionType,
    OrganizationLetterhead,
)
from .services import motion_ai_service


class MotionListView(WorkViewMixin, TemplateView):
    """List of motions."""

    template_name = "work/motions/list.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"

        # Base queryset - exclude deleted items by default
        motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            motions = motions.filter(status=status)
            context["selected_status"] = status

        # Filter by type
        motion_type = self.request.GET.get("type")
        if motion_type:
            motions = motions.filter(motion_type=motion_type)
            context["selected_type"] = motion_type

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
        if order in ["-updated_at", "-created_at", "title", "-title"]:
            motions = motions.order_by(order)

        # Select related
        motions = motions.select_related("author__user", "template")

        # Pagination
        paginator = Paginator(motions, 20)
        page = self.request.GET.get("page", 1)
        context["motions"] = paginator.get_page(page)
        context["paginator"] = paginator

        # Statistics (exclude deleted)
        all_motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")
        context["stats"] = {
            "total": all_motions.count(),
            "draft": all_motions.filter(status="draft").count(),
            "submitted": all_motions.filter(status="submitted").count(),
            "completed": all_motions.filter(status="completed").count(),
        }

        # Filter out 'deleted' from visible status choices
        context["status_choices"] = [(value, label) for value, label in Motion.STATUS_CHOICES if value != "deleted"]
        context["type_choices"] = Motion.LEGACY_TYPE_CHOICES

        # Also get custom document types for this organization
        from .models import MotionType

        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )

        return context


class MotionCreateView(WorkViewMixin, TemplateView):
    """Create a new motion (Step 1: Basic data)."""

    template_name = "work/motions/create.html"
    permission_required = "motions.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"
        context["form"] = MotionForm(organization=self.organization)
        context["ai_available"] = motion_ai_service.is_available()

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

                # Pre-fill content from template
                if template.content_template:
                    motion.content_encrypted = template.content_template
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

        messages.success(request, "Dokument erstellt. Sie können jetzt den Inhalt bearbeiten.")
        return redirect("work:motion_edit", org_slug=self.organization.slug, motion_id=motion.id)


class MotionDetailView(WorkViewMixin, TemplateView):
    """Detail view of a motion."""

    template_name = "work/motions/detail.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        context["motion"] = motion
        context["is_author"] = motion.author == self.membership

        # Check edit permission
        context["can_edit"] = (
            motion.author == self.membership or self.membership.has_permission("motions.edit_all")
        ) and motion.status in ["draft", "review"]

        # Get comments
        context["comments"] = (
            motion.comments.filter(parent__isnull=True)
            .select_related("author__user")
            .prefetch_related("replies__author__user")
            .order_by("created_at")
        )

        # Get documents
        context["documents"] = motion.documents.all()

        # Get revisions
        context["revisions"] = motion.revisions.all()[:10]

        # Get shares
        if motion.author == self.membership or self.membership.has_permission("motions.share"):
            context["shares"] = motion.shares.select_related("user", "role", "organization").order_by("-created_at")

        context["comment_form"] = MotionCommentForm()
        context["status_form"] = MotionStatusForm(initial={"status": motion.status})

        return context


class MotionEditView(WorkViewMixin, TemplateView):
    """Edit a motion with rich text editor."""

    template_name = "work/motions/edit.html"
    permission_required = "motions.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        # Check permission
        if motion.author != self.membership and not self.membership.has_permission("motions.edit_all"):
            messages.error(self.request, "Keine Berechtigung zum Bearbeiten.")
            return redirect("work:motion_detail", org_slug=self.organization.slug, motion_id=motion.id)

        if motion.status not in ["draft", "review"]:
            messages.warning(self.request, "Dieser Antrag kann nicht mehr bearbeitet werden.")

        context["motion"] = motion
        context["motion_content"] = motion.get_content_decrypted()  # Decrypted content for template
        context["form"] = MotionForm(instance=motion, organization=self.organization)
        context["content_form"] = MotionContentForm(initial={"content": motion.get_content_decrypted()})
        context["document_form"] = MotionDocumentForm()
        context["ai_available"] = motion_ai_service.is_available()
        context["ai_form"] = AIAssistantForm()

        return context

    def post(self, request, *args, **kwargs):
        import logging

        logger = logging.getLogger("apps.work.motions")

        motion_id = kwargs.get("motion_id")
        logger.info(f"[MotionEdit] POST request for motion {motion_id}")
        logger.info(f"[MotionEdit] User: {request.user.email}, Org: {self.organization.slug}")
        logger.info(
            f"[MotionEdit] Membership: {self.membership}, Roles: {list(self.membership.roles.values_list('name', flat=True))}"
        )

        motion = get_object_or_404(Motion, id=motion_id, organization=self.organization)
        logger.info(f"[MotionEdit] Motion found: {motion.title}, Author: {motion.author}")

        # Check permission
        is_author = motion.author == self.membership
        has_edit_all = self.membership.has_permission("motions.edit_all")
        logger.info(f"[MotionEdit] Permission check: is_author={is_author}, has_edit_all={has_edit_all}")

        if not is_author and not has_edit_all:
            logger.warning(f"[MotionEdit] PERMISSION DENIED for {request.user.email}")
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"error": "Keine Berechtigung"}, status=403)
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:motions", org_slug=self.organization.slug)

        action = request.POST.get("action", "save")
        logger.info(f"[MotionEdit] Action: {action}")

        # Handle delete action (soft delete - move to trash)
        if action == "delete":
            try:
                motion.status = "deleted"
                motion.deleted_at = timezone.now()
                motion.save()
                logger.info("[MotionEdit] Motion deleted (soft)")

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True, "redirect": True})

                messages.success(request, "Dokument wurde in den Papierkorb verschoben.")
                return redirect("work:motions", org_slug=self.organization.slug)
            except Exception as e:
                logger.exception(f"[MotionEdit] DELETE FAILED: {e}")
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Löschen fehlgeschlagen."}, status=500)
                raise

        # Handle save action - simplified, only update content and title
        if action == "save":
            try:
                # Update title if provided
                title = request.POST.get("title", "").strip()
                if title:
                    motion.title = title
                    logger.info(f"[MotionEdit] Title updated: {title[:50]}...")

                # Update summary if provided
                summary = request.POST.get("summary", "").strip()
                motion.summary = summary

                # Get content
                logger.info("[MotionEdit] Decrypting old content...")
                try:
                    old_content = motion.get_content_decrypted()
                    logger.info(f"[MotionEdit] Old content length: {len(old_content) if old_content else 0}")
                except Exception as e:
                    logger.exception(f"[MotionEdit] DECRYPTION FAILED: {e}")
                    old_content = ""

                new_content = request.POST.get("content", "")
                logger.info(f"[MotionEdit] New content length: {len(new_content)}")

                # Create revision if content changed significantly
                if old_content != new_content and old_content:
                    logger.info("[MotionEdit] Creating revision...")
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
                        logger.info(f"[MotionEdit] Revision {version} created")
                    except Exception as e:
                        logger.exception(f"[MotionEdit] REVISION CREATION FAILED: {e}")
                        # Continue anyway - revision is not critical

                logger.info("[MotionEdit] Encrypting new content...")
                motion.set_content_encrypted(new_content)

                logger.info("[MotionEdit] Saving motion...")
                motion.save()
                logger.info("[MotionEdit] Motion saved successfully!")

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True, "saved_at": timezone.now().isoformat()})

                messages.success(request, "Änderungen gespeichert.")
                return redirect("work:motion_edit", org_slug=self.organization.slug, motion_id=motion.id)

            except Exception as e:
                logger.exception(f"[MotionEdit] SAVE FAILED: {e}")
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Speichern fehlgeschlagen."}, status=500)
                messages.error(request, "Speichern fehlgeschlagen.")
                return redirect("work:motion_edit", org_slug=self.organization.slug, motion_id=motion.id)

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
            return redirect("work:motion_edit", org_slug=self.organization.slug, motion_id=motion.id)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": form.errors}, status=400)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class MotionShareView(WorkViewMixin, TemplateView):
    """Share settings for a motion."""

    template_name = "work/motions/share.html"
    permission_required = "motions.share"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        context["motion"] = motion
        context["form"] = MotionShareForm()
        context["shares"] = motion.shares.select_related("user", "role", "organization", "party_group").order_by(
            "-created_at"
        )

        return context

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
                    return redirect("work:motion_share", org_slug=self.organization.slug, motion_id=motion.id)

            share.save()
            messages.success(request, "Freigabe erstellt.")

        return redirect("work:motion_share", org_slug=self.organization.slug, motion_id=motion.id)


class MotionAIAssistantView(WorkViewMixin, View):
    """API endpoint for AI assistant actions."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        if not motion_ai_service.is_available():
            return JsonResponse({"error": "AI-Service nicht verfügbar"}, status=503)

        form = AIAssistantForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"error": "Ungültige Anfrage"}, status=400)

        action = form.cleaned_data["action"]
        text = form.cleaned_data.get("text", "")
        instruction = form.cleaned_data.get("instruction", "")
        motion_type = form.cleaned_data.get("motion_type", "motion")

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
            else:
                return JsonResponse({"error": "Unbekannte Aktion"}, status=400)

            if result.success:
                return JsonResponse({"success": True, "content": result.content, "suggestions": result.suggestions})
            else:
                return JsonResponse({"error": result.error}, status=500)

        except Exception as e:
            logger.exception(f"[MotionAI] Action failed: {e}")
            return JsonResponse({"error": "KI-Aktion fehlgeschlagen."}, status=500)


class MotionCommentView(WorkViewMixin, View):
    """API endpoint for motion comments."""

    permission_required = "motions.comment"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        form = MotionCommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.motion = motion
            comment.author = self.membership
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
                        },
                    }
                )

            messages.success(request, "Kommentar hinzugefügt.")
            return redirect("work:motion_detail", org_slug=self.organization.slug, motion_id=motion.id)

        return JsonResponse({"error": form.errors}, status=400)


class MotionStatusView(WorkViewMixin, View):
    """API endpoint for changing motion status."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        new_status = request.POST.get("status")
        if new_status not in dict(Motion.STATUS_CHOICES):
            return JsonResponse({"error": "Ungültiger Status"}, status=400)

        # Validate status transitions
        valid_transitions = {
            "draft": ["review", "archived"],
            "review": ["draft", "approved", "rejected"],
            "approved": ["submitted", "draft"],
            "submitted": ["completed", "rejected"],
            "completed": ["archived"],
            "rejected": ["draft", "archived"],
            "archived": ["draft"],
        }

        if new_status not in valid_transitions.get(motion.status, []):
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
        return redirect("work:motion_detail", org_slug=self.organization.slug, motion_id=motion.id)


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

        # Only author or someone with edit permission can resolve
        if comment.author != self.membership:
            if not self.membership.has_permission("motions.edit_all"):
                return JsonResponse({"error": "Keine Berechtigung"}, status=403)

        comment.is_resolved = True
        comment.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        messages.success(request, "Kommentar als erledigt markiert.")
        return redirect("work:motion_detail", org_slug=self.organization.slug, motion_id=comment.motion.id)


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
            return JsonResponse({"error": "DOCX-Export wird noch implementiert"}, status=501)

        else:
            return JsonResponse({"error": f"Unbekanntes Export-Format: {export_format}"}, status=400)


class MotionImportView(WorkViewMixin, TemplateView):
    """Import PDFs as documents."""

    template_name = "work/motions/import.html"
    permission_required = "motions.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )
        return context

    def post(self, request, *args, **kwargs):
        pdf_files = request.FILES.getlist("pdf_files")

        if not pdf_files:
            messages.error(request, "Bitte wählen Sie mindestens eine PDF-Datei aus.")
            return redirect("work:motion_import", org_slug=self.organization.slug)

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

        # Import PDFs
        results = motion_import_service.import_multiple_pdfs(
            pdf_files=pdf_files,
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
                # Redirect to edit page for single import
                return redirect("work:motion_edit", org_slug=self.organization.slug, motion_id=motion.id)
            else:
                messages.success(request, f"{len(successes)} Dokumente erfolgreich importiert.")

        if failures:
            for failure in failures:
                messages.error(request, f"Import fehlgeschlagen: {failure.error}")

        return redirect("work:motions", org_slug=self.organization.slug)


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
        context["type_count"] = MotionType.objects.filter(organization=self.organization).count()
        context["template_count"] = MotionTemplate.objects.filter(organization=self.organization).count()
        context["letterhead_count"] = OrganizationLetterhead.objects.filter(organization=self.organization).count()

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
        )

        messages.success(request, f"Dokumenttyp '{name}' erstellt.")
        return redirect("work:motion_type_list", org_slug=self.organization.slug)


class MotionTypeEditView(WorkViewMixin, TemplateView):
    """Edit a document type."""

    template_name = "work/motions/settings/type_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"
        context["is_new"] = False

        context["motion_type"] = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)
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
        is_default = request.POST.get("is_default") == "on"

        if is_default and not motion_type.is_default:
            MotionType.objects.filter(organization=self.organization, is_default=True).update(is_default=False)
        motion_type.is_default = is_default

        motion_type.save()

        messages.success(request, f"Dokumenttyp '{motion_type.name}' aktualisiert.")
        return redirect("work:motion_type_list", org_slug=self.organization.slug)


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

        return redirect("work:motion_type_list", org_slug=self.organization.slug)


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

            # If setting as default, unset others
            if template.is_default:
                MotionTemplate.objects.filter(organization=self.organization, is_default=True).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' erstellt.")
            return redirect("work:motion_template_list", org_slug=self.organization.slug)

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

            if template.is_default:
                MotionTemplate.objects.filter(organization=self.organization, is_default=True).exclude(
                    id=template.id
                ).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' aktualisiert.")
            return redirect("work:motion_template_list", org_slug=self.organization.slug)

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

        return redirect("work:motion_template_list", org_slug=self.organization.slug)


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


class LetterheadCreateView(WorkViewMixin, TemplateView):
    """Create a new letterhead."""

    template_name = "work/motions/settings/letterhead_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"
        context["is_new"] = True
        return context

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        pdf_file = request.FILES.get("pdf_file")

        if not name or not pdf_file:
            messages.error(request, "Name und PDF-Datei sind erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Validate file is PDF
        if not pdf_file.name.lower().endswith(".pdf"):
            messages.error(request, "Nur PDF-Dateien sind erlaubt.")
            return self.render_to_response(self.get_context_data(**kwargs))

        is_default = request.POST.get("is_default") == "on"
        if is_default:
            OrganizationLetterhead.objects.filter(organization=self.organization, is_default=True).update(
                is_default=False
            )

        OrganizationLetterhead.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            pdf_file=pdf_file,
            content_margin_top=int(request.POST.get("content_margin_top", 60)),
            content_margin_left=int(request.POST.get("content_margin_left", 25)),
            content_margin_right=int(request.POST.get("content_margin_right", 20)),
            content_margin_bottom=int(request.POST.get("content_margin_bottom", 30)),
            font_family=request.POST.get("font_family", "Arial").strip(),
            font_size=int(request.POST.get("font_size", 11)),
            is_default=is_default,
        )

        messages.success(request, f"Briefkopf '{name}' erstellt.")
        return redirect("work:letterhead_list", org_slug=self.organization.slug)


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

        # Handle file upload (optional for edit)
        new_file = request.FILES.get("pdf_file")
        if new_file:
            if not new_file.name.lower().endswith(".pdf"):
                messages.error(request, "Nur PDF-Dateien sind erlaubt.")
                return self.render_to_response(self.get_context_data(**kwargs))
            letterhead.pdf_file = new_file

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
        return redirect("work:letterhead_list", org_slug=self.organization.slug)


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

        return redirect("work:letterhead_list", org_slug=self.organization.slug)


# =============================================================================
# Trash (Papierkorb) Views
# =============================================================================


class MotionTrashView(WorkViewMixin, TemplateView):
    """View deleted motions (Papierkorb)."""

    template_name = "work/motions/trash.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "motions"

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
        return redirect("work:motion_trash", org_slug=self.organization.slug)


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
        return redirect("work:motion_trash", org_slug=self.organization.slug)


class MotionEmptyTrashView(WorkViewMixin, View):
    """Empty all items from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        count = Motion.objects.filter(organization=self.organization, status="deleted").count()

        Motion.objects.filter(organization=self.organization, status="deleted").delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "count": count})

        messages.success(request, f"Papierkorb geleert ({count} Dokumente gelöscht).")
        return redirect("work:motion_trash", org_slug=self.organization.slug)
