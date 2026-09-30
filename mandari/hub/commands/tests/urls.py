# SPDX-License-Identifier: AGPL-3.0-or-later
"""URLs für die Tests des HTTP-Wegs: der Dispatcher der Installation unter ``/befehle/``."""

from django.urls import include, path

from hub.commands import command_urlpatterns
from hub.commands.tests.hilfen import anmelden

urlpatterns = [path("befehle/", include(command_urlpatterns(authenticate=anmelden)))]
