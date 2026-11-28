# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle als Operationen der gemeinsamen Schnittstelle (``docs/adr/20261128-api-first-ohne-ausnahme.md``).

Ein Eigentümer gibt einen Befehl mit ``Exposure`` frei, wenn er den Handler registriert (``command_handler(…,
exposure=…)``, meist über den Helfer seines Moduls, etwa ``apps.session.befehle.befehl``). Ohne weiteren Code ist der
Befehl dann unter ``POST /api/mandari/v1/<bereich>/<mandant>/befehle/<befehl>/v<version>`` erreichbar und steht in der
OpenAPI-Beschreibung (``hub.api.befehle``):

- ``area``: der **Mandantenbereich** der Adresse. Er bildet den Kurznamen eines Mandanten in der Adresse auf seine
  Kennung ab (``session/<kurzname>`` -> ``session:<uuid>``). Bereiche meldet der Eigentümer beim Start an
  (``register_tenant_area``), denn die Drehscheibe kennt die Fachmodule nicht (A1).
- ``scopes``: Scopes des OpenID-Connect-Anbieters (``apps.accounts.oidc.catalog``), von denen ein Access-Token im Namen
  einer Person einen braucht. Die eigene Oberfläche ruft denselben Befehl mit der Browser-Sitzung (CSRF-Schutz) oder
  im selben Prozess (``hub.commands.oberflaeche.ausfuehren``).

Rechte prüft immer der Handler des Eigentümers mit dem Rechtemodell seines Moduls: Ein Scope öffnet nur den Weg, nie
mehr, als die Person selbst darf.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from django.core.exceptions import ImproperlyConfigured

#: Kennung eines Mandantenbereichs: zugleich Teil der Adresse
_AREA_KEY: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
#: Arten eines Mandanten in ``tenant_ref`` (wie in der Ereignishülle)
TENANT_KINDS: Final = ("session", "org", "source")

#: Kurzname -> Kennung eines aktiven Mandanten; ``None``, wenn es keinen gibt
Resolver = Callable[[str], uuid.UUID | None]


@dataclass(frozen=True)
class TenantArea:
    """Ein Mandantenbereich der Adresse, z. B. ``session`` für die Mandanten von Session."""

    #: Teil der Adresse, z. B. ``session``
    key: str
    #: Art in ``tenant_ref``, z. B. ``session`` oder ``org``
    kind: str
    #: Kurzname -> Kennung eines aktiven Mandanten
    resolve: Resolver
    #: Beschreibung des Kurznamens in der OpenAPI-Beschreibung
    description: str

    def __post_init__(self) -> None:
        if not _AREA_KEY.fullmatch(self.key):
            raise ValueError(f"Ungültige Kennung eines Mandantenbereichs: {self.key!r}")
        if self.kind not in TENANT_KINDS:
            raise ValueError(f"Unbekannte Art eines Mandanten: {self.kind!r}")

    def tenant_ref(self, slug: str) -> str | None:
        """``<art>:<uuid>`` des aktiven Mandanten mit diesem Kurznamen, sonst ``None``."""
        tenant_id = self.resolve(slug)
        return f"{self.kind}:{tenant_id}" if tenant_id is not None else None


@dataclass(frozen=True)
class Exposure:
    """Freigabe eines Befehls für die gemeinsame Schnittstelle."""

    #: Mandantenbereich der Adresse (``TenantArea.key``)
    area: str
    #: Scopes, von denen ein Access-Token im Namen einer Person einen braucht (mindestens einer)
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _AREA_KEY.fullmatch(self.area):
            raise ValueError(f"Ungültiger Mandantenbereich: {self.area!r}")
        if not self.scopes or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Eine Freigabe nennt mindestens einen Scope, jeden höchstens einmal")


def check_scopes(name: str, exposure: Exposure) -> None:
    """``ImproperlyConfigured``, wenn ein Scope nicht im Katalog steht oder nicht im Namen einer Person gilt."""
    from apps.accounts.oidc import catalog

    for scope_name in exposure.scopes:
        scope = catalog.get(scope_name)
        if scope is None or not scope.person:
            raise ImproperlyConfigured(
                f"Freigabe für {name}: Scope {scope_name!r} steht nicht im Katalog oder gilt nicht für Personen"
            )


_TENANT_AREAS: Final[dict[str, TenantArea]] = {}


def register_tenant_area(area: TenantArea) -> None:
    """Mandantenbereich anmelden (aus ``AppConfig.ready`` des Eigentümers); erneute Anmeldung ersetzt ihn."""
    _TENANT_AREAS[area.key] = area


def tenant_area(key: str) -> TenantArea | None:
    return _TENANT_AREAS.get(key)


def tenant_areas() -> list[TenantArea]:
    """Alle angemeldeten Mandantenbereiche, sortiert nach Kennung."""
    return sorted(_TENANT_AREAS.values(), key=lambda area: area.key)
