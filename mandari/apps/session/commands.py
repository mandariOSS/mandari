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
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import PurePosixPath
from typing import Any, Final

from django.core.files import File
from django.db import transaction

from hub.commands import Command, CommandError, HandlerResult, Problem, command_handler
from hub.commands.problems import validation_problem

from .models import SessionAPIToken, SessionApplication, SessionOrganization, SessionTenant

logger = logging.getLogger(__name__)

SUBMIT: Final = "submission.submit"
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
        co_signers="\n".join(name.strip() for name in body.get("co_signers") or () if name.strip()),
        target_organization=target,
        is_urgent=bool(body.get("is_urgent")),
        urgency_reason=(body.get("urgency_reason") or "").strip(),
        deadline=date.fromisoformat(body["deadline"]) if body.get("deadline") else None,
        submitted_via_token=token,
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


# =============================================================================
# Prüfungen
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
