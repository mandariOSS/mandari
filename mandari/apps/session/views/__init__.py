# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.session import views``)
unverändert funktionieren.
"""

from .agenda import (
    AgendaItemCreateView,
    AttendanceUpdateView,
)
from .applications import (
    ApplicationConvertView,
    ApplicationDetailView,
    ApplicationListView,
    ApplicationProcessView,
)
from .audit import (
    AuditLogListView,
)
from .dashboard import (
    DashboardView,
)
from .files import (
    FileDeleteView,
    FileDownloadView,
    FileReplaceView,
    FileUpdateView,
    FileUploadView,
)
from .meetings import (
    MeetingCreateView,
    MeetingDetailView,
    MeetingListView,
    MeetingUpdateView,
)
from .organizations import (
    OrganizationDetailView,
    OrganizationListView,
)
from .papers import (
    PaperCreateView,
    PaperDetailView,
    PaperListView,
    PaperUpdateView,
)
from .persons import (
    PersonDetailView,
    PersonListView,
)
from .settings import (
    SettingsView,
    UserListView,
)

__all__ = [
    "AgendaItemCreateView",
    "ApplicationConvertView",
    "ApplicationDetailView",
    "ApplicationListView",
    "ApplicationProcessView",
    "AttendanceUpdateView",
    "AuditLogListView",
    "DashboardView",
    "FileDeleteView",
    "FileDownloadView",
    "FileReplaceView",
    "FileUpdateView",
    "FileUploadView",
    "MeetingCreateView",
    "MeetingDetailView",
    "MeetingListView",
    "MeetingUpdateView",
    "OrganizationDetailView",
    "OrganizationListView",
    "PaperCreateView",
    "PaperDetailView",
    "PaperListView",
    "PaperUpdateView",
    "PersonDetailView",
    "PersonListView",
    "SettingsView",
    "UserListView",
]
