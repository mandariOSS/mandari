"""
Sitemap-Generierung für Mandari Insight.

Hierarchische Sitemap-Struktur:
/sitemap-insight-<body-slug>.xml   (Pro Kommune)
    ├── Vorgänge (Papers)
    ├── Termine (Meetings)
    ├── Gremien (Organizations)
    └── Personen (Persons)

Note: /sitemap.xml, /sitemap-pages.xml, and /robots.txt are handled
by the Wagtail marketing site.
"""

from __future__ import annotations

from django.contrib.sitemaps import Sitemap

from .models import (
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)


class BodyListSitemap(Sitemap):
    """Sitemap für die Kommune-Übersichtsseiten."""

    changefreq = "daily"
    priority = 0.8
    protocol = "https"

    def items(self):
        """Gibt alle aktiven Kommunen zurück."""
        return OParlBody.objects.filter(slug__isnull=False).order_by("name")

    def location(self, body):
        """Gibt die Portal-URL für eine Kommune zurück."""
        return f"/insight/kommune/{body.id}/"

    def lastmod(self, body):
        """Gibt das letzte Änderungsdatum zurück."""
        return body.updated_at


class BaseMunicipalSitemap(Sitemap):
    """Basisklasse für kommunenspezifische Sitemaps."""

    changefreq = "monthly"
    priority = 0.5
    protocol = "https"
    limit = 50000  # Google-Limit pro Sitemap

    def __init__(self, body_slug: str):
        """
        Initialisiert die Sitemap für eine Kommune.

        Args:
            body_slug: Slug der Kommune
        """
        self.body_slug = body_slug
        self._body = None
        super().__init__()

    @property
    def body(self) -> OParlBody | None:
        """Lazy-Loading der Kommune."""
        if self._body is None:
            try:
                self._body = OParlBody.objects.get(slug=self.body_slug)
            except OParlBody.DoesNotExist:
                return None
        return self._body


class PaperSitemap(BaseMunicipalSitemap):
    """Sitemap für Vorgänge einer Kommune."""

    changefreq = "monthly"
    priority = 0.6

    def items(self):
        """Gibt alle Papers der Kommune zurück."""
        if not self.body:
            return []
        return OParlPaper.objects.filter(body=self.body).order_by("-date", "-oparl_created")[: self.limit]

    def location(self, paper):
        """Gibt die URL für einen Vorgang zurück."""
        return f"/insight/vorgaenge/{paper.id}/"

    def lastmod(self, paper):
        """Gibt das letzte Änderungsdatum zurück."""
        return paper.oparl_modified or paper.updated_at


class MeetingSitemap(BaseMunicipalSitemap):
    """Sitemap für Sitzungen einer Kommune."""

    changefreq = "weekly"
    priority = 0.7

    def items(self):
        """Gibt alle Meetings der Kommune zurück."""
        if not self.body:
            return []
        return OParlMeeting.objects.filter(body=self.body).order_by("-start")[: self.limit]

    def location(self, meeting):
        """Gibt die URL für eine Sitzung zurück."""
        return f"/insight/termine/{meeting.id}/"

    def lastmod(self, meeting):
        """Gibt das letzte Änderungsdatum zurück."""
        return meeting.oparl_modified or meeting.updated_at


class OrganizationSitemap(BaseMunicipalSitemap):
    """Sitemap für Gremien einer Kommune."""

    changefreq = "monthly"
    priority = 0.5

    def items(self):
        """Gibt alle Organizations der Kommune zurück."""
        if not self.body:
            return []
        return OParlOrganization.objects.filter(body=self.body).order_by("name")[: self.limit]

    def location(self, org):
        """Gibt die URL für ein Gremium zurück."""
        return f"/insight/gremien/{org.id}/"

    def lastmod(self, org):
        """Gibt das letzte Änderungsdatum zurück."""
        return org.oparl_modified or org.updated_at


class PersonSitemap(BaseMunicipalSitemap):
    """Sitemap für Personen einer Kommune."""

    changefreq = "monthly"
    priority = 0.4

    def items(self):
        """Gibt alle Persons der Kommune zurück."""
        if not self.body:
            return []
        return OParlPerson.objects.filter(body=self.body).order_by("family_name", "given_name")[: self.limit]

    def location(self, person):
        """Gibt die URL für eine Person zurück."""
        return f"/insight/personen/{person.id}/"

    def lastmod(self, person):
        """Gibt das letzte Änderungsdatum zurück."""
        return person.oparl_modified or person.updated_at


def get_body_sitemap(body_slug: str) -> dict[str, Sitemap]:
    """
    Gibt alle Sitemaps für eine Kommune zurück.

    Args:
        body_slug: Slug der Kommune

    Returns:
        Dict mit Sitemap-Name → Sitemap-Instanz
    """
    return {
        "papers": PaperSitemap(body_slug),
        "meetings": MeetingSitemap(body_slug),
        "organizations": OrganizationSitemap(body_slug),
        "persons": PersonSitemap(body_slug),
    }
