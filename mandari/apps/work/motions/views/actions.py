# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging
import uuid

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView, View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..forms import (
    AIAssistantForm,
    MotionCommentForm,
    MotionDocumentForm,
    MotionShareForm,
)
from ..import_service import motion_import_service
from ..models import (
    Motion,
    MotionApproval,
    MotionChecklistItem,
    MotionComment,
    MotionShare,
    MotionType,
)
from ..services import MotionAIService
from ._helpers import _broadcast_doc_reload, _get_org_folder_or_404


class MotionShareView(WorkViewMixin, TemplateView):
    """Share settings for a motion (legacy - redirects to editor)."""

    template_name = "work/motions/share.html"
    permission_required = "motions.share"

    def get(self, request, *args, **kwargs):
        # Redirect to editor page - sharing is now a modal in the editor
        return redirect("work:document_editor", org_slug=self.organization.slug, motion_id=kwargs.get("motion_id"))

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        # Per-Objekt-Recht: Freigeben nur durch Autor oder motions.edit_all
        # (konsistent zu MotionShareUpdateView/MotionShareRemoveView).
        if motion.author != self.membership and not self.membership.has_permission("motions.edit_all"):
            messages.error(request, "Keine Berechtigung für dieses Dokument.")
            return redirect("work:documents", org_slug=self.organization.slug)

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
                quota = motion_ai_service.get_quota_status()
                return JsonResponse(
                    {
                        "success": True,
                        "content": result.content,
                        "suggestions": result.suggestions,
                        "tokens_used": result.total_tokens,
                        "quota": quota,
                    }
                )
            return JsonResponse({"error": result.error}, status=500)

        except Exception as e:
            logger.exception(f"[MotionAI] Action failed: {e}")
            return JsonResponse({"error": "KI-Aktion fehlgeschlagen."}, status=500)


class MotionCommentView(WorkViewMixin, View):
    """API endpoint for motion comments."""

    permission_required = "motions.comment"
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_comment)

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

        # Per-Objekt-Recht: org-weites motions.edit reicht NICHT — sonst
        # könnten Mitglieder ohne Dokumentzugriff den Status fremder
        # privater Dokumente manipulieren (IDOR).
        if not motion.can_edit(self.membership):
            return JsonResponse({"error": "Kein Zugriff auf dieses Dokument."}, status=403)

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

        was_locked = motion.is_status_locked
        motion.status = new_status
        if new_status == "submitted":
            motion.submitted_at = timezone.now()
        motion.save()

        # Statuswechsel über die Sperrgrenze (Motion.EDITABLE_STATUSES):
        # offene Kollab-Editoren neu laden lassen, damit die herabgestufte
        # (bzw. wiedererlangte) Zugriffsstufe sofort greift.
        if motion.is_status_locked != was_locked:
            _broadcast_doc_reload(motion)

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

        # Per-Objekt-Recht (siehe MotionStatusView): kein Meta-Update ohne
        # Zugriff auf das konkrete Dokument.
        if not motion.can_edit(self.membership):
            return JsonResponse({"error": "Kein Zugriff auf dieses Dokument."}, status=403)

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

        elif action == "set_folder":
            folder_id = request.POST.get("folder", "").strip()
            if folder_id:
                motion.folder = _get_org_folder_or_404(self.organization, folder_id)
            else:
                motion.folder = None
            motion.save(update_fields=["folder", "updated_at"])

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

        # Per-Objekt-Recht (siehe MotionStatusView)
        if not motion.can_edit(self.membership):
            return JsonResponse({"error": "Kein Zugriff auf dieses Dokument."}, status=403)

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

        # Nur wer das Dokument selbst sehen darf, kann Freigaben anfragen.
        # Sonst könnte ein motions.edit-Mitglied über den Freigabe-Weg einer
        # beliebigen Person (auch sich selbst) Zugriff auf ein PRIVATES Dokument
        # verschaffen und dessen Sichtbarkeit auf "shared" heben (IDOR/Leak).
        if not motion.can_access(self.membership):
            return JsonResponse({"error": "Kein Zugriff auf dieses Dokument."}, status=403)

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

        # Per-Objekt-Recht (siehe MotionStatusView)
        if not motion.can_edit(self.membership):
            return JsonResponse({"error": "Kein Zugriff auf dieses Dokument."}, status=403)

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
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_access)

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
    guest_allowed = True  # Zugriff wird share-basiert geprüft (can_access)

    def get(self, request, *args, **kwargs):
        from django.http import HttpResponse

        from ..export_service import motion_export_service

        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization)

        if not motion.can_access(self.membership):
            return JsonResponse({"error": "Keine Berechtigung"}, status=403)

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
