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
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from apps.common.admin_mixins import ImmutableAdminMixin, status_pill

from . import audit
from .models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionApplication,
    SessionAuditLog,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
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


def _save_each(queryset, **changes) -> int:
    """Sammel-Aktion als Einzel-Speichern: Signale (Audit, Rückmeldungen) laufen für jedes Objekt."""
    count = 0
    for obj in queryset:
        for field, value in changes.items():
            setattr(obj, field, value)
        obj.save(update_fields=[*changes, "updated_at"])
        count += 1
    return count


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
        (None, {"fields": ("name", "slug", "short_name", "description", "body_type", "ags")}),
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
        return obj.users.filter(is_active=True).count()

    @admin.display(description="Aktiv", boolean=True)
    def is_active_display(self, obj):
        return obj.is_active

    # Mandant anlegen nur über den Assistenten (Issue #317): Rollen, Nummernkreis, Wahlperiode und
    # Administrator entstehen dort gemeinsam; das nackte Formular legte Mandanten ohne Rollen an.
    def get_urls(self):
        from django.urls import path

        from .admin_provisioning import provision_view

        eigene = [
            path(
                "anlegen/",
                self.admin_site.admin_view(lambda request: provision_view(self, request)),
                name="session_sessiontenant_provision",
            ),
        ]
        return eigene + super().get_urls()

    def add_view(self, request, form_url="", extra_context=None):
        from django.shortcuts import redirect

        return redirect("admin:session_sessiontenant_provision")

    def save_model(self, request, obj, form, change):
        # Deaktivieren/Reaktivieren im Formular: Signal nimmt die Bürgerportal-Quelle zurück bzw.
        # stellt sie wieder her und protokolliert, wer gehandelt hat (Issue #317)
        from .admin_provisioning import actor_for

        obj._lifecycle_actor = actor_for(request)
        obj._lifecycle_request = request
        super().save_model(request, obj, form, change)

    # Einzeln über den Service statt queryset.update(): Rücknahme bzw. Wiederherstellung der
    # Bürgerportal-Quelle und Audit-Log laufen für jeden Mandanten (Issue #317).
    @admin.action(description="Ausgewählte Mandanten aktivieren (Bürgerportal wiederherstellen)")
    def activate_tenants(self, request, queryset):
        from .admin_provisioning import actor_for
        from .services import tenant_provisioning

        count = sum(
            tenant_provisioning.set_tenant_active(tenant, True, actor=actor_for(request), request=request).changed
            for tenant in queryset
        )
        messages.success(request, f"{count} Mandant(en) wurden aktiviert.")

    @admin.action(description="Ausgewählte Mandanten deaktivieren (Bürgerportal zurücknehmen)")
    def deactivate_tenants(self, request, queryset):
        from .admin_provisioning import actor_for
        from .services import tenant_provisioning

        count = 0
        entries = 0
        for tenant in queryset:
            result = tenant_provisioning.set_tenant_active(tenant, False, actor=actor_for(request), request=request)
            count += int(result.changed)
            entries += result.portal.entries if result.portal else 0
        messages.success(
            request,
            f"{count} Mandant(en) wurden deaktiviert; {entries} Einträge aus dem Bürgerportal zurückgenommen.",
        )

    @action(description="API-Token generieren", url_path="generate-token", permissions=["change"])
    def generate_api_token_action(self, request, object_id):
        """
        Neuen API-Token für den Mandanten erzeugen – nur mit Änderungsrecht am Mandanten. Der Token
        erscheint einmalig auf einer eigenen, nicht zwischengespeicherten Seite, nie in einer Meldung
        (die im Messages-Cookie stünde).
        """
        tenant = get_object_or_404(self.model, pk=object_id)
        token_obj, raw_token = SessionAPIToken.create_token(
            tenant=tenant,
            name=f"Auto-generiert am {timezone.now().strftime('%Y-%m-%d %H:%M')}",
        )
        context = {
            **self.admin_site.each_context(request),
            "title": "API-Token erzeugt",
            "opts": self.model._meta,
            "tenant": tenant,
            "token": token_obj,
            "raw_token": raw_token,
        }
        response = TemplateResponse(request, "admin/session/sessiontenant/token_created.html", context)
        response["Cache-Control"] = "private, no-store"
        return response


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
                    "can_export_audit_log",
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


def _locked(meeting_id) -> bool:
    """Niederschrift der Sitzung genehmigt? Dann schreibgeschützt (Issue #318)."""
    from .services import protocol_lock

    return meeting_id is not None and protocol_lock.is_locked(meeting_id)


class SessionAgendaItemInline(TabularInline):
    """Inline for agenda items."""

    model = SessionAgendaItem
    extra = 1
    fields = ["number", "name", "is_public", "paper", "order"]

    # Genehmigte Niederschrift (Issue #318): Vorlagenzuordnung fest, keine neuen oder gelöschten TOPs.
    # Das Modell setzt die Sperre ohnehin durch; der Admin bietet sie gar nicht erst an.
    def get_readonly_fields(self, request, obj=None):
        if obj is not None and _locked(obj.pk):
            return ["paper"]
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and _locked(obj.pk):
            return False
        return super().has_delete_permission(request, obj)

    def get_extra(self, request, obj=None, **kwargs):
        if obj is not None and _locked(obj.pk):
            return 0
        return super().get_extra(request, obj, **kwargs)


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

    def has_delete_permission(self, request, obj=None):
        """Sitzungen mit genehmigter Niederschrift bleiben erhalten (Issue #318)."""
        if obj is not None and _locked(obj.pk):
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        """Sammel-Löschen überspringt Sitzungen mit genehmigter Niederschrift (Issue #318)."""
        from .services import protocol_lock

        locked = protocol_lock.locked_meeting_ids(queryset.values_list("pk", flat=True))
        if locked:
            self.message_user(
                request,
                f"{len(locked)} Sitzung(en) mit genehmigter Niederschrift wurden nicht gelöscht.",
                level=messages.WARNING,
                fail_silently=True,
            )
        super().delete_queryset(request, queryset.exclude(pk__in=locked))

    @admin.display(description="Status")
    def meeting_state_display(self, obj):
        colors = {
            "scheduled": "#60a5fa",  # blue
            "invitation_sent": "#a78bfa",  # purple
            "in_progress": "#fbbf24",  # yellow
            "completed": "#34d399",  # green
            "archived": "#9ca3af",  # gray
        }
        return status_pill(colors.get(obj.meeting_state, "#9ca3af"), obj.get_meeting_state_display())

    @admin.display(description="Öffentlich", boolean=True)
    def is_public_display(self, obj):
        return obj.is_public

    @admin.display(description="Abgesagt", boolean=True)
    def cancelled_display(self, obj):
        return obj.cancelled

    @admin.display(description="Teilnehmer")
    def attendance_count(self, obj):
        return obj.attendances.count()

    # Sammel-Aktionen speichern jede Sitzung einzeln statt per queryset.update(): Nur so laufen
    # Audit-Log, OParl-Veröffentlichung und die Rückmeldung an einreichende Fraktionen (Issue #316).

    @admin.action(description="Als geplant markieren")
    def mark_scheduled(self, request, queryset):
        count = _save_each(queryset, meeting_state="scheduled", cancelled=False)
        messages.success(request, f"{count} Sitzung(en) als geplant markiert.")

    @admin.action(description="Als abgeschlossen markieren")
    def mark_completed(self, request, queryset):
        count = _save_each(queryset, meeting_state="completed")
        messages.success(request, f"{count} Sitzung(en) als abgeschlossen markiert.")

    @admin.action(description="Absagen")
    def cancel_meetings(self, request, queryset):
        count = _save_each(queryset, cancelled=True)
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
# LEGISLATIVE TERM ADMIN (Wahlperioden, Issue #35)
# =============================================================================


@admin.register(SessionLegislativeTerm)
class SessionLegislativeTermAdmin(ModelAdmin):
    """Admin für Wahlperioden (werden über die OParl-API als legislativeTerm ausgeliefert)."""

    list_display = ["name", "tenant", "start_date", "end_date"]
    list_filter = ["tenant"]
    search_fields = ["name"]


# =============================================================================
# CONSULTATION ADMIN (Beratungsfolge, Issue #34)
# =============================================================================


@admin.register(SessionConsultation)
class SessionConsultationAdmin(ModelAdmin):
    """Admin für Beratungsstationen (Beratungsfolge einer Vorlage)."""

    list_display = ["paper", "order", "organization", "role", "authoritative", "result", "meeting"]
    list_filter = ["role", "authoritative", "result", "paper__tenant"]
    search_fields = ["paper__reference", "paper__name", "organization__name"]
    raw_id_fields = ["paper", "organization", "meeting", "agenda_item"]
    ordering = ["paper", "order"]

    def get_readonly_fields(self, request, obj=None):
        """Station eines TOP mit genehmigter Niederschrift: Ergebnis folgt dem TOP (Issue #318)."""
        if obj is not None and obj.agenda_item_id and _locked(obj.agenda_item.meeting_id):
            return ["result", "agenda_item", "meeting"]
        return super().get_readonly_fields(request, obj)


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
        return status_pill(colors.get(obj.status, "#9ca3af"), obj.get_status_display())

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

    # Einzel-Speichern statt queryset.update(): Statuswechsel lösen Audit-Log und die Rückmeldung an
    # die einreichende Fraktion aus – genau wie im Session-Portal (Issue #316).

    @admin.action(description="In Prüfung setzen")
    def mark_in_review(self, request, queryset):
        count = _save_each(queryset.filter(status__in=["submitted", "received"]), status="in_review")
        messages.success(request, f"{count} Antrag/Anträge in Prüfung gesetzt.")

    @admin.action(description="Annehmen")
    def mark_accepted(self, request, queryset):
        count = _save_each(
            queryset.exclude(status__in=["accepted", "rejected", "withdrawn", "converted"]), status="accepted"
        )
        messages.success(request, f"{count} Antrag/Anträge angenommen.")

    @admin.action(description="Ablehnen")
    def mark_rejected(self, request, queryset):
        count = _save_each(
            queryset.exclude(status__in=["accepted", "rejected", "withdrawn", "converted"]), status="rejected"
        )
        messages.success(request, f"{count} Antrag/Anträge abgelehnt.")

    @action(description="Vorlage erstellen", url_path="create-paper", permissions=["change"])
    def create_paper_from_application(self, request, object_id):
        """
        Vorlage über denselben Dienst wie das Session-Portal anlegen (Nummernkreis, Rückmeldung) –
        nur mit Änderungsrecht am Antrag.
        """
        from apps.session.services.application_service import ConversionError, convert_to_paper
        from apps.session.services.numbering_service import NumberingError

        app = get_object_or_404(self.model.objects.select_related("tenant"), pk=object_id)
        back = redirect(reverse("admin:session_sessionapplication_change", args=[app.pk]))
        try:
            paper, created = convert_to_paper(app, main_organization_id=app.target_organization_id)
        except (ConversionError, NumberingError):
            messages.error(
                request,
                "Umwandlung nicht möglich – bitte Zielgremium und Nummernkreis im Sitzungsdienst prüfen.",
            )
            return back
        if created:
            messages.success(request, f"Vorlage '{paper.display_reference}' wurde aus dem Antrag erstellt.")
        else:
            messages.info(request, f"Der Antrag wurde bereits in die Vorlage '{paper.display_reference}' umgewandelt.")
        return back


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

    def get_readonly_fields(self, request, obj=None):
        """Genehmigte Niederschrift: Inhalt und Status nur über Workflow und Berichtigung (Issue #318)."""
        if obj is not None and obj.is_locked:
            return ["meeting", "content", "status", "approved_at"]
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_locked:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        """Sammel-Löschen überspringt genehmigte Niederschriften (Issue #318)."""
        from .services import protocol_lock

        super().delete_queryset(request, queryset.exclude(status__in=protocol_lock.LOCKED_STATUSES))

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
class SessionAuditLogAdmin(ImmutableAdminMixin, ModelAdmin):
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
        "seq",
        "prev_hash",
        "entry_hash",
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
        # Hash-Kette je Mandant (Issue #221)
        ("Hash-Kette", {"fields": ("seq", "prev_hash", "entry_hash")}),
        # User/IP information not shown - privacy
    )


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
            perms.append("Öffentliche Sitzungen")
        if obj.can_read_papers:
            perms.append("Öffentliche Vorlagen")
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


# =============================================================================
# MANDANTENGRUPPEN UND LEITSTELLE (Issue #317)
# =============================================================================


class SessionTenantGroupTenantInline(TabularInline):
    model = SessionTenantGroupTenant
    extra = 0
    autocomplete_fields = ["tenant"]
    verbose_name = "Mandant"
    verbose_name_plural = "Mandanten der Gruppe (ein Mandant gehört höchstens einer Gruppe an)"


class SessionTenantGroupMembershipInline(TabularInline):
    model = SessionTenantGroupMembership
    extra = 0
    autocomplete_fields = ["user"]
    fields = ["user", "role", "is_active", "note"]
    verbose_name = "Mitglied der Leitstelle"
    verbose_name_plural = "Leitstelle (Mitglieder und Gruppenrolle)"


@admin.register(SessionTenantGroup)
class SessionTenantGroupAdmin(ModelAdmin):
    """
    Mandantengruppen mit Leitstelle (Issue #317).

    Die Gruppenrolle gilt nur für die Leitstellen-Übersicht; sie öffnet keine Mandantenseite und
    ersetzt keine Rolle im Mandanten. Jede Änderung an Mitgliedern und Mandanten der Gruppe steht im
    Protokoll der betroffenen Mandanten („Rechte geändert“).
    """

    list_display = ["name", "slug", "tenant_count", "member_count", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]
    inlines = [SessionTenantGroupTenantInline, SessionTenantGroupMembershipInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "description", "is_active")}),
        ("Zeitstempel", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Mandanten")
    def tenant_count(self, obj):
        return obj.tenant_links.count()

    @admin.display(description="Leitstelle")
    def member_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    # Das Protokoll der Mandanten nennt, wer die Leitstellen-Rechte geändert hat (Signale lesen den Request)
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        audit.set_current_request(request)
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        finally:
            audit.clear_current_request()

    def delete_view(self, request, object_id, extra_context=None):
        audit.set_current_request(request)
        try:
            return super().delete_view(request, object_id, extra_context)
        finally:
            audit.clear_current_request()
