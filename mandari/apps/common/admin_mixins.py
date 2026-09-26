# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wiederkehrende Bausteine für Admin-Klassen.

Die Mixins stehen in der Basisliste vor ``ModelAdmin`` bzw. ``TabularInline``. Die Rechte-Methoden
nehmen ``obj`` optional an und passen damit sowohl zu ``ModelAdmin.has_add_permission(request)``
als auch zu ``InlineModelAdmin.has_add_permission(request, obj)``.
"""

from typing import Any

from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString


class NoAddAdminMixin:
    """Keine Datensätze im Admin anlegen; sie entstehen im Betrieb."""

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReadOnlyAdminMixin(NoAddAdminMixin):
    """Nur ansehen: weder anlegen noch ändern. Löschen regelt die Admin-Klasse selbst."""

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ImmutableAdminMixin(ReadOnlyAdminMixin):
    """Protokolle und Nachweise: weder anlegen, ändern noch löschen."""

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class SingletonAdminMixin:
    """Genau ein Datensatz: anlegen nur, solange keiner existiert; löschen nie."""

    model: Any

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return not self.model.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def status_text(color: str, label: object) -> SafeString:
    """Farbiger, fetter Status-Text für Admin-Listen."""
    return format_html('<span style="color: {}; font-weight: 600;">{}</span>', color, label)


def status_pill(color: str, label: object) -> SafeString:
    """Weißer Status-Text auf farbiger Pille für Admin-Listen."""
    return format_html(
        '<span style="background: {}; color: white; padding: 2px 8px; border-radius: 9999px; '
        'font-size: 0.75rem;">{}</span>',
        color,
        label,
    )
