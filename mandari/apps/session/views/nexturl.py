# SPDX-License-Identifier: AGPL-3.0-or-later
"""Absicherung benutzergelieferter "next"-Redirect-Ziele im Session-RIS."""

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request, tenant_slug: str) -> str | None:
    """
    Liefert das "next"-Ziel aus dem POST nur zurück, wenn es eine relative
    URL innerhalb des eigenen Mandanten ist — sonst None (Open-Redirect-Schutz).
    """
    next_url = request.POST.get("next", "")
    if not next_url.startswith(f"/session/{tenant_slug}/"):
        return None
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts=None, require_https=request.is_secure()):
        return None
    return next_url
