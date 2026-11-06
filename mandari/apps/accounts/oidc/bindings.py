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
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Art der Bindung an einen Session-Mandanten; Session meldet sie an (``apps.session.apps``)
TENANT: Final = "session-mandant"

#: Aufbau einer Bindung: Art (Kleinbuchstaben, Ziffern, Bindestrich), Doppelpunkt, Schlüssel
PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,39}:[A-Za-z0-9._-]{1,80}")


@dataclass(frozen=True)
class Kind:
    """Eine Art der Bindung, angemeldet von der Fachschicht, die sie auswertet."""

    name: str
    label: str
    resolve: Callable[[str], str | None]
    describe: Callable[[str], str | None]


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
