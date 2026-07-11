# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ASGI config for Mandari project.

Supports both HTTP and WebSocket protocols via Django Channels.
WebSocket routes are used for real-time document collaboration.
"""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mandari.settings")

# Initialize Django ASGI application early to ensure AppRegistry is populated
django_asgi_app = get_asgi_application()

# Import WebSocket routing after Django is initialized
from apps.work.meetings.routing import websocket_urlpatterns as meetings_websocket_urlpatterns  # noqa: E402
from apps.work.motions.routing import websocket_urlpatterns as motions_websocket_urlpatterns  # noqa: E402

websocket_urlpatterns = motions_websocket_urlpatterns + meetings_websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
