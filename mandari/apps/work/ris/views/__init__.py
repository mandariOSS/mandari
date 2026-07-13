# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context.
Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.work.ris import views``)
unverändert funktionieren.
"""

from ._mixins import (
    RISBodiesMixin,
)
from .files import (
    RISFilesView,
)
from .map import (
    RISMapDataView,
    RISMapView,
)
from .meetings import (
    RISMeetingDetailView,
    RISMeetingsView,
)
from .organizations import (
    RISOrganizationDetailView,
    RISOrganizationsView,
)
from .overview import (
    RISOverviewView,
)
from .papers import (
    RISPaperDetailView,
    RISPapersView,
)
from .persons import (
    RISPersonDetailView,
    RISPersonsView,
)
from .search import (
    RISSearchView,
)

__all__ = [
    "RISBodiesMixin",
    "RISFilesView",
    "RISMapDataView",
    "RISMapView",
    "RISMeetingDetailView",
    "RISMeetingsView",
    "RISOrganizationDetailView",
    "RISOrganizationsView",
    "RISOverviewView",
    "RISPaperDetailView",
    "RISPapersView",
    "RISPersonDetailView",
    "RISPersonsView",
    "RISSearchView",
]
