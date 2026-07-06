# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from . import views

app_name = "provisioning"

urlpatterns = [
    path("organizations/", views.OrganizationCollectionView.as_view(), name="organizations"),
    path("organizations/<slug:slug>/", views.OrganizationDetailView.as_view(), name="organization_detail"),
]
