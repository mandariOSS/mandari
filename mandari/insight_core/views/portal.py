# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstieg in das Bürgerportal einer Körperschaft: ``/insight/k/<slug>/`` (Issue #317).

Der Einstieg zeigt die Startseite der Kommune mit ihrem Namen, Logo und ihrer Akzentfarbe und
hält den Kontext für alle weiteren Seiten fest (``insight_core/portal.py``). Mit Pfad
(``/insight/k/<slug>/termine/kalender/``) führt er nach dem Festhalten auf die genannte Portalseite –
nur auf geprüfte, relative Pfade und ohne Parameter.

Die Listen der Sitzungen, Vorgänge, Gremien und Personen haben je Kommune eine eigene Adresse
(``/insight/k/<slug>/vorgaenge/``, Issue #939, ``portal_list``): Sie antworten ohne Sitzung direkt mit der Liste
statt mit einer Weiterleitung, damit Suchmaschinen über ihre Seiten die Detailseiten finden. Sie schreiben nichts
in die Sitzung (``portal.betrachten``).
"""

from __future__ import annotations

from typing import cast

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme

from .. import portal, publication
from ..models import OParlBody
from .home import PortalHomeView
from .meetings import MeetingListView
from .organizations import OrganizationListView
from .papers import PaperListView
from .persons import PersonListView

#: Listen mit eigener Adresse je Kommune (``navigation.KOMMUNEN_LISTEN``): URL-Name der Liste → ihr View
LISTEN_VIEWS = {
    "meeting_list": MeetingListView.as_view(),
    "paper_list": PaperListView.as_view(),
    "organization_list": OrganizationListView.as_view(),
    "person_list": PersonListView.as_view(),
}


def _kommune_des_einstiegs(request: HttpRequest, slug: str) -> OParlBody | HttpResponse:
    """Gelistete Kommune zum Slug – oder die Antwort an ihrer Stelle (410 zurückgenommen, sonst 404)."""
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
    return body


def _stand_der_kommune(request: HttpRequest, body: OParlBody) -> HttpResponse | None:
    """
    Veröffentlichungsstand durchsetzen (Issue #618): 503 abgeschaltet, 410 zurückgenommen, Archiv nur als Hinweis.

    Diese Seiten laufen ohne eigene URL-Auflösung der Kommune an der Middleware vorbei.
    """
    state = publication.body_state(body.pk)
    if state is None:
        return None
    request.insight_publication_state = state  # type: ignore[attr-defined]
    return publication.state_response(request, state)


def portal_list(request: HttpRequest, slug: str, liste: str) -> HttpResponse:
    """
    Liste einer Kommune unter eigener Adresse (``/insight/k/<slug>/vorgaenge/``, Issue #939).

    Dieselbe Liste wie ``/insight/vorgaenge/`` mit gewählter Kommune, aber ohne Sitzung: Der Kontext gilt nur für
    diese Anfrage (``portal.betrachten``). Nicht gelistete Kommunen antworten wie die Stadtseite (404, 410),
    abgeschaltete mit 503.
    """
    body = _kommune_des_einstiegs(request, slug)
    if isinstance(body, HttpResponse):
        return body
    portal.betrachten(request, body, slug)
    antwort = _stand_der_kommune(request, body)
    if antwort is not None:
        return antwort
    return cast(HttpResponse, LISTEN_VIEWS[liste](request))


def portal_entry(request: HttpRequest, slug: str, rest: str = "") -> HttpResponse:
    body = _kommune_des_einstiegs(request, slug)
    if isinstance(body, HttpResponse):
        return body
    if not getattr(request, "insight_portal_host_slug", None):
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
    antwort = _stand_der_kommune(request, body)
    if antwort is not None:
        return antwort
    return PortalHomeView.as_view()(request)
