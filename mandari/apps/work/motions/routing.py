# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket URL routing for real-time document collaboration."""

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"ws/documents/(?P<document_id>[0-9a-f-]+)/$",
        consumers.DocumentCollaborationConsumer.as_asgi(),
    ),
]
