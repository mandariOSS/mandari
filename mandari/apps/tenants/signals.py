# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signals for the tenants app.

Handles automatic setup of organizations, including default role creation.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Organization, Role

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Organization)
def create_default_roles_on_organization_create(sender, instance, created, **kwargs):
    """
    Automatically create default roles when a new organization is created.

    This ensures every organization starts with the standard faction roles:
    - Administrator
    - Fraktionsvorsitz
    - Stellv. Vorsitz
    - Fraktionsmitglied
    - Sachkundige/r Bürger/in
    - etc.
    """
    if created:
        try:
            roles = Role.create_default_roles(instance)
            logger.info(
                f"Created {len(roles)} default roles for organization: {instance.name}"
            )
        except Exception as e:
            logger.error(
                f"Failed to create default roles for {instance.name}: {e}"
            )
