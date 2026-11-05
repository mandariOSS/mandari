# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehler eines Befehls nach RFC 9457 („Problem Details for HTTP APIs“).

Beide Wege liefern dasselbe Problem: ``InProcessClient`` als ``CommandError.problem``, der HTTP-Weg
als ``application/problem+json``; ``HttpClient`` macht daraus wieder ein ``CommandError``. Das Problem
selbst ist der gemeinsame Baustein aller Schnittstellen (``apps.common.api.problems``). Meldungen
sind feste Texte oder stammen vom Eigentümer, nie aus einer Ausnahme; Validierungsfehler nennen nur
Stelle und Regel, nie Werte (``hub.contracts.validation``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

# Das Problem selbst ist ein Baustein der Plattform, gemeinsam für alle Schnittstellen.
from apps.common.api.problems import PROBLEM_TYPE_BASE, RETRYABLE_STATUS, TITLES, Problem

__all__ = [
    "PROBLEM_TYPE_BASE",
    "RETRYABLE_STATUS",
    "TITLES",
    "CommandError",
    "Problem",
    "json_pointer",
    "validation_problem",
]

_PATH_PART = re.compile(r"\.([^.\[]+)|\[(\d+)\]")


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
