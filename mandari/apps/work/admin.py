# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin configuration for Work module.

Provides admin interfaces for:
- Knowledge Base (categories and articles)
- Support tickets with full management capabilities
"""

from django.contrib import admin
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from unfold.admin import ModelAdmin, StackedInline, TabularInline

from .support.models import (
    ArticleFeedback,
    KnowledgeBaseArticle,
    KnowledgeBaseCategory,
    SupportTicket,
    SupportTicketAttachment,
    SupportTicketMessage,
)

# =============================================================================
# Support Ticket Badge for Unfold Sidebar
# =============================================================================


def support_ticket_badge(request):
    """
    Returns badge content for the Support-Tickets menu item.
    Shows count of open/in-progress tickets that need attention.
    """
    open_count = SupportTicket.objects.filter(status__in=["open", "in_progress", "escalated"]).count()
    if open_count > 0:
        return str(open_count)
    return None


# =============================================================================
# Knowledge Base Admin
# =============================================================================


@admin.register(KnowledgeBaseCategory)
class KnowledgeBaseCategoryAdmin(ModelAdmin):
    """
    Admin für Knowledge Base Kategorien.

    Ermöglicht:
    - Erstellen und Bearbeiten von Kategorien
    - Festlegen von Icons und Farben
    - Sortierung anpassen
    """

    list_display = (
        "name",
        "slug",
        "icon_preview",
        "color_preview",
        "article_count",
        "is_active",
        "sort_order",
    )
    list_filter = ("is_active", "color")
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")

    fieldsets = (
        (
            "Kategorie",
            {
                "fields": ("name", "slug", "description"),
            },
        ),
        (
            "Darstellung",
            {
                "fields": ("icon", "color", "sort_order"),
                "description": "Icon: Lucide Icon Name (z.B. 'book-open', 'settings', 'help-circle'). Farbe: Tailwind Farbname (z.B. 'blue', 'green', 'purple').",
            },
        ),
        (
            "Status",
            {
                "fields": ("is_active",),
            },
        ),
    )

    def icon_preview(self, obj):
        return format_html(
            '<span style="font-family: monospace; background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">{}</span>',
            obj.icon,
        )

    icon_preview.short_description = "Icon"

    def color_preview(self, obj):
        colors = {
            "blue": "#3b82f6",
            "green": "#22c55e",
            "red": "#ef4444",
            "amber": "#f59e0b",
            "purple": "#a855f7",
            "pink": "#ec4899",
            "gray": "#6b7280",
            "indigo": "#6366f1",
            "teal": "#14b8a6",
        }
        hex_color = colors.get(obj.color, "#6b7280")
        return format_html(
            '<span style="display: inline-block; width: 20px; height: 20px; background: {}; border-radius: 4px; vertical-align: middle;"></span> {}',
            hex_color,
            obj.color,
        )

    color_preview.short_description = "Farbe"

    def article_count(self, obj):
        count = obj.articles.filter(is_published=True).count()
        total = obj.articles.count()
        if total > count:
            return f"{count} ({total} gesamt)"
        return str(count)

    article_count.short_description = "Artikel"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_article_count=Count("articles", filter=Q(articles__is_published=True)))
        )


@admin.register(KnowledgeBaseArticle)
class KnowledgeBaseArticleAdmin(ModelAdmin):
    """
    Admin für Knowledge Base Artikel.

    Ermöglicht:
    - Erstellen und Bearbeiten von Artikeln
    - Markdown-Inhalte
    - Veröffentlichung steuern
    - Tags und SEO
    """

    list_display = (
        "title",
        "category",
        "is_published",
        "is_featured",
        "views_count",
        "helpful_rating",
        "updated_at",
    )
    list_filter = ("is_published", "is_featured", "category")
    search_fields = ("title", "excerpt", "content", "tags")
    prepopulated_fields = {"slug": ("title",)}
    ordering = ("-updated_at",)
    date_hierarchy = "created_at"
    autocomplete_fields = ("category", "author")

    fieldsets = (
        (
            "Artikel",
            {
                "fields": ("title", "slug", "category", "excerpt"),
            },
        ),
        (
            "Inhalt",
            {
                "fields": ("content",),
                "description": "Markdown-formatierter Inhalt. Unterstützt: # Überschriften, **fett**, *kursiv*, `code`, - Listen, [Links](url).",
            },
        ),
        (
            "Veröffentlichung",
            {
                "fields": ("is_published", "is_featured", "published_at"),
            },
        ),
        (
            "SEO & Suche",
            {
                "fields": ("tags",),
                "description": "Kommagetrennte Tags für die Suche, z.B. 'anmeldung, login, passwort'.",
                "classes": ("collapse",),
            },
        ),
        (
            "Metadaten",
            {
                "fields": ("author", "views_count", "helpful_yes", "helpful_no"),
                "classes": ("collapse",),
            },
        ),
    )

    readonly_fields = ("views_count", "helpful_yes", "helpful_no")

    def helpful_rating(self, obj):
        total = obj.helpful_yes + obj.helpful_no
        if total == 0:
            return "—"
        percentage = int((obj.helpful_yes / total) * 100)
        color = "green" if percentage >= 70 else "amber" if percentage >= 50 else "red"
        return format_html(
            '<span style="color: {};">{} / {} ({}%)</span>',
            {"green": "#22c55e", "amber": "#f59e0b", "red": "#ef4444"}[color],
            obj.helpful_yes,
            total,
            percentage,
        )

    helpful_rating.short_description = "Bewertung"

    def save_model(self, request, obj, form, change):
        if obj.is_published and not obj.published_at:
            obj.published_at = timezone.now()
        if not obj.author:
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(ArticleFeedback)
class ArticleFeedbackAdmin(ModelAdmin):
    """
    Admin für Artikel-Feedback (nur lesen).

    Ermöglicht Monitoring des Feedbacks ohne Bearbeitung.
    """

    list_display = ("article", "is_helpful", "has_comment", "created_at")
    list_filter = ("is_helpful", "created_at")
    search_fields = ("article__title", "comment")
    readonly_fields = ("article", "is_helpful", "comment", "user", "session_key", "created_at")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_comment(self, obj):
        return bool(obj.comment)

    has_comment.boolean = True
    has_comment.short_description = "Kommentar"


# =============================================================================
# Support Tickets Admin - Full Management Interface
# =============================================================================


class AssignedToMeFilter(admin.SimpleListFilter):
    """Filter to show only tickets assigned to the current user."""

    title = "Meine Tickets"
    parameter_name = "assigned_to_me"

    def lookups(self, request, model_admin):
        return [
            ("yes", "Mir zugewiesen"),
            ("unassigned", "Nicht zugewiesen"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(assigned_to=request.user)
        if self.value() == "unassigned":
            return queryset.filter(assigned_to__isnull=True)
        return queryset


class OpenTicketsFilter(admin.SimpleListFilter):
    """Quick filter for open/active tickets."""

    title = "Schnellfilter"
    parameter_name = "quick"

    def lookups(self, request, model_admin):
        return [
            ("active", "Aktive Tickets"),
            ("needs_response", "Antwort erforderlich"),
            ("urgent", "Dringend/Eskaliert"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.exclude(status__in=["resolved", "closed"])
        if self.value() == "needs_response":
            return queryset.filter(status="open")
        if self.value() == "urgent":
            return queryset.filter(Q(priority="urgent") | Q(status="escalated"))
        return queryset


class SupportTicketAttachmentInline(TabularInline):
    """Inline für Ticket-Anhänge."""

    model = SupportTicketAttachment
    extra = 0
    readonly_fields = ("filename", "mime_type", "file_size_display", "uploaded_at", "file_link")
    fields = ("filename", "file_link", "file_size_display", "uploaded_at")
    can_delete = False

    def file_size_display(self, obj):
        if obj.file_size < 1024:
            return f"{obj.file_size} B"
        elif obj.file_size < 1024 * 1024:
            return f"{obj.file_size / 1024:.1f} KB"
        return f"{obj.file_size / (1024 * 1024):.1f} MB"

    file_size_display.short_description = "Größe"

    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Herunterladen</a>', obj.file.url)
        return "—"

    file_link.short_description = "Datei"

    def has_add_permission(self, request, obj=None):
        return False


class SupportTicketMessageInline(StackedInline):
    """Inline für Ticket-Nachrichten mit besserer Darstellung."""

    model = SupportTicketMessage
    extra = 0
    readonly_fields = ("message_display",)
    fields = ("message_display",)
    can_delete = False
    max_num = 0  # Prevent adding via inline

    def message_display(self, obj):
        """Rich display of message with author and timestamp."""
        import html as html_module

        try:
            content = html_module.escape(obj.get_content_decrypted())
        except Exception:
            content = "[Inhalt verschlüsselt]"

        if obj.author_staff:
            author_name = html_module.escape(obj.author_staff.get_full_name() or obj.author_staff.email)
            author = f"<strong class='ticket-msg-staff'>⚡ {author_name} (Support)</strong>"
            msg_class = "ticket-msg ticket-msg-from-staff"
        else:
            author_name = html_module.escape(
                obj.author_membership.user.get_full_name() if obj.author_membership else "Unbekannt"
            )
            author_email = html_module.escape(obj.author_membership.user.email if obj.author_membership else "")
            author = f"<strong class='ticket-msg-customer'>{author_name}</strong> <span class='ticket-msg-email'>({author_email})</span>"
            msg_class = "ticket-msg ticket-msg-from-customer"

        internal_badge = ""
        if obj.is_internal:
            internal_badge = "<span class='ticket-msg-internal-badge'>Interne Notiz</span>"

        return mark_safe(f"""
            <div class="{msg_class}">
                <div class="ticket-msg-header">
                    {author} {internal_badge}
                    <span class="ticket-msg-time">{obj.created_at.strftime("%d.%m.%Y %H:%M")}</span>
                </div>
                <div class="ticket-msg-content">{content}</div>
            </div>
        """)

    message_display.short_description = ""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SupportTicket)
class SupportTicketAdmin(ModelAdmin):
    """
    Vollständiges Admin-Interface für Support-Tickets.

    Features:
    - Übersicht mit Farbcodierung nach Status/Priorität
    - Schnellfilter für aktive/dringende Tickets
    - Filter für "Meine Tickets"
    - Inline-Anzeige aller Nachrichten
    - Antwortformular direkt im Admin
    - Massenaktionen für Statusänderungen
    - Automatische Benachrichtigungen
    """

    list_display = (
        "ticket_id",
        "priority_icon",
        "subject_short",
        "organization_name",
        "category_badge",
        "status_badge",
        "assigned_badge",
        "message_count",
        "age_display",
    )
    list_display_links = ("ticket_id", "subject_short")
    list_filter = (
        OpenTicketsFilter,
        AssignedToMeFilter,
        "status",
        "priority",
        "category",
        "organization",
    )
    search_fields = ("subject", "organization__name", "id")
    readonly_fields = (
        "id",
        "organization",
        "subject",
        "description_display",
        "category",
        "priority",
        "created_by_display",
        "created_at",
        "updated_at",
        "resolved_at",
        "closed_at",
        "escalated_at",
        "on_hold_at",
        "last_customer_reply_at",
        "ticket_stats",
    )
    ordering = ("-updated_at",)
    date_hierarchy = "created_at"
    inlines = [SupportTicketMessageInline, SupportTicketAttachmentInline]
    actions = [
        "assign_to_me",
        "mark_in_progress",
        "mark_waiting",
        "mark_escalated",
        "mark_on_hold",
        "mark_resolved",
        "mark_closed",
    ]
    list_per_page = 25
    save_on_top = True

    fieldsets = (
        (
            "Ticket-Details",
            {
                "fields": (
                    "ticket_stats",
                    "subject",
                    "description_display",
                    "category",
                    "priority",
                ),
            },
        ),
        (
            "Kunde",
            {
                "fields": ("organization", "created_by_display"),
            },
        ),
        (
            "Bearbeitung",
            {
                "fields": ("status", "assigned_to", "on_hold_reason"),
                "classes": ("wide",),
            },
        ),
        (
            "Zeitverlauf",
            {
                "fields": (
                    ("created_at", "updated_at"),
                    ("last_customer_reply_at", "escalated_at"),
                    ("on_hold_at", "resolved_at", "closed_at"),
                ),
                "classes": ("collapse",),
            },
        ),
    )

    class Media:
        css = {
            "all": ["admin/css/support_tickets.css"],
        }

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("organization", "created_by__user", "assigned_to")
            .prefetch_related("messages")
        )

    def ticket_stats(self, obj):
        """Display ticket statistics as a dashboard."""
        messages_count = obj.messages.count()
        customer_messages = obj.messages.filter(author_membership__isnull=False).count()
        staff_messages = obj.messages.filter(author_staff__isnull=False).count()
        attachments = obj.attachments.count()

        age = timezone.now() - obj.created_at
        age_str = f"{age.days} Tage" if age.days > 0 else f"{age.seconds // 3600} Stunden"

        return mark_safe(f"""
            <div class="ticket-stats">
                <div class="ticket-stat">
                    <div class="ticket-stat-value ticket-stat-blue">{messages_count}</div>
                    <div class="ticket-stat-label">Nachrichten</div>
                </div>
                <div class="ticket-stat">
                    <div class="ticket-stat-value ticket-stat-purple">{customer_messages}</div>
                    <div class="ticket-stat-label">Vom Kunden</div>
                </div>
                <div class="ticket-stat">
                    <div class="ticket-stat-value ticket-stat-green">{staff_messages}</div>
                    <div class="ticket-stat-label">Vom Support</div>
                </div>
                <div class="ticket-stat">
                    <div class="ticket-stat-value ticket-stat-amber">{attachments}</div>
                    <div class="ticket-stat-label">Anhänge</div>
                </div>
                <div class="ticket-stat">
                    <div class="ticket-stat-value ticket-stat-gray">{age_str}</div>
                    <div class="ticket-stat-label">Alter</div>
                </div>
            </div>
        """)

    ticket_stats.short_description = "Übersicht"

    def description_display(self, obj):
        """Display decrypted description with formatting."""
        import html as html_module

        try:
            desc = html_module.escape(obj.get_description_decrypted())
            return mark_safe(f'<div class="ticket-description">{desc}</div>')
        except Exception:
            return "[Verschlüsselt - Kein Zugriff]"

    description_display.short_description = "Beschreibung"

    def created_by_display(self, obj):
        """Display creator info."""
        if obj.created_by:
            user = obj.created_by.user
            return format_html(
                '<strong>{}</strong><br><span style="color: #6b7280;">{}</span>',
                user.get_full_name() or user.email,
                user.email,
            )
        return "—"

    created_by_display.short_description = "Erstellt von"

    def ticket_id(self, obj):
        return format_html(
            '<code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">#{}</code>',
            obj.id.hex[:8],
        )

    ticket_id.short_description = "ID"
    ticket_id.admin_order_field = "id"

    def priority_icon(self, obj):
        icons = {
            "urgent": ("🔴", "Dringend"),
            "high": ("🟠", "Hoch"),
            "normal": ("🔵", "Normal"),
            "low": ("⚪", "Niedrig"),
        }
        icon, title = icons.get(obj.priority, ("⚪", ""))
        return format_html('<span title="{}">{}</span>', title, icon)

    priority_icon.short_description = "!"
    priority_icon.admin_order_field = "priority"

    def subject_short(self, obj):
        max_len = 60
        if len(obj.subject) > max_len:
            return obj.subject[:max_len] + "..."
        return obj.subject

    subject_short.short_description = "Betreff"
    subject_short.admin_order_field = "subject"

    def organization_name(self, obj):
        return obj.organization.name if obj.organization else "—"

    organization_name.short_description = "Organisation"
    organization_name.admin_order_field = "organization__name"

    def category_badge(self, obj):
        colors = {
            "bug": "#ef4444",
            "feature": "#8b5cf6",
            "question": "#3b82f6",
            "account": "#f59e0b",
            "other": "#6b7280",
        }
        color = colors.get(obj.category, "#6b7280")
        return format_html('<span style="color: {}; font-size: 12px;">{}</span>', color, obj.get_category_display())

    category_badge.short_description = "Kategorie"

    def status_badge(self, obj):
        colors = {
            "open": ("#3b82f6", "#eff6ff", "Offen"),
            "in_progress": ("#f59e0b", "#fffbeb", "In Bearbeitung"),
            "waiting": ("#a855f7", "#faf5ff", "Wartet"),
            "escalated": ("#ef4444", "#fef2f2", "Eskaliert"),
            "on_hold": ("#6b7280", "#f3f4f6", "Zurückgestellt"),
            "resolved": ("#22c55e", "#f0fdf4", "Gelöst"),
            "closed": ("#9ca3af", "#f9fafb", "Geschlossen"),
        }
        fg, bg, label = colors.get(obj.status, ("#6b7280", "#f3f4f6", obj.status))
        return format_html(
            '<span style="background: {}; color: {}; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 500;">{}</span>',
            bg,
            fg,
            label,
        )

    status_badge.short_description = "Status"
    status_badge.admin_order_field = "status"

    def assigned_badge(self, obj):
        if obj.assigned_to:
            name = obj.assigned_to.get_full_name() or obj.assigned_to.email.split("@")[0]
            return format_html(
                '<span style="background: #dbeafe; color: #1e40af; padding: 2px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
                name[:15],
            )
        return mark_safe('<span style="color: #9ca3af; font-size: 11px;">Nicht zugewiesen</span>')

    assigned_badge.short_description = "Zugewiesen"
    assigned_badge.admin_order_field = "assigned_to"

    def message_count(self, obj):
        count = obj.messages.count()
        if count == 0:
            return mark_safe('<span style="color: #9ca3af;">0</span>')
        return format_html(
            '<span style="background: #e0e7ff; color: #3730a3; padding: 2px 8px; border-radius: 10px; font-size: 11px;">{}</span>',
            count,
        )

    message_count.short_description = "Msg"

    def age_display(self, obj):
        age = timezone.now() - obj.created_at
        if age.days > 7:
            return format_html('<span style="color: #ef4444;">{} T</span>', age.days)
        elif age.days > 0:
            return format_html('<span style="color: #f59e0b;">{} T</span>', age.days)
        else:
            hours = age.seconds // 3600
            return format_html('<span style="color: #22c55e;">{} h</span>', hours)

    age_display.short_description = "Alter"
    age_display.admin_order_field = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        """Handle status changes and timestamps."""
        if change:
            old_obj = SupportTicket.objects.filter(pk=obj.pk).first()
            old_status = old_obj.status if old_obj else None

            if obj.status != old_status:
                now = timezone.now()
                if obj.status == "escalated" and not obj.escalated_at:
                    obj.escalated_at = now
                elif obj.status == "on_hold" and not obj.on_hold_at:
                    obj.on_hold_at = now
                elif obj.status == "resolved" and not obj.resolved_at:
                    obj.resolved_at = now
                elif obj.status == "closed" and not obj.closed_at:
                    obj.closed_at = now

                # Clear timestamps when reopening
                if obj.status in ["open", "in_progress"]:
                    if old_status in ["resolved", "closed"]:
                        obj.resolved_at = None
                        obj.closed_at = None

                # Send notification
                from apps.work.notifications.services import NotificationHub

                NotificationHub.notify_support_ticket_status_change(obj, old_status, obj.status)

        super().save_model(request, obj, form, change)

    # Admin actions
    @admin.action(description="➡️ Mir zuweisen")
    def assign_to_me(self, request, queryset):
        count = queryset.update(assigned_to=request.user)
        self.message_user(request, f"{count} Ticket(s) Ihnen zugewiesen.")

    @admin.action(description="🔄 In Bearbeitung")
    def mark_in_progress(self, request, queryset):
        count = queryset.update(status="in_progress")
        self.message_user(request, f"{count} Ticket(s) als 'In Bearbeitung' markiert.")

    @admin.action(description="⏳ Wartet auf Antwort")
    def mark_waiting(self, request, queryset):
        count = queryset.update(status="waiting")
        self.message_user(request, f"{count} Ticket(s) als 'Wartet auf Antwort' markiert.")

    @admin.action(description="🚨 Eskalieren")
    def mark_escalated(self, request, queryset):
        count = queryset.update(status="escalated", escalated_at=timezone.now())
        self.message_user(request, f"{count} Ticket(s) eskaliert.")

    @admin.action(description="⏸️ Zurückstellen")
    def mark_on_hold(self, request, queryset):
        count = queryset.update(status="on_hold", on_hold_at=timezone.now())
        self.message_user(request, f"{count} Ticket(s) zurückgestellt.")

    @admin.action(description="✅ Als gelöst markieren")
    def mark_resolved(self, request, queryset):
        count = queryset.update(status="resolved", resolved_at=timezone.now())
        self.message_user(request, f"{count} Ticket(s) als gelöst markiert.")

    @admin.action(description="🔒 Schließen")
    def mark_closed(self, request, queryset):
        count = queryset.update(status="closed", closed_at=timezone.now())
        self.message_user(request, f"{count} Ticket(s) geschlossen.")

    def get_urls(self):
        """Add custom URLs for ticket actions."""
        from django.urls import path

        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/reply/",
                self.admin_site.admin_view(self.reply_view),
                name="work_supportticket_reply",
            ),
        ]
        return custom_urls + urls

    def reply_view(self, request, object_id):
        """Handle reply submission."""
        from django.contrib import messages as django_messages
        from django.shortcuts import get_object_or_404, redirect
        from django.urls import reverse

        ticket = get_object_or_404(SupportTicket, pk=object_id)

        if request.method == "POST":
            content = request.POST.get("reply_content", "").strip()
            is_internal = request.POST.get("is_internal") == "on"

            if content:
                msg = SupportTicketMessage(
                    ticket=ticket,
                    author_staff=request.user,
                    is_internal=is_internal,
                )
                msg.set_content_encrypted(content)
                msg.save()

                if not is_internal:
                    if ticket.status == "open":
                        ticket.status = "waiting"
                        ticket.save()

                    from apps.work.notifications.services import NotificationHub

                    NotificationHub.notify_support_ticket_reply(ticket, msg, is_staff_reply=True)

                django_messages.success(
                    request,
                    "✅ Antwort gesendet." if not is_internal else "📝 Interne Notiz hinzugefügt.",
                )
            else:
                django_messages.error(request, "Bitte geben Sie eine Nachricht ein.")

        # Use absolute URL for redirect
        return redirect(reverse("admin:work_supportticket_change", args=[object_id]))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """Enhanced change view with reply form."""
        from django.urls import reverse

        extra_context = extra_context or {}
        extra_context["show_reply_form"] = True
        # Use absolute URL to avoid relative path issues
        extra_context["reply_url"] = reverse("admin:work_supportticket_reply", args=[object_id])
        return super().change_view(request, object_id, form_url, extra_context)
