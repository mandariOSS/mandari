# SPDX-License-Identifier: AGPL-3.0-or-later
"""URL-Routing der OParl-Aggregations-API (unter /oparl/)."""

from django.urls import path

from . import views

app_name = "oparl_api"

urlpatterns = [
    path("", views.root_view, name="root"),
    path("v1/", views.root_view, name="root_v1"),
    path("v1/system", views.system_view, name="system"),
    path("v1/bodies", views.bodies_view, name="bodies"),
    path("v1/body/<uuid:pk>", views.object_view, {"kind": "body"}, name="body"),
    path("v1/body/<uuid:pk>/<str:segment>", views.body_sub_list, name="body_list"),
    path("v1/<str:kind>/<uuid:pk>", views.object_view, name="object"),
]
