# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Postausgang und Versandauftrag (Issue #528, ``docs/adr/20260929-auftraege-und-zeitplaene.md``).

``put`` legt die Mail verschlüsselt in ``common_mail_outbox`` und reiht den Auftrag ``deliver_mail``
in der Warteschlange ``mail`` ein – in derselben Transaktion wie die fachliche Änderung: Wird sie
zurückgerollt, gibt es weder Zeile noch Auftrag noch Mail. Der Auftrag bekommt nur die Kennung der
Zeile.

**Wiederholung:** Scheitert der Versand, wirft der Auftrag; der Runner wiederholt ihn mit wachsender
Wartezeit (Standard acht Versuche). Endgültig ist ein Fehler nur, wenn der Server dauerhaft ablehnt –
alle Empfänger mit 5xx, Absender oder Nachricht mit 5xx außer Anmeldung – oder der Inhalt nicht mehr
lesbar ist. Vorübergehende Antworten (4xx, etwa 421 oder Greylisting mit 451) werden wiederholt. Nach
einem endgültigen Fehler und nach dem letzten Versuch steht die Zeile auf „fehlgeschlagen“, ihr Inhalt
wird gelöscht. Die Ausnahme des Auftrags nennt nur Fehlerklasse und SMTP-Codes (Protokoll des Runners
ohne Empfängeradressen).

**Verwaiste Zeilen:** Stirbt der Auftrag, ohne ``deliver_mail`` auszuführen (etwa ein älterer Worker, der
den Auftragstyp nicht kennt), bleibt die Zeile „wartend“. ``manage.py postausgang`` zeigt solche Zeilen,
``--einreihen`` reiht sie neu ein, ``--verwerfen`` löscht ihren Inhalt.

**Schlüssel:** Mails einer Organisation versiegelt ihr Mandantenschlüssel, alle übrigen der
Hauptschlüssel – auch die des Sitzungsdienstes: Die Plattform kennt die Fachmodule nicht und damit
keinen Session-Mandanten, und der Inhalt liegt nur bis zum Versand im Postausgang.

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
        _einreihen(row)
    MAILS.labels(kind=kind, route="postausgang", result="queued").inc()
    return row, True


def _einreihen(row: Any, backend: Any = None) -> None:
    """Versandauftrag für die Zeile einreihen und seine Kennung an der Zeile vermerken."""
    from apps.common.models import MailOutbox

    if backend is None:
        ergebnis = deliver_mail.enqueue(str(row.pk))
    else:
        ergebnis = backend.enqueue(deliver_mail, [str(row.pk)], {})
    row.task_id = str(ergebnis.id)
    MailOutbox.objects.filter(pk=row.pk).update(task_id=row.task_id)


def _dauerhaft(code: object) -> bool:
    try:
        return 500 <= int(str(code)) < 600
    except (TypeError, ValueError):
        return False


def _permanent(exc: BaseException) -> bool:
    """Lehnt der Server dauerhaft ab? Alle Empfänger mit 5xx bzw. Absender/Nachricht mit 5xx außer Anmeldung.

    4xx (vorübergehend: 421 Dienst nicht verfügbar, 450/451 Greylisting, Ratenbegrenzung) wird wiederholt.
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [antwort[0] for antwort in exc.recipients.values()]
        return bool(codes) and all(_dauerhaft(code) for code in codes)
    if isinstance(exc, smtplib.SMTPResponseException) and not isinstance(exc, smtplib.SMTPAuthenticationError):
        return _dauerhaft(exc.smtp_code)
    return False


class MailVersandError(Exception):
    """Versand gescheitert; die Meldung nennt nur Fehlerklasse und SMTP-Codes (Protokoll des Runners)."""


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
    fehler: BaseException | None = None
    try:
        genutzt = delivery.deliver(mail, route, kind=row.kind)
    except Exception as exc:  # noqa: BLE001 – ohne Stacktrace weiter (SMTP-Ausnahmen nennen Empfänger)
        fehler = exc
    if fehler is None:
        _abschliessen(row.pk, MailOutbox.Status.VERSENDET, attempt=attempt, route=genutzt)
        return genutzt

    code = delivery.fehlercode(fehler)
    dauerhaft = _permanent(fehler)
    if dauerhaft or attempt >= _max_attempts():
        _abschliessen(row.pk, MailOutbox.Status.FEHLGESCHLAGEN, attempt=attempt, route=route.name, error_code=code)
        logger.error("Postausgang: Mail %s (%s) endgültig nicht versendet (%s)", row.pk, row.kind, code)
        if dauerhaft:
            raise PermanentTaskError(code) from None
    else:
        MailOutbox.objects.filter(pk=row.pk).update(attempts=attempt, error_code=code)
    raise MailVersandError(code) from None


def verwaist(aelter_als: timedelta = timedelta(minutes=15), now: datetime | None = None) -> Any:
    """Wartende Zeilen ohne wartenden oder laufenden Auftrag (älter als ``aelter_als``)."""
    from apps.common.models import MailOutbox
    from apps.events.models import Task as TaskRow
    from apps.events.models import TaskStatus

    lebend = TaskRow.objects.filter(status__in=(TaskStatus.WARTEND, TaskStatus.LAEUFT)).values_list("pk", flat=True)
    lebende = {str(pk) for pk in lebend.filter(task_path=deliver_mail.module_path)}
    kandidaten = MailOutbox.objects.filter(
        status=MailOutbox.Status.WARTEND, created_at__lt=(now or timezone.now()) - aelter_als
    )
    return [row for row in kandidaten.order_by("created_at") if not row.task_id or row.task_id not in lebende]


def neu_einreihen(rows: list[Any]) -> int:
    """Verwaiste Zeilen neu einreihen (immer in ``events_task``, auch ohne umgestelltes Backend)."""
    from apps.events.tasks_backend import journal_backend

    backend = journal_backend()
    for row in rows:
        with transaction.atomic():
            _einreihen(row, backend)
    return len(rows)


def verwerfen(rows: list[Any]) -> int:
    """Verwaiste Zeilen aufgeben: „fehlgeschlagen“, Inhalt gelöscht."""
    from apps.common.models import MailOutbox

    for row in rows:
        _abschliessen(row.pk, MailOutbox.Status.FEHLGESCHLAGEN, attempt=row.attempts, error_code="verworfen")
    return len(rows)


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
