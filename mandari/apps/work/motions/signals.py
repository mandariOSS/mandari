# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signale der Dokument-Freigaben.

Persönliche Freigaben (MotionShare scope=user) und Ordner-Freigaben
(FolderGuestShare) hängen am User, nicht an der Mitgliedschaft. Wird eine
Mitgliedschaft entfernt, müssen die Freigaben dieser Organisation mit
verschwinden – sonst wären sie sofort wieder wirksam, sobald derselbe
Account später erneut (z. B. als Gast) aufgenommen wird.
"""

from django.db.models.signals import post_delete


def membership_post_delete(sender, instance, **kwargs):
    """post_delete(Membership): Freigaben des Users in dieser Organisation entfernen."""
    from .models import FolderGuestShare, MotionShare

    MotionShare.objects.filter(
        scope="user", user_id=instance.user_id, motion__organization_id=instance.organization_id
    ).delete()
    FolderGuestShare.objects.filter(user_id=instance.user_id, folder__organization_id=instance.organization_id).delete()


def register():
    from apps.tenants.models import Membership

    post_delete.connect(membership_post_delete, sender=Membership, dispatch_uid="motions_membership_shares_cleanup")
