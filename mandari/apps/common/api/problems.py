# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehler nach RFC 9457 („Problem Details for HTTP APIs“) für alle Schnittstellen.

- ``Problem``: ein Problem als Wert (Status, Art, Meldung, ``errors``, Erweiterungen). Die Art ``kind`` ist
  der letzte Teil des Typs unter ``PROBLEM_TYPE_BASE`` (z. B. ``validierung``); ohne Art gilt
  ``about:blank``.
- ``ProblemError``: dasselbe als Ausnahme, für Endpunkte unter django-ninja (``install_problem_handlers``).
- ``problem_response``: die Antwort ``application/problem+json``. Felder in fester Reihenfolge: ``type``,
  ``title``, ``status``, ``detail``, ``instance``, ``request_id`` (Korrelation mit den Logs, siehe
  ``apps.common.observability``), ``errors`` und weitere Erweiterungen.
- ``TITLES``: Titel je Status. Eine veröffentlichte Schnittstelle mit eigener, kleinerer Tabelle gibt diese als
  ``titles`` mit, damit sich ihre Antworten nicht ändern (Session-API v1).

Meldungen sind feste Texte oder stammen vom Eigentümer, nie aus dem Text einer Ausnahme.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from django.http import HttpResponse, JsonResponse
from ninja.errors import HttpError

from apps.common.observability import current_request_id

#: Grundadresse der Problemtypen (beschrieben auf docs.mandari.de).
PROBLEM_TYPE_BASE: Final = "https://docs.mandari.de/api/probleme/"

#: Typ eines Problems ohne eigene Art (RFC 9457, Abschnitt 4.2.1).
ABOUT_BLANK: Final = "about:blank"

PROBLEM_CONTENT_TYPE: Final = "application/problem+json; charset=utf-8"

TITLES: Final[dict[int, str]] = {
    400: "Ungültige Anfrage",
    401: "Nicht authentifiziert",
    403: "Keine Berechtigung",
    404: "Nicht gefunden",
    405: "Methode nicht erlaubt",
    409: "Konflikt",
    410: "Nicht mehr verfügbar",
    413: "Anfrage zu groß",
    415: "Nicht unterstütztes Format",
    422: "Validierung fehlgeschlagen",
    429: "Zu viele Anfragen",
    500: "Interner Fehler",
    501: "Nicht verfügbar",
    502: "Fehlerhafte Antwort",
    503: "Nicht erreichbar",
    504: "Zeitüberschreitung",
}

#: Probleme, bei denen dieselbe Anfrage mit demselben Idempotenzschlüssel später gelingen kann.
RETRYABLE_STATUS: Final = frozenset({409, 429, 502, 503, 504})

#: Felder, die RFC 9457 festlegt, und ``errors``; Erweiterungen überschreiben sie nicht.
_STANDARD: Final = frozenset({"type", "title", "status", "detail", "instance", "errors"})


#: Titel eines Status, den die Tabelle nicht kennt
FALLBACK_TITLE: Final = "Fehler"


def title_for(status: int, titles: Mapping[int, str] | None = None) -> str:
    """Titel des Status aus ``titles`` (ohne Angabe: ``TITLES``), sonst ``FALLBACK_TITLE``."""
    return (TITLES if titles is None else titles).get(status, FALLBACK_TITLE)


@dataclass(frozen=True)
class Problem:
    """Ein Problem nach RFC 9457; ``kind`` ist der letzte Teil des Typs (z. B. ``validierung``)."""

    status: int
    kind: str | None
    detail: str
    errors: tuple[Mapping[str, Any], ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return f"{PROBLEM_TYPE_BASE}{self.kind}" if self.kind else ABOUT_BLANK

    @property
    def title(self) -> str:
        return title_for(self.status)

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS

    def to_dict(
        self, *, instance: str | None = None, request_id: bool = False, titles: Mapping[int, str] | None = None
    ) -> dict[str, Any]:
        """
        Inhalt der Antwort. Mit ``request_id`` steht die Kennung der Anfrage hinter ``instance`` (leer: ``null``);
        ``titles`` ersetzt die Tabelle der Titel (``title_for``).
        """
        data: dict[str, Any] = {
            "type": self.type,
            "title": title_for(self.status, titles),
            "status": self.status,
            "detail": self.detail,
        }
        if instance is not None:
            data["instance"] = instance
        if request_id:
            data["request_id"] = current_request_id() or None
        if self.errors:
            data["errors"] = [dict(error) for error in self.errors]
        data.update({key: value for key, value in self.extensions.items() if key not in data})
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, status: int) -> Problem:
        """
        Problem aus einer Antwort; fehlende oder fremde Angaben ergeben neutrale Werte.

        Maßgeblich ist der HTTP-Status der Antwort (``status``), nicht die Angabe im Inhalt: RFC 9457
        nennt ``status`` dort einen Hinweis, und ein fremder Wert darf den Aufrufer nicht täuschen.
        """
        type_uri = str(data.get("type") or "")
        kind = type_uri.removeprefix(PROBLEM_TYPE_BASE) if type_uri.startswith(PROBLEM_TYPE_BASE) else "unbekannt"
        errors = data.get("errors")
        return cls(
            status=status,
            kind=kind,
            detail=str(data.get("detail") or title_for(status)),
            errors=tuple(
                {str(key): str(value) for key, value in error.items()}
                for error in (errors if isinstance(errors, list) else [])
                if isinstance(error, Mapping)
            ),
            extensions={key: value for key, value in data.items() if key not in _STANDARD},
        )


class ProblemError(HttpError):  # noqa: N818 – RFC-9457-Begriff „Problem“, kein Error-Suffix nötig
    """
    Ein Problem als Ausnahme: ``ProblemError(404, "…", kind="nicht-gefunden")``.

    ``headers`` gehen in die Antwort (etwa ``Retry-After``), alle weiteren Schlüsselwörter sind Erweiterungen
    des Problems. Unter django-ninja macht ``install_problem_handlers`` daraus die Antwort.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        kind: str | None = None,
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(status, detail)
        self.kind = kind
        self.headers = dict(headers or {})
        self.extensions = extensions

    @property
    def problem(self) -> Problem:
        return Problem(status=self.status_code, kind=self.kind, detail=str(self.message), extensions=self.extensions)


def problem_response(
    problem: Problem,
    *,
    instance: str | None = None,
    request_id: bool = True,
    headers: Mapping[str, str] | None = None,
    titles: Mapping[int, str] | None = None,
) -> HttpResponse:
    """``application/problem+json`` mit dem Status des Problems; ``headers`` kommen dazu, ``titles`` wie ``to_dict``."""
    response = JsonResponse(
        problem.to_dict(instance=instance, request_id=request_id, titles=titles),
        status=problem.status,
        json_dumps_params={"ensure_ascii": False},
    )
    response["Content-Type"] = PROBLEM_CONTENT_TYPE
    for name, value in (headers or {}).items():
        response[name] = value
    return response


#: Schema eines Problems für OpenAPI-Beschreibungen (RFC 9457 mit den Erweiterungen von mandari)
PROBLEM_SCHEMA: Final[dict[str, Any]] = {
    "title": "Problem",
    "description": (
        "Fehler nach RFC 9457 (application/problem+json). Weitere Felder je Problemtyp sind möglich; "
        f"die Typen sind unter {PROBLEM_TYPE_BASE} beschrieben."
    ),
    "type": "object",
    "required": ["type", "title", "status", "detail"],
    "properties": {
        "type": {"type": "string", "description": "Art des Problems (URL) oder about:blank"},
        "title": {"type": "string"},
        "status": {"type": "integer", "minimum": 400, "maximum": 599},
        "detail": {"type": "string"},
        "instance": {"type": "string", "description": "Adresse der Anfrage"},
        "request_id": {
            "type": ["string", "null"],
            "description": "Kennung der Anfrage zur Korrelation mit den Protokollen",
        },
        "errors": {"type": "array", "items": {"type": "object"}, "description": "Einzelne Verstöße"},
    },
}
