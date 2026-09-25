# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signale der Dokumente.

Freigaben: Persönliche Freigaben (MotionShare scope=user) und Ordner-Freigaben
(FolderGuestShare) hängen am User, nicht an der Mitgliedschaft. Wird eine
Mitgliedschaft entfernt, müssen die Freigaben dieser Organisation mit
verschwinden – sonst wären sie sofort wieder wirksam, sobald derselbe
Account später erneut (z. B. als Gast) aufgenommen wird.

Rückmeldung der Verwaltung: Änderungen in Session an eingereichten Anträgen,
ihren Vorlagen, Stationen, TOPs und Sitzungen stoßen die Rückmeldung an das
Work-Dokument an (Issues #40, #316; siehe administration_feedback).
"""

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save


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


# =============================================================================
# Rückmeldung der Verwaltung (Issues #40, #316)
# =============================================================================
#
# Jede Änderung in Session, die den Rückmeldestand eines eingereichten Antrags betreffen kann, meldet
# die Antrags-ID. Die eigentliche Arbeit läuft nach dem Commit (administration_feedback.schedule_sync)
# und ist idempotent. Nur Anträge mit verknüpftem Work-Dokument lösen überhaupt etwas aus.

MEETING_FIELDS = ("is_public", "start", "cancelled", "organization_id", "name")
AGENDA_ITEM_FIELDS = ("is_public", "vote_result", "resolution_number", "is_withdrawn", "meeting_id", "paper_id")
CONSULTATION_FIELDS = ("meeting_id", "agenda_item_id", "result", "role", "authoritative", "organization_id", "paper_id")
PAPER_FIELDS = ("is_public", "status", "reference", "source_application_id")


def _changed(instance, fields, created=False) -> bool:
    """Wurde eines der Felder geändert? Ohne bekannten Altzustand (Audit-Receiver) gilt: ja."""
    if created:
        return True
    old = getattr(instance, "_audit_old", None)
    if old is None:
        return True
    return any(getattr(old, field) != getattr(instance, field) for field in fields)


def _schedule_for_papers(paper_ids) -> None:
    from apps.session.models import SessionApplication

    from .administration_feedback import schedule_sync

    ids = {paper_id for paper_id in paper_ids if paper_id}
    if not ids:
        return
    applications = SessionApplication.objects.filter(created_papers__in=ids, work_motion__isnull=False).values_list(
        "pk", flat=True
    )
    for application_id in set(applications):
        schedule_sync(application_id)


def application_post_save(sender, instance, created, **kwargs):
    """Statuswechsel der Verwaltung ins verknüpfte Work-Dokument spiegeln."""
    if created or kwargs.get("raw"):
        return
    previous = getattr(instance, "_previous_status", None)
    if previous == instance.status:
        return
    from .administration_feedback import schedule_sync

    schedule_sync(instance.pk)


def paper_post_save(sender, instance, created, **kwargs):
    """Vorlage aus einem Antrag: Nummer, Veröffentlichung oder Status geändert."""
    if kwargs.get("raw") or not instance.source_application_id:
        return
    if _changed(instance, PAPER_FIELDS, created):
        from .administration_feedback import schedule_sync

        schedule_sync(instance.source_application_id)


def paper_post_delete(sender, instance, **kwargs):
    if instance.source_application_id:
        from .administration_feedback import schedule_sync

        schedule_sync(instance.source_application_id)


def consultation_post_save(sender, instance, created, **kwargs):
    """Station der Beratungsfolge angelegt, terminiert, verschoben oder mit Ergebnis."""
    if kwargs.get("raw"):
        return
    if _changed(instance, CONSULTATION_FIELDS, created):
        _schedule_for_papers([instance.paper_id])


def consultation_post_delete(sender, instance, **kwargs):
    _schedule_for_papers([instance.paper_id])


def agenda_item_post_save(sender, instance, created, **kwargs):
    """TOP mit Vorlage: Ergebnis, Beschlussnummer, Ö/NÖ oder Absetzung geändert."""
    if kwargs.get("raw") or not _changed(instance, AGENDA_ITEM_FIELDS, created):
        return
    from apps.session.models import SessionConsultation

    paper_ids = {instance.paper_id}
    paper_ids.update(SessionConsultation.objects.filter(agenda_item=instance).values_list("paper_id", flat=True))
    _schedule_for_papers(paper_ids)


def agenda_item_post_delete(sender, instance, **kwargs):
    _schedule_for_papers([instance.paper_id])


def meeting_post_save(sender, instance, created, **kwargs):
    """Sitzung verlegt, abgesagt oder Ö/NÖ geändert: alle Stationen und TOPs dieser Sitzung."""
    if created or kwargs.get("raw") or not _changed(instance, MEETING_FIELDS):
        return
    _schedule_for_papers(_meeting_paper_ids(instance))


def meeting_pre_delete(sender, instance, **kwargs):
    _schedule_for_papers(_meeting_paper_ids(instance))


def _meeting_paper_ids(meeting) -> set:
    from apps.session.models import SessionAgendaItem, SessionConsultation

    paper_ids = set(SessionConsultation.objects.filter(meeting=meeting).values_list("paper_id", flat=True))
    paper_ids.update(
        SessionAgendaItem.objects.filter(meeting=meeting, paper__isnull=False).values_list("paper_id", flat=True)
    )
    return paper_ids


def register():
    from apps.session.models import (
        SessionAgendaItem,
        SessionApplication,
        SessionConsultation,
        SessionMeeting,
        SessionPaper,
    )
    from apps.tenants.models import Membership

    post_delete.connect(membership_post_delete, sender=Membership, dispatch_uid="motions_membership_shares_cleanup")
    pre_save.connect(application_pre_save, sender=SessionApplication, dispatch_uid="motions_ris_application_pre")
    post_save.connect(application_post_save, sender=SessionApplication, dispatch_uid="motions_ris_application_post")
    post_save.connect(paper_post_save, sender=SessionPaper, dispatch_uid="motions_ris_paper_post")
    post_delete.connect(paper_post_delete, sender=SessionPaper, dispatch_uid="motions_ris_paper_delete")
    post_save.connect(consultation_post_save, sender=SessionConsultation, dispatch_uid="motions_ris_consultation_post")
    post_delete.connect(
        consultation_post_delete, sender=SessionConsultation, dispatch_uid="motions_ris_consultation_delete"
    )
    post_save.connect(agenda_item_post_save, sender=SessionAgendaItem, dispatch_uid="motions_ris_agenda_item_post")
    post_delete.connect(
        agenda_item_post_delete, sender=SessionAgendaItem, dispatch_uid="motions_ris_agenda_item_delete"
    )
    post_save.connect(meeting_post_save, sender=SessionMeeting, dispatch_uid="motions_ris_meeting_post")
    pre_delete.connect(meeting_pre_delete, sender=SessionMeeting, dispatch_uid="motions_ris_meeting_delete")
