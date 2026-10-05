# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Postausgang und Versandauftrag (Issue #528, ``docs/adr/20260929-auftraege-und-zeitplaene.md``).

``put`` legt die Mail verschlüsselt in ``common_mail_outbox`` und reiht den Auftrag ``deliver_mail``
in der Warteschlange ``mail`` ein – in derselben Transaktion wie die fachliche Änderung: Wird sie
zurückgerollt, gibt es weder Zeile noch Auftrag noch Mail. Der Auftrag bekommt nur die Kennung der
Zeile.

**Wiederholung:** Scheitert der Versand, wirft der Auftrag; der Runner wiederholt ihn mit wachsender
Wartezeit (Standard acht Versuche). Endgültig ist ein Fehler, wenn der Server alle Empfänger oder die
Nachricht dauerhaft ablehnt (5xx außer Anmeldung) oder der Inhalt nicht mehr lesbar ist; dann und nach
dem letzten Versuch steht die Zeile auf „fehlgeschlagen“, und ihr Inhalt wird gelöscht.

**Idempotenz:** Ein Idempotenzschlüssel (z. B. je Ereignis und Empfänger) legt dieselbe Mail nur einmal
an. Der Auftrag versendet nur Zeilen im Zustand „wartend“ und setzt sie danach auf „versendet“; eine
zweite Ausführung desselben Auftrags versendet nichts. Bricht der Worker zwischen Versand und Vermerk
ab, kann eine Mail doppelt ankommen (Zustellung mindestens einmal).

**Aufbewahrung:** versendete Zeilen 14 Tage, fehlgeschlagene 90 Tage, jeweils ohne Inhalt (``purge``,
täglicher Zeitplan in ``apps/common/schedules.py``).
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from typing import Any, Final

from django.db import IntegrityError, transaction
from django.tasks import TaskContext, task
from django.utils import timezone

from apps.common.metrics import MAILS

from . import config, delivery
from .message import Mail

logger = logging.getLogger("apps.common.mail")

#: Aufbewahrung der Zeilen (ohne Inhalt)
KEEP_SENT: Final = timedelta(days=14)
KEEP_FAILED: Final = timedelta(days=90)
#: Zeilen je Löschschritt
PURGE_BATCH: Final = 5000


def _seal(organization: Any, data: bytes) -> dict[str, bytes | None]:
    """Inhalt verschlüsseln: mit dem Mandantenschlüssel der Organisation, sonst mit dem Hauptschlüssel."""
    from apps.common.encryption import TenantEncryption, encrypt_key

    if organization is not None:
        return {
            "payload_encrypted": TenantEncryption(organization).encrypt_bytes(data),
            "payload_platform_encrypted": None,
        }
    return {"payload_encrypted": None, "payload_platform_encrypted": encrypt_key(data)}


def _open(row: Any) -> Mail:
    from apps.common.encryption import TenantEncryption, decrypt_key

    if row.organization_id is not None:
        if not row.payload_encrypted:
            raise ValueError("Mail ohne Inhalt")
        return Mail.from_bytes(TenantEncryption(row.organization).decrypt_bytes(row.payload_encrypted))
    if not row.payload_platform_encrypted:
        raise ValueError("Mail ohne Inhalt")
    return Mail.from_bytes(decrypt_key(bytes(row.payload_platform_encrypted)))


def put(
    mail: Mail,
    *,
    kind: str,
    organization: Any = None,
    via_organization: bool = True,
    idempotency_key: str | None = None,
) -> tuple[Any, bool]:
    """Mail in den Postausgang legen und den Versand einreihen; liefert (Zeile, neu angelegt).

    Gibt es den Idempotenzschlüssel schon, bleibt es bei der vorhandenen Zeile (kein zweiter Auftrag).
    """
    from apps.common.models import MailOutbox

    with transaction.atomic():
        if idempotency_key:
            vorhanden = MailOutbox.objects.filter(idempotency_key=idempotency_key).first()
            if vorhanden is not None:
                return vorhanden, False
        row = MailOutbox(
            kind=kind,
            organization=organization,
            via_organization=via_organization,
            idempotency_key=idempotency_key or None,
            **_seal(organization, mail.to_bytes()),
        )
        try:
            with transaction.atomic():
                row.save(force_insert=True)
        except IntegrityError:
            # Gleichzeitig mit demselben Schlüssel angelegt: Es bleibt bei der anderen Zeile
            vorhanden = MailOutbox.objects.filter(idempotency_key=idempotency_key).first() if idempotency_key else None
            if vorhanden is None:
                raise
            return vorhanden, False
        deliver_mail.enqueue(str(row.pk))
    MAILS.labels(kind=kind, route="postausgang", result="queued").inc()
    return row, True


def _permanent(exc: BaseException) -> bool:
    """Lehnt der Server dauerhaft ab (alle Empfänger, Absender oder Nachricht, 5xx außer Anmeldung)?"""
    ursache: BaseException | None = exc
    while ursache is not None:
        if isinstance(ursache, smtplib.SMTPRecipientsRefused):
            return True
        if isinstance(ursache, smtplib.SMTPResponseException) and not isinstance(
            ursache, smtplib.SMTPAuthenticationError
        ):
            return 500 <= int(ursache.smtp_code) < 600
        ursache = ursache.__cause__
    return False


def _max_attempts() -> int:
    from apps.events.tasks_backend import journal_options

    optionen, _ = journal_options()
    return optionen.max_attempts_for(deliver_mail.module_path)


def _abschliessen(row_id: Any, status: str, *, attempt: int, route: str = "", error_code: str = "") -> None:
    from apps.common.models import MailOutbox

    MailOutbox.objects.filter(pk=row_id, status=MailOutbox.Status.WARTEND).update(
        status=status,
        attempts=attempt,
        route=route,
        error_code=error_code,
        payload_encrypted=None,
        payload_platform_encrypted=None,
        finished_at=timezone.now(),
    )


@task(queue_name="mail", takes_context=True)
def deliver_mail(context: TaskContext[Any, Any], outbox_id: str) -> str:
    """Versendet die Mail aus dem Postausgang (Zeile ``outbox_id``); liefert den genutzten Weg."""
    from apps.common.models import MailOutbox
    from apps.events.tasks_backend import PermanentTaskError

    row = MailOutbox.objects.select_related("organization").filter(pk=outbox_id).first()
    if row is None:
        logger.warning("Postausgang: Mail %s gibt es nicht mehr", outbox_id)
        return "fehlt"
    if row.status != MailOutbox.Status.WARTEND:
        return str(row.status)  # schon erledigt: eine Wiederholung versendet nichts

    attempt = int(context.attempt)
    try:
        mail = _open(row)
    except Exception as exc:  # noqa: BLE001 – unlesbar bleibt unlesbar, Wiederholen hilft nicht
        logger.error("Postausgang: Mail %s (%s) ist nicht lesbar (%s)", row.pk, row.kind, type(exc).__name__)
        _abschliessen(row.pk, MailOutbox.Status.FEHLGESCHLAGEN, attempt=attempt, error_code=type(exc).__name__)
        MAILS.labels(kind=row.kind, route="postausgang", result="expired").inc()
        raise PermanentTaskError("Mail im Postausgang nicht lesbar") from None

    route = config.resolve(row.organization, via_organization=row.via_organization)
    try:
        genutzt = delivery.deliver(mail, route, kind=row.kind)
    except Exception as exc:
        code = type(exc).__name__
        if _permanent(exc) or attempt >= _max_attempts():
            _abschliessen(row.pk, MailOutbox.Status.FEHLGESCHLAGEN, attempt=attempt, route=route.name, error_code=code)
            logger.error("Postausgang: Mail %s (%s) endgültig nicht versendet (%s)", row.pk, row.kind, code)
            if _permanent(exc):
                raise PermanentTaskError(code) from exc
        else:
            MailOutbox.objects.filter(pk=row.pk).update(attempts=attempt, error_code=code)
        raise
    _abschliessen(row.pk, MailOutbox.Status.VERSENDET, attempt=attempt, route=genutzt)
    return genutzt


def purge(now: datetime | None = None, batch: int = PURGE_BATCH) -> int:
    """Löscht Zeilen nach ihrer Frist; liefert ihre Anzahl.

    Versendete nach 14 Tagen, fehlgeschlagene nach 90 Tagen. Eine Zeile, die nach 90 Tagen noch wartet
    (ihr Auftrag ist längst verfallen), verfällt ebenfalls.
    """
    from apps.common.models import MailOutbox

    jetzt = now or timezone.now()
    regeln = (
        {"status": MailOutbox.Status.VERSENDET, "finished_at__lt": jetzt - KEEP_SENT},
        {"status": MailOutbox.Status.FEHLGESCHLAGEN, "finished_at__lt": jetzt - KEEP_FAILED},
        {"status": MailOutbox.Status.WARTEND, "created_at__lt": jetzt - KEEP_FAILED},
    )
    geloescht = 0
    for regel in regeln:
        while True:
            schritt = list(MailOutbox.objects.filter(**regel).values_list("pk", flat=True)[:batch])
            if not schritt:
                break
            geloescht += MailOutbox.objects.filter(pk__in=schritt).delete()[0]
    return geloescht
