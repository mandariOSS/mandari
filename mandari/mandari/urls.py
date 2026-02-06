"""
URL configuration for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import include, path


def health_check(request):
    """Health check endpoint for Docker/Kubernetes."""
    # Check database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"

    return JsonResponse(
        {
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
        }
    )


urlpatterns = [
    # Health check (for Docker/Kubernetes)
    path("health/", health_check, name="health_check"),
    # Admin
    path("admin/", admin.site.urls),
    # Authentication (login, logout, password reset)
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    # Session RIS (administrative portal)
    path("session/", include("apps.session.urls", namespace="session")),
    # Work module (portal for organizations)
    path("work/", include("apps.work.urls", namespace="work")),
    # Main application (public RIS)
    path("", include("insight_core.urls")),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# =============================================================================
# Custom Error Handlers
# =============================================================================


def handler_400(request, exception=None):
    """Bad Request error handler."""
    return render(request, "400.html", status=400)


def handler_403(request, exception=None):
    """Permission Denied error handler."""
    return render(request, "403.html", status=403)


def handler_404(request, exception=None):
    """Page Not Found error handler."""
    return render(request, "404.html", status=404)


def handler_500(request):
    """Server Error handler."""
    import uuid

    return render(request, "500.html", {"request_id": str(uuid.uuid4())[:8]}, status=500)


# Register custom error handlers
handler400 = handler_400
handler403 = handler_403
handler404 = handler_404
handler500 = handler_500
