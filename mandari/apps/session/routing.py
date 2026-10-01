# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket-Routing des Session RIS: Sitzungscockpit (Issue #140)."""

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"^ws/session/(?P<tenant_slug>[-\w]+)/cockpit/(?P<meeting_id>[0-9a-f-]+)/$",
        consumers.CockpitConsumer.as_asgi(),
    ),
]
