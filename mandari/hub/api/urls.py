# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Routen des Aggregators (eingebunden unter ``/oparl/``).

Die Adressen sind die Kennungen der Objekte und ändern sich nicht. Der Namensraum heißt weiter
``oparl_api``, damit bestehende ``reverse()``-Aufrufe gültig bleiben.

Zu jeder Adresse gibt es die abweichende Schreibweise mit Schrägstrich am Ende; sie leitet dauerhaft
auf die gültige Adresse weiter (``hub.api.redirects``).

Kurzadressen (Issue #1309): Unter ``v1/body/<slug>`` und allem darunter steht der Name der Kommune statt ihrer
Kennung; sie leiten vorläufig auf die gültige Adresse weiter. Ihre Routen stehen hinter allen gültigen Adressen,
damit eine UUID nie als Name gelesen wird, und nicht in ``ROUTES``: Sie sind keine Kennungen.
"""

from django.urls import path

from hub.api import aggregator, redirects

app_name = "oparl_api"

#: Gültige Adressen: (Route, Endpunkt, feste Parameter, Name)
ROUTES = [
    ("v1/system", aggregator.system_view, {}, "system"),
    ("v1/bodies", aggregator.bodies_view, {}, "bodies"),
    ("v1/body/<uuid:pk>", aggregator.object_view, {"kind": "body"}, "body"),
    ("v1/body/<uuid:pk>/changes", aggregator.body_changes, {}, "body_changes"),
    ("v1/body/<uuid:pk>/snapshot", aggregator.body_snapshot, {}, "body_snapshot"),
    ("v1/body/<uuid:pk>/kommparl/changes", aggregator.body_kommparl_changes, {}, "body_kommparl_changes"),
    ("v1/body/<uuid:pk>/kommparl/snapshot", aggregator.body_kommparl_snapshot, {}, "body_kommparl_snapshot"),
    ("v1/body/<uuid:pk>/<str:segment>", aggregator.body_sub_list, {}, "body_list"),
    ("v1/<str:kind>/<uuid:pk>", aggregator.object_view, {}, "object"),
]

urlpatterns = [
    path("", aggregator.root_view, name="root"),
    path("v1/", aggregator.root_view, name="root_v1"),
    *(path(route, view, kwargs, name=name) for route, view, kwargs, name in ROUTES),
    *(path(f"{route}/", redirects.without_trailing_slash) for route, _, _, _ in ROUTES),
    path("v1/body/<slug:slug>", redirects.body_by_slug, name="body_by_slug"),
    path("v1/body/<slug:slug>/", redirects.body_by_slug),
    path("v1/body/<slug:slug>/<path:rest>", redirects.body_by_slug, name="body_by_slug_rest"),
]
