"""
Positivliste erlaubter KI-Endpunkte (Issue #950), eine Quelle für Anwendung und Ingestor.

Jeder Aufruf eines KI-Dienstes (Schreibhilfe und Co-Editor in Work, Zusammenfassung, Bürger-Chat und
Verortung im Bürgerportal, Texterkennung) geht nur an einen Host dieser Liste. Sie ist eine technische
Sperre neben dem Vertrag: Auch ein im Admin oder direkt in der Datenbank eingetragener Endpunkt wirkt nur,
wenn sein Host hier steht.

``KI_ERLAUBTE_HOSTS`` (kommagetrennte Hostnamen) ersetzt den Standard, wenn sie gesetzt ist; leer gilt der
Standard. Verglichen wird der Hostname exakt (ohne Groß-/Kleinschreibung und ohne Punkt am Ende), nie über
Präfix oder Suffix. Erlaubt sind nur ``https``-Adressen ohne Zugangsdaten in der Adresse und ohne anderen
Port als 443.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

#: Standard der Positivliste: STACKIT AI Model Serving (Rechenzentren in Deutschland)
STANDARD_ERLAUBTE_HOSTS: tuple[str, ...] = ("api.openai-compat.model-serving.eu01.onstackit.cloud",)

#: Name der Umgebungsvariable (Anwendung und Ingestor)
UMGEBUNGSVARIABLE = "KI_ERLAUBTE_HOSTS"

#: Gültiger Hostname nach der Normalisierung (Kleinbuchstaben, Ziffern, Bindestrich, Punkt)
_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def normalisiere_host(host: str) -> str:
    """Hostname zum Vergleich: ohne Leerraum, ohne Punkt am Ende, klein geschrieben."""
    return str(host).strip().rstrip(".").lower()


def erlaubte_hosts_aus_umgebung(wert: str | None = None) -> tuple[str, ...]:
    """
    Positivliste aus ``KI_ERLAUBTE_HOSTS`` (oder aus ``wert``, etwa aus den Einstellungen des Ingestors).

    Gesetzt ersetzt sie den Standard; leer oder nur aus Trennzeichen gilt ``STANDARD_ERLAUBTE_HOSTS``.
    """
    roh = os.environ.get(UMGEBUNGSVARIABLE, "") if wert is None else wert
    hosts = tuple(dict.fromkeys(host for host in (normalisiere_host(teil) for teil in roh.split(",")) if host))
    return hosts or STANDARD_ERLAUBTE_HOSTS


def gepruefter_host(url: str) -> str | None:
    """
    Hostname einer zulässigen Adresse, sonst ``None``.

    Zulässig ist nur ``https`` ohne Zugangsdaten (``user@host``), ohne Port außer 443 und mit einem
    gewöhnlichen Hostnamen. Leerraum, Steuerzeichen und Backslashes machen die Adresse unzulässig, damit
    kein anderer Parser einen anderen Host darin liest.
    """
    roh = str(url or "").strip()
    if not roh or any(zeichen.isspace() or ord(zeichen) < 0x20 or zeichen in "\\\x7f" for zeichen in roh):
        return None
    try:
        teile = urlsplit(roh)
        port = teile.port
    except ValueError:
        return None
    if teile.scheme.lower() != "https" or "@" in teile.netloc:
        return None
    if port not in (None, 443):
        return None
    host = normalisiere_host(teile.hostname or "")
    if not _HOSTNAME.match(host):
        return None
    return host


def ist_erlaubter_host(url: str, hosts: Iterable[str]) -> bool:
    """Steht der Host der Adresse exakt in der Positivliste ``hosts``? Unzulässige Adressen nie."""
    host = gepruefter_host(url)
    if host is None:
        return False
    return host in {normalisiere_host(eintrag) for eintrag in hosts if eintrag}
