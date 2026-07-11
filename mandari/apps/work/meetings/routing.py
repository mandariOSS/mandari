# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket-Routing für die Echtzeit-Sitzungsvorbereitung."""

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"ws/preparation/(?P<org_slug>[\w-]+)/(?P<scope>item|paper)/(?P<object_id>[0-9a-f-]+)/$",
        consumers.PreparationConsumer.as_asgi(),
    ),
]
