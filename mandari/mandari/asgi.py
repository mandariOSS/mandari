# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ASGI config for Mandari project.

Supports both HTTP and WebSocket protocols via Django Channels.
WebSocket routes are used for real-time document collaboration.
"""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mandari.settings")

# Initialize Django ASGI application early to ensure AppRegistry is populated
django_asgi_app = get_asgi_application()

# Import WebSocket routing after Django is initialized
from apps.work.meetings.routing import websocket_urlpatterns as meetings_websocket_urlpatterns  # noqa: E402
from apps.work.motions.routing import websocket_urlpatterns as motions_websocket_urlpatterns  # noqa: E402

websocket_urlpatterns = motions_websocket_urlpatterns + meetings_websocket_urlpatterns

# Was beim Start im Hauptthread an Datenbankverbindungen geöffnet wurde (Importe,
# App-Initialisierung), zurückgeben. Dieser Thread bedient keine Anfragen und hielte sie
# sonst für immer fest — mit Pool ein dauerhaft belegter Platz (Issue #344).
from apps.common.db_connections import release_idle_thread_connections  # noqa: E402

release_idle_thread_connections()

# WebSockets nur vom eigenen Host oder aus CSRF_TRUSTED_ORIGINS – nicht von jeder Subdomain,
# die ein Platzhalter in ALLOWED_HOSTS zulässt (apps/common/websocket_origin.py).
from apps.common.websocket_origin import SameOriginWebSocketValidator  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": SameOriginWebSocketValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
