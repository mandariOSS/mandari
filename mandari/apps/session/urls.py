# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for Session RIS.

All URLs are prefixed with /session/<tenant_slug>/
"""

from django.urls import path

from . import views
from .api import views as api_views

app_name = "session"

urlpatterns = [
    # Dashboard
    path(
        "<slug:tenant_slug>/",
        views.DashboardView.as_view(),
        name="dashboard",
    ),
    path(
        "<slug:tenant_slug>/dashboard/",
        views.DashboardView.as_view(),
        name="dashboard_explicit",
    ),
    # Meetings
    path(
        "<slug:tenant_slug>/meetings/",
        views.MeetingListView.as_view(),
        name="meetings",
    ),
    path(
        "<slug:tenant_slug>/meetings/create/",
        views.MeetingCreateView.as_view(),
        name="meeting_create",
    ),
    path(
        "<slug:tenant_slug>/meetings/<uuid:meeting_id>/",
        views.MeetingDetailView.as_view(),
        name="meeting_detail",
    ),
    path(
        "<slug:tenant_slug>/meetings/<uuid:meeting_id>/edit/",
        views.MeetingUpdateView.as_view(),
        name="meeting_edit",
    ),
    path(
        "<slug:tenant_slug>/meetings/<uuid:meeting_id>/agenda/add/",
        views.AgendaItemCreateView.as_view(),
        name="agenda_item_create",
    ),
    # Papers
    path(
        "<slug:tenant_slug>/papers/",
        views.PaperListView.as_view(),
        name="papers",
    ),
    path(
        "<slug:tenant_slug>/papers/create/",
        views.PaperCreateView.as_view(),
        name="paper_create",
    ),
    path(
        "<slug:tenant_slug>/papers/<uuid:paper_id>/",
        views.PaperDetailView.as_view(),
        name="paper_detail",
    ),
    path(
        "<slug:tenant_slug>/papers/<uuid:paper_id>/edit/",
        views.PaperUpdateView.as_view(),
        name="paper_edit",
    ),
    # Applications
    path(
        "<slug:tenant_slug>/applications/",
        views.ApplicationListView.as_view(),
        name="applications",
    ),
    path(
        "<slug:tenant_slug>/applications/<uuid:application_id>/",
        views.ApplicationDetailView.as_view(),
        name="application_detail",
    ),
    path(
        "<slug:tenant_slug>/applications/<uuid:application_id>/process/",
        views.ApplicationProcessView.as_view(),
        name="application_process",
    ),
    path(
        "<slug:tenant_slug>/applications/<uuid:application_id>/convert/",
        views.ApplicationConvertView.as_view(),
        name="application_convert",
    ),
    # Organizations
    path(
        "<slug:tenant_slug>/organizations/",
        views.OrganizationListView.as_view(),
        name="organizations",
    ),
    path(
        "<slug:tenant_slug>/organizations/<uuid:organization_id>/",
        views.OrganizationDetailView.as_view(),
        name="organization_detail",
    ),
    # Persons
    path(
        "<slug:tenant_slug>/persons/",
        views.PersonListView.as_view(),
        name="persons",
    ),
    path(
        "<slug:tenant_slug>/persons/<uuid:person_id>/",
        views.PersonDetailView.as_view(),
        name="person_detail",
    ),
    # Settings
    path(
        "<slug:tenant_slug>/settings/",
        views.SettingsView.as_view(),
        name="settings",
    ),
    path(
        "<slug:tenant_slug>/settings/users/",
        views.UserListView.as_view(),
        name="users",
    ),
    # HTMX endpoints
    path(
        "<slug:tenant_slug>/attendance/<uuid:attendance_id>/update/",
        views.AttendanceUpdateView.as_view(),
        name="attendance_update",
    ),
    # API (OParl + Session extension)
    path(
        "<slug:tenant_slug>/api/",
        api_views.APIRootView.as_view(),
        name="api_root",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/",
        api_views.OParlSystemView.as_view(),
        name="oparl_system",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/bodies/",
        api_views.OParlBodyListView.as_view(),
        name="oparl_bodies",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/body/",
        api_views.OParlBodyView.as_view(),
        name="oparl_body",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/organizations/",
        api_views.OParlOrganizationListView.as_view(),
        name="oparl_organizations",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/persons/",
        api_views.OParlPersonListView.as_view(),
        name="oparl_persons",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/meetings/",
        api_views.OParlMeetingListView.as_view(),
        name="oparl_meetings",
    ),
    path(
        "<slug:tenant_slug>/api/oparl/papers/",
        api_views.OParlPaperListView.as_view(),
        name="oparl_papers",
    ),
    # Session API (extended, authenticated)
    path(
        "<slug:tenant_slug>/api/session/meetings/",
        api_views.SessionMeetingListAPIView.as_view(),
        name="api_meetings",
    ),
    path(
        "<slug:tenant_slug>/api/session/papers/",
        api_views.SessionPaperListAPIView.as_view(),
        name="api_papers",
    ),
    path(
        "<slug:tenant_slug>/api/session/applications/",
        api_views.SessionApplicationListAPIView.as_view(),
        name="api_applications",
    ),
    path(
        "<slug:tenant_slug>/api/session/applications/submit/",
        api_views.ApplicationSubmitAPIView.as_view(),
        name="api_application_submit",
    ),
]
