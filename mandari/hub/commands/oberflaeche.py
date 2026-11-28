# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle aus der eigenen Oberfläche (``docs/adr/20261128-api-first-ohne-ausnahme.md``).

Eine View liest nur ihr Formular ein und ruft den Befehl in einer Zeile auf; geprüft und geschrieben wird beim
Eigentümer, genau wie bei einem Aufruf über die Schnittstelle (derselbe Dispatcher, derselbe Handler)::

    ergebnis = ausfuehren(request, "session.textblock.create", inhalt, mandant=f"session:{tenant.pk}")
    if ergebnis.ok:
        messages.success(request, "Textbaustein angelegt.")
    else:
        messages.error(request, ergebnis.meldung)      # oder ergebnis.in_formular(form)

Module bieten dafür einen kurzen Helfer, der den Mandanten aus der Anfrage nimmt (``apps.session.befehle.ausfuehren``).

- **Auslöser** ist die angemeldete Person (``user:<id>``); Rechte prüft der Handler mit dem Rechtemodell seines Moduls.
- **Idempotenzschlüssel:** aus dem Formularfeld ``idempotency_key`` (``{% idempotenzschluessel %}`` aus
  ``apps/common/templatetags/befehle.py``), sonst ein neuer. Mit dem Feld wirkt ein doppelt abgeschicktes Formular nur
  einmal: Die zweite Anfrage erhält dieselbe Quittung. Das Feld gehört nur in Formulare, nach denen die Seite neu lädt.
- **Ergebnis** statt Ausnahme: ``Ergebnis.ok`` mit ``quittung`` oder ``problem`` (RFC 9457). ``meldung`` ist ein Satz
  für die Meldungen der Oberfläche, ``feldfehler`` ordnet die Fehler den Feldern des Inhalts zu, ``in_formular``
  überträgt sie in ein Django-Formular. Felder heißen wie im Vertrag, ihre Namen für Menschen stehen dort als
  ``title`` der Eigenschaft.
- ``dispatch()`` darf nicht in einer offenen Transaktion laufen (``NestedTransactionError``): Die View öffnet keine
  eigene um den Aufruf.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Final

from django.http import HttpRequest

from apps.common.api.idempotency import FORM_FIELD, valid_idempotency_key
from apps.common.observability import current_request_id
from apps.events.publishing import correlation_id_for_request
from hub.contracts import UnknownContractError

from .channel import OBERFLAECHE, Channel, channel
from .clients import CommandClient, InProcessClient
from .dispatcher import KEY_REUSED, get_dispatcher
from .problems import CONTRACT_VIOLATED, VALIDATION_KIND, CommandError, Problem
from .types import Command, Receipt

#: Formularfeld mit dem Idempotenzschlüssel (``{% idempotenzschluessel %}``)
IDEMPOTENCY_FIELD: Final = FORM_FIELD

#: Meldungen für die Oberfläche, wo das Problem für Aufrufer der Schnittstelle formuliert ist
RESENT: Final = "Dieses Formular wurde schon gesendet. Bitte die Seite neu laden und erneut versuchen."
TRY_LATER: Final = "Die Aktion konnte gerade nicht ausgeführt werden. Bitte später erneut versuchen."
CHECK_INPUT: Final = "Bitte die Angaben prüfen"
INVALID: Final = "ist ungültig"
MISSING: Final = "fehlt"
EXTRA: Final = "Das Formular enthält nicht vorgesehene Angaben."

#: Regel der Vertragsprüfung (``verletzt „…“``) -> Meldung am Feld
RULE_TEXTS: Final[dict[str, str]] = {
    "minLength": "ist leer oder zu kurz",
    "maxLength": "ist zu lang",
    "minimum": "ist zu klein",
    "exclusiveMinimum": "ist zu klein",
    "maximum": "ist zu groß",
    "exclusiveMaximum": "ist zu groß",
    "minItems": "hat zu wenige Einträge",
    "maxItems": "hat zu viele Einträge",
    "enum": "hat einen nicht vorgesehenen Wert",
    "const": "hat einen nicht vorgesehenen Wert",
    "type": INVALID,
    "format": INVALID,
    "pattern": INVALID,
}
_RULE = re.compile(r"verletzt „(?P<rule>[A-Za-z]+)“")
_REQUIRED = re.compile(r"Pflichtfeld fehlt \((?P<names>[^)]*)\)")
_EXTRA = re.compile(r"nicht vorgesehene Felder")
#: Fehler, der keinem Feld gehört
NON_FIELD: Final = ""


@dataclass(frozen=True)
class Ergebnis:
    """Ausgang eines Befehls aus der Oberfläche: Quittung oder Problem."""

    befehl: str
    version: int
    quittung: Receipt | None = None
    problem: Problem | None = None

    @property
    def ok(self) -> bool:
        return self.quittung is not None

    @cached_property
    def feldfehler(self) -> dict[str, list[str]]:
        """Fehler je Feld des Inhalts (erster Teil des JSON-Pointers); ``""`` für Fehler ohne Feld."""
        found: dict[str, list[str]] = defaultdict(list)
        for error in self.problem.errors if self.problem else ():
            pointer, detail = str(error.get("pointer", "")), str(error.get("detail", ""))
            if (required := _REQUIRED.fullmatch(detail)) is not None:
                for name in (part.strip() for part in required.group("names").split(",")):
                    found[_join(pointer, name)].append(MISSING)
                continue
            if _EXTRA.match(detail):
                found[NON_FIELD].append(EXTRA)
                continue
            rule = _RULE.fullmatch(detail)
            found[_field(pointer)].append(RULE_TEXTS.get(rule.group("rule"), INVALID) if rule else detail)
        return dict(found)

    @cached_property
    def labels(self) -> dict[str, str]:
        """Feld -> Name für Menschen (``title`` der Eigenschaft im Vertrag), soweit vorhanden."""
        try:
            schema = get_dispatcher().registry.schema(self.befehl, self.version)
        except UnknownContractError:
            return {}
        properties = schema.get("properties", {})
        return {name: str(sub["title"]) for name, sub in properties.items() if isinstance(sub, dict) and "title" in sub}

    def label(self, field: str) -> str:
        return self.labels.get(field, field)

    @property
    def meldung(self) -> str:
        """Ein Satz für die Meldungen der Oberfläche; leer bei Erfolg."""
        if self.problem is None:
            return ""
        problem = self.problem
        if problem.kind == "idempotenzschluessel-wiederverwendet" or problem.detail == KEY_REUSED:
            return RESENT
        if problem.status >= 500:
            return TRY_LATER
        if problem.kind != VALIDATION_KIND or not self.feldfehler:
            return problem.detail
        parts = [
            ("; ".join(texts) if field == NON_FIELD else f"{self.label(field)} {', '.join(texts)}")
            for field, texts in self.feldfehler.items()
        ]
        lead = CHECK_INPUT if problem.detail == CONTRACT_VIOLATED else problem.detail.rstrip(".")
        return f"{lead}: {'; '.join(parts)}."

    def in_formular(self, form: Any) -> None:
        """Fehler in ein Django-Formular übernehmen: an das Feld gleichen Namens, sonst als Fehler des Formulars."""
        if self.problem is None:
            return
        if self.problem.kind != VALIDATION_KIND or not self.feldfehler:
            form.add_error(None, self.meldung)
            return
        for field, texts in self.feldfehler.items():
            for text in texts:
                if field and field in form.fields:
                    form.add_error(field, f"{self.label(field)} {text}.")
                else:
                    form.add_error(None, text if not field else f"{self.label(field)} {text}.")


def ausfuehren(
    request: HttpRequest,
    name: str,
    inhalt: Mapping[str, Any],
    *,
    mandant: str,
    version: int = 1,
    anlagen: Mapping[str, Any] | None = None,
    client: CommandClient | None = None,
) -> Ergebnis:
    """
    Sendet den Befehl für die angemeldete Person an den Eigentümer und liefert Quittung oder Problem.

    ``mandant`` ist der Mandant wie in der Ereignishülle (``session:<uuid>``, ``org:<uuid>``). ``client`` ist sonst
    der ``InProcessClient``; derselbe Befehl über HTTP liefe durch denselben Dispatcher.
    """
    user = getattr(request, "user", None)
    actor = f"user:{user.pk}" if user is not None and user.is_authenticated else None
    command = Command(
        name=name,
        version=version,
        body=inhalt,
        idempotency_key=_key(request),
        tenant_ref=mandant,
        actor_ref=actor,
        correlation_id=_correlation(),
        attachments=anlagen or {},
    )
    try:
        with channel(Channel(OBERFLAECHE)):
            receipt = (client or InProcessClient()).send(command)
    except CommandError as exc:
        return Ergebnis(befehl=name, version=version, problem=exc.problem)
    return Ergebnis(befehl=name, version=version, quittung=receipt)


def _key(request: HttpRequest) -> str:
    """Schlüssel aus dem Formular (doppelt abgeschickt: dieselbe Quittung), sonst ein neuer."""
    raw = request.POST.get(IDEMPOTENCY_FIELD) if request.method == "POST" else None
    return raw if isinstance(raw, str) and valid_idempotency_key(raw) else str(uuid.uuid4())


def _correlation() -> uuid.UUID:
    """Korrelation mit der Anfrage (Protokoll und Ereignisse des Befehls), sonst neu."""
    request_id = current_request_id()
    return correlation_id_for_request(request_id) if request_id else uuid.uuid4()


def _field(pointer: str) -> str:
    """Erster Teil eines JSON-Pointers (``/title`` -> ``title``), unmaskiert nach RFC 6901."""
    first = pointer.lstrip("/").split("/", 1)[0] if pointer else ""
    return first.replace("~1", "/").replace("~0", "~")


def _join(pointer: str, name: str) -> str:
    """Feld eines fehlenden Pflichtfelds: am Inhalt selbst der Name, sonst der erste Teil des Pointers."""
    return name if not pointer else _field(pointer)
