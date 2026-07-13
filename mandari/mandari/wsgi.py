# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WSGI config for Mandari project.

It exposes the WSGI callable as a module-level variable named ``application``.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mandari.settings")

application = get_wsgi_application()
