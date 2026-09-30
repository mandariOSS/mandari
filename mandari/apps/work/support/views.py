# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Support views for the Work module.

Support-Tickets der Organisationen. Anleitungen stehen in der Anwenderdokumentation
(``apps.common.hilfe``); die frühere Wissensdatenbank leitet nur noch dorthin weiter (Issue #589).
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.hilfe import HILFETHEMEN, alte_wissensdatenbank_url
from apps.common.mixins import WorkViewMixin
from apps.common.uploads import DOCUMENTS, MB, validate_upload
from apps.work.files import attachment_response
from apps.work.notifications.services import NotificationHub

from .models import (
    SupportTicket,
    SupportTicketAttachment,
    SupportTicketMessage,
)


class SupportListView(WorkViewMixin, TemplateView):
    """Eigene Support-Tickets und Themenübersicht der Anwenderdokumentation."""

    template_name = "work/support/list.html"
    permission_required = "support.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        # Get user's tickets
        tickets = SupportTicket.objects.filter(organization=self.organization, created_by=self.membership).order_by(
            "-updated_at"
        )

        # Filter by status
        status_filter = self.request.GET.get("status", "")
        if status_filter:
            tickets = tickets.filter(status=status_filter)

        context["tickets"] = tickets
        context["status_filter"] = status_filter
        context["status_choices"] = SupportTicket.STATUS_CHOICES

        # Statistics
        context["ticket_stats"] = {
            "total": tickets.count(),
            "open": tickets.filter(status="open").count(),
            "in_progress": tickets.filter(status="in_progress").count(),
            "waiting": tickets.filter(status="waiting").count(),
            "escalated": tickets.filter(status="escalated").count(),
            "on_hold": tickets.filter(status="on_hold").count(),
            "resolved": tickets.filter(status__in=["resolved", "closed"]).count(),
        }

        # Anleitungen in der Dokumentation (ersetzt die frühere Wissensdatenbank, Issue #589)
        context["hilfethemen"] = HILFETHEMEN

        return context


class SupportCreateView(WorkViewMixin, TemplateView):
    """Create a new support ticket."""

    template_name = "work/support/create.html"
    permission_required = "support.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"
        context["categories"] = SupportTicket.CATEGORY_CHOICES
        context["priorities"] = SupportTicket.PRIORITY_CHOICES
        return context

    def post(self, request, *args, **kwargs):
        """Create a new ticket."""
        subject = request.POST.get("subject", "").strip()
        description = request.POST.get("description", "").strip()
        category = request.POST.get("category", "question")
        priority = request.POST.get("priority", "normal")

        if not subject or not description:
            messages.error(request, "Bitte füllen Sie alle Pflichtfelder aus.")
            return redirect("work:support_create", org_slug=self.organization.slug)

        # Create ticket (without encrypted field)
        ticket = SupportTicket(
            organization=self.organization,
            subject=subject,
            category=category,
            priority=priority,
            created_by=self.membership,
        )
        # Set encrypted description using the helper method
        ticket.set_description_encrypted(description)
        ticket.save()

        # Handle file attachments
        files = request.FILES.getlist("attachments")
        for f in files[:5]:  # Limit to 5 files
            try:
                validate_upload(f, allowed=DOCUMENTS, max_bytes=10 * MB, bezeichnung="Anlage")
            except ValidationError:
                continue  # ungeeignete Anlage uebergehen, Ticket bzw. Nachricht bleibt bestehen
            SupportTicketAttachment.objects.create(
                ticket=ticket,
                file=f,
                filename=f.name,
                mime_type=f.content_type or "application/octet-stream",
                file_size=f.size,
            )

        # Send notification (for staff/logging purposes)
        NotificationHub.notify_support_ticket_created(ticket, self.membership)

        messages.success(request, "Ihr Support-Ticket wurde erstellt.")
        return redirect("work:support_detail", org_slug=self.organization.slug, ticket_id=ticket.id)


class SupportDetailView(WorkViewMixin, TemplateView):
    """Detail view of a support ticket with message thread."""

    template_name = "work/support/detail.html"
    permission_required = "support.view"

    def get(self, request, *args, **kwargs):
        # Zugriffsprüfung vor dem Rendern: Eine Weiterleitung ist keine Kontextangabe
        self.ticket = get_object_or_404(
            SupportTicket.objects.select_related("created_by__user", "assigned_to"),
            id=self.kwargs.get("ticket_id"),
            organization=self.organization,
        )
        if self.ticket.created_by != self.membership and not self.has_permission("support.manage"):
            messages.error(request, "Sie haben keinen Zugriff auf dieses Ticket.")
            return redirect("work:support", org_slug=self.organization.slug)
        return self.render_to_response(self.get_context_data(**kwargs))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        ticket = self.ticket
        context["ticket"] = ticket

        # Get messages (exclude internal notes for non-staff)
        messages_qs = ticket.messages.select_related("author_membership__user", "author_staff").prefetch_related(
            "attachments"
        )

        if not self.request.user.is_staff:
            messages_qs = messages_qs.filter(is_internal=False)

        context["messages"] = messages_qs

        # Get attachments
        context["attachments"] = ticket.attachments.filter(message__isnull=True)

        return context

    def post(self, request, *args, **kwargs):
        """Add a message to the ticket."""
        ticket_id = self.kwargs.get("ticket_id")
        ticket = get_object_or_404(SupportTicket, id=ticket_id, organization=self.organization)

        # Gleiche Zugriffsprüfung wie im GET: nur Ersteller:in oder support.manage
        # dürfen antworten/schließen/wieder öffnen - sonst könnte ein Mitglied
        # mit reinem support.view fremde Tickets manipulieren.
        if ticket.created_by != self.membership and not self.has_permission("support.manage"):
            messages.error(request, "Sie haben keinen Zugriff auf dieses Ticket.")
            return redirect("work:support", org_slug=self.organization.slug)

        action = request.POST.get("action", "reply")

        if action == "reply":
            content = request.POST.get("content", "").strip()
            if not content:
                messages.error(request, "Bitte geben Sie eine Nachricht ein.")
                return redirect("work:support_detail", org_slug=self.organization.slug, ticket_id=ticket.id)

            # Create message (without encrypted field)
            msg = SupportTicketMessage(
                ticket=ticket,
                author_membership=self.membership,
            )
            # Set encrypted content using the helper method
            msg.set_content_encrypted(content)
            msg.save()

            # Handle attachments
            files = request.FILES.getlist("attachments")
            for f in files[:3]:  # Limit to 3 files per message
                try:
                    validate_upload(f, allowed=DOCUMENTS, max_bytes=10 * MB, bezeichnung="Anlage")
                except ValidationError:
                    continue  # ungeeignete Anlage uebergehen, Ticket bzw. Nachricht bleibt bestehen
                SupportTicketAttachment.objects.create(
                    ticket=ticket,
                    message=msg,
                    file=f,
                    filename=f.name,
                    mime_type=f.content_type or "application/octet-stream",
                    file_size=f.size,
                )

            # Update ticket status and track customer reply timestamp
            ticket.last_customer_reply_at = timezone.now()
            old_status = ticket.status

            # Auto-reopen if waiting for response or on hold
            if ticket.status in ["waiting", "on_hold", "resolved"]:
                if ticket.status == "on_hold":
                    ticket.on_hold_at = None
                    ticket.on_hold_reason = ""
                ticket.status = "open"

                # Notify about status change
                NotificationHub.notify_support_ticket_status_change(ticket, old_status, ticket.status)
            ticket.save()

            messages.success(request, "Ihre Nachricht wurde gesendet.")

        elif action == "close":
            old_status = ticket.status
            ticket.status = "closed"
            ticket.closed_at = timezone.now()
            ticket.save()

            # Notify about status change
            NotificationHub.notify_support_ticket_status_change(ticket, old_status, "closed")
            messages.success(request, "Das Ticket wurde geschlossen.")

        elif action == "reopen":
            if ticket.status in ["resolved", "closed", "on_hold"]:
                old_status = ticket.status
                ticket.status = "open"
                ticket.resolved_at = None
                ticket.closed_at = None
                ticket.on_hold_at = None
                ticket.on_hold_reason = ""
                ticket.save()

                # Notify about status change
                NotificationHub.notify_support_ticket_status_change(ticket, old_status, "open")
                messages.success(request, "Das Ticket wurde wieder geöffnet.")

        return redirect("work:support_detail", org_slug=self.organization.slug, ticket_id=ticket.id)


class SupportAttachmentDownloadView(WorkViewMixin, View):
    """
    Anhang eines Support-Tickets herunterladen.

    Anhänge gehen nicht über ``/media/`` hinaus (apps/work/files.py), sondern nur hier – mit
    derselben Grenze wie die Ticket-Ansicht: Ersteller:in oder ``support.manage``; Anhänge
    interner Notizen nur für das Support-Team.
    """

    permission_required = "support.view"

    def get(self, request, *args, **kwargs):
        ticket = get_object_or_404(SupportTicket, id=kwargs["ticket_id"], organization=self.organization)
        if ticket.created_by != self.membership and not self.has_permission("support.manage"):
            raise Http404("Datei nicht gefunden.")
        attachment = get_object_or_404(SupportTicketAttachment, id=kwargs["attachment_id"], ticket=ticket)
        if attachment.message is not None and attachment.message.is_internal and not request.user.is_staff:
            raise Http404("Datei nicht gefunden.")
        return attachment_response(attachment.file, attachment.filename)


class SupportTicketMessagesPartialView(WorkViewMixin, View):
    """HTMX partial view for ticket messages - enables real-time updates."""

    permission_required = "support.view"

    def get(self, request, *args, **kwargs):
        """Return message thread HTML partial for HTMX polling."""
        from django.http import HttpResponse
        from django.template.loader import render_to_string

        ticket_id = self.kwargs.get("ticket_id")
        ticket = get_object_or_404(SupportTicket, id=ticket_id, organization=self.organization)

        # Check access
        if ticket.created_by != self.membership and not self.has_permission("support.manage"):
            return HttpResponse("", status=403)

        # Get messages (exclude internal notes for non-staff)
        messages_qs = ticket.messages.select_related("author_membership__user", "author_staff").prefetch_related(
            "attachments"
        )

        if not request.user.is_staff:
            messages_qs = messages_qs.filter(is_internal=False)

        # Get current message count for comparison
        message_count = messages_qs.count()

        # Check if client already has this count (no update needed)
        client_count = request.GET.get("count")
        if client_count and int(client_count) == message_count:
            # Return 204 No Content - no update needed
            return HttpResponse(status=204)

        html = render_to_string(
            "work/support/partials/message_thread.html",
            {
                "messages": messages_qs,
                "ticket": ticket,
                "organization": self.organization,
            },
            request=request,
        )

        response = HttpResponse(html)
        response["X-Message-Count"] = str(message_count)
        return response


class KnowledgeBaseRedirectView(View):
    """
    Adressen der früheren Wissensdatenbank auf die Nachfolgeseite der Dokumentation umleiten.

    Die Dokumentation ist öffentlich; die Weiterleitung braucht daher weder Anmeldung noch Recht.
    """

    def get(self, request, *args, **kwargs):
        return redirect(alte_wissensdatenbank_url(kwargs.get("category_slug"), kwargs.get("article_slug")))
