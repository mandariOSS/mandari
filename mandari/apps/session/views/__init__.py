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
    AgendaItemDeleteView,
    AgendaItemMoveView,
    AgendaItemUpdateView,
    AgendaItemWithdrawView,
    AgendaReorderView,
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
from .invitations import (
    MeetingAgendaPdfView,
    MeetingIcsView,
    MeetingInvitationView,
)
from .meetings import (
    MeetingCreateView,
    MeetingDetailView,
    MeetingListView,
    MeetingUpdateView,
)
from .memberships import (
    MembershipCreateView,
    MembershipEndView,
    MembershipSuccessionView,
    MembershipUpdateView,
)
from .organizations import (
    OrganizationCreateView,
    OrganizationDeactivateView,
    OrganizationDetailView,
    OrganizationListView,
    OrganizationUpdateView,
)
from .papers import (
    PaperCreateView,
    PaperDetailView,
    PaperListView,
    PaperUpdateView,
)
from .persons import (
    PersonCreateView,
    PersonDeactivateView,
    PersonDetailView,
    PersonListView,
    PersonUpdateView,
)
from .settings import (
    InvitationAcceptView,
    InvitationCancelView,
    SettingsView,
    UserDeactivateView,
    UserInviteView,
    UserListView,
    UserRolesUpdateView,
)

__all__ = [
    "AgendaItemCreateView",
    "AgendaItemDeleteView",
    "AgendaItemMoveView",
    "AgendaItemUpdateView",
    "AgendaItemWithdrawView",
    "AgendaReorderView",
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
    "InvitationAcceptView",
    "InvitationCancelView",
    "MembershipCreateView",
    "MembershipEndView",
    "MembershipSuccessionView",
    "MembershipUpdateView",
    "MeetingAgendaPdfView",
    "MeetingCreateView",
    "MeetingDetailView",
    "MeetingIcsView",
    "MeetingInvitationView",
    "MeetingListView",
    "MeetingUpdateView",
    "OrganizationCreateView",
    "OrganizationDeactivateView",
    "OrganizationDetailView",
    "OrganizationListView",
    "OrganizationUpdateView",
    "PaperCreateView",
    "PaperDetailView",
    "PaperListView",
    "PaperUpdateView",
    "PersonCreateView",
    "PersonDeactivateView",
    "PersonDetailView",
    "PersonListView",
    "PersonUpdateView",
    "SettingsView",
    "UserDeactivateView",
    "UserInviteView",
    "UserListView",
    "UserRolesUpdateView",
]
