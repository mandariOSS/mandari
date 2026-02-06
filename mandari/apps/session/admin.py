# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session admin configuration.

Uses Django Unfold admin theme for a modern, clean interface.

IMPORTANT: This admin is for SYSTEM-LEVEL management only.
Personal data (users, persons, attendance, allowances, etc.) is managed
through the Session portal itself, NOT through Django admin.

This ensures data isolation between tenants and prevents Django admins
from accessing personal information.
"""

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from .models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionApplication,
    SessionAuditLog,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
    SessionRole,
    SessionTenant,
)

# NOTE: The following models are intentionally NOT registered in Django admin
# to ensure data isolation and privacy:
# - SessionUser: User-tenant relationships (personal data)
# - SessionPerson: Council members with personal details (address, bank, etc.)
# - SessionAttendance: Individual attendance records
# - SessionAllowance: Payment information
# - SessionOrganizationMembership: Member relationships
#
# These are managed through the Session portal itself.


# =============================================================================
# TENANT ADMIN
# =============================================================================


@admin.register(SessionTenant)
class SessionTenantAdmin(ModelAdmin):
    """Admin for Session tenants."""

    list_display = [
        "name",
        "slug",
        "contact_email",
        "organization_count",
        "user_count",
        "is_active_display",
        "created_at",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name", "slug", "contact_email"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["encryption_key", "created_at", "updated_at"]
    actions = ["activate_tenants", "deactivate_tenants"]
    actions_detail = ["generate_api_token_action"]

    fieldsets = (
        (None, {"fields": ("name", "slug", "short_name", "description")}),
        (
            "OParl-Verknüpfung",
            {
                "fields": ("oparl_body",),
                "classes": ("collapse",),
                "description": "Verknüpfung mit einer OParl-Kommune für die automatische Synchronisation öffentlicher Daten.",
            },
        ),
        (
            "Branding",
            {
                "fields": ("logo", "primary_color", "secondary_color"),
            },
        ),
        (
            "Kontakt",
            {
                "fields": ("contact_email", "contact_phone", "website", "address"),
            },
        ),
        (
            "Einstellungen",
            {
                "fields": ("settings", "is_active"),
            },
        ),
        (
            "System",
            {
                "fields": ("encryption_key", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Gremien")
    def organization_count(self, obj):
        return obj.organizations.count()

    @admin.display(description="Benutzer")
    def user_count(self, obj):
        return obj.session_users.filter(is_active=True).count()

    @admin.display(description="Aktiv", boolean=True)
    def is_active_display(self, obj):
        return obj.is_active

    @admin.action(description="Ausgewählte Mandanten aktivieren")
    def activate_tenants(self, request, queryset):
        count = queryset.update(is_active=True)
        messages.success(request, f"{count} Mandant(en) wurden aktiviert.")

    @admin.action(description="Ausgewählte Mandanten deaktivieren")
    def deactivate_tenants(self, request, queryset):
        count = queryset.update(is_active=False)
        messages.success(request, f"{count} Mandant(en) wurden deaktiviert.")

    @action(description="API-Token generieren", url_path="generate-token")
    def generate_api_token_action(self, request, object_id):
        """Generate a new API token for this tenant."""
        tenant = self.model.objects.get(pk=object_id)
        token_obj, raw_token = SessionAPIToken.create_token(
            tenant=tenant,
            name=f"Auto-generiert am {timezone.now().strftime('%Y-%m-%d %H:%M')}",
        )
        messages.success(
            request,
            mark_safe(
                f"Neuer API-Token erstellt! <strong>Token (nur einmal sichtbar):</strong><br>"
                f"<code style='background: #fef3c7; padding: 4px 8px; border-radius: 4px; font-family: monospace;'>{raw_token}</code><br>"
                f"<small>Kopieren Sie diesen Token sofort, er kann nicht erneut angezeigt werden!</small>"
            ),
        )
        return None


# =============================================================================
# ROLE ADMIN (no personal data)
# =============================================================================


@admin.register(SessionRole)
class SessionRoleAdmin(ModelAdmin):
    """Admin for Session roles (role definitions only, no user assignments)."""

    list_display = ["name", "tenant", "is_admin", "is_system_role", "priority"]
    list_filter = ["tenant", "is_admin", "is_system_role"]
    search_fields = ["name", "tenant__name"]
    ordering = ["tenant__name", "-priority", "name"]

    fieldsets = (
        (None, {"fields": ("tenant", "name", "description")}),
        (
            "Dashboard",
            {
                "fields": ("can_view_dashboard",),
            },
        ),
        (
            "Sitzungen",
            {
                "fields": (
                    "can_view_meetings",
                    "can_create_meetings",
                    "can_edit_meetings",
                    "can_delete_meetings",
                    "can_view_non_public_meetings",
                ),
            },
        ),
        (
            "Vorlagen",
            {
                "fields": (
                    "can_view_papers",
                    "can_create_papers",
                    "can_edit_papers",
                    "can_delete_papers",
                    "can_approve_papers",
                    "can_view_non_public_papers",
                ),
            },
        ),
        (
            "Anträge",
            {
                "fields": (
                    "can_view_applications",
                    "can_process_applications",
                ),
            },
        ),
        (
            "Protokolle",
            {
                "fields": (
                    "can_view_protocols",
                    "can_create_protocols",
                    "can_edit_protocols",
                    "can_approve_protocols",
                ),
            },
        ),
        (
            "Anwesenheit & Sitzungsgelder",
            {
                "fields": (
                    "can_manage_attendance",
                    "can_manage_allowances",
                ),
            },
        ),
        (
            "Administration",
            {
                "fields": (
                    "can_manage_users",
                    "can_manage_organizations",
                    "can_manage_settings",
                    "can_view_audit_log",
                ),
            },
        ),
        (
            "API",
            {
                "fields": (
                    "can_access_api",
                    "can_access_oparl_api",
                ),
            },
        ),
        (
            "Rolleneinstellungen",
            {
                "fields": ("is_admin", "is_system_role", "priority", "color"),
            },
        ),
    )


# NOTE: SessionUser admin removed - user management is done through Session portal


# =============================================================================
# ORGANIZATION ADMIN (committee structure only, no members)
# =============================================================================


@admin.register(SessionOrganization)
class SessionOrganizationAdmin(ModelAdmin):
    """
    Admin for Session organizations (committee structure).

    NOTE: Organization memberships are NOT shown here to protect personal data.
    Members are managed through the Session portal.
    """

    list_display = ["name", "tenant", "organization_type", "member_count", "is_active"]
    list_filter = ["tenant", "organization_type", "is_active"]
    search_fields = ["name", "short_name"]
    # NOTE: No membership inline - protects personal data

    fieldsets = (
        (None, {"fields": ("tenant", "name", "short_name", "organization_type")}),
        (
            "OParl-Verknüpfung",
            {
                "fields": ("oparl_organization",),
                "classes": ("collapse",),
            },
        ),
        (
            "Hierarchie",
            {
                "fields": ("parent",),
            },
        ),
        (
            "Einstellungen",
            {
                "fields": (
                    "default_meeting_location",
                    "default_meeting_start_time",
                ),
            },
        ),
        (
            "Sitzungsgelder",
            {
                "fields": ("allowance_amount", "allowance_currency"),
            },
        ),
        (
            "Status",
            {
                "fields": ("is_active", "start_date", "end_date"),
            },
        ),
    )

    @admin.display(description="Mitglieder")
    def member_count(self, obj):
        """Show member count without exposing personal data."""
        return obj.memberships.count()


# NOTE: SessionPerson admin removed - person management is done through Session portal
# This protects sensitive personal data (contact info, bank details, etc.)


# =============================================================================
# MEETING ADMIN (no attendance data - personal information)
# =============================================================================


class SessionAgendaItemInline(TabularInline):
    """Inline for agenda items."""

    model = SessionAgendaItem
    extra = 1
    fields = ["number", "name", "is_public", "paper", "order"]


# NOTE: SessionAttendanceInline removed - attendance is managed through Session portal
# This protects personal data (who attended which meetings)


@admin.register(SessionMeeting)
class SessionMeetingAdmin(ModelAdmin):
    """
    Admin for Session meetings.

    NOTE: Attendance records are NOT shown here to protect personal data.
    Attendance is managed through the Session portal.
    """

    list_display = [
        "name",
        "organization",
        "start",
        "meeting_state_display",
        "is_public_display",
        "cancelled_display",
        "attendance_count",
    ]
    list_filter = ["tenant", "organization", "meeting_state", "is_public", "cancelled"]
    search_fields = ["name", "organization__name"]
    date_hierarchy = "start"
    inlines = [SessionAgendaItemInline]  # Attendance inline removed for privacy
    actions = ["mark_scheduled", "mark_completed", "cancel_meetings"]

    fieldsets = (
        (None, {"fields": ("tenant", "name", "organization")}),
        (
            "OParl-Verknüpfung",
            {
                "fields": ("oparl_meeting",),
                "classes": ("collapse",),
            },
        ),
        (
            "Datum & Zeit",
            {
                "fields": (
                    ("start", "end"),
                    ("actual_start", "actual_end"),
                ),
            },
        ),
        (
            "Ort",
            {
                "fields": (
                    "location",
                    "room",
                    ("street_address", "postal_code", "locality"),
                ),
            },
        ),
        (
            "Status",
            {
                "fields": ("meeting_state", "is_public", "cancelled", "cancellation_reason"),
            },
        ),
        (
            "Einladung",
            {
                "fields": ("invitation_sent_at", "invitation_text"),
                "classes": ("collapse",),
            },
        ),
        # NOTE: created_by removed - references SessionUser (personal data)
    )

    @admin.display(description="Status")
    def meeting_state_display(self, obj):
        colors = {
            "scheduled": "#60a5fa",  # blue
            "invitation_sent": "#a78bfa",  # purple
            "in_progress": "#fbbf24",  # yellow
            "completed": "#34d399",  # green
            "archived": "#9ca3af",  # gray
        }
        color = colors.get(obj.meeting_state, "#9ca3af")
        return format_html(
            '<span style="background: {}; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem;">{}</span>',
            color,
            obj.get_meeting_state_display(),
        )

    @admin.display(description="Öffentlich", boolean=True)
    def is_public_display(self, obj):
        return obj.is_public

    @admin.display(description="Abgesagt", boolean=True)
    def cancelled_display(self, obj):
        return obj.cancelled

    @admin.display(description="Teilnehmer")
    def attendance_count(self, obj):
        return obj.attendances.count()

    @admin.action(description="Als geplant markieren")
    def mark_scheduled(self, request, queryset):
        count = queryset.update(meeting_state="scheduled", cancelled=False)
        messages.success(request, f"{count} Sitzung(en) als geplant markiert.")

    @admin.action(description="Als abgeschlossen markieren")
    def mark_completed(self, request, queryset):
        count = queryset.update(meeting_state="completed")
        messages.success(request, f"{count} Sitzung(en) als abgeschlossen markiert.")

    @admin.action(description="Absagen")
    def cancel_meetings(self, request, queryset):
        count = queryset.update(cancelled=True)
        messages.success(request, f"{count} Sitzung(en) abgesagt.")


# =============================================================================
# PAPER ADMIN
# =============================================================================


class SessionFileInline(TabularInline):
    """Inline for file attachments."""

    model = SessionFile
    extra = 1
    fields = ["name", "file", "is_public"]


@admin.register(SessionPaper)
class SessionPaperAdmin(ModelAdmin):
    """
    Admin for Session papers.

    NOTE: Workflow fields (created_by, approved_by) removed to protect personal data.
    """

    list_display = ["reference", "name", "paper_type", "status", "is_public", "date"]
    list_filter = ["tenant", "paper_type", "status", "is_public"]
    search_fields = ["reference", "name"]
    date_hierarchy = "date"
    inlines = [SessionFileInline]

    fieldsets = (
        (None, {"fields": ("tenant", "reference", "name", "paper_type")}),
        (
            "OParl-Verknüpfung",
            {
                "fields": ("oparl_paper",),
                "classes": ("collapse",),
            },
        ),
        (
            "Inhalt",
            {
                "fields": ("main_text", "resolution_text"),
            },
        ),
        (
            "Sichtbarkeit",
            {
                "fields": ("is_public", "status"),
            },
        ),
        (
            "Termine",
            {
                "fields": ("date", "deadline"),
            },
        ),
        (
            "Zuordnungen",
            {
                "fields": (
                    "main_organization",
                    # originator_person removed - personal data
                ),
            },
        ),
        (
            "Herkunft",
            {
                "fields": ("source_application",),
                "classes": ("collapse",),
            },
        ),
        # NOTE: Workflow section removed - created_by/approved_by reference SessionUser
    )


# =============================================================================
# APPLICATION ADMIN
# =============================================================================


@admin.register(SessionApplication)
class SessionApplicationAdmin(ModelAdmin):
    """
    Admin for Session applications.

    NOTE: Personal submitter details are hidden. Only organization shown.
    Detailed application data is managed through the Session portal.
    """

    list_display = [
        "reference",
        "title",
        "application_type",
        "status_display",
        "submitting_organization",
        "is_urgent_display",
        "submitted_at",
    ]
    list_filter = ["tenant", "application_type", "status", "is_urgent"]
    search_fields = ["reference", "title"]  # Personal data removed from search
    date_hierarchy = "submitted_at"
    readonly_fields = ["reference", "submitted_at"]
    actions = ["mark_received", "mark_in_review", "mark_accepted", "mark_rejected"]
    actions_detail = ["create_paper_from_application"]

    fieldsets = (
        (None, {"fields": ("tenant", "reference", "title", "application_type")}),
        (
            "Inhalt",
            {
                "fields": ("justification", "resolution_proposal", "financial_impact"),
            },
        ),
        (
            "Einreicher (nur Organisation)",
            {
                "fields": (
                    "submitting_organization",
                    # Personal data fields removed: submitter_name, email, phone
                ),
                "description": "Detaillierte Einreicher-Informationen sind im Session-Portal einsehbar.",
            },
        ),
        (
            "Zielgremium",
            {
                "fields": ("target_organization",),
            },
        ),
        (
            "Dringlichkeit",
            {
                "fields": ("is_urgent", "urgency_reason", "deadline"),
            },
        ),
        (
            "Status",
            {
                "fields": ("status", "received_at", "processing_notes"),
                # received_by removed - references SessionUser
            },
        ),
    )

    @admin.display(description="Status")
    def status_display(self, obj):
        colors = {
            "submitted": "#fbbf24",  # yellow
            "received": "#60a5fa",  # blue
            "in_review": "#a78bfa",  # purple
            "accepted": "#34d399",  # green
            "rejected": "#f87171",  # red
            "withdrawn": "#9ca3af",  # gray
        }
        color = colors.get(obj.status, "#9ca3af")
        return format_html(
            '<span style="background: {}; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Dringend", boolean=True)
    def is_urgent_display(self, obj):
        return obj.is_urgent

    @admin.action(description="Als empfangen markieren")
    def mark_received(self, request, queryset):
        count = 0
        for app in queryset.filter(status="submitted"):
            app.status = "received"
            app.received_at = timezone.now()
            # received_by not set through admin - handled in Session portal
            app.save()
            count += 1
        messages.success(request, f"{count} Antrag/Anträge als empfangen markiert.")

    @admin.action(description="In Prüfung setzen")
    def mark_in_review(self, request, queryset):
        count = queryset.filter(status__in=["submitted", "received"]).update(status="in_review")
        messages.success(request, f"{count} Antrag/Anträge in Prüfung gesetzt.")

    @admin.action(description="Annehmen")
    def mark_accepted(self, request, queryset):
        count = queryset.exclude(status__in=["accepted", "rejected", "withdrawn"]).update(status="accepted")
        messages.success(request, f"{count} Antrag/Anträge angenommen.")

    @admin.action(description="Ablehnen")
    def mark_rejected(self, request, queryset):
        count = queryset.exclude(status__in=["accepted", "rejected", "withdrawn"]).update(status="rejected")
        messages.success(request, f"{count} Antrag/Anträge abgelehnt.")

    @action(description="Vorlage erstellen", url_path="create-paper")
    def create_paper_from_application(self, request, object_id):
        """Create a Paper from this application."""
        app = self.model.objects.get(pk=object_id)

        # Create paper from application
        paper = SessionPaper.objects.create(
            tenant=app.tenant,
            name=app.title,
            paper_type="motion",
            main_text=f"{app.justification}\n\n---\n\n{app.resolution_proposal}",
            main_organization=app.target_organization,
            source_application=app,
            is_public=False,
            status="draft",
        )

        # Link application to paper
        app.status = "accepted"
        app.save()

        messages.success(request, f"Vorlage '{paper.reference}' wurde aus dem Antrag erstellt.")
        return None


# =============================================================================
# PROTOCOL ADMIN
# =============================================================================


@admin.register(SessionProtocol)
class SessionProtocolAdmin(ModelAdmin):
    """
    Admin for Session protocols.

    NOTE: Workflow data (created_by, approved_by) hidden for privacy.
    """

    list_display = ["meeting", "status", "approved_at"]
    list_filter = ["status", "meeting__tenant"]
    search_fields = ["meeting__name"]

    fieldsets = (
        (None, {"fields": ("meeting",)}),
        (
            "Inhalt",
            {
                "fields": ("content",),
            },
        ),
        (
            "Status",
            {
                "fields": ("status", "approved_at"),
            },
        ),
        # Workflow section removed - references SessionUser
    )


# =============================================================================
# AUDIT LOG ADMIN
# =============================================================================


@admin.register(SessionAuditLog)
class SessionAuditLogAdmin(ModelAdmin):
    """
    Admin for Session audit logs.

    NOTE: User information is hidden for privacy.
    Only shows action types and counts, not who performed them.
    """

    list_display = ["created_at", "action", "model_name", "object_repr"]
    list_filter = ["tenant", "action", "model_name", "created_at"]
    search_fields = ["object_repr"]  # Removed user search
    date_hierarchy = "created_at"
    readonly_fields = [
        "tenant",
        "action",
        "model_name",
        "object_id",
        "object_repr",
        "changes",
        "created_at",
    ]
    # Excluded: user, ip_address, user_agent - personal data

    fieldsets = (
        (None, {"fields": ("tenant", "action", "model_name", "object_id", "object_repr")}),
        (
            "Änderungen",
            {
                "fields": ("changes",),
            },
        ),
        (
            "Zeitstempel",
            {
                "fields": ("created_at",),
            },
        ),
        # User/IP information not shown - privacy
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# =============================================================================
# API TOKEN ADMIN
# =============================================================================


@admin.register(SessionAPIToken)
class SessionAPITokenAdmin(ModelAdmin):
    """Admin for Session API tokens."""

    list_display = [
        "name",
        "tenant",
        "token_prefix_display",
        "is_active_display",
        "permissions_display",
        "last_used_at",
        "usage_count",
        "expires_at",
    ]
    list_filter = [
        "tenant",
        "is_active",
        "can_submit_applications",
        "can_read_meetings",
        "can_read_papers",
    ]
    search_fields = ["name", "tenant__name", "token_prefix"]
    readonly_fields = [
        "token",
        "token_prefix",
        "last_used_at",
        "usage_count",
        "created_at",
        "updated_at",
    ]
    # last_used_ip removed for privacy
    actions = ["deactivate_tokens", "activate_tokens"]

    fieldsets = (
        (None, {"fields": ("tenant", "name", "description")}),
        (
            "Token",
            {
                "fields": ("token_prefix", "token"),
                "description": "Der Token wird beim Erstellen einmalig angezeigt und kann danach nicht mehr abgerufen werden. Nutzen Sie 'Neuen Token generieren' um einen Token zu erstellen.",
            },
        ),
        (
            "Berechtigungen",
            {
                "fields": (
                    "can_submit_applications",
                    "can_read_meetings",
                    "can_read_papers",
                ),
            },
        ),
        (
            "Sicherheit",
            {
                "fields": (
                    "is_active",
                    "rate_limit_per_minute",
                    "allowed_ips",
                    "expires_at",
                ),
            },
        ),
        # created_by removed - references SessionUser
        (
            "Nutzungsstatistik",
            {
                "fields": ("last_used_at", "usage_count"),
                # last_used_ip removed for privacy
                "classes": ("collapse",),
            },
        ),
        (
            "Zeitstempel",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Token")
    def token_prefix_display(self, obj):
        return format_html(
            '<code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">{}</code>...',
            obj.token_prefix,
        )

    @admin.display(description="Aktiv", boolean=True)
    def is_active_display(self, obj):
        return obj.is_valid()

    @admin.display(description="Berechtigungen")
    def permissions_display(self, obj):
        perms = []
        if obj.can_submit_applications:
            perms.append("Anträge")
        if obj.can_read_meetings:
            perms.append("Sitzungen")
        if obj.can_read_papers:
            perms.append("Vorlagen")
        return ", ".join(perms) if perms else "-"

    @admin.action(description="Ausgewählte Tokens deaktivieren")
    def deactivate_tokens(self, request, queryset):
        count = queryset.update(is_active=False)
        messages.success(request, f"{count} Token(s) wurden deaktiviert.")

    @admin.action(description="Ausgewählte Tokens aktivieren")
    def activate_tokens(self, request, queryset):
        count = queryset.update(is_active=True)
        messages.success(request, f"{count} Token(s) wurden aktiviert.")

    # NOTE: save_model for created_by removed - handled in Session portal

    def add_view(self, request, form_url="", extra_context=None):
        """Show info message when adding new token."""
        extra_context = extra_context or {}
        messages.info(
            request,
            "Nach dem Speichern wird der vollständige Token einmalig angezeigt. "
            "Kopieren Sie ihn sofort, da er danach nicht mehr abgerufen werden kann!",
        )
        return super().add_view(request, form_url, extra_context)
