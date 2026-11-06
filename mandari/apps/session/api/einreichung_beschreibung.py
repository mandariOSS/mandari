# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschreibung des Profils Einreichung je Session-Mandant in der gemeinsamen Schnittstelle (Issue #1228).

Die Endpunkte (``apps/session/api/einreichung.py``) nehmen und liefern die Objekte des Profils; ihre Schemas stammen
unverändert aus kommParl (Komponenten ``Kommparl…``, ``hub.api.kommparl.profile_component``). Jeder Endpunkt, der
einen Befehl annimmt, nennt ihn in ``x-mandari-command``. Anmeldung: OAuth 2.0 mit dem Scope
``einreichung:schreiben`` (Sicherheitsschema ``mandariOidc``).
"""

from __future__ import annotations

from typing import Any, Final

from django.urls import reverse

from apps.accounts.oidc import catalog
from hub.api import kommparl
from hub.api.openapi import COMMAND_EXTENSION, Area, Description

#: Platzhalter für Mandant und Kennungen in Adressen (gültige Werte, die in keiner echten Adresse vorkommen)
_TENANT: Final = "mandant-platzhalter-1228"
_MARKER: Final = "00000000-0000-0000-0000-000000001228"

SECURITY_SCHEME: Final = "mandariOidc"
SCOPE: Final = catalog.EINREICHUNG_SCHREIBEN

#: Schemas des Profils, auf die die Beschreibung verweist
SCHEMAS: Final = (
    "AttendanceResponse",
    "AttendanceResponseRequest",
    "InvitationReceipt",
    "Problem",
    "Submission",
    "SubmissionList",
    "SubmissionRequest",
    "SubmissionRules",
    "SubmissionUpload",
)

_TENANT_PARAMETER: Final[dict[str, Any]] = {
    "name": "tenant_slug",
    "in": "path",
    "required": True,
    "description": "Kurzname des Mandanten (Kommune)",
    "schema": {"type": "string"},
}
_IDEMPOTENCY: Final[dict[str, Any]] = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "description": (
        "Pflicht. Dieselbe Anfrage mit demselben Schlüssel wirkt nur einmal und erhält dieselbe Antwort; derselbe "
        "Schlüssel mit anderem Inhalt ergibt 422. 1 bis 255 sichtbare ASCII-Zeichen, auch in Anführungszeichen."
    ),
    "schema": {"type": "string", "minLength": 1, "maxLength": 257},
}
_CONTENT_DIGEST: Final[dict[str, Any]] = {
    "name": "Content-Digest",
    "in": "header",
    "required": False,
    "description": "Prüfsumme des Inhalts nach RFC 9530 (sha-256 oder sha-512); weicht sie ab, antwortet der Server mit 400.",
    "schema": {"type": "string"},
}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{kommparl.profile_component_name(name)}"}


def _json(name: str) -> dict[str, Any]:
    return {"application/json": {"schema": _ref(name)}}


def _problem(description: str, **headers: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {"application/problem+json": {"schema": _ref("Problem")}},
    }
    if headers:
        response["headers"] = {
            name: {"description": text, "schema": {"type": "string"}} for name, text in headers.items()
        }
    return response


def _id(description: str) -> dict[str, Any]:
    return {
        "name": "id",
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": "string", "format": "uuid"},
    }


_AUTH_ERRORS: Final[dict[str, Any]] = {
    "401": _problem(
        "Anmeldung fehlt oder Token ungültig (auch API-Tokens der Session-API)",
        **{"WWW-Authenticate": "Bearer nach RFC 6750"},
    ),
    "403": _problem("Token ohne Person (Dienst) oder ohne Scope einreichung:schreiben; Handlung nicht erlaubt"),
    "404": _problem("Mandant, Objekt oder Ladung nicht gefunden, nicht sichtbar oder Profil nicht eingeschaltet"),
    "429": _problem("Zu viele Anfragen", **{"Retry-After": "Wartezeit in Sekunden"}),
}
_GET_HEADERS: Final[dict[str, Any]] = {
    "ETag": {"description": "Kennung des Inhalts für bedingte Abrufe", "schema": {"type": "string"}},
}


def _operation(
    operation_id: str,
    summary: str,
    description: str,
    responses: dict[str, Any],
    *,
    parameters: list[dict[str, Any]],
    command: str | None = None,
    body: dict[str, Any] | None = None,
    write: bool = False,
) -> dict[str, Any]:
    all_responses = {**responses, **_AUTH_ERRORS}
    if write:
        all_responses.setdefault(
            "400", _problem("Idempotency-Key fehlt, Inhalt kein JSON-Objekt oder Content-Digest abweichend")
        )
        all_responses.setdefault("415", _problem("Kein application/json"))
        all_responses.setdefault(
            "422", _problem("Inhalt passt nicht zum Profil oder zu den Einreichungsregeln; Schlüssel schon verwendet")
        )
    else:
        all_responses.setdefault("304", {"description": "Unverändert (If-None-Match passt zum ETag)"})
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "description": description,
        "security": [{SECURITY_SCHEME: [SCOPE]}],
        "parameters": [dict(parameter) for parameter in parameters],
        "responses": dict(sorted(all_responses.items())),
    }
    if body is not None:
        operation["requestBody"] = body
    if command:
        operation[COMMAND_EXTENSION] = command
    return operation


def _path(name: str, **kwargs: Any) -> str:
    path = reverse(f"session:{name}", kwargs={"tenant_slug": _TENANT, **kwargs})
    return path.replace(_TENANT, "{tenant_slug}").replace(_MARKER, "{id}")


def description() -> Description:
    """Pfade, Schemas und Sicherheitsschema des Profils Einreichung."""
    tenant = [_TENANT_PARAMETER]
    submission_id = _id("Kennung der Einreichung")
    meeting_id = _id("Kennung der Sitzung")
    write = [*tenant, _IDEMPOTENCY]
    profile = kommparl.PROFILE_SUBMISSION
    paths: dict[str, Any] = {
        _path("oparl_kommparl_submission_rules"): {
            "get": _operation(
                "einreichung_regeln",
                "Einreichungsregeln",
                f"Einreichungsregeln für die angemeldete Person (kommparl:submissionRules, Profil {profile}).",
                {
                    "200": {
                        "description": "Einreichungsregeln",
                        "headers": _GET_HEADERS,
                        "content": _json("SubmissionRules"),
                    }
                },
                parameters=tenant,
            )
        },
        _path("oparl_kommparl_uploads"): {
            "post": _operation(
                "einreichung_anlage_hochladen",
                "Anlage hochladen",
                (
                    "Eine Datei im Teil „file“ (multipart/form-data), geprüft wie Antrags-Anhänge (Dateityp, Größe, "
                    "Virenprüfung). Die Antwort nennt die Adresse, die beim Einreichen in upload steht; der Inhalt "
                    "wird nie ausgeliefert."
                ),
                {
                    "201": {
                        "description": "Hochgeladen",
                        "headers": {"Location": {"description": "Adresse der Datei", "schema": {"type": "string"}}},
                        "content": _json("SubmissionUpload"),
                    },
                    "400": _problem("Nicht genau eine Datei im Teil file"),
                    "413": _problem("Datei zu groß"),
                    "415": _problem("Dateityp nicht angenommen oder kein multipart/form-data"),
                    "422": _problem("Virenprüfung nicht bestanden oder nicht verfügbar"),
                },
                parameters=tenant,
                body={
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["file"],
                                "properties": {
                                    "file": {"type": "string", "contentMediaType": "application/octet-stream"}
                                },
                            }
                        }
                    },
                },
            )
        },
        _path("oparl_kommparl_upload", pk=_MARKER): {
            "get": _operation(
                "einreichung_anlage",
                "Hochgeladene Anlage",
                "Angaben zu einer eigenen hochgeladenen Datei, nicht ihr Inhalt.",
                {"200": {"description": "Angaben", "headers": _GET_HEADERS, "content": _json("SubmissionUpload")}},
                parameters=[*tenant, _id("Kennung der Datei")],
            )
        },
        _path("oparl_kommparl_submissions"): {
            "get": _operation(
                "einreichung_liste",
                "Einreichungen",
                (
                    "Einreichungen der Stellen, für die die angemeldete Person einreichen darf, die jüngste zuerst; "
                    "Seiten wie in OParl (data, pagination, links). Jedes Element ist eine Einreichung "
                    f"({kommparl.profile_component_name('Submission')})."
                ),
                {
                    "200": {
                        "description": "Seite der Einreichungen",
                        "headers": {
                            **_GET_HEADERS,
                            "Link": {"description": "Blätter-Verweise nach RFC 8288", "schema": {"type": "string"}},
                        },
                        "content": _json("SubmissionList"),
                    },
                    "400": _problem("Ungültige Seitennummer"),
                },
                parameters=[
                    *tenant,
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "description": "Seite, ab 1",
                        "schema": {"type": "integer", "minimum": 1},
                    },
                ],
            ),
            "post": _operation(
                "einreichung_einreichen",
                "Einreichen",
                (
                    "Antrag, Änderungsantrag oder Anfrage einreichen (Befehl submission.submit). Die Antwort 201 ist "
                    "die Eingangsbestätigung mit Eingangsnummer (receiptNumber), Eingangszeit (receivedAt) und "
                    "Prüfsumme des Inhalts (contentDigest, RFC 9530); eine Wiederholung mit demselben "
                    "Idempotency-Key liefert dieselbe."
                ),
                {
                    "201": {
                        "description": "Eingangsbestätigung",
                        "headers": {
                            "Location": {"description": "Adresse der Einreichung", "schema": {"type": "string"}}
                        },
                        "content": _json("Submission"),
                    },
                    "503": _problem("Anlagen konnten nicht abgelegt werden; mit demselben Schlüssel wiederholen"),
                },
                parameters=[*write, _CONTENT_DIGEST],
                command="submission.submit",
                body={"required": True, "content": _json("SubmissionRequest")},
                write=True,
            ),
        },
        _path("oparl_kommparl_submission", pk=_MARKER): {
            "get": _operation(
                "einreichung",
                "Einreichung",
                "Eine Einreichung mit Eingangsbestätigung und Bearbeitungsstand; fremde Einreichungen ergeben 404.",
                {"200": {"description": "Einreichung", "headers": _GET_HEADERS, "content": _json("Submission")}},
                parameters=[*tenant, submission_id],
            )
        },
        _path("oparl_kommparl_submission_withdraw", pk=_MARKER): {
            "post": _operation(
                "einreichung_zuruecknehmen",
                "Einreichung zurücknehmen",
                (
                    "Rücknahme (Befehl submission.withdraw), solange der Stand es zulässt (withdrawableStatus der "
                    "Einreichungsregeln); sonst 409. Inhalt: leer oder ein leeres JSON-Objekt."
                ),
                {
                    "200": {"description": "Zurückgenommen", "content": _json("Submission")},
                    "409": _problem("Rücknahme in diesem Stand nicht zulässig"),
                },
                parameters=[*write, submission_id],
                command="submission.withdraw",
                write=True,
            )
        },
        _path("oparl_kommparl_attendance", pk=_MARKER): {
            "get": _operation(
                "einreichung_rueckmeldung",
                "Rückmeldung zur Ladung",
                "Stand der eigenen Rückmeldung zu einer Sitzung, zu der die Person geladen ist (kommparl:attendanceResponse).",
                {"200": {"description": "Stand", "headers": _GET_HEADERS, "content": _json("AttendanceResponse")}},
                parameters=[*tenant, meeting_id],
            ),
            "post": _operation(
                "einreichung_zurueckmelden",
                "Zurückmelden",
                (
                    "Teilnahme oder Absage, bei einer Absage mit Vertretungswunsch und Grund (Befehl "
                    "attendance.respond). Der Grund ist nur für die Verwaltung bestimmt und erscheint in keiner "
                    "Ausgabe. Die Rückmeldung bestätigt zugleich den Empfang der Ladung."
                ),
                {
                    "200": {"description": "Neuer Stand", "content": _json("AttendanceResponse")},
                    "409": _problem("Die Sitzung nimmt keine Rückmeldungen mehr an"),
                },
                parameters=[*write, meeting_id],
                command="attendance.respond",
                body={"required": True, "content": _json("AttendanceResponseRequest")},
                write=True,
            ),
        },
        _path("oparl_kommparl_invitation_receipt", pk=_MARKER): {
            "post": _operation(
                "einreichung_empfang_bestaetigen",
                "Empfang der Ladung bestätigen",
                (
                    "Bestätigt den Empfang der Ladung (Befehl invitation.acknowledge); ein Abruf zählt nicht. "
                    "Inhalt: leer oder ein leeres JSON-Objekt. Der Zeitpunkt der ersten Bestätigung bleibt."
                ),
                {
                    "200": {"description": "Bestätigt", "content": _json("InvitationReceipt")},
                    "405": _problem("Nur POST"),
                    "409": _problem("Die Ladung ist noch nicht zugestellt"),
                },
                parameters=[*write, meeting_id],
                command="invitation.acknowledge",
                write=True,
            )
        },
    }
    schemas = {
        kommparl.profile_component_name(name): kommparl.profile_component("submission", name) for name in SCHEMAS
    }
    security = {
        SECURITY_SCHEME: {
            "type": "oauth2",
            "description": (
                "OpenID-Connect-Anbieter der Installation (Discovery unter /.well-known/openid-configuration). Nur "
                "Access-Tokens im Namen einer Person; PKCE mit S256 ist Pflicht."
            ),
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": "/oauth/authorize/",
                    "tokenUrl": "/oauth/token/",
                    "refreshUrl": "/oauth/token/",
                    "scopes": {SCOPE: catalog.DESCRIPTIONS[SCOPE]},
                }
            },
        }
    }
    return {"paths": paths, "components": {"schemas": schemas, "securitySchemes": security}}


EINREICHUNG: Final = Area(
    key="einreichung",
    title="Profil Einreichung",
    description=(
        f"Profil Einreichung von kommParl {kommparl.FASSUNG} je Session-Mandant: einreichen, Bearbeitungsstand, "
        "Rücknahme, Rückmeldung zur Ladung. Nur mit Access-Token im Namen einer Person (Scope einreichung:schreiben), "
        "nur wenn die Installation den OpenID-Connect-Anbieter eingeschaltet hat."
    ),
    entry="/session/{tenant_slug}/api/oparl/body/kommparl/submissions/",
    openapi=description,
    order=25,
)
