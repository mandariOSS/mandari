# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle, die Session als Eigentümer ausführt (``docs/adr/20260929-befehle-synchron.md``, Issue #540).

``submission.submit`` ist der eine Einreichungsweg für Anträge. Work (``InProcessClient``), die Session-API v1
und der abgekündigte Pfad der alten Session-API senden denselben Befehl; geprüft wird nur hier:

1. **Mandant** aktiv, sonst 404.
2. **Auslöser** berechtigt, sonst 403:

   - ``system:session.api-token.<id>`` (``token_actor_ref``): API-Token dieses Mandanten, gültig und mit
     „Anträge einreichen“. Die einreichende Organisation ist die, die das Token in Work verbunden hat; eine
     abweichende Angabe im Inhalt wird abgelehnt.
   - ``user:<id>`` (Work): aktives Mitglied der einreichenden Organisation mit dem Recht
     ``motions.submit_to_ris``; die Organisation ist mit dem Mandanten verbunden, ihr Token gültig und
     darf einreichen.

3. **Inhalt** (422, ``errors`` mit JSON-Pointer): Den Vertrag hat der Dispatcher schon geprüft. Dazu kommen
   Pflichttexte, die nicht nur aus Leerraum bestehen, ein Zielgremium des Mandanten, das aktiv ist, und Anlagen,
   die vollständig vorliegen und zu Größe und SHA-256 im Inhalt passen.
4. Antrag anlegen (Eingangsnummer aus dem Nummernkreis), auf Wunsch die Antragsfassung als PDF (best effort),
   Anlagen prüfen (Dateityp, Größe, Virenprüfung) und nichtöffentlich ablegen. Scheitert die Ablage, rollt der
   ganze Befehl zurück (503, mit demselben Idempotenzschlüssel wiederholbar).

Die Quittung nennt Eingangsnummer, Eingangszeit (``submitted_at``) und Inhalts-Hash; ``data`` enthält den Status
und die nicht angenommenen Anlagen als Stelle im Inhalt und Code, nie mit Namen. Alle Meldungen sind feste Texte
ohne Inhalte, das Protokoll nennt nur Kennungen und Typen von Ausnahmen.

Seit dem Profil Einreichung (Issue #1228) führt Session drei weitere Befehle aus. Auslöser ist dort eine Person
(``user:<id>``, angemeldet über ein Access-Token mit dem Scope ``einreichung:schreiben``):

- ``submission.withdraw``: Die Person hat die Einreichung selbst eingereicht (``submitted_by_user``, aus Work oder
  über das Profil), und sie darf für deren Organisation weiter einreichen
  (``application_service.submission_originators``, dieselbe Regel wie beim Einreichen), sonst 404. Andere Mitglieder
  der Organisation nehmen fremde Einreichungen nicht zurück. Zurückgenommen wird nur in ``WITHDRAWABLE_STATUSES``,
  sonst 409. Ein API-Token (``system:session.api-token.<id>``) darf Einreichungen seiner Organisation zurücknehmen.
- ``attendance.respond``: Die Person des Kontos (``portal_link_service.person_for_user_in_tenant``, dieselben
  Bedingungen wie im Portal) ist die im Inhalt genannte und zu der Sitzung geladen, sonst 403 bzw. 404; die Sitzung
  nimmt noch Rückmeldungen an, sonst 409. Gespeichert wird wie im Portal (Herkunft „portal“): Rückmeldung, Grund
  verschlüsselt, Vertretungsanfrage, Empfangsbestätigung aller Versände der Sitzung.
- ``invitation.acknowledge``: Der Ladungsempfänger gehört zur Person des Kontos, sonst 404; zugestellt, sonst 409.
  Bestätigt werden wie im Portal alle zugestellten Versände der Sitzung an die Person.

Beide Rückmeldebefehle sind personenbezogen: Der Dispatcher bildet ihre Hashes mit einem Schlüssel der Installation.
Die Wege über Rückmeldelink, Portal und Sitzungsdienst nutzen die Befehle noch nicht (#541).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import PurePosixPath
from typing import Any, Final, cast

from django.core.files import File
from django.db import transaction

from hub.commands import Command, CommandError, HandlerResult, Problem, command_handler
from hub.commands.problems import validation_problem

from .models import (
    SessionAPIToken,
    SessionApplication,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionTenant,
)

logger = logging.getLogger(__name__)

SUBMIT: Final = "submission.submit"
WITHDRAW: Final = "submission.withdraw"
RESPOND: Final = "attendance.respond"
ACKNOWLEDGE: Final = "invitation.acknowledge"
#: Stände, in denen eine Einreichung zurückgenommen werden kann: solange sie nicht abgelehnt, zur Vorlage gemacht oder
#: schon zurückgenommen ist (Vertrag ``submission.withdraw``)
WITHDRAWABLE_STATUSES: Final = frozenset({"submitted", "received", "in_review", "accepted"})
WITHDRAWN: Final = "withdrawn"
#: Herkunft einer Rückmeldung oder Empfangsbestätigung, die eine angemeldete Person selbst abgibt
PORTAL: Final = "portal"
#: Versände, deren Empfang sich bestätigen lässt (zugestellt bzw. als Brief versandt)
DELIVERED: Final = ("sent", "letter_sent")
#: Auslöser einer Einreichung mit API-Token: ``system:session.api-token.<Kennung des Tokens>``
TOKEN_ACTOR_PREFIX: Final = "system:session.api-token."
USER_ACTOR_PREFIX: Final = "user:"
#: Recht in Work, Anträge an die Verwaltung zu übermitteln (``apps.common.permissions``)
SUBMIT_PERMISSION: Final = "motions.submit_to_ris"
#: Pflichttexte, die nicht nur aus Leerraum bestehen dürfen (der Vertrag prüft nur die Länge)
REQUIRED_TEXTS: Final = ("title", "resolution_proposal", "justification")
_CHUNK: Final = 1024 * 1024

#: Feste Meldungen (nie mit Werten aus dem Inhalt)
TENANT_UNKNOWN: Final = "Die Verwaltung ist nicht bekannt oder deaktiviert."
NOT_ALLOWED: Final = "Diese Anmeldung darf bei dieser Verwaltung keine Anträge einreichen."
NOT_CONNECTED: Final = "Die einreichende Organisation ist nicht mit dieser Verwaltung verbunden."
TOKEN_INVALID: Final = "Der Einreichungs-Token ist abgelaufen, zurückgezogen oder erlaubt keine Antragseinreichung."
INVALID: Final = "Die Einreichung ist unvollständig oder ungültig."
ATTACHMENTS_FAILED: Final = (
    "Die Anhänge konnten nicht an die Verwaltung übergeben werden. Der Antrag wurde nicht eingereicht – "
    "bitte später erneut versuchen."
)
SUBMISSION_UNKNOWN: Final = "Die Einreichung ist nicht bekannt."
NOT_WITHDRAWABLE: Final = "In diesem Bearbeitungsstand lässt sich die Einreichung nicht mehr zurücknehmen."
MEETING_UNKNOWN: Final = "Die Sitzung ist nicht bekannt, oder die Person ist zu ihr nicht geladen."
NOT_THIS_PERSON: Final = "Diese Anmeldung darf nur für die eigene Person zurückmelden."
RESPONSES_CLOSED: Final = "Die Sitzung hat begonnen oder ist abgesagt; Rückmeldungen sind nicht mehr möglich."
CONFIRM_WITH_SUBSTITUTE: Final = "Eine Zusage kann keine Vertretung und keinen Grund enthalten."
INVITATION_UNKNOWN: Final = "Die Ladung ist nicht bekannt."
NOT_DELIVERED: Final = "Die Ladung ist noch nicht zugestellt; ihr Empfang lässt sich noch nicht bestätigen."


def tenant_ref(tenant: SessionTenant) -> str:
    """Mandant eines Befehls an Session (wie in der Ereignishülle)."""
    return f"session:{tenant.pk}"


def token_actor_ref(token: SessionAPIToken) -> str:
    """Auslöser einer Einreichung mit API-Token."""
    return f"{TOKEN_ACTOR_PREFIX}{token.pk}"


@command_handler(SUBMIT)
def submit(command: Command) -> HandlerResult:
    """Antrag einreichen (siehe Moduldokumentation)."""
    from .services.application_service import attach_application_files

    body = command.body
    tenant = _tenant(command.tenant_ref)
    token, organization, via_work = _authorize(command, tenant)
    target, attachments = _check(command, tenant)
    # Auslöser ``user:<id>`` (Work, Profil Einreichung): Die Person gehört zur Einreichung (Sicht und Rücknahme)
    actor = command.actor_ref or ""
    submitted_by = _uuid(actor.removeprefix(USER_ACTOR_PREFIX)) if via_work else None

    submitter = body["submitter"]
    application = SessionApplication.objects.create(
        tenant=tenant,
        title=body["title"].strip(),
        application_type=body["application_type"],
        justification=body["justification"].strip(),
        resolution_proposal=body["resolution_proposal"].strip(),
        financial_impact=(body.get("financial_impact") or "").strip(),
        submitting_organization=organization,
        submitter_name=submitter["name"].strip(),
        submitter_email=submitter["email"].strip().lower(),
        submitter_phone=(submitter.get("phone") or "").strip(),
        co_signers=_co_signers(body),
        target_organization=target,
        is_urgent=bool(body.get("is_urgent")),
        urgency_reason=(body.get("urgency_reason") or "").strip(),
        deadline=date.fromisoformat(body["deadline"]) if body.get("deadline") else None,
        submitted_via_token=token,
        submitted_by_user_id=submitted_by,
        status="submitted",
    )

    if body.get("application_pdf"):
        _attach_application_pdf(application)

    try:
        rejected = attach_application_files(application, attachments)
    except Exception as exc:
        # Ablage gescheitert (Speicher, Datenbank): Der ganze Befehl rollt zurück, der Aufrufer wiederholt ihn
        # später mit demselben Schlüssel. Ins Log nur der Typ; Meldungen von Ausnahmen nennen Pfade und Werte.
        logger.warning("Einreichung %s: Anhänge nicht abgelegt (%s)", application.pk, type(exc).__qualname__)
        raise CommandError.of(503, "anlagen-nicht-abgelegt", ATTACHMENTS_FAILED) from None

    if via_work:
        # Die API-Wege zählen die Nutzung selbst, mit der IP-Adresse der Anfrage.
        token.record_usage()

    data: dict[str, Any] = {"status": application.status}
    if rejected:
        data["attachments_rejected"] = [{"index": index, "reason": reason} for index, reason in rejected]
    return HandlerResult(
        reference=application.reference,
        aggregate_id=application.pk,
        received_at=application.submitted_at,
        data=data,
    )


@command_handler(WITHDRAW)
def withdraw(command: Command) -> HandlerResult:
    """Einreichung zurücknehmen (siehe Moduldokumentation)."""
    body = command.body
    tenant = _tenant(command.tenant_ref)
    # Nur die Zeile des Antrags sperren: PostgreSQL sperrt keine Zeilen auf der leeren Seite eines äußeren Joins
    application = (
        SessionApplication.objects.select_for_update(of=("self",)).filter(pk=body["submission"], tenant=tenant).first()
    )
    if application is None or not _may_withdraw(command, tenant, application):
        raise CommandError.of(404, "nicht-gefunden", SUBMISSION_UNKNOWN)
    claimed = body.get("submitting_organization")
    if claimed and str(claimed) != str(application.submitting_organization_id):
        raise CommandError.of(404, "nicht-gefunden", SUBMISSION_UNKNOWN)
    if application.status not in WITHDRAWABLE_STATUSES:
        raise CommandError.of(409, "ruecknahme-nicht-zulaessig", NOT_WITHDRAWABLE)
    previous = application.status
    application.status = WITHDRAWN
    # save() statt update(): Das Protokoll des Mandanten hält den Wechsel des Stands fest (Signal)
    cast(Any, application).save(update_fields=["status", "updated_at"])  # save() des Modells ist untypisiert
    return HandlerResult(
        reference=application.reference or str(application.pk),
        aggregate_id=application.pk,
        data={"status": WITHDRAWN, "previous_status": previous},
    )


@command_handler(RESPOND)
def respond(command: Command) -> HandlerResult:
    """Zu- oder Absage einer geladenen Person (siehe Moduldokumentation)."""
    from .services import invitation_response_service

    body = command.body
    tenant = _tenant(command.tenant_ref)
    person = _acting_person(command, tenant)
    if str(person.pk) != str(body["person"]):
        raise CommandError.of(403, "keine-berechtigung", NOT_THIS_PERSON)
    meeting = SessionMeeting.objects.filter(pk=body["meeting"], tenant=tenant).first()
    if meeting is None or not _invited(meeting, person):
        raise CommandError.of(404, "nicht-gefunden", MEETING_UNKNOWN)
    if not invitation_response_service.is_open_for_responses(meeting):
        raise CommandError.of(409, "rueckmeldung-geschlossen", RESPONSES_CLOSED)
    confirmed = body["response"] == "confirmed"
    if confirmed and (body.get("substitute_requested") or (body.get("reason") or "").strip()):
        errors = validation_problem(("$.substitute_requested: nur bei einer Absage",)).errors
        raise CommandError(Problem(status=422, kind="validierung", detail=CONFIRM_WITH_SUBSTITUTE, errors=errors))
    result = invitation_response_service.record_response(
        meeting,
        person,
        decision="confirm" if confirmed else "decline",
        source=PORTAL,
        reason=str(body.get("reason") or ""),
        substitute_requested=bool(body.get("substitute_requested")),
    )
    invitation_response_service.acknowledge_meeting(person, meeting, via=PORTAL)
    attendance = result.attendance
    return HandlerResult(
        reference=str(attendance.pk),
        aggregate_id=attendance.pk,
        received_at=attendance.responded_at,
        data={"status": attendance.status},
    )


@command_handler(ACKNOWLEDGE)
def acknowledge(command: Command) -> HandlerResult:
    """Empfang einer Ladung bestätigen (siehe Moduldokumentation)."""
    from .services import invitation_response_service

    body = command.body
    tenant = _tenant(command.tenant_ref)
    person = _acting_person(command, tenant)
    recipient = (
        SessionInvitationRecipient.objects.filter(pk=body["recipient"], person=person, dispatch__meeting__tenant=tenant)
        .select_related("dispatch__meeting")
        .first()
    )
    if recipient is None or (body.get("meeting") and str(body["meeting"]) != str(recipient.dispatch.meeting_id)):
        raise CommandError.of(404, "nicht-gefunden", INVITATION_UNKNOWN)
    meeting = recipient.dispatch.meeting
    if recipient.acknowledged_at is None and recipient.status not in DELIVERED:
        raise CommandError.of(409, "ladung-nicht-zugestellt", NOT_DELIVERED)
    invitation_response_service.acknowledge_meeting(person, meeting, via=PORTAL)
    first = (
        SessionInvitationRecipient.objects.filter(
            dispatch__meeting=meeting, person=person, acknowledged_at__isnull=False
        )
        .order_by("acknowledged_at")
        .values_list("acknowledged_at", flat=True)
        .first()
    )
    return HandlerResult(reference=str(recipient.pk), aggregate_id=recipient.pk, received_at=first)


# =============================================================================
# Prüfungen der Rücknahme und der Rückmeldungen
# =============================================================================


def _actor_user(command: Command) -> Any:
    """Konto des Auslösers ``user:<id>`` (aktiv), sonst ``None``."""
    actor = command.actor_ref or ""
    user_id = _uuid(actor.removeprefix(USER_ACTOR_PREFIX)) if actor.startswith(USER_ACTOR_PREFIX) else None
    if user_id is None:
        return None
    from apps.accounts.models import User

    return User.objects.filter(pk=user_id, is_active=True).first()


def _may_withdraw(command: Command, tenant: SessionTenant, application: SessionApplication) -> bool:
    """Darf der Auslöser die Einreichung zurücknehmen? (Person: eigene Einreichung, Organisation weiter erlaubt)"""
    from .services.application_service import submission_originators, submitting_organization_for_token

    actor = command.actor_ref or ""
    if actor.startswith(TOKEN_ACTOR_PREFIX):
        token_id = _uuid(actor.removeprefix(TOKEN_ACTOR_PREFIX))
        token = SessionAPIToken.objects.filter(pk=token_id, tenant=tenant).first() if token_id else None
        if token is None or not token.is_valid() or not token.can_submit_applications:
            return False
        bound = submitting_organization_for_token(token)
        return application.submitted_via_token_id == token.pk or (
            bound is not None and application.submitting_organization_id == bound.pk
        )
    user = _actor_user(command)
    if user is None or application.submitting_organization_id is None or application.submitted_by_user_id != user.pk:
        return False
    return any(
        organization.pk == application.submitting_organization_id
        for organization in submission_originators(tenant, user)
    )


def _acting_person(command: Command, tenant: SessionTenant) -> SessionPerson:
    """Session-Person des Auslösers ``user:<id>``; andere Auslöser und Konten ohne Zuordnung: 403."""
    from .services.portal_link_service import person_for_user_in_tenant

    user = _actor_user(command)
    person = person_for_user_in_tenant(user, tenant) if user is not None else None
    if person is None:
        raise CommandError.of(403, "keine-berechtigung", NOT_THIS_PERSON)
    return person


def _invited(meeting: SessionMeeting, person: SessionPerson) -> bool:
    return SessionInvitationRecipient.objects.filter(dispatch__meeting=meeting, person=person).exists()


# =============================================================================
# Prüfungen der Einreichung
# =============================================================================


def _tenant(ref: str) -> SessionTenant:
    kind, _, raw_id = ref.partition(":")
    tenant = SessionTenant.objects.filter(pk=raw_id, is_active=True).first() if kind == "session" else None
    if tenant is None:
        raise CommandError.of(404, "mandant-unbekannt", TENANT_UNKNOWN)
    return tenant


def _authorize(command: Command, tenant: SessionTenant) -> tuple[SessionAPIToken, Any, bool]:
    """(Token der Einreichung, einreichende Organisation, aus Work?) – oder 403."""
    from .services.application_service import (
        SUBMITTING_ORGANIZATION_MISMATCH,
        SubmittingOrganizationMismatchError,
        connected_token,
        submitting_organization_for_token,
    )

    actor = command.actor_ref or ""
    claimed = command.body.get("submitting_organization")
    if actor.startswith(TOKEN_ACTOR_PREFIX):
        token = _token(tenant, actor.removeprefix(TOKEN_ACTOR_PREFIX))
        try:
            organization = submitting_organization_for_token(token, claimed)
        except SubmittingOrganizationMismatchError:
            raise CommandError.of(403, "keine-berechtigung", SUBMITTING_ORGANIZATION_MISMATCH) from None
        return token, organization, False

    user_id = _uuid(actor.removeprefix(USER_ACTOR_PREFIX)) if actor.startswith(USER_ACTOR_PREFIX) else None
    if user_id is None or not claimed:
        raise CommandError.of(403, "keine-berechtigung", NOT_ALLOWED)
    from apps.tenants.models import Membership

    membership = (
        Membership.objects.select_related("organization")
        .filter(user_id=user_id, organization_id=claimed, is_active=True)
        .first()
    )
    if membership is None or not membership.has_permission(SUBMIT_PERMISSION):
        raise CommandError.of(403, "keine-berechtigung", NOT_ALLOWED)
    token = connected_token(tenant, claimed)
    if token is None:
        raise CommandError.of(403, "keine-berechtigung", NOT_CONNECTED)
    if not token.is_valid() or not token.can_submit_applications:
        raise CommandError.of(403, "keine-berechtigung", TOKEN_INVALID)
    return token, membership.organization, True


def _token(tenant: SessionTenant, raw_id: str) -> SessionAPIToken:
    token_id = _uuid(raw_id)
    token = SessionAPIToken.objects.filter(pk=token_id, tenant=tenant).first() if token_id else None
    if token is None or not token.is_valid() or not token.can_submit_applications:
        raise CommandError.of(403, "keine-berechtigung", NOT_ALLOWED)
    return token


def _uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _check(command: Command, tenant: SessionTenant) -> tuple[SessionOrganization | None, list[tuple[str, File[Any]]]]:
    """Zielgremium und Anlagen (Name, Datei); 422 mit allen Verstößen auf einmal."""
    body = command.body
    problems = [f"$.{name}: darf nicht leer sein" for name in REQUIRED_TEXTS if not body[name].strip()]
    problems += [
        f"$.submitter.{name}: darf nicht leer sein" for name in ("name", "email") if not body["submitter"][name].strip()
    ]

    if body.get("co_signers") and body.get("co_signers_text"):
        problems.append("$.co_signers_text: nicht zusammen mit co_signers")

    target = None
    if body.get("target_organization"):
        target = SessionOrganization.objects.filter(
            pk=body["target_organization"], tenant=tenant, is_active=True
        ).first()
        if target is None:
            problems.append("$.target_organization: Gremium nicht gefunden")

    attachments, attachment_problems = _attachments(body.get("attachments") or (), command.attachments)
    problems += attachment_problems
    if problems:
        errors = validation_problem(problems).errors
        raise CommandError(Problem(status=422, kind="validierung", detail=INVALID, errors=errors))
    return target, attachments


def _co_signers(body: Mapping[str, Any]) -> str:
    """Mitunterzeichnende wie gespeichert: der Text unverändert, die Liste eine Person je Zeile."""
    if body.get("co_signers_text"):
        return str(body["co_signers_text"]).strip()
    return "\n".join(name.strip() for name in body.get("co_signers") or () if name.strip())


def _attachments(
    items: Sequence[Mapping[str, Any]], files: Mapping[str, Any]
) -> tuple[list[tuple[str, File[Any]]], list[str]]:
    """Anlagen aus dem Inhalt mit den mitgegebenen Dateien abgleichen (Größe und SHA-256)."""
    attachments: list[tuple[str, File[Any]]] = []
    problems: list[str] = []
    for index, item in enumerate(items):
        handle = files.get(item["sha256"])
        if handle is None:
            problems.append(f"$.attachments[{index}]: Datei fehlt")
            continue
        if _measure(handle) != (item["size"], item["sha256"]):
            problems.append(f"$.attachments[{index}]: Datei passt nicht zu Größe und SHA-256")
            continue
        name = PurePosixPath(item["name"].replace("\\", "/")).name or "Anhang"
        attachments.append((item["name"], File(handle, name=name)))
    if set(files) - {item["sha256"] for item in items}:
        problems.append("$.attachments: Datei ohne Angabe im Inhalt")
    return attachments, problems


def _measure(handle: Any) -> tuple[int, str]:
    """Größe und SHA-256 einer Datei; liest sie einmal ganz und setzt sie an den Anfang zurück."""
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    for chunk in iter(lambda: handle.read(_CHUNK), b""):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return size, digest.hexdigest()


def _attach_application_pdf(application: SessionApplication) -> None:
    """
    Antragsfassung als PDF an den Eingang hängen (Teil von #584). Best effort: Scheitert die Erzeugung, wird ohne
    PDF eingereicht; der Sicherungspunkt hält die Transaktion des Befehls intakt.
    """
    from .services.application_service import attach_application_pdf

    try:
        with transaction.atomic():
            attach_application_pdf(application)
    except Exception as exc:
        # Gewollt weiter ohne PDF (wie bisher). Ins Log nur der Typ: Die Meldung kann Inhalte zitieren.
        logger.warning(
            "Einreichung %s: Antragsfassung (PDF) nicht erzeugt (%s)", application.pk, type(exc).__qualname__
        )
