# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Routen des Einstiegs (eingebunden unter ``/api/``, Namensraum ``schnittstelle``).

Nur diese drei Adressen; alle anderen unter ``/api/`` gehören ihren Bereichen und bleiben unverändert.
"""

from django.urls import path

from hub.api import entry

app_name = "schnittstelle"

urlpatterns = [
    path("", entry.entry_view, name="entry"),
    path("openapi.json", entry.openapi_view, name="openapi"),
    path("docs/", entry.docs_view, name="docs"),
]
