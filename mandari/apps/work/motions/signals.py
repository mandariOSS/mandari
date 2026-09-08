# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signale der Dokument-Freigaben.

Persönliche Freigaben (MotionShare scope=user) und Ordner-Freigaben
(FolderGuestShare) hängen am User, nicht an der Mitgliedschaft. Wird eine
Mitgliedschaft entfernt, müssen die Freigaben dieser Organisation mit
verschwinden – sonst wären sie sofort wieder wirksam, sobald derselbe
Account später erneut (z. B. als Gast) aufgenommen wird.
"""

from django.db.models.signals import post_delete, post_save, pre_save


def membership_post_delete(sender, instance, **kwargs):
    """post_delete(Membership): Freigaben des Users in dieser Organisation entfernen."""
    from .models import FolderGuestShare, MotionShare

    MotionShare.objects.filter(
        scope="user", user_id=instance.user_id, motion__organization_id=instance.organization_id
    ).delete()
    FolderGuestShare.objects.filter(user_id=instance.user_id, folder__organization_id=instance.organization_id).delete()


def application_pre_save(sender, instance, **kwargs):
    """Alten Antragsstatus merken, damit post_save nur echte Wechsel meldet (Issue #40)."""
    if instance.pk:
        instance._previous_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    else:
        instance._previous_status = None


def application_post_save(sender, instance, created, **kwargs):
    """Statuswechsel der Verwaltung ins verknüpfte Work-Dokument spiegeln."""
    if created:
        return
    previous = getattr(instance, "_previous_status", None)
    if previous == instance.status:
        return
    from .ris_submission import sync_motion_from_application

    sync_motion_from_application(instance, previous)


def consultation_post_save(sender, instance, created, **kwargs):
    """Beratung mit Sitzung angelegt → Work-Dokument „Auf Tagesordnung“."""
    if not instance.meeting_id:
        return
    from .ris_submission import sync_motion_from_consultation

    sync_motion_from_consultation(instance)


def register():
    from apps.session.models import SessionApplication, SessionConsultation
    from apps.tenants.models import Membership

    post_delete.connect(membership_post_delete, sender=Membership, dispatch_uid="motions_membership_shares_cleanup")
    pre_save.connect(application_pre_save, sender=SessionApplication, dispatch_uid="motions_ris_application_pre")
    post_save.connect(application_post_save, sender=SessionApplication, dispatch_uid="motions_ris_application_post")
    post_save.connect(consultation_post_save, sender=SessionConsultation, dispatch_uid="motions_ris_consultation_post")
