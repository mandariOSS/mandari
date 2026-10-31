# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path, re_path

from . import views

app_name = "provisioning_v2"

urlpatterns = [
    path("work/", views.WorkCollectionView.as_view(), name="work_list"),
    # Die Kennung prüft die View (Antwort 400 mit Feldfehler statt einer stummen 404)
    re_path(r"^work/(?P<ref>[^/]+)/?$", views.WorkDetailView.as_view(), name="work_detail"),
]
