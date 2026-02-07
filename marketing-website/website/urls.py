"""
URL configuration for Mandari Marketing Website.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from wagtail import urls as wagtail_urls
from wagtail.admin import urls as wagtailadmin_urls
from wagtail.contrib.sitemaps.views import sitemap
from wagtail.documents import urls as wagtaildocs_urls


def health_check(request):
    """Health check endpoint for Docker."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("django-admin/", admin.site.urls),
    path("cms-admin/", include(wagtailadmin_urls)),
    path("documents/", include(wagtaildocs_urls)),
    path("sitemap.xml", sitemap, name="sitemap"),
    # Wagtail catch-all (serves all CMS pages)
    path("", include(wagtail_urls)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
