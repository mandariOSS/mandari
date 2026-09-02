# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.

Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.work.organization import views``)
unverändert funktionieren.
"""

from .activity import (
    ProfileActivityView,
)
from .api_settings import OrganizationApiSettingsView
from .change_requests import (
    ProfileChangeRequestsView,
)
from .council import (
    CouncilPartyListView,
)
from .email_settings import (
    OrganizationEmailSettingsView,
)
from .invitations import (
    AcceptInvitationView,
    GuestInviteView,
    InvitationCancelView,
    InvitationResendView,
    MemberInviteView,
)
from .members import (
    MemberDetailView,
    MemberListView,
)
from .privacy import (
    DataExportDeleteView,
    DataExportDownloadView,
    DataExportStatusView,
    ProfileDataPrivacyView,
)
from .profile import (
    ProfileView,
    SecurityView,
)
from .profile_settings import (
    ProfileAbsenceView,
    ProfileNotificationsView,
)
from .registration import (
    MemberApproveView,
    MemberRejectView,
    RegistrationSettingsView,
)
from .roles import (
    RoleCreateView,
    RoleDeleteView,
    RoleEditView,
    RoleListView,
    RoleResetView,
    RoleRestoreDefaultsView,
)
from .team import (
    OrganizationDocumentsView,
    OrganizationFactionSettingsView,
    OrganizationSettingsView,
    TeamDirectoryView,
    TeamMemberProfileView,
)
from .visibility import (
    ProfileCommitteesView,
    ProfileVisibilityView,
)

__all__ = [
    "AcceptInvitationView",
    "CouncilPartyListView",
    "DataExportDeleteView",
    "DataExportDownloadView",
    "DataExportStatusView",
    "GuestInviteView",
    "InvitationCancelView",
    "InvitationResendView",
    "MemberApproveView",
    "MemberDetailView",
    "MemberInviteView",
    "MemberListView",
    "MemberRejectView",
    "OrganizationDocumentsView",
    "OrganizationEmailSettingsView",
    "OrganizationApiSettingsView",
    "OrganizationFactionSettingsView",
    "OrganizationSettingsView",
    "ProfileAbsenceView",
    "ProfileActivityView",
    "ProfileChangeRequestsView",
    "ProfileCommitteesView",
    "ProfileDataPrivacyView",
    "ProfileNotificationsView",
    "ProfileView",
    "ProfileVisibilityView",
    "RegistrationSettingsView",
    "RoleCreateView",
    "RoleDeleteView",
    "RoleEditView",
    "RoleListView",
    "RoleResetView",
    "RoleRestoreDefaultsView",
    "SecurityView",
    "TeamDirectoryView",
    "TeamMemberProfileView",
]
