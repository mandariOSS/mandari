# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signals for the tenants app.

Handles automatic setup of organizations, including default role creation.
"""

import logging

from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from .models import Membership, Organization, Role

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
            logger.info(f"Created {len(roles)} default roles for organization: {instance.name}")
        except Exception as e:
            logger.error(f"Failed to create default roles for {instance.name}: {e}")


def _permissions_changed(sender, instance, **kwargs):
    """Zwischengespeicherte Rechte einer Mitgliedschaft verwerfen, wenn sich Rollen oder Rechte ändern."""
    instance.__dict__.pop("_permission_checker", None)


for _feld in ("roles", "individual_permissions", "denied_permissions"):
    m2m_changed.connect(
        _permissions_changed,
        sender=getattr(Membership, _feld).through,
        dispatch_uid=f"membership_permissions_changed_{_feld}",
    )
