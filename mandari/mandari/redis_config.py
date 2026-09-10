# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Konfiguration des Channel-Layers für WebSockets.

channels_redis wartet blockierend auf neue Nachrichten (``brpop_timeout``,
5 Sekunden). redis-py 8 bricht asynchrone Leseoperationen standardmäßig ebenfalls
nach 5 Sekunden ab. Sind beide Zeiten gleich lang, endet jede WebSocket-Verbindung
nach etwa 5 Sekunden mit ``TimeoutError`` – die Live-Kollaboration im Editor
verbindet sich dann im Sekundentakt neu (#216). Der Lese-Timeout des
Channel-Layers liegt deshalb bewusst weit über der blockierenden Wartezeit.
"""

from __future__ import annotations

from typing import Any

CHANNEL_LAYER_SOCKET_TIMEOUT_SECONDS = 30


def build_channel_layers(redis_url: str | None) -> dict[str, Any]:
    """Channel-Layer: Redis im Betrieb, In-Memory ohne ``REDIS_URL`` (Entwicklung)."""
    if not redis_url:
        return {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    return {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [
                    {
                        "address": redis_url,
                        "socket_timeout": CHANNEL_LAYER_SOCKET_TIMEOUT_SECONDS,
                    }
                ],
            },
        }
    }
