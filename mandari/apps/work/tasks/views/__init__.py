# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.work.tasks import views``)
unverändert funktionieren.
"""

from ._helpers import (
    _task_base_queryset,
    _task_panel_queryset,
)
from .create import (
    TaskCreateView,
    TaskShareView,
)
from .list import (
    TaskBoardAPIView,
    TaskListView,
)
from .manage import (
    TaskImportView,
    TaskLabelManageView,
)
from .panel import (
    TaskPanelActionView,
    TaskPanelView,
)

__all__ = [
    "TaskBoardAPIView",
    "TaskCreateView",
    "TaskImportView",
    "TaskLabelManageView",
    "TaskListView",
    "TaskPanelActionView",
    "TaskPanelView",
    "TaskShareView",
    "_task_base_queryset",
    "_task_panel_queryset",
]
