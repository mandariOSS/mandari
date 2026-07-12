# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class OparlApiConfig(AppConfig):
    """OParl-1.1-konforme Aggregations-API (Issue #17).

    Stellt die von insight_core gespiegelten Ratsinformationen aus allen
    angebundenen Kommunen als eigene, lesende OParl-Datenquelle bereit.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "oparl_api"
    verbose_name = "OParl-API (aggregierte Datenquelle)"
