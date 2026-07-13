# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Provides Kanban-style task management with:
- 3-column board (TODO, In Progress, Done)
- Drag & drop reordering
- Slide-over panel with auto-save + explicit save
- Checklists, attachments, labels, activity feed
"""

import logging

from django.db.models import Prefetch

from ..models import Task, TaskActivity

logger = logging.getLogger(__name__)


def _task_base_queryset():
    """Base queryset with standard select_related."""
    return Task.objects.select_related(
        "assigned_to__user",
        "created_by__user",
        "related_meeting",
        "related_motion",
        "related_faction_meeting",
    )


def _task_panel_queryset():
    """Queryset with all relations needed for the panel."""
    return _task_base_queryset().prefetch_related(
        "labels",
        "checklist_items",
        "attachments",
        Prefetch(
            "activities",
            queryset=TaskActivity.objects.select_related("actor__user").order_by("created_at"),
        ),
    )
