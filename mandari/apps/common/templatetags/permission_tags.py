# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Filter für Berechtigungen.

Usage:
    {% load permission_tags %}

    {% if membership|has_perm:"organization.view" %}
        ...
    {% endif %}
"""

from django import template

register = template.Library()


@register.filter("has_perm")
def has_perm_filter(membership, permission):
    """
    Filter to check if a membership has a specific permission.

    Usage: {% if membership|has_perm:"organization.view" %}
    """
    if not membership:
        return False
    return membership.has_permission(permission)


@register.filter("dict_get")
def dict_get(d, key):
    """
    Dict-Zugriff mit dynamischem Schlüssel.

    Usage: {{ role_permission_sources|dict_get:perm.code }}
    """
    if not d:
        return None
    return d.get(key)
