"""
SEO Utilities für Mandari Insight.

Generiert SEO-relevante Metadaten für alle öffentlichen Seiten.
- Open Graph Tags
- Twitter Cards
- Canonical URLs
- JSON-LD Structured Data
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin

from django.conf import settings
from django.http import HttpRequest
from django.utils.html import escape


@dataclass
class SEOContext:
    """
    Container für SEO-Metadaten einer Seite.

    Wird in Templates für Meta-Tags verwendet.
    """

    title: str
    description: str
    canonical_url: str
    og_type: str = "website"
    og_image: Optional[str] = None
    og_locale: str = "de_DE"
    twitter_card: str = "summary"
    json_ld: Optional[dict] = None
    keywords: list[str] = field(default_factory=list)
    robots: str = "index, follow"
    author: str = "Mandari"

    def to_dict(self) -> dict[str, Any]:
        """Konvertiert zu Dictionary für Template-Rendering."""
        return {
            "title": self.title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "og_type": self.og_type,
            "og_image": self.og_image,
            "og_locale": self.og_locale,
            "twitter_card": self.twitter_card,
            "json_ld": json.dumps(self.json_ld, ensure_ascii=False) if self.json_ld else None,
            "keywords": ", ".join(self.keywords) if self.keywords else None,
            "robots": self.robots,
            "author": self.author,
        }


def get_site_url() -> str:
    """Gibt die konfigurierte Site-URL zurück."""
    return getattr(settings, "SITE_URL", "https://mandari.de")


def build_canonical_url(request: HttpRequest, path: Optional[str] = None) -> str:
    """
    Baut die kanonische URL für eine Seite.

    Args:
        request: Django HttpRequest
        path: Optionaler Pfad (sonst request.path)

    Returns:
        Vollständige kanonische URL
    """
    site_url = get_site_url()
    path = path or request.path
    return urljoin(site_url, path)


def get_default_og_image() -> str:
    """Gibt die Standard Open Graph Bild-URL zurück."""
    site_url = get_site_url()
    return urljoin(site_url, "/static/images/og-default.png")


# =============================================================================
# Entitäts-spezifische SEO-Generatoren
# =============================================================================

def get_paper_seo(paper, request: HttpRequest) -> SEOContext:
    """
    Generiert SEO-Kontext für eine Vorgangs-Detailseite.

    Args:
        paper: OParlPaper Objekt
        request: Django HttpRequest

    Returns:
        SEOContext mit Paper-spezifischen Metadaten
    """
    title = f"{paper.reference or ''}: {paper.name or 'Vorgang'}"[:60]
    description = f"Vorgang {paper.reference or ''} vom {paper.date.strftime('%d.%m.%Y') if paper.date else 'unbekannt'}"

    if paper.paper_type:
        description += f" - {paper.paper_type}"

    # Kürzen auf 160 Zeichen
    description = description[:160]

    # JSON-LD für CreativeWork
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CreativeWork",
        "name": paper.name or "Vorgang",
        "identifier": paper.reference,
        "dateCreated": paper.date.isoformat() if paper.date else None,
        "author": {
            "@type": "Organization",
            "name": paper.body.get_display_name() if paper.body else "Kommune",
        },
        "publisher": {
            "@type": "Organization",
            "name": "Mandari",
            "url": get_site_url(),
        },
    }

    return SEOContext(
        title=title,
        description=description,
        canonical_url=build_canonical_url(request),
        og_type="article",
        json_ld=json_ld,
        keywords=["Kommunalpolitik", "Vorgang", paper.paper_type or "Dokument"],
    )


def get_meeting_seo(meeting, request: HttpRequest) -> SEOContext:
    """
    Generiert SEO-Kontext für eine Sitzungs-Detailseite.

    Args:
        meeting: OParlMeeting Objekt
        request: Django HttpRequest

    Returns:
        SEOContext mit Meeting-spezifischen Metadaten
    """
    org_name = meeting.get_display_name()
    date_str = meeting.start.strftime("%d.%m.%Y") if meeting.start else "unbekannt"

    title = f"{org_name} - {date_str}"[:60]
    description = f"Sitzung: {org_name} am {date_str}"

    if meeting.location_name:
        description += f" in {meeting.location_name}"

    description = description[:160]

    # JSON-LD für Event
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": org_name,
        "startDate": meeting.start.isoformat() if meeting.start else None,
        "endDate": meeting.end.isoformat() if meeting.end else None,
        "location": {
            "@type": "Place",
            "name": meeting.location_name or "Rathaus",
            "address": meeting.location_address,
        } if meeting.location_name else None,
        "organizer": {
            "@type": "Organization",
            "name": meeting.body.get_display_name() if meeting.body else "Kommune",
        },
        "eventStatus": "https://schema.org/EventCancelled" if meeting.cancelled else "https://schema.org/EventScheduled",
    }

    return SEOContext(
        title=title,
        description=description,
        canonical_url=build_canonical_url(request),
        og_type="event",
        json_ld=json_ld,
        keywords=["Sitzung", "Kommunalpolitik", org_name],
    )


def get_organization_seo(organization, request: HttpRequest) -> SEOContext:
    """
    Generiert SEO-Kontext für eine Gremiums-Detailseite.

    Args:
        organization: OParlOrganization Objekt
        request: Django HttpRequest

    Returns:
        SEOContext mit Organization-spezifischen Metadaten
    """
    title = f"{organization.name or 'Gremium'}"[:60]
    description = f"{organization.name or 'Gremium'}"

    if organization.organization_type:
        description += f" ({organization.organization_type})"

    if organization.body:
        description += f" - {organization.body.get_display_name()}"

    description = description[:160]

    # JSON-LD für Organization
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": organization.name,
        "alternateName": organization.short_name,
        "parentOrganization": {
            "@type": "GovernmentOrganization",
            "name": organization.body.get_display_name() if organization.body else None,
        } if organization.body else None,
    }

    return SEOContext(
        title=title,
        description=description,
        canonical_url=build_canonical_url(request),
        og_type="organization",
        json_ld=json_ld,
        keywords=["Gremium", "Kommunalpolitik", organization.organization_type or "Ausschuss"],
    )


def get_person_seo(person, request: HttpRequest) -> SEOContext:
    """
    Generiert SEO-Kontext für eine Personen-Detailseite.

    Args:
        person: OParlPerson Objekt
        request: Django HttpRequest

    Returns:
        SEOContext mit Person-spezifischen Metadaten
    """
    name = person.display_name or str(person)
    title = f"{name} - Ratsmitglied"[:60]
    description = f"{name}"

    if person.body:
        description += f" - {person.body.get_display_name()}"

    description = description[:160]

    # JSON-LD für Person
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": name,
        "givenName": person.given_name,
        "familyName": person.family_name,
        "honorificPrefix": person.title,
        "worksFor": {
            "@type": "GovernmentOrganization",
            "name": person.body.get_display_name() if person.body else None,
        } if person.body else None,
    }

    return SEOContext(
        title=title,
        description=description,
        canonical_url=build_canonical_url(request),
        og_type="profile",
        json_ld=json_ld,
        keywords=["Ratsmitglied", "Kommunalpolitik", name],
    )


def get_body_seo(body, request: HttpRequest) -> SEOContext:
    """
    Generiert SEO-Kontext für eine Kommune-Übersichtsseite.

    Args:
        body: OParlBody Objekt
        request: Django HttpRequest

    Returns:
        SEOContext mit Body-spezifischen Metadaten
    """
    name = body.get_display_name()
    title = f"{name} - Ratsinformationen"[:60]
    description = f"Öffentliche Ratsinformationen für {name}. Sitzungen, Vorgänge, Gremien und Personen."
    description = description[:160]

    # JSON-LD für GovernmentOrganization
    json_ld = {
        "@context": "https://schema.org",
        "@type": "GovernmentOrganization",
        "name": body.name,
        "alternateName": body.short_name or body.display_name,
        "url": body.website,
        "areaServed": {
            "@type": "AdministrativeArea",
            "name": name,
        },
    }

    return SEOContext(
        title=title,
        description=description,
        canonical_url=build_canonical_url(request),
        og_type="website",
        json_ld=json_ld,
        keywords=["Ratsinformationssystem", "Kommunalpolitik", name, "Transparenz"],
    )


# =============================================================================
# Statische Seiten
# =============================================================================

def get_home_seo(request: HttpRequest) -> SEOContext:
    """SEO für die Startseite."""
    return SEOContext(
        title="Mandari - Kommunalpolitische Transparenz",
        description="Mandari macht kommunalpolitische Entscheidungen transparent. Durchsuchen Sie Ratsinformationen, Sitzungen und Vorgänge Ihrer Kommune.",
        canonical_url=build_canonical_url(request, "/"),
        og_type="website",
        og_image=get_default_og_image(),
        json_ld={
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "Mandari",
            "url": get_site_url(),
            "description": "Plattform für kommunalpolitische Transparenz",
            "potentialAction": {
                "@type": "SearchAction",
                "target": f"{get_site_url()}/insight/suche/?q={{search_term_string}}",
                "query-input": "required name=search_term_string",
            },
        },
        keywords=["Kommunalpolitik", "Transparenz", "Ratsinformationssystem", "Open Data"],
    )


def get_search_seo(request: HttpRequest, query: str = "") -> SEOContext:
    """SEO für die Suchseite."""
    title = f"Suche: {query}"[:60] if query else "Suche"
    description = f"Suchergebnisse für '{query}'" if query else "Durchsuchen Sie alle kommunalpolitischen Informationen"

    return SEOContext(
        title=title,
        description=description[:160],
        canonical_url=build_canonical_url(request),
        robots="noindex, follow",  # Suchseiten nicht indexieren
    )


def get_portal_home_seo(request: HttpRequest, body=None) -> SEOContext:
    """SEO für die Portal-Startseite."""
    if body:
        name = body.get_display_name()
        title = f"{name} - Insight Portal"[:60]
        description = f"Ratsinformationen für {name}. Aktuelle Sitzungen, Vorgänge und Beschlüsse."
    else:
        title = "Insight Portal - Alle Kommunen"
        description = "Übersicht aller verfügbaren Ratsinformationssysteme. Wählen Sie Ihre Kommune."

    return SEOContext(
        title=title,
        description=description[:160],
        canonical_url=build_canonical_url(request),
        og_type="website",
    )
