# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

from ..models import (
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

# =============================================================================
# SEO: robots.txt und Sitemaps
# =============================================================================


@require_GET
def body_sitemap(request, body_slug):
    """
    Generiert die Sitemap für eine Kommune.

    Enthält alle Vorgänge, Sitzungen, Gremien und Personen.
    """
    from django.conf import settings

    site_url = getattr(settings, "SITE_URL", "https://mandari.de")

    try:
        body = OParlBody.objects.get(slug=body_slug)
    except OParlBody.DoesNotExist:
        raise Http404("Kommune nicht gefunden")

    # XML generieren
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    def add_url(loc, lastmod=None, changefreq="monthly", priority=0.5):
        xml_parts.append("  <url>")
        xml_parts.append(f"    <loc>{site_url}{loc}</loc>")
        if lastmod:
            xml_parts.append(f"    <lastmod>{lastmod.strftime('%Y-%m-%dT%H:%M:%S+00:00')}</lastmod>")
        xml_parts.append(f"    <changefreq>{changefreq}</changefreq>")
        xml_parts.append(f"    <priority>{priority}</priority>")
        xml_parts.append("  </url>")

    # Vorgänge (max 10000 pro Sitemap für Performance)
    for paper in OParlPaper.objects.filter(body=body, deleted=False).order_by("-date")[:10000]:
        add_url(
            f"/insight/vorgaenge/{paper.id}/",
            paper.oparl_modified or paper.updated_at,
            "monthly",
            0.6,
        )

    # Sitzungen
    for meeting in OParlMeeting.objects.filter(body=body, deleted=False).order_by("-start")[:10000]:
        add_url(
            f"/insight/termine/{meeting.id}/",
            meeting.oparl_modified or meeting.updated_at,
            "weekly",
            0.7,
        )

    # Gremien
    for org in OParlOrganization.objects.filter(body=body, deleted=False).order_by("name")[:5000]:
        add_url(f"/insight/gremien/{org.id}/", org.oparl_modified or org.updated_at, "monthly", 0.5)

    # Personen
    for person in OParlPerson.objects.filter(body=body, deleted=False).order_by("family_name")[:5000]:
        add_url(
            f"/insight/personen/{person.id}/",
            person.oparl_modified or person.updated_at,
            "monthly",
            0.4,
        )

    xml_parts.append("</urlset>")

    response = HttpResponse("\n".join(xml_parts), content_type="application/xml; charset=utf-8")

    # Cache für 24 Stunden
    response["Cache-Control"] = "public, max-age=86400"

    return response
