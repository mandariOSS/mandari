# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from insight_core import views``)
unverändert funktionieren.
"""

from ._helpers import (
    get_active_body,
    is_all_bodies_mode,
)
from .bookmarks import (
    MerklisteView,
    bookmark_entities,
    bookmark_ids,
    bookmark_toggle,
)
from .chat import (
    ChatView,
    _check_rate_limit,
    _get_client_ip,
    chat_message,
)
from .decisions import (  # noqa: F401
    DecisionDetailView,
    DecisionListView,
    confirm_decision_subscription,
    unsubscribe_decision,
)
from .feedback import page_feedback
from .files import (
    FileListView,
    _annotate_files_with_context,
    _file_proxy_error,
    file_proxy,
)
from .home import (
    PortalHomeView,
    clear_body,
    set_body,
)
from .kommunen import (
    kommunen_naehe,
    kommunen_seite,
    kommunen_stoebern,
    kommunen_vorschlaege,
)
from .live import meeting_live
from .maps import (
    MapView,
    map_markers,
    tile_proxy,
)
from .meetings import (
    MeetingCalendarView,
    MeetingDetailView,
    MeetingListView,
    MeetingYearPlanView,
    calendar_events,
    calendar_feed,
)
from .neighborhood import (
    NeighborhoodView,
    neighborhood_autocomplete,
    neighborhood_results,
)
from .organizations import (
    OrganizationDetailView,
    OrganizationListView,
)
from .papers import (
    PaperDetailView,
    PaperListView,
    paper_summary,
)
from .persons import (
    COUNCIL_ROLES,
    PersonDetailView,
    PersonListView,
)
from .portal import portal_entry
from .protocols import (
    PublicProtocolDetailView,
    PublicProtocolListView,
)
from .questions import (
    AnswerQuestionView,
    AskQuestionStartView,
    AskQuestionView,
    QuestionDetailView,
    QuestionPortalView,
    QuestionSubmittedView,
    VerifyQuestionView,
)
from .search import (
    SearchView,
    search_results,
)
from .sitemap import (
    body_sitemap,
    robots_txt,
    sitemap_index,
)
from .subscriptions import (
    SubscribeView,
    _send_confirmation_email,
    confirm_subscription,
    manage_subscription,
    unsubscribe,
)

__all__ = [
    "AnswerQuestionView",
    "AskQuestionStartView",
    "AskQuestionView",
    "COUNCIL_ROLES",
    "ChatView",
    "FileListView",
    "MapView",
    "MeetingCalendarView",
    "MeetingYearPlanView",
    "MeetingDetailView",
    "MeetingListView",
    "MerklisteView",
    "NeighborhoodView",
    "OrganizationDetailView",
    "OrganizationListView",
    "PaperDetailView",
    "PaperListView",
    "PersonDetailView",
    "PersonListView",
    "PortalHomeView",
    "kommunen_naehe",
    "kommunen_seite",
    "kommunen_stoebern",
    "kommunen_vorschlaege",
    "portal_entry",
    "PublicProtocolDetailView",
    "PublicProtocolListView",
    "QuestionDetailView",
    "QuestionPortalView",
    "QuestionSubmittedView",
    "SearchView",
    "SubscribeView",
    "VerifyQuestionView",
    "_annotate_files_with_context",
    "_check_rate_limit",
    "_file_proxy_error",
    "_get_client_ip",
    "_send_confirmation_email",
    "body_sitemap",
    "robots_txt",
    "sitemap_index",
    "bookmark_entities",
    "bookmark_ids",
    "bookmark_toggle",
    "calendar_events",
    "calendar_feed",
    "chat_message",
    "clear_body",
    "confirm_subscription",
    "file_proxy",
    "get_active_body",
    "is_all_bodies_mode",
    "manage_subscription",
    "map_markers",
    "meeting_live",
    "neighborhood_autocomplete",
    "neighborhood_results",
    "page_feedback",
    "paper_summary",
    "search_results",
    "set_body",
    "tile_proxy",
    "unsubscribe",
]
