# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dispatcher für Befehle: ``dispatch(command) -> Receipt`` (``docs/adr/20260929-befehle-synchron.md``).

Ablauf je Befehl:

1. Idempotenzschlüssel vorhanden und gültig (sonst 400), Vertrag im Register (sonst 404), Handler
   beim Eigentümer registriert (sonst 501), Inhalt passt zum Schema (sonst 422 mit ``errors``).
2. In einer Transaktion: Schlüssel belegen (``apps.events.idempotency``), Handler ausführen,
   Quittung speichern. Der Handler schreibt die Fachdaten und veröffentlicht Ereignisse in dieser
   Transaktion. Scheitert er, rollt alles zurück, auch der Schlüssel.
3. Wiederholung mit gleichem Schlüssel und gleichem Befehl: dieselbe Quittung, ohne den Handler
   erneut auszuführen; gleicher Schlüssel mit anderem Befehl oder Inhalt: 422.

Handler registriert der Eigentümer, meist in ``<modul>/commands.py`` und geladen aus
``AppConfig.ready``:

    from hub.commands import HandlerResult, command_handler

    @command_handler("submission.submit")
    def submit(command: Command) -> HandlerResult:
        ...
        return HandlerResult(reference=antrag.reference, aggregate_id=antrag.id)

Der Handler muss im Paket liegen, das der Vertrag als ``x-owner`` nennt. Fachliche Ablehnungen meldet
er mit ``CommandError`` (z. B. 409); jede andere Ausnahme wird zu einem 500 mit festem Text. ``hub.commands``
enthält keine Fachregeln, nur Dispatcher, Quittung, Fehlerformat und Clients.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final, TypeVar

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.events.idempotency import MAX_KEY_LENGTH, IdempotencyConflictError, run_once
from hub.contracts import COMMAND, ContractViolationError, Registry, UnknownContractError, get_registry

from .canonical import content_hash
from .problems import CommandError, Problem, validation_problem
from .types import Command, HandlerResult, Receipt

logger = logging.getLogger("hub.commands")

Handler = Callable[[Command], HandlerResult]
H = TypeVar("H", bound=Handler)

#: Feste Meldungen (nie aus Ausnahmen, nie mit Werten aus dem Inhalt)
KEY_MISSING: Final = (
    "Idempotenzschlüssel fehlt oder ist ungültig (1 bis 255 sichtbare ASCII-Zeichen, ohne Anführungszeichen)."
)
UNKNOWN_COMMAND: Final = "Diesen Befehl in dieser Version gibt es nicht."
NOT_AVAILABLE: Final = "Diese Installation führt den Befehl nicht aus."
KEY_REUSED: Final = "Der Idempotenzschlüssel wurde schon für einen anderen Befehl oder Inhalt verwendet."
INTERNAL: Final = (
    "Der Befehl konnte nicht ausgeführt werden. Bitte später mit demselben Idempotenzschlüssel wiederholen."
)


def valid_idempotency_key(key: object) -> bool:
    """1 bis 255 sichtbare ASCII-Zeichen ohne Anführungszeichen und Rückstrich (IETF-Entwurf: String)."""
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_LENGTH
        and all(0x21 <= ord(char) <= 0x7E and char not in '"\\' for char in key)
    )


class Dispatcher:
    """Handler je (Befehl, Version) und das Register, gegen das Befehle geprüft werden."""

    def __init__(self, registry: Registry | Callable[[], Registry] = get_registry) -> None:
        self._registry = registry
        self._handlers: dict[tuple[str, int], Handler] = {}

    @property
    def registry(self) -> Registry:
        return self._registry if isinstance(self._registry, Registry) else self._registry()

    # --- Registrierung ------------------------------------------------------------------------

    def register(self, name: str, version: int, handler: Handler) -> None:
        """Handler eintragen; ``ImproperlyConfigured``, wenn Vertrag oder Eigentümer nicht passen."""
        try:
            contract = self.registry.get(name, version)
        except UnknownContractError:
            raise ImproperlyConfigured(f"Handler für {name} v{version}: kein Vertrag im Register") from None
        if contract.kind != COMMAND:
            raise ImproperlyConfigured(f"Handler für {name} v{version}: der Vertrag ist ein Ereignis")
        module = str(getattr(handler, "__module__", ""))
        if not (module == contract.owner or module.startswith(contract.owner + ".")):
            raise ImproperlyConfigured(
                f"Handler für {name} v{version} liegt in {module}, Eigentümer ist {contract.owner}"
            )
        existing = self._handlers.get((name, version))
        if existing is not None and existing is not handler:
            raise ImproperlyConfigured(f"Für {name} v{version} ist schon ein Handler registriert")
        self._handlers[(name, version)] = handler

    def handler(self, name: str, version: int = 1) -> Callable[[H], H]:
        """Dekorator für ``register``."""

        def decorate(function: H) -> H:
            self.register(name, version, function)
            return function

        return decorate

    def has_handler(self, name: str, version: int) -> bool:
        return (name, version) in self._handlers

    @property
    def handlers(self) -> Mapping[tuple[str, int], Handler]:
        """Registrierte Handler je (Befehl, Version), als Kopie."""
        return dict(self._handlers)

    # --- Ausführen -----------------------------------------------------------------------------

    def dispatch(self, command: Command) -> Receipt:
        """Führt den Befehl beim Eigentümer aus; ``CommandError`` mit Problem nach RFC 9457."""
        try:
            receipt, replayed = self._dispatch(command)
        except CommandError as exc:
            self._log(command, "abgelehnt", status=exc.problem.status, kind=exc.problem.kind)
            raise
        except Exception:
            # Details nur ins Log; der Aufrufer bekommt einen festen Text.
            logger.exception(
                "Befehl %s v%s gescheitert (correlation_id=%s)", command.name, command.version, command.correlation_id
            )
            raise CommandError(Problem(status=500, kind="interner-fehler", detail=INTERNAL)) from None
        self._log(command, "wiederholt" if replayed else "ausgeführt")
        return receipt

    def _dispatch(self, command: Command) -> tuple[Receipt, bool]:
        if not valid_idempotency_key(command.idempotency_key):
            raise CommandError.of(400, "idempotenzschluessel-fehlt", KEY_MISSING)
        name, version = command.name, command.version
        registry = self.registry
        try:
            contract = registry.get(name, version)
        except UnknownContractError:
            raise CommandError.of(404, "befehl-unbekannt", UNKNOWN_COMMAND) from None
        if contract.kind != COMMAND:
            raise CommandError.of(404, "befehl-unbekannt", UNKNOWN_COMMAND)
        handler = self._handlers.get((name, version))
        if handler is None:
            raise CommandError.of(501, "befehl-nicht-verfuegbar", NOT_AVAILABLE)
        try:
            registry.validate_command(name, version, command.body)
        except ContractViolationError as exc:
            raise CommandError(validation_problem(exc.problems)) from None

        body_hash = content_hash(command.body)
        request_hash = content_hash({"command": name, "version": version, "content_hash": body_hash})
        received_at = timezone.now()

        def execute() -> dict[str, Any]:
            result = handler(command)
            if not isinstance(result, HandlerResult):
                raise TypeError("Handler müssen ein HandlerResult liefern")
            return Receipt(
                command=name,
                version=version,
                reference=result.reference,
                received_at=result.received_at or received_at,
                content_hash=body_hash,
                aggregate_id=result.aggregate_id,
                data=result.data,
            ).to_dict()

        try:
            stored, replayed = run_once(scope(command), command.idempotency_key, request_hash, execute, label=name)
        except IdempotencyConflictError:
            raise CommandError.of(422, "idempotenzschluessel-wiederverwendet", KEY_REUSED) from None
        return Receipt.from_dict(stored), replayed

    def _log(self, command: Command, outcome: str, **extra: Any) -> None:
        # Nie den Inhalt loggen: nur Name, Version, Mandant und Kennungen.
        logger.info(
            "Befehl %s v%s %s",
            command.name,
            command.version,
            outcome,
            extra={
                "command": command.name,
                "command_version": command.version,
                "tenant_ref": command.tenant_ref,
                "correlation_id": str(command.correlation_id),
                **extra,
            },
        )


def scope(command: Command) -> str:
    """Bereich des Idempotenzschlüssels: Mandant und Auslöser (Schlüssel fremder Auslöser kollidieren nie)."""
    return f"{command.tenant_ref} {command.actor_ref or '-'}"


#: Dispatcher der Installation; Eigentümer registrieren ihre Handler hier.
_DEFAULT = Dispatcher()


def get_dispatcher() -> Dispatcher:
    return _DEFAULT


def command_handler(name: str, version: int = 1) -> Callable[[H], H]:
    """Registriert einen Handler beim Dispatcher der Installation (siehe Moduldokumentation)."""
    return get_dispatcher().handler(name, version)


def dispatch(command: Command) -> Receipt:
    """Führt einen Befehl über den Dispatcher der Installation aus."""
    return get_dispatcher().dispatch(command)
