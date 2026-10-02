# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Routen des DCAT-AP.de-Katalogs des Aggregators (eingebunden unter ``/data/dcat/``, Namensraum ``dcat``).

Die Adressen sind die Kennungen der Kataloge und ihrer Datensätze und ändern sich nicht. Je Katalog gibt es
die Adresse ohne Endung (Inhaltsaushandlung) und je Form eine mit Endung (``.ttl``, ``.rdf``, ``.jsonld``).
"""

from django.urls import path

from hub.adapters.dcat import aggregator

app_name = "dcat"

urlpatterns = [
    path("catalog", aggregator.catalog_view, name="catalog"),
    path("catalog.<str:endung>", aggregator.catalog_view, name="catalog_format"),
    path("body/<uuid:pk>/catalog", aggregator.body_catalog_view, name="body_catalog"),
    path("body/<uuid:pk>/catalog.<str:endung>", aggregator.body_catalog_view, name="body_catalog_format"),
]
