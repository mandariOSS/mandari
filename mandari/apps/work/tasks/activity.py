# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Activity logging helpers for tasks.

Wird explizit aus Views aufgerufen (keine Signals),
um Kontrolle über Auto-Save-Spam zu behalten.
"""

from .models import TaskActivity


def log_activity(task, actor, activity_type, content="", details=None) -> TaskActivity:
    """Erstellt einen Aktivitätseintrag für eine Aufgabe."""
    return TaskActivity.objects.create(
        task=task,
        actor=actor,
        activity_type=activity_type,
        content=content,
        details=details or {},
    )


def log_field_change(task, actor, field_name, old_value, new_value, activity_type) -> TaskActivity | None:
    """Loggt eine Feldänderung, nur wenn sich der Wert tatsächlich geändert hat."""
    if str(old_value) == str(new_value):
        return None
    return log_activity(
        task=task,
        actor=actor,
        activity_type=activity_type,
        details={"old": str(old_value), "new": str(new_value), "field": field_name},
    )
