"""
Context processors for the Mandari Marketing Website.
"""

from django.conf import settings


def site_context(request):
    """Provides site-wide context variables."""
    return {
        "MANDARI_API_URL": getattr(settings, "MANDARI_API_URL", ""),
        "SITE_URL": getattr(settings, "SITE_URL", ""),
    }
