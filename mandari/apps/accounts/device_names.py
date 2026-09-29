# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grobe Gerätebezeichnung aus dem User-Agent (Sitzungsliste, Sicherheitshinweise)."""

from __future__ import annotations

from typing import Any


def device_name(request: Any) -> str:
    """„Windows PC“, „Mac“, „iPhone“ … aus dem User-Agent der Anfrage; sonst „Unbekanntes Gerät“."""
    ua = str(request.META.get("HTTP_USER_AGENT", ""))
    if "Windows" in ua:
        return "Windows PC"
    if "Mac" in ua:
        return "Mac"
    if "Linux" in ua:
        return "Linux PC"
    if "iPhone" in ua:
        return "iPhone"
    if "Android" in ua:
        return "Android"
    return "Unbekanntes Gerät"
