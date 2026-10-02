# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanonische Kennungen für Objekte des RIS-Bestands.

Architekturentscheidung: ``docs/adr/20260929-kanonisches-modell.md``.

Jedes RIS-Objekt trägt die Kennung ``uuid5(NS_MANDARI_RIS, kanonische URI)``:

- **Fremd-RIS:** Die URI ist die ``id`` des Objekts in der Quelle, genau so, wie die Quelle sie liefert.
  Die Funktion normalisiert nichts; jede Umformung würde bestehende Kennungen verändern.
- **Eigene Session-Objekte:** Die URI ist die öffentliche OParl-URL auf Basis der **festgeschriebenen
  Basisadresse** der Installation (einmal je Installation gespeichert, anfangs gleich ``SITE_URL``), nicht
  des Hosts der Anfrage und nicht der aktuellen Domain.

**Adresse und Kennung.** Die Adresse eines Objekts ist die URL, unter der seine Quelle es heute ausgibt; sie
folgt der Domain. Die Kennung folgt der festgeschriebenen Basis. Zieht eine Quelle um (eigene Installation auf
eine neue Domain), ersetzt :func:`canonical_uri` den Präfix der Adresse durch die Basis; die Kennung bleibt.
Ohne Umzug ist die kanonische URI die Adresse selbst.

**Umzug mit neuer Form der Adressen.** Ändert eine Quelle beim Umzug auch den Aufbau ihrer Adressen
(ALLRIS: ``…/public/oparl/papers?id=5`` wird ``…/oparl/papers/5``), bilden Abbildungsregeln
(``sync_config["id_rules"]``) den Rest der Adresse hinter dem Präfix auf die bisherige Form ab. Es gilt die
erste Regel, deren Muster den ganzen Rest erfasst; ohne passende Regel ist die Adresse selbst kanonisch
(neue Objekte, oder Objekte, deren bisherige Adresse sich aus der neuen nicht ableiten lässt).

Ingestor und Django verwenden ausschließlich diese Funktionen. Gleiche URIs ergeben überall dieselbe
Kennung, auch über Installationen hinweg. Beide Testsuiten prüfen dieselben Testvektoren
(``ids_testvektoren.json`` in diesem Paket).

Namensraum: ``NS_MANDARI_RIS`` ist der URL-Namensraum aus RFC 9562 (``uuid.NAMESPACE_URL``), denn die
kanonische URI ist eine URL. Der Ingestor vergibt seine Kennungen von Beginn an so; die Kennungen des
Bestands aus Fremd-RIS sind damit bereits kanonisch. Der Namensraum darf sich nie ändern: Jede Änderung
würde alle Kennungen, Links und Suchindex-Einträge ungültig machen.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import NAMESPACE_URL, UUID, uuid5

#: Namensraum der kanonischen RIS-Kennungen (RFC 9562, URL-Namensraum). Unveränderlich.
NS_MANDARI_RIS: UUID = NAMESPACE_URL

#: Schlüssel in der Konfiguration einer Quelle (``OParlSource.sync_config``): festgeschriebene Basis der Kennungen
#: für alle Adressen unter der URL der Quelle. Fehlt er, sind ihre Adressen kanonisch.
SOURCE_ID_BASE_KEY = "id_base"

#: Schlüssel: Präfix der heutigen Adressen, falls die Objekte nicht unter der URL der Quelle liegen (ALLRIS:
#: Quelle ``…/oparl/system``, Objekte ``…/oparl/papers/5``). Fehlt er, gilt die URL der Quelle.
SOURCE_ID_ADDRESS_KEY = "id_address"

#: Schlüssel: Abbildungsregeln ``[[muster, ersatz], …]`` vom Rest der heutigen Adresse auf den Rest der
#: bisherigen (``re.fullmatch`` bzw. ``Match.expand``, Gruppen als ``\1`` oder ``\g<1>``; in JSON ``"\\1"``).
SOURCE_ID_RULES_KEY = "id_rules"

#: Höchstzahl der Regeln je Quelle und Höchstlänge von Muster und Ersatz (Schutz vor Fehleingaben)
MAX_ID_RULES = 50
MAX_ID_RULE_LENGTH = 300

#: Regel als (Muster, Ersatz)
IdRule = tuple[str, str]


def canonical_id(uri: str) -> UUID:
    """
    Kanonische Kennung eines RIS-Objekts: ``uuid5(NS_MANDARI_RIS, uri)``.

    Die URI geht unverändert ein (keine Normalisierung von Schema, Host, Groß-/Kleinschreibung oder
    abschließendem Schrägstrich). Leere URIs ergeben keine sinnvolle Kennung; Aufrufer prüfen das vorher.
    """
    return uuid5(NS_MANDARI_RIS, uri)


def _prefix(value: str) -> str:
    """Präfix mit abschließendem Schrägstrich: ``https://a.example`` erfasst nicht ``https://a.example.org/…``."""
    return value if value.endswith("/") else f"{value}/"


def id_rules(value: object) -> tuple[IdRule, ...]:
    """
    Abbildungsregeln prüfen und vereinheitlichen; leer, wenn keine angegeben sind (``None``, leere Liste).

    Raises:
        ValueError: Die Angabe ist keine Liste von Paaren aus Zeichenketten, ein Muster ist kein gültiger
            regulärer Ausdruck, oder der Ersatz ist ungültig (z. B. Bezug auf eine Gruppe, die das Muster
            nicht hat). Ungültige
            Regeln werden nicht stillschweigend übergangen: Sonst bekämen Objekte andere Kennungen als
            vorgesehen.
    """
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError("Abbildungsregeln: Liste von Paaren [muster, ersatz] erwartet.")
    if len(value) > MAX_ID_RULES:
        raise ValueError(f"Abbildungsregeln: höchstens {MAX_ID_RULES} Regeln.")
    rules: list[IdRule] = []
    for number, rule in enumerate(value, start=1):
        if (
            isinstance(rule, str | bytes)
            or not isinstance(rule, Sequence)
            or len(rule) != 2
            or not all(isinstance(part, str) and part for part in rule)
        ):
            raise ValueError(
                f"Abbildungsregel {number}: Paar [muster, ersatz] aus nicht leeren Zeichenketten erwartet."
            )
        pattern, template = rule[0], rule[1]
        if len(pattern) > MAX_ID_RULE_LENGTH or len(template) > MAX_ID_RULE_LENGTH:
            raise ValueError(f"Abbildungsregel {number}: Muster oder Ersatz zu lang.")
        _compile_rule(number, pattern, template)
        rules.append((pattern, template))
    return tuple(rules)


def _compile_rule(number: int, pattern: str, template: str) -> re.Pattern[str]:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Abbildungsregel {number}: Muster ungültig ({exc}).") from None
    try:
        # Prüft den Ersatz vollständig (Escapes, Gruppenbezüge), auch ohne Treffer
        compiled.sub(template, "")
    except (re.error, IndexError) as exc:
        raise ValueError(f"Abbildungsregel {number}: Ersatz ungültig ({exc}).") from None
    return compiled


@lru_cache(maxsize=64)
def _compiled_rules(rules: tuple[IdRule, ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((_compile_rule(n, pattern, template), template) for n, (pattern, template) in enumerate(rules, 1))


def _apply_rules(rest: str, rules: tuple[IdRule, ...]) -> str | None:
    """Rest der bisherigen Adresse nach der ersten passenden Regel; ``None``, wenn keine passt."""
    for pattern, template in _compiled_rules(rules):
        match = pattern.fullmatch(rest)
        if match is not None:
            return match.expand(template)
    return None


def canonical_uri(uri: str, address: str, base: str, rules: Sequence[IdRule] | None = None) -> str:
    """
    Kanonische URI zu einer Adresse.

    ``address`` ist die Adresse, unter der eine Quelle ihre Objekte heute ausgibt, ``base`` die
    festgeschriebene Basis ihrer Kennungen (beides Präfixe; ein fehlender abschließender Schrägstrich wird
    ergänzt). Liegt ``uri`` unter ``address``, tritt ``base`` an dessen Stelle. Sonst – und ohne Umzug
    (``address == base``) oder ohne Angaben – bleibt ``uri`` unverändert.

    Mit ``rules`` (Abbildungsregeln, siehe :func:`id_rules`) wird zusätzlich der Rest hinter ``address``
    umgeformt; ohne passende Regel bleibt ``uri`` unverändert. Ohne ``base`` gilt dann ``address`` als Basis
    (gleicher Präfix, andere Form).
    """
    return _canonical_uri(uri, address, base, id_rules(rules) if rules else ())


def _canonical_uri(uri: str, address: str, base: str, rules: tuple[IdRule, ...]) -> str:
    """:func:`canonical_uri` mit bereits geprüften Regeln."""
    if not address or not (base or rules):
        return uri
    address = _prefix(address)
    base = _prefix(base) if base else address
    if not uri.startswith(address):
        return uri
    if rules:
        rest = _apply_rules(uri[len(address) :], rules)
        return uri if rest is None else f"{base}{rest}"
    if address == base:
        return uri
    return f"{base}{uri[len(address) :]}"


def source_id_base(sync_config: object) -> str:
    """Festgeschriebene Basis der Kennungen aus der Konfiguration einer Quelle; leer, wenn keine festgelegt ist."""
    if isinstance(sync_config, Mapping):
        value = sync_config.get(SOURCE_ID_BASE_KEY)
        if isinstance(value, str):
            return value
    return ""


def source_id_address(sync_config: object) -> str:
    """Präfix der heutigen Adressen aus der Konfiguration einer Quelle; leer = URL der Quelle."""
    if isinstance(sync_config, Mapping):
        value = sync_config.get(SOURCE_ID_ADDRESS_KEY)
        if isinstance(value, str):
            return value
    return ""


def source_id_rules(sync_config: object) -> tuple[IdRule, ...]:
    """Abbildungsregeln aus der Konfiguration einer Quelle (:func:`id_rules`; ``ValueError`` bei ungültigen)."""
    if isinstance(sync_config, Mapping):
        return id_rules(sync_config.get(SOURCE_ID_RULES_KEY))
    return ()


@dataclass(frozen=True)
class _Entry:
    base: str
    rules: tuple[IdRule, ...] = field(default=())


class IdBases:
    """
    Festgeschriebene Basen der Kennungen umgezogener Quellen: Adresse (Präfix) -> Basis (Präfix) und Regeln.

    Für Abgleiche über mehrere Quellen (Ingestor): Jede Quelle trägt höchstens einen Eintrag, ihre Präfixe
    überschneiden sich nicht; bei verschachtelten Präfixen gilt der längste. Quellen ohne Umzug brauchen
    keinen Eintrag, ihre Adressen sind bereits kanonisch.
    """

    def __init__(self, bases: Mapping[str, str] | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._order: list[str] = []
        for address, base in (bases or {}).items():
            self.add(address, base)

    @classmethod
    def for_source(cls, url: str, sync_config: object) -> "IdBases":
        """Kennungen der Objekte einer Quelle (:meth:`add_source`)."""
        bases = cls()
        bases.add_source(url, sync_config)
        return bases

    def add_source(self, url: str, sync_config: object) -> bool:
        """
        Eintrag einer Quelle aus ihrer Konfiguration (``id_address``, ``id_base``, ``id_rules``).

        Raises:
            ValueError: Die Abbildungsregeln der Quelle sind ungültig.
        """
        address = source_id_address(sync_config) or url
        return self.add(address, source_id_base(sync_config), source_id_rules(sync_config))

    def add(self, address: str, base: str, rules: Sequence[IdRule] | None = None) -> bool:
        """Basis (und Regeln) für die Adressen unter ``address`` festhalten; ``True``, wenn sich dadurch etwas ändert."""
        if not address:
            return False
        checked = id_rules(rules) if rules else ()
        address = _prefix(address)
        base = _prefix(base) if base else address
        if address == base and not checked:
            changed = self._entries.pop(address, None) is not None
        else:
            entry = _Entry(base, checked)
            changed = self._entries.get(address) != entry
            self._entries[address] = entry
        if changed:
            self._order = sorted(self._entries, key=len, reverse=True)
        return changed

    def uri(self, uri: str) -> str:
        """Kanonische URI (:func:`canonical_uri` mit dem passenden Eintrag)."""
        for address in self._order:
            if uri.startswith(address):
                entry = self._entries[address]
                return _canonical_uri(uri, address, entry.base, entry.rules)
        return uri

    def id(self, uri: str) -> UUID:
        """Kanonische Kennung einer Adresse: ``canonical_id(self.uri(uri))``."""
        return canonical_id(self.uri(uri))

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __repr__(self) -> str:
        return f"IdBases({self._entries!r})"
