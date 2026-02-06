"""
Template tags for checking permissions in templates.

Usage:
    {% load permission_tags %}

    {% has_perm membership "organization.view" as can_view_settings %}
    {% if can_view_settings %}
        <a href="...">Einstellungen</a>
    {% endif %}

    Or inline:
    {% if membership|has_perm:"organization.view" %}
        ...
    {% endif %}
"""

from django import template

register = template.Library()


@register.simple_tag
def has_perm(membership, permission):
    """
    Check if a membership has a specific permission.

    Usage: {% has_perm membership "organization.view" as can_view %}
    """
    if not membership:
        return False
    return membership.has_permission(permission)


@register.filter("has_perm")
def has_perm_filter(membership, permission):
    """
    Filter to check if a membership has a specific permission.

    Usage: {% if membership|has_perm:"organization.view" %}
    """
    if not membership:
        return False
    return membership.has_permission(permission)


@register.simple_tag
def has_any_perm(membership, *permissions):
    """
    Check if a membership has any of the given permissions.

    Usage: {% has_any_perm membership "motions.create" "motions.edit" as can_edit %}
    """
    if not membership:
        return False
    from apps.common.permissions import PermissionChecker

    checker = PermissionChecker(membership)
    return checker.has_any_permission(list(permissions))


@register.simple_tag
def has_all_perms(membership, *permissions):
    """
    Check if a membership has all of the given permissions.

    Usage: {% has_all_perms membership "motions.create" "motions.delete" as can_manage %}
    """
    if not membership:
        return False
    from apps.common.permissions import PermissionChecker

    checker = PermissionChecker(membership)
    return checker.has_all_permissions(list(permissions))
