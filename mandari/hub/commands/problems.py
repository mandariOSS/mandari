# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehler eines Befehls nach RFC 9457 („Problem Details for HTTP APIs“).

Beide Wege liefern dasselbe Problem: ``InProcessClient`` als ``CommandError.problem``, der HTTP-Weg
als ``application/problem+json``; ``HttpClient`` macht daraus wieder ein ``CommandError``. Meldungen
sind feste Texte oder stammen vom Eigentümer, nie aus einer Ausnahme; Validierungsfehler nennen nur
Stelle und Regel, nie Werte (``hub.contracts.validation``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

#: Grundadresse der Problemtypen (wie in der Session-API v1).
PROBLEM_TYPE_BASE: Final = "https://docs.mandari.de/api/probleme/"

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

_PATH_PART = re.compile(r"\.([^.\[]+)|\[(\d+)\]")


@dataclass(frozen=True)
class Problem:
    """Ein Problem nach RFC 9457; ``kind`` ist der letzte Teil des Typs (z. B. ``validierung``)."""

    status: int
    kind: str
    detail: str
    errors: tuple[Mapping[str, str], ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return f"{PROBLEM_TYPE_BASE}{self.kind}"

    @property
    def title(self) -> str:
        return TITLES.get(self.status, "Fehler")

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS

    def to_dict(self, *, instance: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance is not None:
            data["instance"] = instance
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
        known = {"type", "title", "status", "detail", "instance", "errors"}
        return cls(
            status=status,
            kind=kind,
            detail=str(data.get("detail") or TITLES.get(status, "Fehler")),
            errors=tuple(
                {str(key): str(value) for key, value in error.items()}
                for error in (errors if isinstance(errors, list) else [])
                if isinstance(error, Mapping)
            ),
            extensions={key: value for key, value in data.items() if key not in known},
        )


class CommandError(Exception):
    """Ein Befehl ist gescheitert; ``problem`` beschreibt den Grund nach RFC 9457."""

    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(f"{problem.status} {problem.kind}")

    @classmethod
    def of(cls, status: int, kind: str, detail: str, **extensions: Any) -> CommandError:
        return cls(Problem(status=status, kind=kind, detail=detail, extensions=extensions))


def validation_problem(problems: Sequence[str]) -> Problem:
    """422 aus den Verstößen der Vertragsprüfung (``$.pfad: regel``); ``errors`` mit JSON-Pointer."""
    return Problem(
        status=422,
        kind="validierung",
        detail="Der Befehl entspricht nicht seinem Vertrag.",
        errors=tuple(_error(problem) for problem in problems),
    )


def _error(problem: str) -> dict[str, str]:
    path, _, rule = problem.partition(": ")
    return {"pointer": json_pointer(path), "detail": rule}


def json_pointer(path: str) -> str:
    """JSON-Pfad der Vertragsprüfung (``$.a[0].b``) als JSON-Pointer (``/a/0/b``, RFC 6901)."""
    if not path.startswith("$"):
        return ""
    return "".join(f"/{_escape(name or index)}" for name, index in _parts(path[1:]))


def _parts(rest: str) -> Iterable[tuple[str, str]]:
    return ((match.group(1) or "", match.group(2) or "") for match in _PATH_PART.finditer(rest))


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
