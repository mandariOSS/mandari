"""
API URL configuration for Mandari Insight Core.

All endpoints are public and read-only (except contact submission).
"""

from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import api

app_name = "insight_api"

urlpatterns = [
    path("stats/", api.stats, name="stats"),
    path("stats/bodies/", api.stats_bodies, name="stats_bodies"),
    path("contact/", csrf_exempt(api.contact_submit), name="contact_submit"),
    path("contact/subjects/", api.contact_subjects, name="contact_subjects"),
]
