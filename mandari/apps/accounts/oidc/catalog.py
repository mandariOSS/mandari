# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog der Scopes des OpenID-Connect-Anbieters (Issue #1206).

Der Katalog steht nur hier im Code. Jede Anwendung (Client) bekommt im Admin eine Teilmenge davon als
erlaubte Scopes; mehr kann sie weder im Namen einer Person noch als Dienst anfordern.

Ob ein Scope zu einem Ablauf passt, legt ``person`` bzw. ``service`` fest:

- ``person``: nutzbar im Code-Ablauf, also im Namen einer angemeldeten Person
- ``service``: nutzbar mit Client Credentials, also für einen Dienst ohne Person

Zwei Merkmale schränken weiter ein (Issue #565, Profil Nichtöffentliches):

- ``bound``: nur für Anwendungen, die an genau einen Bereich gebunden sind (``OAuthApplication.binding``, etwa einen
  Session-Mandanten). Was die Bindung bedeutet, wertet die Fachschicht aus.
- ``protected``: Tokens mit diesem Scope verlangen einen zweiten Faktor und gelten höchstens
  :data:`PROTECTED_MAX_LOGIN_AGE` nach der Anmeldung; danach gibt ein Refresh-Token keine neuen aus.

Das Modul hat keine Abhängigkeiten zu Django-Modellen, weil ``settings.py`` es liest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

OPENID = "openid"
PROFILE = "profile"
EMAIL = "email"
RATSDATEN_LESEN = "ratsdaten:lesen"
EINREICHUNG_SCHREIBEN = "einreichung:schreiben"
BEREITSTELLUNG = "mandari:bereitstellung"
#: Nichtöffentliche Ratsdaten im Namen einer Person (Profil Nichtöffentliches, Issue #565)
RATSDATEN_NICHTOEFFENTLICH = "ratsdaten:nichtoeffentlich"
#: Alle Sitzungsdaten des gebundenen Mandanten einschließlich Nichtöffentlichem, nur für gebundene Dienste (#565)
SITZUNG_LESEN = "sitzung:lesen"

#: Höchstens so lange nach der Anmeldung gibt es Tokens mit einem geschützten Scope (``Scope.protected``)
PROTECTED_MAX_LOGIN_AGE: Final = timedelta(days=7)


@dataclass(frozen=True)
class Scope:
    """Ein Scope des Katalogs mit dem Text, den die Zustimmungsseite zeigt."""

    name: str
    description: str
    person: bool
    service: bool
    #: nur für Anwendungen mit Bindung an genau einen Bereich (``OAuthApplication.binding``)
    bound: bool = False
    #: zweiter Faktor Pflicht, höchstens ``PROTECTED_MAX_LOGIN_AGE`` nach der Anmeldung
    protected: bool = False


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
    Scope(
        RATSDATEN_NICHTOEFFENTLICH,
        "Nichtöffentliche Sitzungen, Tagesordnungspunkte, Vorlagen und Anlagen lesen, soweit du sie selbst sehen darfst",
        True,
        False,
        protected=True,
    ),
    Scope(
        SITZUNG_LESEN,
        "Alle Sitzungsdaten des gebundenen Mandanten lesen, auch Nichtöffentliches (nur für gebundene Dienste)",
        False,
        True,
        bound=True,
    ),
)

#: Name → Beschreibung, wie django-oauth-toolkit es in ``SCOPES`` erwartet
DESCRIPTIONS: dict[str, str] = {scope.name: scope.description for scope in CATALOG}

_BY_NAME: dict[str, Scope] = {scope.name: scope for scope in CATALOG}


def get(name: str) -> Scope | None:
    return _BY_NAME.get(name)


def allowed_for(names: list[str] | tuple[str, ...], *, person: bool, bound: bool = False) -> list[str]:
    """
    Die Scopes aus ``names``, die es gibt und die zum Ablauf passen, in der Reihenfolge des Katalogs.

    ``bound``: Die Anwendung ist an einen Bereich gebunden; nur dann gelten Scopes mit ``Scope.bound``.
    """
    wanted = set(names)
    return [
        scope.name
        for scope in CATALOG
        if scope.name in wanted and (scope.person if person else scope.service) and (bound or not scope.bound)
    ]


#: Scopes mit erhöhtem Schutz (``Scope.protected``)
PROTECTED: Final[frozenset[str]] = frozenset(scope.name for scope in CATALOG if scope.protected)


def protected_in(names: object) -> bool:
    """Ist unter den Scopes (Liste oder durch Leerzeichen getrennt) einer mit erhöhtem Schutz?"""
    values = names.split() if isinstance(names, str) else list(names) if isinstance(names, list | tuple | set) else []
    return any(str(name) in PROTECTED for name in values)
