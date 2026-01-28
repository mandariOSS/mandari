# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin configuration for tenants app.

Uses Django Unfold admin theme for a modern, clean interface.

IMPORTANT: This admin is for SYSTEM-LEVEL management only.
Personal data (memberships, invitations) is managed through the Work portal
by default. However, owner assignment is available for initial setup.
"""

from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import AutocompleteSelect
from unfold.admin import ModelAdmin

from apps.accounts.models import User
from .models import (
    Organization,
    PartyGroup,
    Permission,
    Role,
    Membership,
)


@admin.register(PartyGroup)
class PartyGroupAdmin(ModelAdmin):
    """Admin for party group hierarchy."""

    list_display = ["name", "parent", "level", "is_active", "created_at"]
    list_filter = ["is_active", "parent"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}
    ordering = ["name"]

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description", "parent")
        }),
        ("Status", {
            "fields": ("is_active",),
        }),
    )

    def level(self, obj):
        return obj.level

    level.short_description = "Hierarchieebene"


class OrganizationAdminForm(forms.ModelForm):
    """Custom form for Organization admin with owner selection."""

    class Meta:
        model = Organization
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Allow selecting any active user as owner
        # This enables initial setup before members are added
        self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("email")
        self.fields["owner"].required = False


@admin.register(Organization)
class OrganizationAdmin(ModelAdmin):
    """
    Admin for Work organizations (political parties, factions).

    OWNER MANAGEMENT:
    - Owner can be set here for initial setup
    - Owner selection is limited to active members of the organization
    - For new organizations, create the org first, then add members via
      Work portal, then return here to set the owner
    """

    form = OrganizationAdminForm

    list_display = [
        "name",
        "party_group",
        "body",
        "member_count",
        "owner_display",
        "is_active",
        "created_at"
    ]
    list_filter = ["is_active", "party_group", "body", "require_2fa"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}
    filter_horizontal = ["oparl_organizations"]
    readonly_fields = ["encryption_key", "created_at", "updated_at", "member_list_link"]
    autocomplete_fields = ["owner"]

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description"),
        }),
        ("Eigentümer", {
            "fields": ("owner", "member_list_link"),
            "description": (
                "Der Eigentümer hat volle Kontrolle über die Organisation. "
                "Wählen Sie einen aktiven Mitglied aus der Liste. "
                "Neue Mitglieder werden über das Work-Portal hinzugefügt."
            )
        }),
        ("Gruppierung", {
            "fields": ("party_group", "body", "oparl_organizations"),
            "description": "Organisation kann einer Partei-Hierarchie UND einer regionalen Gruppe angehören."
        }),
        ("Branding", {
            "fields": ("logo", "primary_color", "secondary_color"),
            "classes": ("collapse",)
        }),
        ("Öffentliche Informationen", {
            "fields": ("website",),
            "classes": ("collapse",)
        }),
        ("E-Mail (SMTP)", {
            "fields": (
                "smtp_host", "smtp_port", "smtp_username",
                "smtp_use_tls", "smtp_from_email", "smtp_from_name"
            ),
            "classes": ("collapse",)
        }),
        ("Einstellungen", {
            "fields": ("settings", "require_2fa", "is_active")
        }),
        ("System", {
            "fields": ("encryption_key", "created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )

    def member_count(self, obj):
        """Show member count."""
        return obj.memberships.filter(is_active=True).count()

    member_count.short_description = "Mitglieder"

    def owner_display(self, obj):
        """Show owner name or status."""
        if obj.owner:
            full_name = f"{obj.owner.first_name} {obj.owner.last_name}".strip()
            return full_name or obj.owner.email
        return "Kein Eigentümer"

    owner_display.short_description = "Eigentümer"

    def member_list_link(self, obj):
        """Link to view members in Work portal."""
        if obj.pk:
            count = obj.memberships.filter(is_active=True).count()
            return f"{count} aktive Mitglieder (Verwaltung im Work-Portal)"
        return "Speichern Sie zuerst die Organisation"

    member_list_link.short_description = "Mitglieder"


@admin.register(Permission)
class PermissionAdmin(ModelAdmin):
    """Admin for Work permissions."""

    list_display = ["codename", "name", "category"]
    list_filter = ["category"]
    search_fields = ["codename", "name"]
    ordering = ["category", "codename"]


@admin.register(Role)
class RoleAdmin(ModelAdmin):
    """Admin for Work roles."""

    list_display = [
        "name",
        "organization",
        "is_admin",
        "is_system_role",
        "priority",
        "permission_count"
    ]
    list_filter = ["is_admin", "is_system_role", "organization"]
    search_fields = ["name", "organization__name"]
    filter_horizontal = ["permissions"]

    fieldsets = (
        (None, {
            "fields": ("organization", "name", "description")
        }),
        ("Berechtigungen", {
            "fields": ("permissions",),
        }),
        ("Einstellungen", {
            "fields": ("is_admin", "is_system_role", "priority", "color"),
        }),
    )

    def permission_count(self, obj):
        if obj.is_admin:
            return "Alle"
        return obj.permissions.count()

    permission_count.short_description = "Berechtigungen"


@admin.register(Membership)
class MembershipAdmin(ModelAdmin):
    """
    Admin for organization memberships.

    Allows managing which users belong to which organizations.
    """

    list_display = [
        "user_display",
        "organization",
        "roles_display",
        "is_active",
        "joined_at",
    ]
    list_filter = ["is_active", "organization", "roles"]
    search_fields = ["user__email", "user__first_name", "user__last_name", "organization__name"]
    autocomplete_fields = ["user", "organization", "oparl_person", "invited_by"]
    filter_horizontal = ["roles", "individual_permissions", "denied_permissions", "oparl_committees"]
    readonly_fields = ["joined_at", "updated_at", "invitation_accepted_at"]
    ordering = ["-joined_at"]

    fieldsets = (
        ("Mitgliedschaft", {
            "fields": ("user", "organization", "is_active"),
        }),
        ("Rollen & Berechtigungen", {
            "fields": ("roles", "individual_permissions", "denied_permissions"),
            "description": (
                "Rollen definieren die Hauptberechtigungen. "
                "Individuelle Berechtigungen werden zusätzlich gewährt. "
                "Verweigerte Berechtigungen überschreiben Rollenberechtigungen."
            )
        }),
        ("OParl-Verknüpfung", {
            "fields": ("oparl_person", "oparl_committees"),
            "classes": ("collapse",),
            "description": "Verknüpfung mit dem Ratsinformationssystem"
        }),
        ("Einladung", {
            "fields": ("invited_by", "invitation_accepted_at"),
            "classes": ("collapse",),
        }),
        ("System", {
            "fields": ("joined_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def user_display(self, obj):
        """Show user name and email."""
        if obj.user.first_name or obj.user.last_name:
            name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return f"{name} ({obj.user.email})"
        return obj.user.email

    user_display.short_description = "Benutzer"

    def roles_display(self, obj):
        """Show role names."""
        roles = obj.roles.all()
        if not roles:
            return "Keine Rollen"
        return ", ".join([r.name for r in roles[:3]])

    roles_display.short_description = "Rollen"


# NOTE: User admin is registered in apps.accounts.admin
# This ensures search_fields are available for autocomplete
