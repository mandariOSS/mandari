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

Ingestor und Django verwenden ausschließlich diese Funktionen. Gleiche URIs ergeben überall dieselbe
Kennung, auch über Installationen hinweg. Beide Testsuiten prüfen dieselben Testvektoren
(``ids_testvektoren.json`` in diesem Paket).

Namensraum: ``NS_MANDARI_RIS`` ist der URL-Namensraum aus RFC 9562 (``uuid.NAMESPACE_URL``), denn die
kanonische URI ist eine URL. Der Ingestor vergibt seine Kennungen von Beginn an so; die Kennungen des
Bestands aus Fremd-RIS sind damit bereits kanonisch. Der Namensraum darf sich nie ändern: Jede Änderung
würde alle Kennungen, Links und Suchindex-Einträge ungültig machen.
"""

from collections.abc import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

#: Namensraum der kanonischen RIS-Kennungen (RFC 9562, URL-Namensraum). Unveränderlich.
NS_MANDARI_RIS: UUID = NAMESPACE_URL

#: Schlüssel in der Konfiguration einer Quelle (``OParlSource.sync_config``): festgeschriebene Basis der Kennungen
#: für alle Adressen unter der URL der Quelle. Fehlt er, sind ihre Adressen kanonisch.
SOURCE_ID_BASE_KEY = "id_base"


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


def canonical_uri(uri: str, address: str, base: str) -> str:
    """
    Kanonische URI zu einer Adresse.

    ``address`` ist die Adresse, unter der eine Quelle ihre Objekte heute ausgibt, ``base`` die
    festgeschriebene Basis ihrer Kennungen (beides Präfixe; ein fehlender abschließender Schrägstrich wird
    ergänzt). Liegt ``uri`` unter ``address``, tritt ``base`` an dessen Stelle. Sonst – und ohne Umzug
    (``address == base``) oder ohne Angaben – bleibt ``uri`` unverändert.
    """
    if not address or not base:
        return uri
    address, base = _prefix(address), _prefix(base)
    if address == base or not uri.startswith(address):
        return uri
    return f"{base}{uri[len(address) :]}"


def source_id_base(sync_config: object) -> str:
    """Festgeschriebene Basis der Kennungen aus der Konfiguration einer Quelle; leer, wenn keine festgelegt ist."""
    if isinstance(sync_config, Mapping):
        value = sync_config.get(SOURCE_ID_BASE_KEY)
        if isinstance(value, str):
            return value
    return ""


class IdBases:
    """
    Festgeschriebene Basen der Kennungen umgezogener Quellen: Adresse (Präfix) -> Basis (Präfix).

    Für Abgleiche über mehrere Quellen (Ingestor): Jede Quelle trägt höchstens einen Eintrag, ihre Präfixe
    überschneiden sich nicht; bei verschachtelten Präfixen gilt der längste. Quellen ohne Umzug brauchen
    keinen Eintrag, ihre Adressen sind bereits kanonisch.
    """

    def __init__(self, bases: Mapping[str, str] | None = None) -> None:
        self._bases: dict[str, str] = {}
        self._order: list[str] = []
        for address, base in (bases or {}).items():
            self.add(address, base)

    def add(self, address: str, base: str) -> bool:
        """Basis für die Adressen unter ``address`` festhalten; ``True``, wenn sich dadurch etwas ändert."""
        if not address:
            return False
        address = _prefix(address)
        base = _prefix(base) if base else address
        if address == base:
            changed = self._bases.pop(address, None) is not None
        elif self._bases.get(address) == base:
            changed = False
        else:
            self._bases[address] = base
            changed = True
        if changed:
            self._order = sorted(self._bases, key=len, reverse=True)
        return changed

    def uri(self, uri: str) -> str:
        """Kanonische URI (:func:`canonical_uri` mit dem passenden Eintrag)."""
        for address in self._order:
            if uri.startswith(address):
                return canonical_uri(uri, address, self._bases[address])
        return uri

    def id(self, uri: str) -> UUID:
        """Kanonische Kennung einer Adresse: ``canonical_id(self.uri(uri))``."""
        return canonical_id(self.uri(uri))

    def __bool__(self) -> bool:
        return bool(self._bases)

    def __repr__(self) -> str:
        return f"IdBases({self._bases!r})"
