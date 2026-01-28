"""
URL-Konfiguration für Blog und Releases
"""

from django.urls import path
from . import views

app_name = "insight_content"

urlpatterns = [
    # Blog
    path("blog/", views.blog_list, name="blog_list"),
    path("blog/<slug:slug>/", views.blog_detail, name="blog_detail"),

    # Releases
    path("releases/", views.releases_list, name="releases_list"),
]
