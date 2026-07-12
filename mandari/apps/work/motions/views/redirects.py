# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.shortcuts import redirect
from django.views.generic import View

logger = logging.getLogger("apps.work.motions")


# =============================================================================
# Legacy /motions/ → /documents/ Redirect Views
# =============================================================================


class MotionRedirectView(View):
    """Redirect /motions/ → /documents/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:documents", org_slug=org_slug, permanent=True)


class MotionDetailRedirectView(View):
    """Redirect /motions/<id>/ and /motions/<id>/edit/ → /documents/<id>/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        motion_id = kwargs.get("motion_id")
        return redirect("work:document_editor", org_slug=org_slug, motion_id=motion_id, permanent=True)


class MotionRedirectCreateView(View):
    """Redirect /motions/create/ → /documents/create/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_create", org_slug=org_slug, permanent=True)


class MotionRedirectTrashView(View):
    """Redirect /motions/trash/ → /documents/trash/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_trash", org_slug=org_slug, permanent=True)


class MotionRedirectImportView(View):
    """Redirect /motions/import/ → /documents/import/."""

    def get(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        return redirect("work:document_import", org_slug=org_slug, permanent=True)
