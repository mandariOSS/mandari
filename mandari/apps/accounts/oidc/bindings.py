# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bindung einer Anwendung an genau einen Bereich (Issue #565, Profil Nichtöffentliches), fachfrei.

Ein Dienst (Client Credentials) kann an genau einen Bereich gebunden sein, etwa an einen Session-Mandanten. Die
Plattform kennt keine Fachbegriffe und importiert keine Fachmodule (A1): Sie speichert an der Anwendung nur eine
Kennung ``<Art>:<Schlüssel>`` (``OAuthApplication.binding``). Was sie bedeutet und welche Daten der Dienst damit
liest, entscheidet die Fachschicht, die die Art anmeldet und auswertet.

Eine Fachschicht meldet ihre Art beim Start an (``AppConfig.ready``)::

    bindings.register(bindings.Kind(name=bindings.TENANT, label="Session-Mandant", resolve=…, describe=…))

- ``resolve(eingabe)``: der Schlüssel zu einer Eingabe auf der Kommandozeile (etwa dem Kurznamen eines Mandanten),
  ``None``, wenn es den Bereich nicht gibt
- ``describe(schlüssel)``: Anzeigename für Listen und Prüfungen, ``None``, wenn es den Bereich nicht (mehr) gibt

Eine Bindung an einen Bereich, den es nicht mehr gibt, gewährt nichts: Die Fachschicht findet ihn nicht.

Dieselbe Kennung nennt auch den Bereich, der eine Anwendung selbst registriert hat (``OAuthApplication.owner``,
Issue #1591): eine Kommune in Session, eine Fraktion in Work. Welche Scopes solche Anwendungen erhalten dürfen, meldet
die Fachschicht mit der Art an (``Kind.scopes``); ohne Scopes registriert die Art keine Anwendungen. Eine Anwendung
eines Bereichs gilt bei Ressourcen derselben Art nur für ihren Bereich (:func:`allows`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Art der Bindung an einen Session-Mandanten; Session meldet sie an (``apps.session.apps``)
TENANT: Final = "session-mandant"
#: Art einer Organisation in Work (Fraktion); Work meldet sie an (``apps.work.apps``, Issue #1591)
ORGANIZATION: Final = "work-organisation"

#: Aufbau einer Bindung: Art (Kleinbuchstaben, Ziffern, Bindestrich), Doppelpunkt, Schlüssel
PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,39}:[A-Za-z0-9._-]{1,80}")


@dataclass(frozen=True)
class Kind:
    """Eine Art der Bindung, angemeldet von der Fachschicht, die sie auswertet."""

    name: str
    label: str
    resolve: Callable[[str], str | None]
    describe: Callable[[str], str | None]
    #: Scopes, die Anwendungen erhalten dürfen, die ein Bereich dieser Art selbst registriert (#1591); leer: keine
    scopes: tuple[str, ...] = ()


_KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> None:
    """Art anmelden; eine erneute Anmeldung unter demselben Namen ersetzt sie."""
    _KINDS[kind.name] = kind


def kind_of(binding: str) -> Kind | None:
    """Die angemeldete Art einer Bindung, sonst ``None``."""
    name, _, _key = binding.partition(":")
    return _KINDS.get(name)


def compose(kind: str, key: str) -> str:
    """Bindung aus Art und Schlüssel."""
    return f"{kind}:{key}"


def key_of(binding: str, kind: str) -> str | None:
    """Schlüssel der Bindung, wenn sie von dieser Art ist, sonst ``None``."""
    name, separator, key = binding.partition(":")
    return key if separator and name == kind and key else None


def resolve(kind: str, value: str) -> str | None:
    """Bindung zu einer Eingabe (etwa dem Kurznamen eines Mandanten); ``None``, wenn es den Bereich nicht gibt."""
    registered = _KINDS.get(kind)
    if registered is None:
        return None
    key = registered.resolve(value.strip())
    return compose(kind, key) if key else None


def describe(binding: str) -> str | None:
    """Anzeigename des gebundenen Bereichs, ``None``, wenn es ihn nicht (mehr) gibt oder die Art unbekannt ist."""
    registered = kind_of(binding)
    if registered is None:
        return None
    _name, _separator, key = binding.partition(":")
    label = registered.describe(key)
    return f"{registered.label} {label}" if label else None


def registrable_scopes(owner: str) -> tuple[str, ...]:
    """Scopes, die ein Bereich für seine Anwendungen vergeben darf; leer, wenn die Art keine Anwendungen registriert."""
    registered = kind_of(owner)
    return registered.scopes if registered is not None else ()


def allows(owner: str, kind: str, key: str) -> bool:
    """
    Darf eine Anwendung des Bereichs ``owner`` auf den Bereich ``kind:key`` zugreifen?

    Ja ohne Eigentümer (Anwendungen des Betreibers) und bei Ressourcen einer anderen Art; bei derselben Art nur für den
    eigenen Bereich. Eine Anwendung einer Kommune liest so nur bei dieser Kommune, eine einer Fraktion nur bei dieser.
    """
    if not owner:
        return True
    name, _separator, owner_key = owner.partition(":")
    return name != kind or (bool(owner_key) and owner_key == key)


def problem(binding: str) -> str:
    """Grund, aus dem eine Bindung nicht taugt, sonst ``""``."""
    if not PATTERN.fullmatch(binding):
        return f"{binding}: erwartet wird <Art>:<Kennung>, etwa {TENANT}:<Kennung des Mandanten>."
    if kind_of(binding) is None:
        known = ", ".join(sorted(_KINDS)) or "keine"
        return f"{binding}: unbekannte Art der Bindung (angemeldet: {known})."
    if describe(binding) is None:
        return f"{binding}: Diesen Bereich gibt es nicht."
    return ""
