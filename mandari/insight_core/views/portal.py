# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstieg in das Bürgerportal einer Körperschaft: ``/insight/k/<slug>/`` (Issue #317).

Der Einstieg zeigt die Startseite der Kommune mit ihrem Namen, Logo und ihrer Akzentfarbe und
hält den Kontext für alle weiteren Seiten fest (``insight_core/portal.py``). Mit Pfad
(``/insight/k/<slug>/termine/``) führt er nach dem Festhalten auf die genannte Portalseite –
nur auf geprüfte, relative Pfade und ohne Parameter.
"""

from __future__ import annotations

from typing import cast

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme

from .. import portal, publication
from .home import PortalHomeView


def portal_entry(request: HttpRequest, slug: str, rest: str = "") -> HttpResponse:
    body = portal.resolve_body(slug)
    if body is None:
        # Dauerhaft zurückgenommen (Issue #618): „nicht mehr verfügbar“ statt „gibt es nicht“
        zurueckgenommen = portal.withdrawn_state(slug)
        if zurueckgenommen is not None:
            return cast(HttpResponse, publication.state_response(request, zurueckgenommen))
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
        # Zweite Schutzschicht: nur relative Ziele auf dem eigenen Host (CodeQL py/url-redirection)
        if ziel is None or not url_has_allowed_host_and_scheme(
            ziel, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            raise Http404("Keine Portalseite unter dieser Adresse")
        return HttpResponseRedirect(ziel)
    # Die Startseite läuft hier ohne eigene URL-Auflösung an der Middleware vorbei
    state = publication.body_state(body.pk)
    if state is not None:
        request.insight_publication_state = state  # type: ignore[attr-defined]
        response = publication.state_response(request, state)
        if response is not None:
            return response
    return PortalHomeView.as_view()(request)
