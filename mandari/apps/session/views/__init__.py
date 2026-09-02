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
from .allowances import (
    AllowanceApproveView,
    AllowanceCsvExportView,
    AllowanceDebtorSaveView,
    AllowanceGenerateView,
    AllowanceListView,
    AllowanceNoticePdfView,
    AllowanceRateDeleteView,
    AllowanceRateSaveView,
    AllowanceSepaExportView,
    AllowanceYearView,
)
from .applications import (
    ApplicationConvertView,
    ApplicationDetailView,
    ApplicationListView,
    ApplicationProcessView,
)
from .attendance import (
    AttendanceAddView,
    AttendanceDeleteView,
    AttendanceGenerateView,
)
from .audit import (
    AuditLogListView,
)
from .calendar import (
    MeetingCalendarView,
    MeetingPlanView,
    OrganizationIcsFeedView,
    YearPlanPdfView,
)
from .consultations import (
    ConsultationCreateView,
    ConsultationDeleteView,
    ConsultationForwardView,
    ConsultationMoveView,
    ConsultationScheduleView,
    ConsultationUpdateView,
)
from .cosign import (
    CosignatureActionView,
    CosignRuleManageView,
    CosignSettingsView,
    DepartmentAssignmentView,
    MyCosignaturesView,
)
from .dashboard import (
    DashboardView,
)
from .devices import (
    DeviceActionView,
    DeviceGrantActionView,
    DeviceGrantCsvExportView,
    DeviceGrantSaveView,
    DeviceHandoverPdfView,
    DeviceListView,
    DeviceSaveView,
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
from .monthly_allowances import (
    MonthlyAllowanceView,
    MonthlyApproveView,
    MonthlyAssignmentDeleteView,
    MonthlyAssignmentSaveView,
    MonthlyCsvExportView,
    MonthlyGenerateView,
    MonthlyRateDeleteView,
    MonthlyRateSaveView,
    MonthlySepaExportView,
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
    PaperReviewListView,
    PaperUpdateView,
    PaperWorkflowView,
)
from .persons import (
    PersonCreateView,
    PersonDeactivateView,
    PersonDetailView,
    PersonListView,
    PersonUpdateView,
)
from .privacy import (
    PersonDataExportView,
    PrivacyNoticeView,
    PrivacyPurgeRunView,
    PrivacySettingsView,
)
from .protocols import (
    ProtocolCreateView,
    ProtocolDetailView,
    ProtocolEditView,
    ProtocolPdfView,
    ProtocolWorkflowView,
)
from .reports import (
    ReportCsvExportView,
    ReportsView,
)
from .resolutions import (
    ResolutionBatchView,
    ResolutionCsvExportView,
    ResolutionExtractPdfView,
    ResolutionForwardingCreateView,
    ResolutionMeetingPdfView,
    ResolutionRegisterView,
    ResolutionTrackingUpdateView,
)
from .search import SessionSearchView
from .settings import (
    InsightPublishView,
    InvitationAcceptView,
    InvitationCancelView,
    ReminderSettingsView,
    SettingsView,
    UserDeactivateView,
    UserInviteView,
    UserListView,
    UserRolesUpdateView,
)
from .terms import (
    ArchiveView,
    TermChangeView,
    TermDeleteView,
    TermListView,
    TermSaveView,
)
from .textblocks import (
    StandardItemManageView,
    TextblockManageView,
    TextblockSettingsView,
)
from .voting import (
    CircularCloseView,
    CircularCreateView,
    CircularDetailView,
    CircularListView,
    CircularVoteView,
    VotingCaptureView,
)

__all__ = [
    "DeviceActionView",
    "DeviceGrantActionView",
    "DeviceGrantCsvExportView",
    "DeviceGrantSaveView",
    "DeviceHandoverPdfView",
    "DeviceListView",
    "DeviceSaveView",
    "MonthlyAllowanceView",
    "MonthlyApproveView",
    "MonthlyAssignmentDeleteView",
    "MonthlyAssignmentSaveView",
    "MonthlyCsvExportView",
    "MonthlyGenerateView",
    "MonthlyRateDeleteView",
    "MonthlyRateSaveView",
    "MonthlySepaExportView",
    "CircularCloseView",
    "CircularCreateView",
    "CircularDetailView",
    "CircularListView",
    "CircularVoteView",
    "VotingCaptureView",
    "CosignatureActionView",
    "CosignRuleManageView",
    "CosignSettingsView",
    "DepartmentAssignmentView",
    "MyCosignaturesView",
    "SessionSearchView",
    "ReportCsvExportView",
    "ReportsView",
    "StandardItemManageView",
    "TextblockManageView",
    "TextblockSettingsView",
    "MeetingCalendarView",
    "MeetingPlanView",
    "OrganizationIcsFeedView",
    "YearPlanPdfView",
    "AgendaItemCreateView",
    "AllowanceApproveView",
    "AllowanceCsvExportView",
    "AllowanceDebtorSaveView",
    "AllowanceGenerateView",
    "AllowanceListView",
    "AllowanceNoticePdfView",
    "AllowanceRateDeleteView",
    "AllowanceRateSaveView",
    "AllowanceSepaExportView",
    "AllowanceYearView",
    "ArchiveView",
    "TermChangeView",
    "TermDeleteView",
    "TermListView",
    "TermSaveView",
    "AgendaItemDeleteView",
    "AgendaItemMoveView",
    "AgendaItemUpdateView",
    "AgendaItemWithdrawView",
    "AgendaReorderView",
    "ApplicationConvertView",
    "ApplicationDetailView",
    "ApplicationListView",
    "ApplicationProcessView",
    "AttendanceAddView",
    "AttendanceDeleteView",
    "AttendanceGenerateView",
    "AttendanceUpdateView",
    "AuditLogListView",
    "ConsultationCreateView",
    "ConsultationDeleteView",
    "ConsultationForwardView",
    "ConsultationMoveView",
    "ConsultationScheduleView",
    "ConsultationUpdateView",
    "DashboardView",
    "FileDeleteView",
    "FileDownloadView",
    "FileReplaceView",
    "FileUpdateView",
    "FileUploadView",
    "InsightPublishView",
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
    "PaperReviewListView",
    "PaperUpdateView",
    "PaperWorkflowView",
    "PersonCreateView",
    "PersonDataExportView",
    "PersonDeactivateView",
    "PrivacyNoticeView",
    "PrivacyPurgeRunView",
    "PrivacySettingsView",
    "PersonDetailView",
    "PersonListView",
    "PersonUpdateView",
    "ProtocolCreateView",
    "ProtocolDetailView",
    "ProtocolEditView",
    "ProtocolPdfView",
    "ProtocolWorkflowView",
    "ResolutionBatchView",
    "ResolutionCsvExportView",
    "ResolutionExtractPdfView",
    "ResolutionForwardingCreateView",
    "ResolutionMeetingPdfView",
    "ResolutionRegisterView",
    "ResolutionTrackingUpdateView",
    "ReminderSettingsView",
    "SettingsView",
    "UserDeactivateView",
    "UserInviteView",
    "UserListView",
    "UserRolesUpdateView",
]
