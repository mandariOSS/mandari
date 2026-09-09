# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.work.tasks import views``)
unverändert funktionieren. Datenzugriff und Fachlogik liegen in
``apps.work.tasks.selectors`` bzw. ``apps.work.tasks.services``.
"""

from .create import (
    TaskCreateView,
    TaskShareView,
)
from .export_import import (
    TaskExportView,
    TaskFileImportView,
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
    "TaskExportView",
    "TaskFileImportView",
    "TaskImportView",
    "TaskLabelManageView",
    "TaskListView",
    "TaskPanelActionView",
    "TaskPanelView",
    "TaskShareView",
]
