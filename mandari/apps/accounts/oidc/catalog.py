# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog der Scopes des OpenID-Connect-Anbieters (Issue #1206).

Der Katalog steht nur hier im Code. Jede Anwendung (Client) bekommt im Admin eine Teilmenge davon als
erlaubte Scopes; mehr kann sie weder im Namen einer Person noch als Dienst anfordern.

Ob ein Scope zu einem Ablauf passt, legt ``person`` bzw. ``service`` fest:

- ``person``: nutzbar im Code-Ablauf, also im Namen einer angemeldeten Person
- ``service``: nutzbar mit Client Credentials, also für einen Dienst ohne Person

Das Modul hat keine Abhängigkeiten zu Django-Modellen, weil ``settings.py`` es liest.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENID = "openid"
PROFILE = "profile"
EMAIL = "email"
RATSDATEN_LESEN = "ratsdaten:lesen"
EINREICHUNG_SCHREIBEN = "einreichung:schreiben"
BEREITSTELLUNG = "mandari:bereitstellung"


@dataclass(frozen=True)
class Scope:
    """Ein Scope des Katalogs mit dem Text, den die Zustimmungsseite zeigt."""

    name: str
    description: str
    person: bool
    service: bool


CATALOG: tuple[Scope, ...] = (
    Scope(OPENID, "Anmeldung mit deinem mandari-Konto (eine Kennung, die dich wiedererkennt)", True, False),
    Scope(PROFILE, "Deinen Namen", True, False),
    Scope(EMAIL, "Deine E-Mail-Adresse", True, False),
    Scope(
        RATSDATEN_LESEN,
        "Ratsdaten lesen, die du selbst lesen darfst; Nichtöffentliches gehört nicht dazu",
        True,
        True,
    ),
    Scope(EINREICHUNG_SCHREIBEN, "Anträge in deinem Namen einreichen", True, True),
    Scope(BEREITSTELLUNG, "Organisationen und Mandanten bereitstellen (nur für Dienste)", False, True),
)

#: Name → Beschreibung, wie django-oauth-toolkit es in ``SCOPES`` erwartet
DESCRIPTIONS: dict[str, str] = {scope.name: scope.description for scope in CATALOG}

_BY_NAME: dict[str, Scope] = {scope.name: scope for scope in CATALOG}


def get(name: str) -> Scope | None:
    return _BY_NAME.get(name)


def allowed_for(names: list[str] | tuple[str, ...], *, person: bool) -> list[str]:
    """Die Scopes aus ``names``, die es gibt und die zum Ablauf passen, in der Reihenfolge des Katalogs."""
    wanted = set(names)
    return [scope.name for scope in CATALOG if scope.name in wanted and (scope.person if person else scope.service)]
