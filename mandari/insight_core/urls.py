# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL-Routing für Mandari Insight Core.

Struktur:
- /insight/         → RIS-Portal (Ratsinformationen)
- /public/          → Öffentliche Protokolle
- /sitemap-insight- → Body-Sitemaps
"""

from django.urls import include, path, re_path

from . import views
from .services.indexierung import listenseite, ohne_index, suchseite

app_name = "insight_core"

# =============================================================================
# Insight Portal URLs (RIS) - unter /insight/
#
# Indexierung (Issue #914, services/indexierung.py): ``listenseite`` = noindex mit Filter-, Seiten- oder
# Sortierparametern, ``suchseite`` = immer noindex, ``ohne_index`` = X-Robots-Tag für Teilansichten und Dateien.
# =============================================================================
insight_patterns = [
    # Portal-Startseite
    path("", views.PortalHomeView.as_view(), name="portal_home"),
    # Kommune wechseln
    path("kommune/<uuid:body_id>/", views.set_body, name="set_body"),
    path("kommune/alle/", views.clear_body, name="clear_body"),
    # Kommunenwechsel für Tausende Kommunen (Issue #783): Vorschläge, Nähe, Stöbern, Seite ohne JavaScript
    path("kommunen/", views.kommunen_seite, name="kommunen"),
    path("kommunen/vorschlaege/", ohne_index(views.kommunen_vorschlaege), name="kommunen_vorschlaege"),
    path("kommunen/naehe/", ohne_index(views.kommunen_naehe), name="kommunen_naehe"),
    path("kommunen/stoebern/", ohne_index(views.kommunen_stoebern), name="kommunen_stoebern"),
    # Bürgerportal je Körperschaft (Issue #317): eigener Einstieg, optional mit Zielseite
    path("k/<slug:slug>/", views.portal_entry, name="portal_entry"),
    path("k/<slug:slug>/<path:rest>", views.portal_entry, name="portal_entry_path"),
    # Gremien (Organizations)
    path("gremien/", listenseite(views.OrganizationListView.as_view()), name="organization_list"),
    path("gremien/<uuid:pk>/", views.OrganizationDetailView.as_view(), name="organization_detail"),
    # Personen
    path("personen/", listenseite(views.PersonListView.as_view()), name="person_list"),
    path("personen/<uuid:pk>/", views.PersonDetailView.as_view(), name="person_detail"),
    path("personen/<uuid:pk>/frage-stellen/", views.AskQuestionView.as_view(), name="ask_question"),
    # Öffentliche Fragen (Ratsfragen-Portal)
    # Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Issue #48)
    path("beschluesse/", listenseite(views.DecisionListView.as_view()), name="decision_list"),
    path("beschluesse/<uuid:pk>/", views.DecisionDetailView.as_view(), name="decision_detail"),
    path("beschluesse/abo/bestaetigen/<uuid:token>/", views.confirm_decision_subscription, name="decision_confirm"),
    path("beschluesse/abo/abmelden/<uuid:token>/", views.unsubscribe_decision, name="decision_unsubscribe"),
    path("fragen/", listenseite(views.QuestionPortalView.as_view()), name="question_portal"),
    path("fragen/stellen/", views.AskQuestionStartView.as_view(), name="question_start"),
    path("fragen/verifizieren/<str:token>/", views.VerifyQuestionView.as_view(), name="verify_question"),
    path("fragen/antworten/<uuid:token>/", views.AnswerQuestionView.as_view(), name="answer_question"),
    path("fragen/gesendet/", views.QuestionSubmittedView.as_view(), name="question_submitted"),
    path("fragen/<uuid:pk>/", views.QuestionDetailView.as_view(), name="question_detail"),
    # Vorgänge (Papers)
    path("vorgaenge/", listenseite(views.PaperListView.as_view()), name="paper_list"),
    path("vorgaenge/<uuid:pk>/", views.PaperDetailView.as_view(), name="paper_detail"),
    path("vorgaenge/<uuid:pk>/zusammenfassung/", ohne_index(views.paper_summary), name="paper_summary"),
    # Termine (Meetings)
    path("termine/", listenseite(views.MeetingListView.as_view()), name="meeting_list"),
    path("termine/kalender/", listenseite(views.MeetingCalendarView.as_view()), name="meeting_calendar"),
    path("termine/kalender.ics", ohne_index(views.calendar_feed), name="calendar_feed"),
    path("termine/jahresplan/", listenseite(views.MeetingYearPlanView.as_view()), name="meeting_year_plan"),
    path("termine/<uuid:pk>/", views.MeetingDetailView.as_view(), name="meeting_detail"),
    # Live-Seite einer Sitzung und Kinomodus (Issue #915): bewusst nicht verlinkt, noindex (setzen den
    # X-Robots-Tag selbst)
    path("termine/<uuid:pk>/live/", views.meeting_live, name="meeting_live"),
    path("termine/<uuid:pk>/live/kino/", views.meeting_live_kino, name="meeting_live_kino"),
    path("termine/partials/calendar-events/", ohne_index(views.calendar_events), name="calendar_events"),
    # Dokumente (Files)
    path("dokumente/", views.FileListView.as_view(), name="file_list"),
    path("dokumente/<uuid:file_id>/preview/", ohne_index(views.file_proxy), name="file_proxy"),
    # Suche
    path("suche/", suchseite(views.SearchView.as_view()), name="search"),
    path("suche/partials/results/", ohne_index(views.search_results), name="search_results"),
    # Karte
    path("karte/", listenseite(views.MapView.as_view()), name="map"),
    path("karte/partials/markers/", ohne_index(views.map_markers), name="map_markers"),
    # Tile Proxy (DSGVO-konform - alle Map-Tiles werden serverseitig geladen)
    path("tiles/<int:z>/<int:x>/<int:y>", ohne_index(views.tile_proxy), name="tile_proxy"),
    # Nachbarschaft
    path("nachbarschaft/", listenseite(views.NeighborhoodView.as_view()), name="neighborhood"),
    path("nachbarschaft/autocomplete/", ohne_index(views.neighborhood_autocomplete), name="neighborhood_autocomplete"),
    path("nachbarschaft/partials/results/", ohne_index(views.neighborhood_results), name="neighborhood_results"),
    # Merkliste (Bookmarks)
    path("gespeichert/", views.MerklisteView.as_view(), name="saved"),
    path("merkliste/api/toggle/", ohne_index(views.bookmark_toggle), name="bookmark_toggle"),
    path("merkliste/api/ids/", ohne_index(views.bookmark_ids), name="bookmark_ids"),
    path("merkliste/api/entities/", ohne_index(views.bookmark_entities), name="bookmark_entities"),
    # Benachrichtigungen (Subscriptions)
    path("benachrichtigungen/", views.SubscribeView.as_view(), name="notifications"),
    path("abo/bestaetigen/<uuid:token>/", views.confirm_subscription, name="confirm_subscription"),
    path("abo/verwalten/<uuid:token>/", views.manage_subscription, name="manage_subscription"),
    path("abo/abmelden/<uuid:token>/", views.unsubscribe, name="unsubscribe"),
    # Chat (KI-Assistent)
    path("chat/", views.ChatView.as_view(), name="chat"),
    path("chat/api/message/", ohne_index(views.chat_message), name="chat_message"),
    # IndexNow (Issue #939, services/indexnow.py): Schlüsseldatei, nur mit INDEXNOW_KEY, sonst 404
    re_path(r"^(?P<name>[A-Za-z0-9-]{8,128})\.txt$", views.indexnow_schluessel, name="indexnow_schluessel"),
    # Rückmeldung am Seitenende („War diese Seite hilfreich?“)
    path("rueckmeldung/", ohne_index(views.page_feedback), name="page_feedback"),
]

# =============================================================================
# Haupt-URL-Patterns
# =============================================================================
urlpatterns = [
    # SEO: robots.txt (greift im Self-Hosting; in Produktion via Marketing-Site)
    path("robots.txt", views.robots_txt, name="robots_txt"),
    # SEO: Sitemap-Index + Body-Sitemaps (bleiben in Mandari, da OParl-Daten hier liegen)
    # Wichtig: Index VOR dem Slug-Pattern registrieren ("index" wäre ein gültiger Slug). Alle Pfade beginnen
    # mit /sitemap-insight-: Nur dieser Präfix geht in der Produktion an Django (Caddyfile).
    path("sitemap-insight-index.xml", views.sitemap_index, name="sitemap_index"),
    # Vorgänge und Sitzungen je Kommune in nummerierten Dateien (Issue #914); vor dem Slug-Pattern, das
    # „koeln-vorgaenge-2“ sonst als Slug läse
    re_path(
        r"^sitemap-insight-(?P<body_slug>[-a-zA-Z0-9_]+)-(?P<art>vorgaenge|sitzungen)-(?P<seite>[1-9][0-9]{0,5})"
        r"\.xml$",
        views.body_sitemap_seite,
        name="body_sitemap_seite",
    ),
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
