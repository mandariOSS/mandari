# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket-Routing für das Live-Mitschreiben in den Notizen eines TOPs (Issue #874)."""

from django.urls import re_path

from . import live

websocket_urlpatterns = [
    re_path(r"ws/fraktion/notizen/(?P<item_id>[0-9a-f-]+)/$", live.ProtokollNotizenConsumer.as_asgi()),
]
