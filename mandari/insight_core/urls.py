# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL-Routing für Mandari Insight Core.

Struktur:
- /insight/         → RIS-Portal (Ratsinformationen)
- /public/          → Öffentliche Protokolle
- /sitemap-insight- → Body-Sitemaps
"""

from django.urls import include, path

from . import views

app_name = "insight_core"

# =============================================================================
# Insight Portal URLs (RIS) - unter /insight/
# =============================================================================
insight_patterns = [
    # Portal-Startseite
    path("", views.PortalHomeView.as_view(), name="portal_home"),
    # Kommune wechseln
    path("kommune/<uuid:body_id>/", views.set_body, name="set_body"),
    path("kommune/alle/", views.clear_body, name="clear_body"),
    # Gremien (Organizations)
    path("gremien/", views.OrganizationListView.as_view(), name="organization_list"),
    path("gremien/<uuid:pk>/", views.OrganizationDetailView.as_view(), name="organization_detail"),
    path(
        "gremien/partials/list/",
        views.OrganizationListPartial.as_view(),
        name="organization_list_partial",
    ),
    # Personen
    path("personen/", views.PersonListView.as_view(), name="person_list"),
    path("personen/<uuid:pk>/", views.PersonDetailView.as_view(), name="person_detail"),
    path("personen/<uuid:pk>/frage-stellen/", views.AskQuestionView.as_view(), name="ask_question"),
    path("personen/partials/list/", views.PersonListPartial.as_view(), name="person_list_partial"),
    # Öffentliche Fragen
    path("fragen/verifizieren/<uuid:token>/", views.VerifyQuestionView.as_view(), name="verify_question"),
    path("fragen/antworten/<uuid:token>/", views.AnswerQuestionView.as_view(), name="answer_question"),
    path("fragen/gesendet/", views.QuestionSubmittedView.as_view(), name="question_submitted"),
    # Vorgänge (Papers)
    path("vorgaenge/", views.PaperListView.as_view(), name="paper_list"),
    path("vorgaenge/<uuid:pk>/", views.PaperDetailView.as_view(), name="paper_detail"),
    path("vorgaenge/<uuid:pk>/zusammenfassung/", views.paper_summary, name="paper_summary"),
    path("vorgaenge/partials/list/", views.PaperListPartial.as_view(), name="paper_list_partial"),
    # Termine (Meetings)
    path("termine/", views.MeetingListView.as_view(), name="meeting_list"),
    path("termine/kalender/", views.MeetingCalendarView.as_view(), name="meeting_calendar"),
    path("termine/<uuid:pk>/", views.MeetingDetailView.as_view(), name="meeting_detail"),
    path("termine/partials/list/", views.MeetingListPartial.as_view(), name="meeting_list_partial"),
    path("termine/partials/calendar-events/", views.calendar_events, name="calendar_events"),
    # Dokumente (Files)
    path("dokumente/", views.FileListView.as_view(), name="file_list"),
    path("dokumente/<uuid:file_id>/preview/", views.file_proxy, name="file_proxy"),
    # Suche
    path("suche/", views.SearchView.as_view(), name="search"),
    path("suche/partials/results/", views.search_results, name="search_results"),
    # Karte
    path("karte/", views.MapView.as_view(), name="map"),
    path("karte/partials/markers/", views.map_markers, name="map_markers"),
    # Tile Proxy (DSGVO-konform - alle Map-Tiles werden serverseitig geladen)
    path("tiles/<int:z>/<int:x>/<int:y>", views.tile_proxy, name="tile_proxy"),
    path("map-style.json", views.style_proxy, name="map_style"),
    path("map-assets/sprite<path:filename>", views.map_sprite, name="map_sprite"),
    path("map-assets/sprite", views.map_sprite, name="map_sprite_base"),
    path("map-assets/glyphs/<str:fontstack>/<str:range_>.pbf", views.map_glyphs, name="map_glyphs"),
    # Nachbarschaft
    path("nachbarschaft/", views.NeighborhoodView.as_view(), name="neighborhood"),
    path("nachbarschaft/autocomplete/", views.neighborhood_autocomplete, name="neighborhood_autocomplete"),
    path("nachbarschaft/partials/results/", views.neighborhood_results, name="neighborhood_results"),
    # Merkliste (Bookmarks)
    path("gespeichert/", views.MerklisteView.as_view(), name="saved"),
    path("merkliste/api/toggle/", views.bookmark_toggle, name="bookmark_toggle"),
    path("merkliste/api/ids/", views.bookmark_ids, name="bookmark_ids"),
    path("merkliste/api/entities/", views.bookmark_entities, name="bookmark_entities"),
    # Benachrichtigungen (Subscriptions)
    path("benachrichtigungen/", views.SubscribeView.as_view(), name="notifications"),
    path("abo/bestaetigen/<uuid:token>/", views.confirm_subscription, name="confirm_subscription"),
    path("abo/verwalten/<uuid:token>/", views.manage_subscription, name="manage_subscription"),
    path("abo/abmelden/<uuid:token>/", views.unsubscribe, name="unsubscribe"),
    # Chat (KI-Assistent)
    path("chat/", views.ChatView.as_view(), name="chat"),
    path("chat/api/message/", views.chat_message, name="chat_message"),
]

# =============================================================================
# Haupt-URL-Patterns
# =============================================================================
urlpatterns = [
    # SEO: Body-Sitemaps (bleiben in Mandari, da OParl-Daten hier liegen)
    path("sitemap-insight-<slug:body_slug>.xml", views.body_sitemap, name="body_sitemap"),
    # Insight Portal (RIS) - alle unter /insight/
    path("insight/", include((insight_patterns, "insight"))),
    # Öffentliche Fraktionsprotokolle (ohne Login)
    path(
        "public/<slug:body_slug>/protokolle/",
        views.PublicProtocolListView.as_view(),
        name="public_protocols",
    ),
    path(
        "public/<slug:body_slug>/protokolle/<uuid:meeting_id>/",
        views.PublicProtocolDetailView.as_view(),
        name="public_protocol_detail",
    ),
]
