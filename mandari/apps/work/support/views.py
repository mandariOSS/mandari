# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Support views for the Work module.

Provides support ticket system and knowledge base for organizations.
"""

import re

from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.notifications.services import NotificationHub

from .models import (
    ArticleFeedback,
    KnowledgeBaseArticle,
    KnowledgeBaseCategory,
    SupportTicket,
    SupportTicketAttachment,
    SupportTicketMessage,
)


class SupportListView(WorkViewMixin, TemplateView):
    """List of support tickets and knowledge base overview."""

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

        # Knowledge base categories
        context["kb_categories"] = (
            KnowledgeBaseCategory.objects.filter(is_active=True)
            .annotate(article_count_published=Count("articles", filter=Q(articles__is_published=True)))
            .order_by("sort_order")
        )

        # Featured articles
        context["featured_articles"] = KnowledgeBaseArticle.objects.filter(
            is_published=True, is_featured=True
        ).select_related("category")[:5]

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

        # Suggested articles based on category (if any)
        category = self.request.GET.get("category", "")
        if category:
            context["suggested_articles"] = KnowledgeBaseArticle.objects.filter(
                is_published=True, category__slug=category
            )[:3]

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
            if f.size <= 10 * 1024 * 1024:  # Max 10MB
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        ticket_id = self.kwargs.get("ticket_id")
        ticket = get_object_or_404(
            SupportTicket.objects.select_related("created_by__user", "assigned_to"),
            id=ticket_id,
            organization=self.organization,
        )

        # Check if user can access this ticket
        if ticket.created_by != self.membership and not self.has_permission("support.manage"):
            messages.error(self.request, "Sie haben keinen Zugriff auf dieses Ticket.")
            return redirect("work:support", org_slug=self.organization.slug)

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
                if f.size <= 10 * 1024 * 1024:
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


class KnowledgeBaseView(WorkViewMixin, TemplateView):
    """Knowledge base main page."""

    template_name = "work/support/knowledge_base.html"
    permission_required = "support.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        # Search query
        query = self.request.GET.get("q", "").strip()
        context["search_query"] = query

        if query:
            # Search articles
            articles = (
                KnowledgeBaseArticle.objects.filter(is_published=True)
                .filter(
                    Q(title__icontains=query)
                    | Q(excerpt__icontains=query)
                    | Q(content__icontains=query)
                    | Q(tags__icontains=query)
                )
                .select_related("category")
                .order_by("-views_count")[:20]
            )
            context["search_results"] = articles
        else:
            # Show categories with articles
            categories = (
                KnowledgeBaseCategory.objects.filter(is_active=True)
                .prefetch_related("articles")
                .annotate(article_count_published=Count("articles", filter=Q(articles__is_published=True)))
                .filter(article_count_published__gt=0)
                .order_by("sort_order")
            )

            context["categories"] = categories

            # Featured articles
            context["featured_articles"] = KnowledgeBaseArticle.objects.filter(
                is_published=True, is_featured=True
            ).select_related("category")[:5]

            # Popular articles
            context["popular_articles"] = (
                KnowledgeBaseArticle.objects.filter(is_published=True)
                .select_related("category")
                .order_by("-views_count")[:10]
            )

        return context


class KnowledgeBaseCategoryView(WorkViewMixin, TemplateView):
    """Knowledge base category view."""

    template_name = "work/support/kb_category.html"
    permission_required = "support.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        category_slug = self.kwargs.get("category_slug")
        category = get_object_or_404(KnowledgeBaseCategory, slug=category_slug, is_active=True)
        context["category"] = category

        # Get articles in this category
        articles = KnowledgeBaseArticle.objects.filter(category=category, is_published=True).order_by(
            "-is_featured", "-published_at"
        )

        context["articles"] = articles

        return context


class KnowledgeBaseArticleView(WorkViewMixin, TemplateView):
    """Knowledge base article view."""

    template_name = "work/support/kb_article.html"
    permission_required = "support.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "support"

        category_slug = self.kwargs.get("category_slug")
        article_slug = self.kwargs.get("article_slug")

        article = get_object_or_404(
            KnowledgeBaseArticle.objects.select_related("category", "author"),
            category__slug=category_slug,
            slug=article_slug,
            is_published=True,
        )

        # Increment view count
        article.views_count += 1
        article.save(update_fields=["views_count"])

        context["article"] = article

        # Convert markdown to HTML (simple conversion)
        context["article_html"] = self._render_markdown(article.content)

        # Related articles
        context["related_articles"] = (
            KnowledgeBaseArticle.objects.filter(category=article.category, is_published=True)
            .exclude(id=article.id)
            .order_by("-views_count")[:5]
        )

        # Check if user already gave feedback
        session_key = self.request.session.session_key or ""
        context["user_feedback"] = (
            ArticleFeedback.objects.filter(article=article, session_key=session_key).first() if session_key else None
        )

        return context

    def _render_markdown(self, text):
        """Simple markdown to HTML conversion."""
        import html

        # Escape HTML
        text = html.escape(text)

        # Headers
        text = re.sub(
            r"^### (.+)$",
            r'<h3 class="text-lg font-semibold mt-6 mb-2">\1</h3>',
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(
            r"^## (.+)$",
            r'<h2 class="text-xl font-semibold mt-8 mb-3">\1</h2>',
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(
            r"^# (.+)$",
            r'<h1 class="text-2xl font-bold mt-8 mb-4">\1</h1>',
            text,
            flags=re.MULTILINE,
        )

        # Bold and italic
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

        # Code blocks
        text = re.sub(
            r"```(\w+)?\n(.*?)\n```",
            r'<pre class="bg-gray-100 dark:bg-gray-800 p-4 rounded-lg overflow-x-auto my-4"><code>\2</code></pre>',
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"`(.+?)`", r'<code class="bg-gray-100 dark:bg-gray-800 px-1 rounded">\1</code>', text)

        # Lists
        lines = text.split("\n")
        result = []
        in_list = False
        for line in lines:
            if line.strip().startswith("- "):
                if not in_list:
                    result.append('<ul class="list-disc pl-6 my-4 space-y-1">')
                    in_list = True
                result.append(f"<li>{line.strip()[2:]}</li>")
            else:
                if in_list:
                    result.append("</ul>")
                    in_list = False
                result.append(line)
        if in_list:
            result.append("</ul>")
        text = "\n".join(result)

        # Paragraphs
        text = re.sub(r"\n\n+", '</p><p class="my-4">', text)
        text = f'<p class="my-4">{text}</p>'

        # Links
        text = re.sub(
            r"\[(.+?)\]\((.+?)\)",
            r'<a href="\2" class="text-primary-600 hover:underline" target="_blank">\1</a>',
            text,
        )

        return text


class ArticleFeedbackView(WorkViewMixin, View):
    """Handle article feedback submission."""

    permission_required = "support.view"

    def post(self, request, *args, **kwargs):
        """Submit feedback for an article."""
        article_id = kwargs.get("article_id")
        article = get_object_or_404(KnowledgeBaseArticle, id=article_id, is_published=True)

        is_helpful = request.POST.get("is_helpful") == "true"
        comment = request.POST.get("comment", "").strip()

        # Get or create session key
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key

        # Check for existing feedback
        existing = ArticleFeedback.objects.filter(article=article, session_key=session_key).first()

        if existing:
            # Update existing feedback
            old_helpful = existing.is_helpful
            existing.is_helpful = is_helpful
            existing.comment = comment
            existing.save()

            # Update article counts
            if old_helpful != is_helpful:
                if is_helpful:
                    article.helpful_yes += 1
                    article.helpful_no -= 1
                else:
                    article.helpful_yes -= 1
                    article.helpful_no += 1
                article.save(update_fields=["helpful_yes", "helpful_no"])
        else:
            # Create new feedback
            ArticleFeedback.objects.create(
                article=article,
                is_helpful=is_helpful,
                comment=comment,
                user=request.user if request.user.is_authenticated else None,
                session_key=session_key,
            )

            # Update article counts
            if is_helpful:
                article.helpful_yes += 1
            else:
                article.helpful_no += 1
            article.save(update_fields=["helpful_yes", "helpful_no"])

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "helpful_percentage": article.helpful_percentage,
                }
            )

        messages.success(request, "Vielen Dank für Ihr Feedback!")
        # SECURITY: Use Django's built-in URL validation to prevent Open Redirect
        default_url = f"/work/{self.organization.slug}/support/"
        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(referer)
        return redirect(default_url)


class ArticleSearchAPIView(WorkViewMixin, View):
    """API for searching knowledge base articles."""

    permission_required = "support.view"

    def get(self, request, *args, **kwargs):
        """Search articles."""
        query = request.GET.get("q", "").strip()

        if len(query) < 2:
            return JsonResponse({"results": []})

        articles = (
            KnowledgeBaseArticle.objects.filter(is_published=True)
            .filter(Q(title__icontains=query) | Q(excerpt__icontains=query) | Q(tags__icontains=query))
            .select_related("category")
            .values("id", "title", "excerpt", "category__name", "category__slug", "slug")[:10]
        )

        results = [
            {
                "id": str(a["id"]),
                "title": a["title"],
                "excerpt": a["excerpt"][:100] + "..." if len(a["excerpt"]) > 100 else a["excerpt"],
                "category": a["category__name"],
                "url": f"/work/{self.organization.slug}/support/kb/{a['category__slug']}/{a['slug']}/",
            }
            for a in articles
        ]

        return JsonResponse({"results": results})
