# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstieg in das Bürgerportal einer Körperschaft: ``/insight/k/<slug>/`` (Issue #317).

Der Einstieg zeigt die Startseite der Kommune mit ihrem Namen, Logo und ihrer Akzentfarbe und
hält den Kontext für alle weiteren Seiten fest (``insight_core/portal.py``). Mit Pfad
(``/insight/k/<slug>/termine/``) führt er nach dem Festhalten auf die genannte Portalseite –
nur auf geprüfte, relative Pfade und ohne Parameter.
"""

from __future__ import annotations

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect

from .. import portal
from .home import PortalHomeView


def portal_entry(request: HttpRequest, slug: str, rest: str = "") -> HttpResponse:
    body = portal.resolve_body(slug)
    if body is None:
        raise Http404("Kein Bürgerportal unter dieser Adresse")
    if getattr(request, "insight_portal_host_slug", None):
        # Eigener Host: nur der Einstieg der zugeordneten Körperschaft
        host_portal = portal.get_portal(request)
        if host_portal is None or host_portal.body.pk != body.pk:
            raise Http404("Kein Bürgerportal unter dieser Adresse")
    else:
        portal.enter(request, body, slug)
    if rest:
        ziel = portal.deep_link_target(rest)
        if ziel is None:
            raise Http404("Keine Portalseite unter dieser Adresse")
        return HttpResponseRedirect(ziel)
    return PortalHomeView.as_view()(request)
