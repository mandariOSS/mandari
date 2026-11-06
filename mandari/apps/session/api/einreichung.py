# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil Einreichung von kommParl je Session-Mandant (Issue #1228, kommParl Kapitel „Profil Einreichung“).

Fraktionen und Mandatstragende reichen über eine Anwendung ein, sehen den Bearbeitungsstand, nehmen zurück und
melden sich zur Ladung zurück. Die Adressen hängen an der OParl-Ausgabe des Mandanten (``…/api/oparl/``) und stehen
an Body und Sitzung (``kommparl:submission`` …), sobald die Installation das Profil anbietet. Der Aggregator nimmt
keine Einreichungen an.

**Anmeldung:** nur Access-Tokens des OpenID-Connect-Anbieters (``apps.accounts.oidc``) im Namen einer Person mit dem
Scope ``einreichung:schreiben``. Ohne eingeschalteten Anbieter gibt es das Profil nicht (404). Tokens von Diensten
(Client Credentials) ergeben 403, API-Tokens der Session-API und unbekannte Tokens 401; Sitzungscookies zählen nicht.

**Ein Weg:** Geschrieben wird nur über die Befehle (``apps/session/commands.py``) mit dem Auslöser ``user:<id>``;
deren Handler prüfen die Rechte wie beim bisherigen Weg (Einreichen wie aus Work, Rückmeldung wie im Portal). Die
Endpunkte lesen nur das Format ein und geben das Ergebnis im Format des Profils aus. Lesen und zurücknehmen dürfen
Personen nur ihre eigenen Einreichungen (``SessionApplication.submitted_by_user``, aus Work oder über das Profil),
solange sie für deren Organisation einreichen dürfen (``application_service.submission_originators``). Einreichungen
anderer Mitglieder derselben Organisation gibt es für sie nicht (404).

**Anlagen** laden Personen vorab hoch (``SessionSubmissionUpload``), nur wenn sie bei der Verwaltung einreichen
dürfen. Geprüft wird wie bei Antrags-Anhängen (Dateityp, Größe, Virenprüfung, ``application_service
.attachment_rejection``) und mit der gemeinsamen Prüfung (``apps.common.uploads.validate_upload``), abgelegt beim
Einreichen wie bisher in der Ablage der Anlagen (``attach_application_files``). Offene Dateien sind je Person über alle
Mandanten begrenzt; eine Datei lässt sich in genau einer Einreichung verwenden. Abgelaufene Dateien löscht der
Zeitplan ``befehl:einreichung_aufraeumen``. Das Profil liefert hochgeladene Inhalte nie aus.

**Gemeinsame Bausteine** aus ``apps.common.api``: Fehler nach RFC 9457, ``Idempotency-Key``, ``ETag`` und Seiten.
Jede schreibende Handlung und jeder Abruf von Einreichungen steht im Prüfprotokoll des Mandanten mit Person und
Anwendung (Client-ID), nie mit Inhalten.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache, wraps
from pathlib import PurePosixPath
from typing import Any, Final

from django.core.cache import cache as django_cache
from django.core.exceptions import RequestDataTooBig
from django.core.exceptions import ValidationError as UploadRejected
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Prefetch, QuerySet, Sum
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from apps.accounts.oidc import access as oidc_access
from apps.accounts.oidc import catalog, provider_enabled
from apps.common.api.conditional import conditional, etag_for
from apps.common.api.idempotency import IDEMPOTENCY_HEADER, KEY_MISSING, KEY_MISSING_KIND, parse_idempotency_key
from apps.common.api.idempotency import valid_idempotency_key as _valid_key
from apps.common.api.pages import list_envelope
from apps.common.api.problems import Problem, problem_response
from apps.session import audit, commands
from apps.session.models import (
    SessionApplication,
    SessionAttendance,
    SessionFile,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionPaper,
    SessionPerson,
    SessionSubmissionUpload,
    SessionTenant,
)
from hub.api import kommparl
from hub.commands import Command, CommandError, Receipt, dispatch
from hub.commands.types import exceeds_depth
from hub.ris.mapping.session import SessionMapping

logger = logging.getLogger(__name__)

#: Profil von kommParl, nach dem die Endpunkte arbeiten (Schemas unter ``hub/api/kommparl/<Fassung>/profiles/``)
PROFILE: Final = "submission"
#: Scope, ohne den ein Access-Token das Profil nicht nutzt
SCOPE: Final = catalog.EINREICHUNG_SCHREIBEN
REALM: Final = "mandari"
#: Vermerk im Prüfprotokoll
ACCESS: Final = "kommParl-Profil Einreichung"

TYPE_SUBMISSION: Final = f"{kommparl.VERSION}Submission"
TYPE_UPLOAD: Final = f"{kommparl.VERSION}SubmissionUpload"
TYPE_RULES: Final = f"{kommparl.VERSION}SubmissionRules"
TYPE_ATTENDANCE: Final = f"{kommparl.VERSION}AttendanceResponse"
TYPE_RECEIPT: Final = f"{kommparl.VERSION}InvitationReceipt"

#: Arten des Profils -> Antragsart in Session (``majorQuestion`` und ``agendaRequest`` kennt Session nicht)
TYPE_TO_SESSION: Final[dict[str, str]] = {
    "motion": "motion",
    "amendment": "amendment",
    "question": "inquiry",
    "resolution": "resolution",
    "other": "other",
}
#: Antragsart in Session -> Art des Profils; der Dringlichkeitsantrag ist ein Antrag mit ``urgent``
SESSION_TO_TYPE: Final[dict[str, str]] = {
    "motion": "motion",
    "urgent": "motion",
    "amendment": "amendment",
    "inquiry": "question",
    "resolution": "resolution",
    "other": "other",
}
#: Bearbeitungsstand in Session -> Stand des Profils
SESSION_TO_STATUS: Final[dict[str, str]] = {
    "submitted": "submitted",
    "received": "registered",
    "in_review": "inReview",
    "accepted": "accepted",
    "rejected": "rejected",
    "converted": "converted",
    "withdrawn": "withdrawn",
}
#: Freiwillige Angaben, die Session verarbeitet, und die, die die Geschäftsordnung in Session zusätzlich verlangt
ACCEPTED_PROPERTIES: Final = (
    "reasoningText",
    "financialImpactText",
    "urgent",
    "urgencyReason",
    "targetOrganization",
    "targetDate",
    "originator",
    "coSigner",
    "contact",
    "upload",
)
REQUIRED_PROPERTIES: Final = ("reasoningText",)
#: Grund, aus dem der Befehl eine Anlage nicht angenommen hat (``application_service.ATTACHMENT_*``) -> Code im Profil
REJECTION_REASONS: Final[dict[str, str]] = {"dateityp": "mediaType", "groesse": "size", "virenpruefung": "scan"}

PAGE_SIZE: Final = 20
#: Hochgeladene Dateien lassen sich so lange verwenden (Profil: mindestens 24 Stunden)
UPLOAD_LIFETIME: Final = timedelta(hours=24)
#: Angaben verwendeter Dateien bleiben so lange (Wiederholungen mit demselben Idempotenzschlüssel, wie der Speicher)
UPLOAD_RETENTION: Final = timedelta(days=30)
#: Höchstzahl offener (hochgeladener, noch nicht verwendeter) Dateien je Person, über alle Mandanten
MAX_OPEN_UPLOADS: Final = 50
#: Höchstens so viele Bytes in offenen Dateien je Person, über alle Mandanten
MAX_OPEN_UPLOAD_BYTES: Final = 500 * 1024 * 1024
#: Höchstzahl von Anfragen je Person und Minute über alle Endpunkte des Profils
REQUESTS_PER_MINUTE: Final = 120
#: Spielraum für Kopfzeilen eines multipart-Inhalts über der Größe der Datei
MULTIPART_OVERHEAD: Final = 64 * 1024
#: Prüfsummen im Header ``Content-Digest`` (RFC 9530), die das Profil nachrechnet
DIGESTS: Final[dict[str, Callable[[bytes], Any]]] = {"sha-256": hashlib.sha256, "sha-512": hashlib.sha512}

#: Feste Meldungen (nie mit Werten aus dem Inhalt)
TENANT_UNKNOWN: Final = "Mandant nicht gefunden."
NOT_OFFERED: Final = "Das Profil Einreichung ist in dieser Installation nicht eingeschaltet."
LOGIN_REQUIRED: Final = "Anmeldung erforderlich: Access-Token im Namen einer Person (Authorization: Bearer)."
TOKEN_INVALID: Final = "Das Zugriffstoken ist ungültig, abgelaufen oder widerrufen."
TOKEN_FOREIGN: Final = "Das Profil nimmt nur Zugriffstokens des OpenID-Connect-Anbieters an, keine API-Tokens."
PERSON_REQUIRED: Final = "Das Profil Einreichung nimmt nur Tokens im Namen einer Person an, keine Dienst-Tokens."
SCOPE_MISSING: Final = f"Dem Zugriffstoken fehlt der Scope „{SCOPE}“."
RATE_LIMITED: Final = "Zu viele Anfragen. Bitte in einer Minute erneut versuchen."
INVALID: Final = "Die Anfrage entspricht nicht dem Profil Einreichung."
JSON_REQUIRED: Final = "Der Inhalt muss ein JSON-Objekt sein (application/json)."
DIGEST_MISMATCH: Final = "Der Header Content-Digest passt nicht zum Inhalt der Anfrage."
DIGEST_INVALID: Final = "Der Header Content-Digest ist ungültig."
SUBMISSION_UNKNOWN: Final = "Einreichung nicht gefunden."
UPLOAD_UNKNOWN: Final = "Datei nicht gefunden."
NOT_INVITED: Final = "Die Sitzung ist nicht bekannt, oder die Person ist zu ihr nicht geladen."
NO_ORIGINATOR: Final = "Diese Person darf bei dieser Verwaltung nicht einreichen."
ORIGINATOR_FORBIDDEN: Final = "Für diese Stelle darf die Person nicht einreichen."
UPLOAD_ONE_FILE: Final = "Erwartet wird genau eine Datei im Teil „file“ (multipart/form-data)."
UPLOAD_TOO_LARGE: Final = "Die Datei ist zu groß."
UPLOAD_TYPE: Final = "Dieser Dateityp wird nicht angenommen."
UPLOAD_SCAN: Final = "Die Datei hat die Virenprüfung nicht bestanden, oder die Prüfung ist nicht verfügbar."
UPLOAD_EMPTY: Final = "Die Datei ist leer."
UPLOAD_LIMIT: Final = "Zu viele offene Dateien. Bitte zuerst einreichen oder den Ablauf abwarten."
UPLOAD_USED: Final = "Datei schon verwendet"
METHOD_NOT_ALLOWED: Final = "Methode nicht erlaubt."
RECEIPT_GET: Final = "Der Empfang wird mit POST bestätigt; ein Abruf zählt nicht als Bestätigung."
PAGE_INVALID: Final = "Ungültige Seitennummer."

#: Regeln von JSON Schema -> Erklärung für ``errors`` (ohne Werte aus der Anfrage)
RULES: Final[dict[str, str]] = {
    "required": "Pflichtangabe fehlt",
    "additionalProperties": "Eigenschaft ist nicht vorgesehen",
    "type": "falscher Datentyp",
    "enum": "Wert ist nicht vorgesehen",
    "const": "Wert ist nicht vorgesehen",
    "pattern": "Format ungültig",
    "minLength": "darf nicht leer sein",
    "uniqueItems": "Einträge doppelt",
    "minimum": "Wert zu klein",
    "maximum": "Wert zu groß",
}

#: Stellen im Befehl ``submission.submit`` -> Stellen in der Anfrage des Profils (für ``errors``)
SUBMIT_POINTERS: Final[dict[str, str]] = {
    "/title": "/name",
    "/resolution_proposal": "/proposalText",
    "/justification": "/reasoningText",
    "/financial_impact": "/financialImpactText",
    "/is_urgent": "/urgent",
    "/urgency_reason": "/urgencyReason",
    "/deadline": "/targetDate",
    "/target_organization": "/targetOrganization",
    "/submitting_organization": "/originator",
    "/submitter/name": "/contact/name",
    "/submitter/email": "/contact/email",
    "/submitter/phone": "/contact/phone",
    "/submitter": "/contact",
    "/co_signers": "/coSigner",
    "/attachments": "/upload",
    "/application_type": "/submissionType",
}
RESPOND_POINTERS: Final[dict[str, str]] = {"/substitute_requested": "/substituteRequested", "/reason": "/reason"}


# =============================================================================
# Fehler, Anmeldung, Rahmen der Endpunkte
# =============================================================================


class ProfileError(Exception):
    """Absage eines Endpunkts als Problem nach RFC 9457."""

    def __init__(
        self,
        status: int,
        kind: str,
        detail: str,
        *,
        errors: Sequence[Mapping[str, str]] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {kind}")
        self.problem = Problem(status=status, kind=kind, detail=detail, errors=tuple(errors))
        self.headers = dict(headers or {})


@dataclass(frozen=True)
class Caller:
    """Angemeldete Person bei einem Mandanten: Token, Konto und die Abbildung der OParl-Ausgabe."""

    tenant: SessionTenant
    access: oidc_access.OAuthAccess
    mapping: SessionMapping = field(repr=False)

    @property
    def user(self) -> Any:
        return self.access.user

    @property
    def actor_ref(self) -> str:
        return f"user:{self.user.pk}"


def _challenge(error: str | None = None) -> dict[str, str]:
    value = f'Bearer realm="{REALM}", scope="{SCOPE}"'
    if error:
        value += f', error="{error}"'
    return {"WWW-Authenticate": value}


def _authenticate(request: HttpRequest) -> oidc_access.OAuthAccess:
    """Access-Token im Namen einer Person mit dem Scope des Profils; sonst 401 bzw. 403."""
    header = str(request.META.get("HTTP_AUTHORIZATION", ""))
    if not header.startswith("Bearer "):
        raise ProfileError(401, "nicht-authentifiziert", LOGIN_REQUIRED, headers=_challenge())
    raw = header[7:].strip()
    if not oidc_access.looks_like_access_token(raw):
        # Etwa ein API-Token der Session-API (64 Zeichen ohne Präfix): hier keine Anmeldung
        raise ProfileError(401, "nicht-authentifiziert", TOKEN_FOREIGN, headers=_challenge("invalid_token"))
    try:
        access = oidc_access.authenticate(raw, resource_uri=request.build_absolute_uri(request.path))
    except oidc_access.InvalidAccessToken:
        raise ProfileError(401, "nicht-authentifiziert", TOKEN_INVALID, headers=_challenge("invalid_token")) from None
    if not access.acts_for_person:
        raise ProfileError(403, "keine-berechtigung", PERSON_REQUIRED)
    if not access.has_scope(SCOPE):
        raise ProfileError(403, "keine-berechtigung", SCOPE_MISSING, headers=_challenge("insufficient_scope"))
    return access


def _throttle(access: oidc_access.OAuthAccess) -> None:
    """Höchstens ``REQUESTS_PER_MINUTE`` Anfragen je Person und Minute; sonst 429 mit ``Retry-After``."""
    key = f"kommparl-einreichung:{getattr(access.user, 'pk', '')}:rate"
    if django_cache.add(key, 1, timeout=60):
        return
    try:
        count = int(django_cache.incr(key))
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        django_cache.add(key, 1, timeout=60)
        return
    if count > REQUESTS_PER_MINUTE:
        raise ProfileError(429, "ratenlimit", RATE_LIMITED, headers={"Retry-After": "60"})


def _caller(request: HttpRequest, tenant_slug: str) -> Caller:
    """Mandant mit freigeschalteter OParl-Ausgabe, Profil eingeschaltet, Person angemeldet."""
    from apps.session.api import oparl

    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True, oparl_public_since__isnull=False).first()
    if tenant is None:
        raise ProfileError(404, "nicht-gefunden", TENANT_UNKNOWN)
    if not offered():
        raise ProfileError(404, "nicht-gefunden", NOT_OFFERED)
    access = _authenticate(request)
    _throttle(access)
    # Dieselbe Abbildung wie die OParl-Ausgabe des Mandanten (Adressen aus SITE_URL)
    mapping_for: Callable[[SessionTenant], SessionMapping] = oparl._mapping
    return Caller(tenant=tenant, access=access, mapping=mapping_for(tenant))


def offered() -> bool:
    """Bietet die Installation das Profil an? Nur mit eingeschaltetem OpenID-Connect-Anbieter."""
    return provider_enabled()


View = Callable[..., HttpResponseBase]


def profile_endpoint(*methods: str, method_detail: str = METHOD_NOT_ALLOWED) -> Callable[[View], View]:
    """
    Rahmen jedes Endpunkts: Methoden (405 mit ``Allow`` und ``method_detail``), ``ProfileError`` als Problem, kein
    CSRF (nur Bearer).
    """
    allowed = ", ".join(methods)

    def decorate(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            try:
                if request.method not in methods:
                    raise ProfileError(405, "methode-nicht-erlaubt", method_detail, headers={"Allow": allowed})
                return view(request, *args, **kwargs)
            except ProfileError as exc:
                headers = {"Cache-Control": "no-store", **exc.headers}
                return problem_response(exc.problem, instance=request.get_full_path(), headers=headers)

        return csrf_exempt(wrapper)

    return decorate


# =============================================================================
# Anfrage lesen
# =============================================================================


def _idempotency_key(request: HttpRequest) -> str:
    key = parse_idempotency_key(request.headers.get(IDEMPOTENCY_HEADER))
    if key is None or not _valid_key(key):
        raise ProfileError(400, KEY_MISSING_KIND, KEY_MISSING)
    return key


def _raw_body(request: HttpRequest) -> bytes:
    try:
        return bytes(request.body)
    except RequestDataTooBig:
        raise ProfileError(413, "anfrage-zu-gross", "Der Inhalt ist zu groß.") from None


def _json_object(request: HttpRequest, raw: bytes, *, allow_empty: bool = False) -> dict[str, Any]:
    """Inhalt als JSON-Objekt (400/415); bei ``allow_empty`` gilt ein leerer Inhalt als ``{}``."""
    if allow_empty and not raw.strip():
        return {}
    if request.content_type != "application/json":
        raise ProfileError(415, "format-nicht-unterstuetzt", JSON_REQUIRED)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProfileError(400, "ungueltiges-json", JSON_REQUIRED) from None
    if not isinstance(data, dict) or exceeds_depth(data):
        raise ProfileError(400, "ungueltiges-json", JSON_REQUIRED)
    return data


def content_digest(content: bytes) -> str:
    """Prüfsumme im Format des Felds ``Content-Digest`` (RFC 9530) mit ``sha-256``."""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii") + ":"


def _check_content_digest(request: HttpRequest, raw: bytes) -> None:
    """Hat der Client ``Content-Digest`` gesendet, muss er für jeden bekannten Algorithmus passen (sonst 400)."""
    header = request.headers.get("Content-Digest")
    if not header:
        return
    for member in header.split(","):
        name, _, value = member.strip().partition("=")
        algorithm = DIGESTS.get(name.strip().lower())
        if algorithm is None:
            continue
        value = value.strip()
        try:
            if len(value) < 2 or not (value.startswith(":") and value.endswith(":")):
                raise ValueError
            expected = base64.b64decode(value[1:-1], validate=True)
        except ValueError:
            raise ProfileError(400, "content-digest", DIGEST_INVALID) from None
        if not hmac.compare_digest(expected, algorithm(raw).digest()):
            raise ProfileError(400, "content-digest", DIGEST_MISMATCH)


@cache
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(kommparl.profile_schema(PROFILE, name))


def _pointer(parts: Iterable[Any]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _error(error: ValidationError) -> dict[str, str]:
    parts = list(error.absolute_path)
    if error.validator == "required" and isinstance(error.message, str) and error.message.count("'") >= 2:
        parts.append(error.message.split("'")[1])
    rule = RULES.get(str(error.validator), "nicht erlaubt")
    return {"pointer": _pointer(parts), "detail": rule}


def validate(data: Any, name: str) -> None:
    """Anfrage gegen das Schema ``name`` des Profils; Verstöße als 422 mit ``errors`` (Stelle und Regel)."""
    errors = sorted(_validator(name).iter_errors(data), key=lambda error: [str(part) for part in error.absolute_path])
    if errors:
        raise ProfileError(422, "validierung", INVALID, errors=[_error(error) for error in errors])


def _uuid_after(url: object, prefix: str) -> uuid.UUID | None:
    """Kennung am Ende einer Adresse der Ausgabe (``<prefix><uuid>/``), sonst ``None``."""
    if not isinstance(url, str) or not url.startswith(prefix):
        return None
    try:
        return uuid.UUID(url.removeprefix(prefix).rstrip("/"))
    except ValueError:
        return None


# =============================================================================
# Antworten
# =============================================================================


def _response(
    request: HttpRequest, data: Mapping[str, Any], *, status: int = 200, headers: Mapping[str, str] | None = None
) -> HttpResponseBase:
    content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    response = HttpResponse(content, status=status, content_type="application/json; charset=utf-8")
    for name, value in (headers or {}).items():
        response[name] = value
    if request.method == "GET":
        response["ETag"] = etag_for(content)
        response["Cache-Control"] = "private, no-cache"
        response["Vary"] = "Authorization"
        return conditional(request, response)
    response["Cache-Control"] = "no-store"
    return response


def _problem_from_command(error: CommandError, pointers: Mapping[str, str]) -> ProfileError:
    """Problem des Befehls mit den Stellen der Anfrage des Profils."""
    problem = error.problem
    errors = [
        {"pointer": _map_pointer(str(item.get("pointer", "")), pointers), "detail": str(item.get("detail", ""))}
        for item in problem.errors
    ]
    return ProfileError(problem.status, problem.kind or "fehler", problem.detail, errors=errors)


def _map_pointer(pointer: str, pointers: Mapping[str, str]) -> str:
    for source in sorted(pointers, key=len, reverse=True):
        if pointer == source or pointer.startswith(source + "/"):
            return pointers[source] + pointer[len(source) :]
    return pointer


def _audit(request: HttpRequest, caller: Caller, action: str, instance: Any, aktion: str, **details: Any) -> None:
    """Eintrag im Prüfprotokoll des Mandanten: Person und Anwendung, nie Inhalte. Scheitert nie die Antwort."""
    changes = {
        "zugriff": ACCESS,
        "aktion": aktion,
        "anwendung": caller.access.client_id,
        "person": caller.actor_ref,
        **details,
    }
    try:
        audit.log_event(action, instance, tenant=caller.tenant, request=request, changes=changes)
    except Exception:
        logger.exception("Profil Einreichung: Prüfprotokoll nicht geschrieben (%s)", aktion)


# =============================================================================
# Einreichungen: Sichtbarkeit und Ausgabe
# =============================================================================


def _originators(caller: Caller) -> list[Any]:
    from apps.session.services.application_service import submission_originators

    return submission_originators(caller.tenant, caller.user)


def originator_id(organization_id: Any) -> str:
    """Kennung einer einreichenden Organisation im Profil (``originator``): sie hat kein OParl-Objekt."""
    return f"urn:uuid:{organization_id}"


def _visible(caller: Caller) -> QuerySet[SessionApplication]:
    """
    Eigene Einreichungen der Person, solange sie für deren Organisation einreichen darf; alles andere gibt es für sie
    nicht. Einreichungen anderer Mitglieder derselben Organisation enthalten deren Kontaktdaten und Texte, die Work
    nur nach seiner Sichtbarkeit zeigt; das Recht, für die Organisation einzureichen, genügt dafür nicht.
    """
    organizations = _originators(caller)
    return _applications(caller.tenant).filter(
        submitted_by_user=caller.user,
        submitting_organization_id__in=[organization.pk for organization in organizations],
    )


def _applications(tenant: SessionTenant) -> QuerySet[SessionApplication]:
    """Anträge des Mandanten mit allem, was die Ausgabe braucht (Organisation, eingereichte Anlagen)."""
    return (
        SessionApplication.objects.filter(tenant=tenant)
        .select_related("submitting_organization")
        .prefetch_related(
            Prefetch(
                "files",
                queryset=SessionFile.objects.filter(is_application_pdf=False)
                .defer("text_content")
                .prefetch_related("versions__blob")
                .order_by("created_at", "pk"),
                to_attr="submitted_files",
            )
        )
    )


def _papers(tenant: SessionTenant, applications: Sequence[SessionApplication]) -> dict[Any, Any]:
    """Vorlage je Antrag, soweit veröffentlicht: dieselbe Wahl wie der Rückmeldestand (die erste Vorlage)."""
    from apps.session.oparl_publication import visible_papers

    first: dict[Any, Any] = {}
    for paper in SessionPaper.objects.filter(
        tenant=tenant, source_application_id__in=[application.pk for application in applications]
    ).order_by("created_at", "pk"):
        first.setdefault(paper.source_application_id, paper.pk)
    visible = set(visible_papers(tenant).filter(pk__in=list(first.values())).values_list("pk", flat=True))
    return {application_id: paper_id for application_id, paper_id in first.items() if paper_id in visible}


def _history(application: SessionApplication) -> list[dict[str, Any]]:
    """Verlauf, soweit Session ihn kennt: Eingang, Erfassung durch die Verwaltung, aktueller Stand."""
    from hub.ris.canonical import iso

    status = SESSION_TO_STATUS.get(application.status, application.status)
    history: list[dict[str, Any]] = [{"status": "submitted", "date": iso(application.submitted_at)}]
    if status != "submitted" and (application.received_at is not None or status == "registered"):
        entry: dict[str, Any] = {"status": "registered"}
        if application.received_at is not None:
            entry["date"] = iso(application.received_at)
        history.append(entry)
    if status not in ("submitted", "registered"):
        history.append({"status": status})
    return history


def _attachments(application: SessionApplication) -> list[dict[str, Any]]:
    result = []
    for session_file in getattr(application, "submitted_files", []):
        versions = list(session_file.versions.all())
        blob = versions[0].blob if versions else None
        if blob is None or not blob.sha256:
            continue
        result.append(
            {
                "name": session_file.name,
                "mediaType": session_file.mime_type or "application/octet-stream",
                "size": int(blob.size),
                "contentDigest": "sha-256=:" + base64.b64encode(bytes.fromhex(blob.sha256)).decode("ascii") + ":",
            }
        )
    return result


def submission_object(caller: Caller, application: SessionApplication, paper_id: Any = None) -> dict[str, Any]:
    """Eine Einreichung im Format des Profils (Schema ``Submission``)."""
    from hub.ris.canonical import clean, iso, iso_date

    uris = caller.mapping.uris
    organization = application.submitting_organization
    co_signers = [line.strip() for line in (application.co_signers or "").splitlines() if line.strip()]
    contact = {
        "name": application.submitter_name or None,
        "email": application.submitter_email or None,
        "phone": application.submitter_phone or None,
    }
    data: dict[str, Any] = {
        "id": uris.submission(application.pk),
        "type": TYPE_SUBMISSION,
        "body": uris.body(),
        "submissionType": SESSION_TO_TYPE.get(application.application_type, "other"),
        "name": application.title,
        "proposalText": application.resolution_proposal,
        "reasoningText": application.justification or None,
        "financialImpactText": application.financial_impact or None,
        "urgent": bool(application.is_urgent or application.application_type == "urgent"),
        "urgencyReason": application.urgency_reason or None,
        "targetOrganization": uris.obj("organization", application.target_organization_id)
        if application.target_organization_id
        else None,
        "targetDate": iso_date(application.deadline),
        "originator": originator_id(organization.pk) if organization is not None else None,
        "originatorName": organization.name if organization is not None else None,
        "coSigner": co_signers or None,
        "contact": {key: value for key, value in contact.items() if value} if contact["name"] else None,
        "attachment": _attachments(application) or None,
        "status": SESSION_TO_STATUS.get(application.status, application.status),
        "statusHistory": _history(application),
        "receiptNumber": application.reference or str(application.pk),
        "receivedAt": iso(application.submitted_at),
        "contentDigest": application.content_digest or None,
        "paper": uris.obj("paper", paper_id) if paper_id else None,
        "withdraw": uris.submission_withdraw(application.pk),
        "created": iso(application.submitted_at),
        "modified": iso(application.updated_at),
    }
    return dict(clean(data))


# =============================================================================
# Endpunkte: Einreichungsregeln, Anlagen
# =============================================================================


@profile_endpoint("GET")
def rules_view(request: HttpRequest, tenant_slug: str) -> HttpResponseBase:
    """Einreichungsregeln für die angemeldete Person (``kommparl:submissionRules``)."""
    from apps.session.services import file_service

    caller = _caller(request, tenant_slug)
    rules = {
        "type": TYPE_RULES,
        "body": caller.mapping.uris.body(),
        "acceptedTypes": list(TYPE_TO_SESSION),
        "acceptedProperties": list(ACCEPTED_PROPERTIES),
        "requiredProperties": list(REQUIRED_PROPERTIES),
        "formRequirement": "text",
        "withdrawableStatus": [
            SESSION_TO_STATUS[status] for status in SESSION_TO_STATUS if status in commands.WITHDRAWABLE_STATUSES
        ],
        "upload": {
            "maxSize": int(file_service.MAX_FILE_SIZE_MB) * 1024 * 1024,
            "mediaTypes": sorted({file_service.MIME_TYPES[extension] for extension in upload_extensions()}),
        },
        "originator": [
            {"id": originator_id(organization.pk), "name": organization.name} for organization in _originators(caller)
        ],
    }
    return _response(request, rules)


def _upload_object(caller: Caller, upload: SessionSubmissionUpload) -> dict[str, Any]:
    from hub.ris.canonical import iso

    return {
        "id": caller.mapping.uris.submission_upload(upload.pk),
        "type": TYPE_UPLOAD,
        "name": upload.name,
        "mediaType": upload.mime_type,
        "size": int(upload.size),
        "contentDigest": "sha-256=:" + base64.b64encode(bytes.fromhex(upload.sha256)).decode("ascii") + ":",
        "created": iso(upload.created_at),
        "expires": iso(upload.expires_at),
    }


def _drop_content(upload: SessionSubmissionUpload) -> bool:
    """Inhalt einer hochgeladenen Datei aus dem Speicher löschen (die Angaben bleiben); ``False``, wenn das scheitert."""
    name = str(upload.file.name or "")
    if not name:
        return True
    try:
        upload.file.storage.delete(name)
    except Exception:  # Speicher nicht erreichbar: Das nächste Aufräumen versucht es erneut
        logger.warning("Profil Einreichung: Datei %s nicht gelöscht", upload.pk)
        return False
    SessionSubmissionUpload.objects.filter(pk=upload.pk).update(file="")
    return True


def purge_uploads(tenant: SessionTenant | None = None) -> int:
    """
    Abgelaufene, nicht verwendete Dateien und alte Angaben verwendeter Dateien löschen, ohne ``tenant`` aller
    Mandanten (Zeitplan ``befehl:einreichung_aufraeumen``). Liefert die Zahl der gelöschten Zeilen.
    """
    now = timezone.now()
    uploads = SessionSubmissionUpload.objects.all()
    if tenant is not None:
        uploads = uploads.filter(tenant=tenant)
    stale = list(uploads.filter(used_at__isnull=True, expires_at__lte=now)) + list(
        uploads.filter(used_at__lte=now - UPLOAD_RETENTION)
    )
    # Zeilen nur ohne Inhalt löschen; sonst bliebe eine Datei ohne Verweis im Speicher
    dropped = [upload.pk for upload in stale if _drop_content(upload)]
    SessionSubmissionUpload.objects.filter(pk__in=dropped).delete()
    return len(dropped)


def upload_extensions() -> frozenset[str]:
    """
    Endungen, die das Profil als Anlage annimmt: die der Antrags-Anhänge in Session, soweit das Profil ``DOCUMENTS``
    der gemeinsamen Prüfung sie erlaubt (``apps.common.uploads``; ``NEVER`` wie SVG nie).
    """
    from apps.common.uploads import DOCUMENTS, NEVER
    from apps.session.services import file_service

    return (frozenset(file_service.ALLOWED_EXTENSIONS) & DOCUMENTS) - NEVER


def _open_uploads(user: Any) -> tuple[int, int]:
    """Offene (hochgeladene, nicht verwendete, nicht abgelaufene) Dateien einer Person über alle Mandanten."""
    totals = SessionSubmissionUpload.objects.filter(
        user=user, used_at__isnull=True, expires_at__gt=timezone.now()
    ).aggregate(count=Count("pk"), size=Sum("size"))
    return int(totals["count"] or 0), int(totals["size"] or 0)


def _measure(handle: Any) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return size, digest.hexdigest()


def _check_upload(uploaded: Any, name: str, open_bytes: int) -> None:
    """Grenze der Person, Dateityp und Größe (wie Antrags-Anhänge, dazu ``validate_upload``), Virenprüfung."""
    from apps.common.uploads import validate_upload
    from apps.session.services import application_service, file_service

    size = int(uploaded.size or 0)
    if open_bytes + size > MAX_OPEN_UPLOAD_BYTES:
        raise ProfileError(429, "ratenlimit", UPLOAD_LIMIT, headers={"Retry-After": "3600"})
    code = application_service.attachment_rejection(name, size)
    if code == application_service.ATTACHMENT_SIZE:
        raise ProfileError(413, "anfrage-zu-gross", UPLOAD_TOO_LARGE)
    if code is not None:
        raise ProfileError(415, "format-nicht-unterstuetzt", UPLOAD_TYPE)
    try:
        validate_upload(
            uploaded, allowed=upload_extensions(), max_bytes=int(file_service.MAX_FILE_SIZE_MB) * 1024 * 1024
        )
    except UploadRejected:
        if size <= 0:
            raise ProfileError(422, "validierung", UPLOAD_EMPTY) from None
        raise ProfileError(415, "format-nicht-unterstuetzt", UPLOAD_TYPE) from None
    try:
        file_service.scan_upload(uploaded)
    except Exception:  # Befund oder Prüfung nicht verfügbar – der Hook wirft beliebige Ausnahmen
        logger.warning("Profil Einreichung: Anlage abgelehnt (Virenprüfung)")
        raise ProfileError(422, "virenpruefung", UPLOAD_SCAN) from None


@profile_endpoint("POST")
def uploads_view(request: HttpRequest, tenant_slug: str) -> HttpResponseBase:
    """
    Anlage hochladen (``kommparl:submissionUpload``): nur mit Einreichungsrecht bei dieser Verwaltung (sonst 403,
    bevor der Inhalt gelesen wird), höchstens ``MAX_OPEN_UPLOADS`` offene Dateien und ``MAX_OPEN_UPLOAD_BYTES`` je
    Person über alle Mandanten (429), geprüft wie Antrags-Anhänge.
    """
    from apps.session.services import file_service

    caller = _caller(request, tenant_slug)
    if not _originators(caller):
        raise ProfileError(403, "keine-berechtigung", NO_ORIGINATOR)
    open_count, open_bytes = _open_uploads(caller.user)
    if open_count >= MAX_OPEN_UPLOADS:
        raise ProfileError(429, "ratenlimit", UPLOAD_LIMIT, headers={"Retry-After": "3600"})
    max_bytes = int(file_service.MAX_FILE_SIZE_MB) * 1024 * 1024
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > max_bytes + MULTIPART_OVERHEAD:
        raise ProfileError(413, "anfrage-zu-gross", UPLOAD_TOO_LARGE)
    if request.content_type != "multipart/form-data":
        raise ProfileError(415, "format-nicht-unterstuetzt", UPLOAD_ONE_FILE)
    try:
        files = request.FILES
    except RequestDataTooBig:
        raise ProfileError(413, "anfrage-zu-gross", UPLOAD_TOO_LARGE) from None
    uploaded = files.get("file")
    if uploaded is None or len(files) != 1 or len(files.getlist("file")) != 1:
        raise ProfileError(400, "ungueltige-anfrage", UPLOAD_ONE_FILE)
    name = PurePosixPath(str(uploaded.name or "").replace("\\", "/")).name.strip()[:255]
    if not name:
        raise ProfileError(400, "ungueltige-anfrage", UPLOAD_ONE_FILE)
    _check_upload(uploaded, name, open_bytes)
    size, sha256 = _measure(uploaded)
    upload = SessionSubmissionUpload(
        tenant=caller.tenant,
        user=caller.user,
        client_id=caller.access.client_id[:100],
        name=name,
        mime_type=file_service.mime_type_for_name(name),
        size=size,
        sha256=sha256,
        expires_at=timezone.now() + UPLOAD_LIFETIME,
    )
    # Gespeichert unter einem zufälligen Namen, nie unter dem Namen aus der Anfrage
    upload.file.save(f"{uuid.uuid4().hex}{PurePosixPath(name).suffix.lower()}", uploaded, save=False)
    upload.save()
    _audit(request, caller, "create", upload, "Anlage hochgeladen", groesse=size)
    data = _upload_object(caller, upload)
    return _response(request, data, status=201, headers={"Location": data["id"]})


@profile_endpoint("GET")
def upload_view(request: HttpRequest, tenant_slug: str, pk: uuid.UUID) -> HttpResponseBase:
    """Angaben zu einer hochgeladenen Datei der Person, nie ihr Inhalt."""
    caller = _caller(request, tenant_slug)
    upload = SessionSubmissionUpload.objects.filter(pk=pk, tenant=caller.tenant, user=caller.user).first()
    if upload is None:
        raise ProfileError(404, "nicht-gefunden", UPLOAD_UNKNOWN)
    return _response(request, _upload_object(caller, upload))


# =============================================================================
# Endpunkte: Einreichen, Bearbeitungsstand, Rücknahme
# =============================================================================


def _page_number(request: HttpRequest) -> int:
    raw = request.GET.get("page", "1")
    try:
        number = int(raw)
    except ValueError:
        raise ProfileError(400, "ungueltige-anfrage", PAGE_INVALID) from None
    if number < 1:
        raise ProfileError(400, "ungueltige-anfrage", PAGE_INVALID)
    return number


def _list(request: HttpRequest, caller: Caller) -> HttpResponseBase:
    paginator: Paginator[SessionApplication] = Paginator(_visible(caller).order_by("-submitted_at", "pk"), PAGE_SIZE)
    try:
        page = paginator.page(_page_number(request))
    except EmptyPage:
        raise ProfileError(404, "nicht-gefunden", PAGE_INVALID) from None
    applications = list(page.object_list)
    papers = _papers(caller.tenant, applications)
    data = [submission_object(caller, application, papers.get(application.pk)) for application in applications]
    envelope, headers = list_envelope(caller.mapping.uris.submissions(), {}, paginator, page, data)
    if applications:
        _audit(
            request,
            caller,
            "view",
            caller.tenant,
            "Einreichungen abgerufen",
            kennungen=[str(application.pk) for application in applications],
        )
    return _response(request, envelope, headers={key: value for key, value in headers.items() if value})


def _key_hash(key: str) -> str:
    """Kennung des Idempotenzschlüssels an einer verwendeten Datei (SHA-256, nicht der Schlüssel selbst)."""
    return hashlib.sha256(f"{commands.SUBMIT} {key}".encode()).hexdigest()


def _resolve_uploads(caller: Caller, urls: Sequence[str], key_hash: str) -> list[SessionSubmissionUpload]:
    """
    Hochgeladene Dateien der Person; fremde, abgelaufene oder in einer anderen Einreichung verwendete ergeben 422
    (Stelle ``/upload/<n>``). Eine Datei, die schon für diesen Schlüssel vorgemerkt ist, gehört zur Wiederholung.
    """
    prefix = caller.mapping.uris.submission_uploads()
    now = timezone.now()
    uploads: list[SessionSubmissionUpload] = []
    errors: list[dict[str, str]] = []
    for index, url in enumerate(urls):
        pk = _uuid_after(url, prefix)
        upload = (
            SessionSubmissionUpload.objects.filter(pk=pk, tenant=caller.tenant, user=caller.user).first()
            if pk
            else None
        )
        if upload is None:
            errors.append({"pointer": f"/upload/{index}", "detail": "Datei nicht gefunden"})
        elif upload.used_at is not None and upload.used_for_key != key_hash:
            errors.append({"pointer": f"/upload/{index}", "detail": UPLOAD_USED})
        elif upload.used_at is None and upload.expires_at <= now:
            errors.append({"pointer": f"/upload/{index}", "detail": "Datei abgelaufen"})
        else:
            uploads.append(upload)
    if errors:
        raise ProfileError(422, "validierung", INVALID, errors=errors)
    return uploads


def _reserve_uploads(uploads: Sequence[SessionSubmissionUpload], key_hash: str) -> list[Any]:
    """
    Unverwendete Dateien vor dem Befehl für diesen Schlüssel vormerken (bedingtes Update): Von zwei gleichzeitigen
    Einreichungen mit verschiedenen Schlüsseln bekommt nur eine die Datei, die andere 422. Liefert die vorgemerkten.
    """
    now = timezone.now()
    reserved: list[Any] = []
    errors: list[dict[str, str]] = []
    for index, upload in enumerate(uploads):
        if upload.used_at is not None:
            continue  # Wiederholung: schon für diesen Schlüssel vorgemerkt
        taken = SessionSubmissionUpload.objects.filter(pk=upload.pk, used_at__isnull=True).update(
            used_at=now, used_for_key=key_hash
        )
        if taken:
            reserved.append(upload.pk)
            upload.used_at, upload.used_for_key = now, key_hash
        elif not SessionSubmissionUpload.objects.filter(pk=upload.pk, used_for_key=key_hash).exists():
            errors.append({"pointer": f"/upload/{index}", "detail": UPLOAD_USED})
    if errors:
        _release_uploads(reserved, key_hash)
        raise ProfileError(422, "validierung", INVALID, errors=errors)
    return reserved


def _release_uploads(pks: Sequence[Any], key_hash: str) -> None:
    """Vormerkung zurücknehmen, wenn der Befehl abgelehnt wurde (die Dateien bleiben verwendbar)."""
    if pks:
        SessionSubmissionUpload.objects.filter(pk__in=pks, used_for_key=key_hash, application__isnull=True).update(
            used_at=None, used_for_key=""
        )


def _originator(caller: Caller, requested: object) -> Any:
    """Einreichende Organisation: die genannte (403, wenn nicht erlaubt) oder die einzige erlaubte."""
    organizations = _originators(caller)
    if requested is not None:
        match = next((o for o in organizations if originator_id(o.pk) == requested), None)
        if match is None:
            raise ProfileError(403, "keine-berechtigung", ORIGINATOR_FORBIDDEN)
        return match
    if not organizations:
        raise ProfileError(403, "keine-berechtigung", NO_ORIGINATOR)
    if len(organizations) > 1:
        raise ProfileError(
            422,
            "validierung",
            INVALID,
            errors=[{"pointer": "/originator", "detail": "Die Person darf für mehrere Stellen einreichen"}],
        )
    return organizations[0]


def _submit_body(caller: Caller, data: Mapping[str, Any], organization: Any) -> dict[str, Any]:
    """Inhalt des Befehls ``submission.submit`` aus der Anfrage des Profils (422 bei Angaben, die Session ablehnt)."""
    errors: list[dict[str, str]] = []
    if data["submissionType"] not in TYPE_TO_SESSION:
        errors.append({"pointer": "/submissionType", "detail": "Art wird nicht angenommen"})
    errors += [
        {"pointer": f"/{name}", "detail": "Pflichtangabe nach der Geschäftsordnung"}
        for name in REQUIRED_PROPERTIES
        if name not in data
    ]
    target = None
    if "targetOrganization" in data:
        target = _uuid_after(data["targetOrganization"], caller.mapping.uris.base + "organization/")
        if target is None:
            errors.append({"pointer": "/targetOrganization", "detail": "Gremium nicht gefunden"})
    if errors:
        raise ProfileError(422, "validierung", INVALID, errors=errors)

    user = caller.user
    contact = dict(data.get("contact") or {})
    submitter: dict[str, Any] = {
        "name": contact.get("name") or user.get_full_name() or user.email,
        "email": contact.get("email") or user.email,
    }
    if contact.get("phone"):
        submitter["phone"] = contact["phone"]
    body: dict[str, Any] = {
        "application_type": TYPE_TO_SESSION[data["submissionType"]],
        "title": data["name"],
        "resolution_proposal": data["proposalText"],
        "justification": data["reasoningText"],
        "submitter": submitter,
        "is_urgent": bool(data.get("urgent")),
        "submitting_organization": str(organization.pk),
    }
    for source, target_name in (
        ("financialImpactText", "financial_impact"),
        ("urgencyReason", "urgency_reason"),
        ("targetDate", "deadline"),
    ):
        if source in data:
            body[target_name] = data[source]
    if target is not None:
        body["target_organization"] = str(target)
    if data.get("coSigner"):
        body["co_signers"] = list(data["coSigner"])
    return body


def _submit(request: HttpRequest, caller: Caller) -> HttpResponseBase:
    key = _idempotency_key(request)
    raw = _raw_body(request)
    _check_content_digest(request, raw)
    data = _json_object(request, raw)
    validate(data, "SubmissionRequest")
    organization = _originator(caller, data.get("originator"))
    body = _submit_body(caller, data, organization)
    upload_urls = list(data.get("upload") or [])
    key_hash = _key_hash(key)
    uploads = _resolve_uploads(caller, upload_urls, key_hash)
    body["attachments"] = [{"name": u.name, "size": int(u.size), "sha256": u.sha256} for u in uploads]
    if not body["attachments"]:
        del body["attachments"]
    reserved = _reserve_uploads(uploads, key_hash)
    # Dateien gehen mit, solange ihr Inhalt hier liegt; eine Wiederholung nach dem Ablegen braucht sie nicht
    handles: dict[str, Any] = {}
    try:
        for upload in uploads:
            if upload.file and upload.sha256 not in handles:
                handles[upload.sha256] = upload.file.open("rb")
        receipt = dispatch(
            Command(
                name=commands.SUBMIT,
                body=body,
                idempotency_key=key,
                tenant_ref=commands.tenant_ref(caller.tenant),
                actor_ref=caller.actor_ref,
                attachments=handles,
            )
        )
    except CommandError as exc:
        _release_uploads(reserved, key_hash)
        raise _problem_from_command(exc, SUBMIT_POINTERS) from None
    except Exception:
        _release_uploads(reserved, key_hash)
        raise
    finally:
        for handle in handles.values():
            handle.close()
    application = _finish_submission(caller, receipt, uploads, raw, key_hash)
    _audit(request, caller, "create", application, "Einreichung", anlagen=len(uploads))
    obj = submission_object(caller, application, _papers(caller.tenant, [application]).get(application.pk))
    rejected = [
        {"upload": upload_urls[int(item["index"])], "reason": REJECTION_REASONS.get(str(item["reason"]), "other")}
        for item in receipt.data.get("attachments_rejected") or ()
        if 0 <= int(item["index"]) < len(upload_urls)
    ]
    if rejected:
        obj["rejectedUpload"] = rejected
    return _response(request, obj, status=201, headers={"Location": obj["id"]})


def _finish_submission(
    caller: Caller, receipt: Receipt, uploads: Sequence[SessionSubmissionUpload], raw: bytes, key_hash: str
) -> SessionApplication:
    """Prüfsumme festhalten (nur beim ersten Mal) und verwendete Dateien vermerken; ihr Inhalt liegt jetzt in der Ablage."""
    application_id = str(receipt.aggregate_id)
    SessionApplication.objects.filter(pk=application_id, content_digest="").update(content_digest=content_digest(raw))
    application = _applications(caller.tenant).get(pk=application_id)
    for upload in uploads:
        SessionSubmissionUpload.objects.filter(pk=upload.pk, used_for_key=key_hash, application__isnull=True).update(
            application=application
        )
        if upload.file:
            _drop_content(upload)
    return application


@profile_endpoint("GET", "POST")
def submissions_view(request: HttpRequest, tenant_slug: str) -> HttpResponseBase:
    """Einreichungen der Person abrufen (GET) oder einreichen (POST, Befehl ``submission.submit``)."""
    caller = _caller(request, tenant_slug)
    if request.method == "POST":
        return _submit(request, caller)
    return _list(request, caller)


def _visible_one(caller: Caller, pk: uuid.UUID) -> SessionApplication:
    application = _visible(caller).filter(pk=pk).first()
    if application is None:
        raise ProfileError(404, "nicht-gefunden", SUBMISSION_UNKNOWN)
    return application


@profile_endpoint("GET")
def submission_view(request: HttpRequest, tenant_slug: str, pk: uuid.UUID) -> HttpResponseBase:
    """Eine Einreichung mit Bearbeitungsstand; fremde Einreichungen gibt es nicht (404)."""
    caller = _caller(request, tenant_slug)
    application = _visible_one(caller, pk)
    _audit(request, caller, "view", application, "Einreichung abgerufen")
    paper = _papers(caller.tenant, [application]).get(application.pk)
    return _response(request, submission_object(caller, application, paper))


def _empty_object(request: HttpRequest) -> None:
    """Inhalt einer Rücknahme oder Empfangsbestätigung: leer oder ein JSON-Objekt nur mit eigenen Feldern."""
    data = _json_object(request, _raw_body(request), allow_empty=True)
    unexpected = [name for name in data if ":" not in name or name.startswith("kommparl:")]
    if unexpected:
        raise ProfileError(
            422,
            "validierung",
            INVALID,
            errors=[{"pointer": _pointer([name]), "detail": RULES["additionalProperties"]} for name in unexpected],
        )


@profile_endpoint("POST")
def withdraw_view(request: HttpRequest, tenant_slug: str, pk: uuid.UUID) -> HttpResponseBase:
    """Einreichung zurücknehmen (Befehl ``submission.withdraw``); 409 in unzulässigem Stand."""
    caller = _caller(request, tenant_slug)
    key = _idempotency_key(request)
    _empty_object(request)
    application = _visible_one(caller, pk)
    try:
        dispatch(
            Command(
                name=commands.WITHDRAW,
                body={
                    "submission": str(application.pk),
                    "submitting_organization": str(application.submitting_organization_id),
                },
                idempotency_key=key,
                tenant_ref=commands.tenant_ref(caller.tenant),
                actor_ref=caller.actor_ref,
            )
        )
    except CommandError as exc:
        raise _problem_from_command(exc, {}) from None
    application = _visible_one(caller, pk)
    _audit(request, caller, "update", application, "Einreichung zurückgenommen")
    paper = _papers(caller.tenant, [application]).get(application.pk)
    return _response(request, submission_object(caller, application, paper))


# =============================================================================
# Endpunkte: Rückmeldung zur Ladung, Empfangsbestätigung
# =============================================================================


@dataclass(frozen=True)
class Invitation:
    """Sitzung, die Person des Kontos und ihre Versände (jüngster zuerst)."""

    meeting: SessionMeeting
    person: SessionPerson
    recipients: list[SessionInvitationRecipient]

    @property
    def acknowledged_at(self) -> Any:
        times = [recipient.acknowledged_at for recipient in self.recipients if recipient.acknowledged_at]
        return min(times) if times else None


def _invitation(caller: Caller, meeting_pk: uuid.UUID) -> Invitation:
    """Geladene Person zu einer Sitzung; ohne Zuordnung oder Ladung 404 (nichts verrät, ob es die Sitzung gibt)."""
    from apps.session.services.portal_link_service import person_for_user_in_tenant

    meeting = SessionMeeting.objects.filter(pk=meeting_pk, tenant=caller.tenant).first()
    person = person_for_user_in_tenant(caller.user, caller.tenant)
    recipients = (
        list(
            SessionInvitationRecipient.objects.filter(dispatch__meeting=meeting, person=person).order_by(
                "-dispatch__sent_at", "-created_at"
            )
        )
        if meeting is not None and person is not None
        else []
    )
    if meeting is None or person is None or not recipients:
        raise ProfileError(404, "nicht-gefunden", NOT_INVITED)
    return Invitation(meeting=meeting, person=person, recipients=recipients)


def attendance_object(caller: Caller, invitation: Invitation) -> dict[str, Any]:
    """Stand der Rückmeldung (Schema ``AttendanceResponse``); der Grund einer Absage erscheint nie."""
    from apps.session.services.invitation_response_service import is_open_for_responses
    from hub.ris.canonical import clean, iso

    attendance = SessionAttendance.objects.filter(meeting=invitation.meeting, person=invitation.person).first()
    response = "pending"
    substitute_requested: bool | None = None
    responded_at = None
    if attendance is not None and attendance.status in ("confirmed", "declined"):
        response = "attending" if attendance.status == "confirmed" else "notAttending"
        substitute_requested = bool(attendance.substitute_requested) if response == "notAttending" else None
        responded_at = attendance.responded_at
    data = {
        "type": TYPE_ATTENDANCE,
        "meeting": caller.mapping.uris.obj("meeting", invitation.meeting.pk),
        "response": response,
        "substituteRequested": substitute_requested,
        "respondedAt": iso(responded_at),
        "acknowledgedAt": iso(invitation.acknowledged_at),
        "acceptsResponses": is_open_for_responses(invitation.meeting),
    }
    return dict(clean(data))


@profile_endpoint("GET", "POST")
def attendance_view(request: HttpRequest, tenant_slug: str, pk: uuid.UUID) -> HttpResponseBase:
    """Rückmeldung zur Ladung abrufen (GET) oder abgeben (POST, Befehl ``attendance.respond``)."""
    caller = _caller(request, tenant_slug)
    if request.method == "GET":
        return _response(request, attendance_object(caller, _invitation(caller, pk)))
    key = _idempotency_key(request)
    data = _json_object(request, _raw_body(request))
    validate(data, "AttendanceResponseRequest")
    invitation = _invitation(caller, pk)
    declined = data["response"] == "notAttending"
    body: dict[str, Any] = {
        "meeting": str(invitation.meeting.pk),
        "person": str(invitation.person.pk),
        "response": "declined" if declined else "confirmed",
        "source": commands.PORTAL,
    }
    if declined:
        body["substitute_requested"] = bool(data.get("substituteRequested"))
        if data.get("reason"):
            body["reason"] = data["reason"]
    try:
        dispatch(
            Command(
                name=commands.RESPOND,
                body=body,
                idempotency_key=key,
                tenant_ref=commands.tenant_ref(caller.tenant),
                actor_ref=caller.actor_ref,
            )
        )
    except CommandError as exc:
        raise _problem_from_command(exc, RESPOND_POINTERS) from None
    _audit(request, caller, "update", invitation.meeting, "Rückmeldung zur Ladung")
    return _response(request, attendance_object(caller, _invitation(caller, pk)))


@profile_endpoint("POST", method_detail=RECEIPT_GET)
def invitation_receipt_view(request: HttpRequest, tenant_slug: str, pk: uuid.UUID) -> HttpResponseBase:
    """Empfang der Ladung bestätigen (Befehl ``invitation.acknowledge``); ein Abruf zählt nicht."""
    from hub.ris.canonical import iso

    caller = _caller(request, tenant_slug)
    key = _idempotency_key(request)
    _empty_object(request)
    invitation = _invitation(caller, pk)
    delivered = [r for r in invitation.recipients if r.status in commands.DELIVERED]
    acknowledged = [r for r in invitation.recipients if r.acknowledged_at is not None]
    recipient = (delivered or acknowledged or invitation.recipients)[0]
    try:
        receipt = dispatch(
            Command(
                name=commands.ACKNOWLEDGE,
                body={"recipient": str(recipient.pk), "meeting": str(invitation.meeting.pk), "source": commands.PORTAL},
                idempotency_key=key,
                tenant_ref=commands.tenant_ref(caller.tenant),
                actor_ref=caller.actor_ref,
            )
        )
    except CommandError as exc:
        raise _problem_from_command(exc, {}) from None
    _audit(request, caller, "update", invitation.meeting, "Empfang der Ladung bestätigt")
    data = {
        "type": TYPE_RECEIPT,
        "meeting": caller.mapping.uris.obj("meeting", invitation.meeting.pk),
        "acknowledgedAt": iso(receipt.received_at),
    }
    return _response(request, data)
