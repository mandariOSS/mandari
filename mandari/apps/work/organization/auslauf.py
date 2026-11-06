# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitgliedschaft „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237, Entscheidungen Sven vom 05.11.2026).

- **Setzen** (:func:`schedule_member_deletion`): Rechte wie Deaktivieren und Entfernen (``members.remove`` in der
  View, nie Eigentümer:in, nie sich selbst, Administratoren nur durch Administratoren). Die Mitgliedschaft wird
  inaktiv (``is_active=False``) und bekommt ein Löschdatum; damit greifen alle Filter auf aktive Mitglieder:
  keine Inhalte, keine Einladungen und Benachrichtigungen, kein Platz im Kontingent. In der Organisation sieht die
  Person nur die Auslauf-Seite mit Löschdatum, dem Export ihrer eigenen Daten und den Anhängen, die am Löschtag
  entfallen (``OrganizationMixin``).
- **Zurücknehmen** (:func:`cancel_member_deletion`) bis zur Löschung: Die Mitgliedschaft ist danach deaktiviert.
  Reaktivieren (``services._restore_membership``) und Deaktivieren leeren das Löschdatum ebenfalls. Alle Wege
  sperren die Zeile und prüfen den Zustand danach erneut (der Lauf kann gleichzeitig löschen).
- **Nachweis** (:func:`scheduling_proven`): Erinnert und gelöscht wird nur, wenn der jüngste Zugangswechsel in der
  Änderungshistorie genau das Setzen mit diesem Löschdatum ist. Sonst hebt der Lauf das Datum auf und vermerkt
  das (Rückfall auf ein älteres Image, das die Spalten nicht kennt; im Django-Admin gesetztes Datum).
- **Erinnerungen** 7 und 3 Tage vor der Löschung, je höchstens einmal (``deletion_reminder_days`` wird vor dem
  Versand bedingt gesetzt; ein verspäteter Lauf schickt nur die jüngste fällige Erinnerung).
- **Löschtag** (:func:`delete_expired_membership`): Regeln wie „Mitglied endgültig entfernen“ (``member_data``:
  Organisationsinhalte bleiben als „Ehemaliges Mitglied“, Namen in Protokollen und Anwesenheit bleiben, persönliche
  Daten entfallen, persönliche Aufgaben samt Dateien ihrer Anhänge), zusätzlich private Dokumente und Entwürfe der
  Person (:func:`private_documents_and_drafts`) samt Versionen, Anhängen und Dateien sowie das Verzeichnis ihrer
  Datenexporte. Eigentümer:innen der Organisation werden nicht gelöscht (Datum aufgehoben, vermerkt). Ohne weitere
  Bindung wird auch das Konto gelöscht (``apps.accounts.orphaned_accounts.loesche_konto_nach_austritt``). Die
  Bestätigung geht erst nach erfolgreicher Löschung raus, an die vorher gesicherte Adresse.

Jede Änderung steht in der Änderungshistorie (``audit.log_member_access``). Der Lauf
:func:`run_membership_expiry_pass` läuft als Zeitplan ``mitgliedschaften_auslaufen`` (``apps/work/schedules.py``)
mit Cache-Sperre gegen parallele Läufe.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import formats, timezone

from apps.tenants.models import Membership, Organization
from apps.work.files import DATA_EXPORTS, remove_files_on_commit

from . import audit, emails
from .services import (
    ServiceError,
    _access_text,
    _active_membership,
    _clear_deletion,
    _discard_open_invitations,
    _ensure_may_remove,
    _lock_membership,
)

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.work.motions.models import Motion, MotionDocument
    from apps.work.tasks.models import TaskAttachment

logger = logging.getLogger(__name__)

#: Frist vom Setzen bis zur Löschung
AUSLAUF_TAGE = 30
#: Erinnerungen vor der Löschung (Tage), jüngste zuerst geprüft
ERINNERUNG_TAGE = (3, 7)
#: So lange halten die Sicherungen gelöschte Daten höchstens (Hinweis in der Bestätigung)
SICHERUNG_TAGE = 30

LOCK_KEY = "work:membership_expiry:lock"
LOCK_TIMEOUT = 30 * 60

#: Gründe, aus denen der Lauf eine geplante Löschung aufhebt statt zu löschen (Änderungshistorie und Log)
OHNE_NACHWEIS = "Auslauf nicht nachweisbar (jüngster Zugangswechsel in der Änderungshistorie ist ein anderer)"
EIGENTUEMER = "Eigentümer:in der Organisation; zuerst die Eigentümerschaft übertragen"


def deletion_date_text(member: Membership) -> str:
    """Löschdatum als TT.MM.JJJJ in Ortszeit."""
    if member.deletion_scheduled_at is None:
        return ""
    return str(formats.date_format(timezone.localtime(member.deletion_scheduled_at), "d.m.Y"))


def access_text(member: Membership) -> str:
    """Zugang für die Änderungshistorie im Auslauf, z. B. „Nur Lesen, Löschung am 05.12.2026“."""
    return f"Nur Lesen, Löschung am {deletion_date_text(member)}"


def scheduling_proven(member: Membership) -> bool:
    """
    Wurde der Auslauf mit diesem Löschdatum nachweislich gesetzt? Ja, wenn der jüngste Zugangswechsel der
    Mitgliedschaft in der Änderungshistorie genau dieses Setzen ist (:func:`schedule_member_deletion` setzt nur mit
    Eintrag).

    Ein älteres Image kennt die Spalten nicht: Reaktiviert und deaktiviert es die Person nach einem Rückfall, bleibt
    das alte Löschdatum stehen, die Historie hat danach aber einen neueren Zugangswechsel. Ein im Django-Admin
    eingetragenes Datum hat gar keinen passenden Eintrag.
    """
    return member.deletion_pending and audit.last_access(member) == access_text(member)


# ---------------------------------------------------------------------------
# Setzen und Zurücknehmen
# ---------------------------------------------------------------------------


def schedule_member_deletion(organization: Organization, member: Membership, actor_user: User) -> bool:
    """
    Mitgliedschaft auf „Nur Lesen, Löschung nach 30 Tagen“ setzen und die Person per Mail informieren.

    Liefert, ob die Mail rausging. Fachliche Hindernisse als ``ServiceError``.
    """
    with transaction.atomic():
        _lock_membership(member)
        if member.is_guest:
            raise ServiceError(
                "Gastzugänge lassen sich nicht auf „Nur Lesen“ setzen. Entziehen Sie den Zugang stattdessen."
            )
        if member.registration_requested_at is not None and not member.is_active:
            raise ServiceError("Offene Registrierungsanfragen bitte ablehnen statt auf „Nur Lesen“ setzen.")
        if member.deletion_pending:
            raise ServiceError(f"Die Löschung ist bereits für den {deletion_date_text(member)} geplant.")
        if member.user == organization.owner:
            raise ServiceError("Der Eigentümer kann nicht auf „Nur Lesen“ gesetzt werden.")
        if member.user == actor_user:
            raise ServiceError("Sie können sich nicht selbst auf „Nur Lesen“ setzen.")
        _ensure_may_remove(organization, member, actor_user)
        before = _access_text(member)
        member.is_active = False
        member.deletion_scheduled_at = timezone.now() + timedelta(days=AUSLAUF_TAGE)
        member.deletion_reminder_days = None
        member.save(update_fields=["is_active", "deletion_scheduled_at", "deletion_reminder_days", "updated_at"])
        _discard_open_invitations(organization, member.user)
        # Der Eintrag ist der Nachweis für den Lauf (scheduling_proven): ohne ihn keine geplante Löschung
        entry = audit.log_member_access(
            member, before, access_text(member), actor=_active_membership(organization, actor_user)
        )
        if entry is None:
            raise ServiceError(
                "Die Änderungshistorie ließ sich nicht schreiben, die Löschung ist deshalb nicht geplant. "
                "Bitte versuchen Sie es später erneut."
            )
    return emails.send_deletion_scheduled(member, deletion_date_text(member))


@transaction.atomic
def cancel_member_deletion(organization: Organization, member: Membership, actor_user: User) -> None:
    """Geplante Löschung zurücknehmen; die Mitgliedschaft bleibt deaktiviert."""
    _lock_membership(member)
    if not member.deletion_pending:
        raise ServiceError("Für dieses Mitglied ist keine Löschung geplant.")
    _ensure_may_remove(organization, member, actor_user)
    before = access_text(member)
    _clear_deletion(member)
    member.save(update_fields=["deletion_scheduled_at", "deletion_reminder_days", "updated_at"])
    audit.log_member_access(
        member,
        before,
        "deaktiviert",
        actor=_active_membership(organization, actor_user),
        reason="Löschung zurückgenommen",
    )


def _release(member: Membership, reason: str) -> None:
    """
    Geplante Löschung ohne Löschen aufheben (die Mitgliedschaft bleibt deaktiviert), mit Log und Eintrag in der
    Änderungshistorie. Die Zeile ist gesperrt.
    """
    before = access_text(member)
    _clear_deletion(member)
    member.save(update_fields=["deletion_scheduled_at", "deletion_reminder_days", "updated_at"])
    audit.log_member_access(member, before, "deaktiviert", reason=f"Löschung entfällt: {reason}")
    logger.warning("Löschung der Mitgliedschaft %s entfällt: %s", member.pk, reason)


# ---------------------------------------------------------------------------
# Private Dokumente und Entwürfe, persönliche Anhänge
# ---------------------------------------------------------------------------


def _guest_folder_q() -> Q:
    """Dokument liegt in einem Ordner, der (direkt oder über einen übergeordneten) für Gäste freigegeben ist."""
    from apps.work.motions.models import DocumentFolder

    condition = Q()
    path = "folder__"
    for _ in range(DocumentFolder.MAX_DEPTH):
        condition |= Q(**{f"{path}guest_shares__isnull": False})
        path = f"{path}parent__"
    return condition


def private_documents_and_drafts(membership: Membership) -> QuerySet[Motion]:
    """
    Private Dokumente und Entwürfe der Person, an denen sonst niemand beteiligt ist – sie entfallen am Löschtag.

    Abgrenzung wie beim Recht „Dokumente ehemaliger Mitglieder einsehen“ (Issue #590): private Dokumente (jeder
    Status) und Entwürfe, die sonst nur die Person selbst sah. Geteilte Dokumente (Sichtbarkeit „Geteilt“) bleiben,
    ebenso Dokumente, die andere erreicht haben: Federführung oder Mitarbeit anderer, Freigaben jeder Art,
    Freigabe-Anfragen, Kommentare anderer, Versionen anderer, Änderungsanträge anderer, Verknüpfung mit einer
    Fraktionssitzung, Ablage in „Nichtöffentliche Vorgänge“ (dort für alle Vereidigten sichtbar) oder in einem für
    Gäste freigegebenen Ordner. Eingereichte Dokumente bleiben immer (Antrag in Session, Einreichungsdatum,
    Rückmeldung oder Stand der Verwaltung, Einreichung per E-Mail).
    """
    from apps.work.motions.models import (
        Motion,
        MotionAdministrationEvent,
        MotionApproval,
        MotionComment,
        MotionEmailSubmission,
        MotionRevision,
        MotionShare,
        sworn_in_only_q,
    )

    contributors = Motion.contributors.through.objects.filter(motion_id=OuterRef("pk")).exclude(
        membership_id=membership.pk
    )
    return (
        Motion.objects.filter(organization=membership.organization, author=membership)
        .filter(Q(visibility="private") | Q(status="draft"))
        .exclude(visibility="shared")
        # Eingereicht: Die Verwaltung hat das Dokument erreicht
        .filter(session_application__isnull=True, submitted_at__isnull=True, administration_status="")
        .exclude(Exists(MotionAdministrationEvent.objects.filter(motion_id=OuterRef("pk"))))
        .exclude(Exists(MotionEmailSubmission.objects.filter(motion_id=OuterRef("pk"))))
        .exclude(Q(responsible__isnull=False) & ~Q(responsible=membership))
        .exclude(Exists(contributors))
        .exclude(Exists(MotionShare.objects.filter(motion_id=OuterRef("pk"))))
        .exclude(Exists(MotionApproval.objects.filter(motion_id=OuterRef("pk"))))
        .exclude(Exists(MotionComment.objects.filter(motion_id=OuterRef("pk")).exclude(author=membership)))
        .exclude(Exists(MotionRevision.objects.filter(motion_id=OuterRef("pk")).exclude(changed_by=membership)))
        .exclude(Exists(Motion.objects.filter(parent_motion_id=OuterRef("pk")).exclude(author=membership)))
        .exclude(linked_agenda_items__isnull=False)
        .exclude(sworn_in_only_q())
        .exclude(_guest_folder_q())
        .distinct()
    )


def document_attachments(membership: Membership) -> QuerySet[MotionDocument]:
    """Anhänge der privaten Dokumente und Entwürfe, die am Löschtag entfallen (Auslauf-Seite, Download)."""
    from apps.work.motions.models import MotionDocument

    ids = private_documents_and_drafts(membership).values("pk")
    return MotionDocument.objects.filter(motion_id__in=ids).select_related("motion").order_by("filename")


def task_attachments(membership: Membership) -> QuerySet[TaskAttachment]:
    """Anhänge der persönlichen Aufgaben, die am Löschtag entfallen (Auslauf-Seite, Download)."""
    from apps.work.tasks.models import TaskAttachment
    from apps.work.tasks.selectors import personal_tasks

    ids = personal_tasks(membership).values("pk")
    return TaskAttachment.objects.filter(task_id__in=ids).select_related("task").order_by("filename")


@dataclass(frozen=True)
class DeletedAttachment:
    """Anhang, der am Löschtag entfällt, für die Liste auf der Auslauf-Seite."""

    kind: str
    attachment: MotionDocument | TaskAttachment
    source: str


def deleted_attachments(membership: Membership) -> list[DeletedAttachment]:
    """Alle Anhänge, die am Löschtag entfallen: erst die der Dokumente, dann die der persönlichen Aufgaben."""
    documents = [DeletedAttachment("dokument", item, item.motion.title) for item in document_attachments(membership)]
    tasks = [DeletedAttachment("aufgabe", item, f"Aufgabe: {item.task.title}") for item in task_attachments(membership)]
    return [*documents, *tasks]


def deleted_attachment(membership: Membership, kind: str, attachment_id: Any) -> MotionDocument | TaskAttachment | None:
    """
    Einen der Anhänge, die am Löschtag entfallen (``kind`` ``dokument`` oder ``aufgabe``), sonst ``None`` – nur aus
    genau dieser Auswahl der eigenen Mitgliedschaft (Download auf der Auslauf-Seite).
    """
    if kind == "dokument":
        return document_attachments(membership).filter(pk=attachment_id).first()
    if kind == "aufgabe":
        return task_attachments(membership).filter(pk=attachment_id).first()
    return None


def _delete_documents(membership: Membership) -> int:
    """Private Dokumente und Entwürfe löschen, die Dateien ihrer Anhänge nach der Transaktion; liefert die Anzahl."""
    from apps.work.motions.models import Motion

    ids = list(private_documents_and_drafts(membership).values_list("pk", flat=True))
    files = [document.file for document in document_attachments(membership)]
    Motion.objects.filter(pk__in=ids).delete()
    remove_files_on_commit(files)
    return len(ids)


def _remove_exports_after_commit(membership: Membership) -> None:
    """
    Dateien der Datenexporte nach erfolgreicher Transaktion entfernen: die eingetragenen und das ganze Verzeichnis
    der Mitgliedschaft (``exports/<organisation>/<mitgliedschaft>/``), also auch die Datei eines Exports, der während
    der Löschung fertig wurde und keinen Eintrag mehr hat.
    """
    paths = [path for export in membership.data_exports.all() if (path := cast(Any, export).get_absolute_path())]
    directory = Path(settings.MEDIA_ROOT) / DATA_EXPORTS / str(membership.organization_id) / str(membership.pk)

    def remove() -> None:
        try:
            for path in paths:
                path.unlink(missing_ok=True)
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            logger.warning("Datenexporte einer gelöschten Mitgliedschaft konnten nicht entfernt werden")

    transaction.on_commit(remove)


# ---------------------------------------------------------------------------
# Löschtag
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeletionResult:
    """Was am Löschtag gelöscht wurde (für Bestätigung und Statistik)."""

    documents: int
    account_deleted: bool
    account_bindings: tuple[str, ...]


def delete_expired_membership(membership_id: Any, now: datetime) -> DeletionResult | None:
    """
    Fällige Mitgliedschaft im Auslauf löschen und die Person benachrichtigen; ``None``, wenn sie nicht (mehr)
    fällig ist (zurückgenommen, reaktiviert oder schon gelöscht) oder nicht gelöscht werden darf (ohne Nachweis,
    Eigentümer:in; dann ist das Datum aufgehoben und vermerkt).
    """
    with transaction.atomic():
        member = (
            Membership.objects.select_for_update()
            .filter(pk=membership_id, is_active=False, deletion_scheduled_at__lte=now)
            .first()
        )
        if member is None:
            return None
        organization = Organization.objects.get(pk=member.organization_id)
        if not scheduling_proven(member):
            _release(member, OHNE_NACHWEIS)
            return None
        if member.user_id == organization.owner_id:
            _release(member, EIGENTUEMER)
            return None
        user = member.user
        # Vorher sichern: Mitgliedschaft und Konto sind danach weg
        email, name = user.email, user.get_full_name()
        documents = _delete_documents(member)
        audit.log_member_access(
            member,
            access_text(member),
            "gelöscht",
            action="delete",
            roles_before=audit.role_names(member.roles.all()),
            reason=f"Löschung nach {AUSLAUF_TAGE} Tagen „Nur Lesen“; private Dokumente und Entwürfe: {documents}",
        )
        _remove_exports_after_commit(member)
        member.delete()
        _discard_open_invitations(organization, user)

    from apps.accounts.orphaned_accounts import loesche_konto_nach_austritt

    try:
        bindings = loesche_konto_nach_austritt(user)
    except Exception:  # noqa: BLE001 – die Mitgliedschaft ist gelöscht; das Konto prüft der nächste Austritt
        logger.exception("Konto nach Löschung der Mitgliedschaft %s nicht gelöscht", membership_id)
        bindings = ["fehler"]
    result = DeletionResult(documents=documents, account_deleted=not bindings, account_bindings=tuple(bindings))
    if bindings:
        logger.info("Mitgliedschaft %s gelöscht, Konto bleibt (Bindungen: %s)", membership_id, ", ".join(bindings))
    emails.send_deletion_done(
        organization,
        email=email,
        name=name,
        documents_deleted=documents,
        account_deleted=result.account_deleted,
        backup_days=SICHERUNG_TAGE,
    )
    return result


# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------


def _release_unproven() -> int:
    """
    Löschdaten ohne Nachweis (:func:`scheduling_proven`) aufheben, bevor erinnert oder gelöscht wird. Aktive
    Mitgliedschaften verlieren ein liegengebliebenes Datum still (sie sind nicht im Auslauf).
    """
    Membership.objects.filter(is_active=True).filter(
        Q(deletion_scheduled_at__isnull=False) | Q(deletion_reminder_days__isnull=False)
    ).update(deletion_scheduled_at=None, deletion_reminder_days=None)
    released = 0
    for member in Membership.objects.filter(is_active=False, deletion_scheduled_at__isnull=False):
        if scheduling_proven(member):
            continue
        with transaction.atomic():
            locked = (
                Membership.objects.select_for_update()
                .filter(pk=member.pk, is_active=False, deletion_scheduled_at=member.deletion_scheduled_at)
                .first()
            )
            if locked is None or scheduling_proven(locked):
                continue
            _release(locked, OHNE_NACHWEIS)
            released += 1
    return released


def _due_reminder(member: Membership, now: datetime) -> int | None:
    """Jüngste fällige Erinnerungsstufe, die noch nicht verschickt ist, sonst ``None``."""
    assert member.deletion_scheduled_at is not None
    for days in ERINNERUNG_TAGE:
        if now >= member.deletion_scheduled_at - timedelta(days=days):
            sent = member.deletion_reminder_days
            return days if sent is None or sent > days else None
    return None


def _claim_reminder(member: Membership, days: int) -> bool:
    """Stufe vor dem Versand bedingt vermerken: Von zwei Läufen verschickt nur einer (je Mail höchstens einmal)."""
    claimed = (
        Membership.objects.filter(pk=member.pk, is_active=False, deletion_scheduled_at=member.deletion_scheduled_at)
        .filter(Q(deletion_reminder_days__isnull=True) | Q(deletion_reminder_days__gt=days))
        .update(deletion_reminder_days=days)
    )
    return bool(claimed)


def _send_reminders(now: datetime) -> int:
    window = now + timedelta(days=max(ERINNERUNG_TAGE))
    sent = 0
    members = Membership.objects.filter(
        is_active=False, deletion_scheduled_at__gt=now, deletion_scheduled_at__lte=window
    ).select_related("user", "organization")
    for member in members:
        days = _due_reminder(member, now)
        if days is None or not _claim_reminder(member, days):
            continue
        assert member.deletion_scheduled_at is not None
        days_left = max(1, -(-(member.deletion_scheduled_at - now) // timedelta(days=1)))
        if emails.send_deletion_reminder(member, deletion_date_text(member), days_left):
            sent += 1
    return sent


def run_membership_expiry_pass(now: datetime | None = None) -> dict[str, Any]:
    """
    Löschdaten ohne Nachweis aufheben, Erinnerungen verschicken und fällige Mitgliedschaften im Auslauf löschen.

    Eine Cache-Sperre verhindert parallele Läufe; jede Löschung prüft unter Zeilensperre erneut, ob sie fällig und
    nachgewiesen ist. Ein Fehler bei einer Mitgliedschaft hält die übrigen nicht auf (der nächste Lauf versucht es
    erneut).
    """
    now = now or timezone.now()
    if not cache.add(LOCK_KEY, "1", timeout=LOCK_TIMEOUT):
        return {"skipped": "lock"}
    try:
        stats: dict[str, Any] = {"released": _release_unproven()}
        stats.update({"reminded": _send_reminders(now), "deleted": 0, "accounts_deleted": 0, "failed": 0})
        due = Membership.objects.filter(is_active=False, deletion_scheduled_at__lte=now).values_list("pk", flat=True)
        for membership_id in list(due):
            try:
                result = delete_expired_membership(membership_id, now)
            except Exception:  # noqa: BLE001 – eine fehlerhafte Mitgliedschaft hält die übrigen nicht auf
                logger.exception("Mitgliedschaft %s konnte nicht gelöscht werden", membership_id)
                stats["failed"] += 1
                continue
            if result is not None:
                stats["deleted"] += 1
                stats["accounts_deleted"] += int(result.account_deleted)
        if any(stats[key] for key in ("released", "reminded", "deleted", "failed")):
            logger.info("Auslauf von Mitgliedschaften: %s", stats)
        return stats
    finally:
        cache.delete(LOCK_KEY)
