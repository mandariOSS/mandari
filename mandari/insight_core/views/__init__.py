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
from .maps import (
    MapView,
    map_glyphs,
    map_markers,
    map_sprite,
    style_proxy,
    tile_proxy,
)
from .meetings import (
    MeetingCalendarView,
    MeetingDetailView,
    MeetingListPartial,
    MeetingListView,
    calendar_events,
)
from .neighborhood import (
    NeighborhoodView,
    neighborhood_autocomplete,
    neighborhood_results,
)
from .organizations import (
    OrganizationDetailView,
    OrganizationListPartial,
    OrganizationListView,
)
from .papers import (
    PaperDetailView,
    PaperListPartial,
    PaperListView,
    paper_summary,
)
from .persons import (
    COUNCIL_ROLES,
    PersonDetailView,
    PersonListPartial,
    PersonListView,
)
from .protocols import (
    PublicProtocolDetailView,
    PublicProtocolListView,
)
from .questions import (
    AnswerQuestionView,
    AskQuestionView,
    QuestionSubmittedView,
    VerifyQuestionView,
)
from .search import (
    SearchView,
    search_results,
)
from .sitemap import (
    body_sitemap,
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
    "AskQuestionView",
    "COUNCIL_ROLES",
    "ChatView",
    "FileListView",
    "MapView",
    "MeetingCalendarView",
    "MeetingDetailView",
    "MeetingListPartial",
    "MeetingListView",
    "MerklisteView",
    "NeighborhoodView",
    "OrganizationDetailView",
    "OrganizationListPartial",
    "OrganizationListView",
    "PaperDetailView",
    "PaperListPartial",
    "PaperListView",
    "PersonDetailView",
    "PersonListPartial",
    "PersonListView",
    "PortalHomeView",
    "PublicProtocolDetailView",
    "PublicProtocolListView",
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
    "bookmark_entities",
    "bookmark_ids",
    "bookmark_toggle",
    "calendar_events",
    "chat_message",
    "clear_body",
    "confirm_subscription",
    "file_proxy",
    "get_active_body",
    "is_all_bodies_mode",
    "manage_subscription",
    "map_glyphs",
    "map_markers",
    "map_sprite",
    "neighborhood_autocomplete",
    "neighborhood_results",
    "paper_summary",
    "search_results",
    "set_body",
    "style_proxy",
    "tile_proxy",
    "unsubscribe",
]
