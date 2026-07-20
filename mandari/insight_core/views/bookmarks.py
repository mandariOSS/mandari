# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from ..models import (
    Bookmark,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

# =============================================================================
# SEO: robots.txt und Sitemaps
# =============================================================================


# =============================================================================
# Merkliste (Bookmarks)
# =============================================================================


class MerklisteView(TemplateView):
    """Merkliste-Seite: Gespeicherte Vorgänge, Sitzungen, Gremien, Personen."""

    template_name = "pages/merkliste.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        if user.is_authenticated:
            bookmarks = Bookmark.objects.filter(user=user)
            bookmark_ids = {}
            for b in bookmarks:
                bookmark_ids.setdefault(b.entity_type, []).append(b.entity_id)

            context["bookmarked_papers"] = OParlPaper.objects.filter(id__in=bookmark_ids.get("paper", [])).order_by(
                "-date", "-oparl_created"
            )
            context["bookmarked_meetings"] = (
                OParlMeeting.objects.filter(id__in=bookmark_ids.get("meeting", []))
                .prefetch_related("organizations")
                .order_by("-start")
            )
            context["bookmarked_organizations"] = OParlOrganization.objects.filter(
                id__in=bookmark_ids.get("organization", [])
            ).order_by("name")
            context["bookmarked_persons"] = (
                OParlPerson.objects.filter(id__in=bookmark_ids.get("person", []))
                .select_related("body")
                .order_by("family_name", "given_name")
            )
            context["has_bookmarks"] = bookmarks.exists()

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Merkliste",
            description="Deine gespeicherten Vorgänge, Sitzungen, Gremien und Personen auf einen Blick.",
            robots="noindex, follow",
        ).to_dict()
        return context


@require_POST
def bookmark_toggle(request):
    """Toggle-Endpoint: Bookmark hinzufügen oder entfernen."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login erforderlich"}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    entity_type = data.get("type", "")
    entity_id = data.get("id", "")

    if entity_type not in ("person", "paper", "meeting", "organization"):
        return JsonResponse({"error": "Ungültiger Typ"}, status=400)

    if not entity_id:
        return JsonResponse({"error": "ID fehlt"}, status=400)

    bookmark, created = Bookmark.objects.get_or_create(
        user=request.user,
        entity_type=entity_type,
        entity_id=entity_id,
    )

    if not created:
        bookmark.delete()

    return JsonResponse({"bookmarked": created})


@require_GET
def bookmark_ids(request):
    """Gibt alle Bookmark-IDs des Users zurück."""
    if not request.user.is_authenticated:
        return JsonResponse({"person": [], "paper": [], "meeting": [], "organization": []})

    bookmarks = Bookmark.objects.filter(user=request.user).values_list("entity_type", "entity_id")
    result = {"person": [], "paper": [], "meeting": [], "organization": []}
    for entity_type, entity_id in bookmarks:
        if entity_type in result:
            result[entity_type].append(str(entity_id))

    return JsonResponse(result)


@require_GET
def bookmark_entities(request):
    """Rendert HTML-Partial für gegebene Entity-IDs (für anonyme Merkliste)."""
    entity_type = request.GET.get("type", "")
    ids_str = request.GET.get("ids", "")

    if not entity_type or not ids_str:
        return HttpResponse("")

    try:
        ids = [id.strip() for id in ids_str.split(",") if id.strip()]
    except Exception:
        return HttpResponse("")

    if not ids:
        return HttpResponse("")

    template_map = {
        "paper": (
            "partials/merkliste_papers.html",
            OParlPaper.objects.filter(id__in=ids).order_by("-date", "-oparl_created"),
        ),
        "meeting": (
            "partials/merkliste_meetings.html",
            OParlMeeting.objects.filter(id__in=ids).prefetch_related("organizations").order_by("-start"),
        ),
        "organization": (
            "partials/merkliste_organizations.html",
            OParlOrganization.objects.filter(id__in=ids).order_by("name"),
        ),
        "person": (
            "partials/merkliste_persons.html",
            OParlPerson.objects.filter(id__in=ids).select_related("body").order_by("family_name", "given_name"),
        ),
    }

    if entity_type not in template_map:
        return HttpResponse("")

    template_name, queryset = template_map[entity_type]
    return render(request, template_name, {"items": queryset})
