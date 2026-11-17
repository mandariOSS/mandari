# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Was ist neu?“ in Work und der Hinweis auf die laufende Version (Issue #1463).

Wie im Bürgerportal (``insight_core/services/neuigkeiten.py``, Issue #1261) sind die Neuigkeiten kuratiert: kurze
Einträge in der Sprache der Menschen, die mit Work arbeiten, aus ``apps/work/neuigkeiten.toml``, nicht die technische
CHANGELOG.md. Gelesen wird die Datei mit dem Leser des Bürgerportals (gleiche Felder, gleiche Prüfungen), beim ersten
Aufruf und dann bis zum Neustart aus dem Speicher. Ein Fehler in der Datei bricht keine Seite: Die Liste bleibt leer,
der Fehler steht im Protokoll.

Die Seitenleiste nennt die Version der Installation und zeigt einen Punkt, solange die Person die neueste Neuigkeit
noch nicht gesehen hat. Was sie gesehen hat, steht in ``User.settings`` (gilt für die Person in allen Organisationen,
wie beim Hinweisband auf Start, ``apps/work/dashboard/hinweise.py``); eine eigene Tabelle braucht es dafür nicht.
"""

from __future__ import annotations

import logging
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

from insight_core.services import neuigkeiten as portal

logger = logging.getLogger(__name__)

DATEI = Path(__file__).resolve().parent / "neuigkeiten.toml"
#: Schlüssel in ``User.settings``: Kennung der neuesten Neuigkeit, die die Person gesehen hat (:func:`stand`)
GESEHEN_SCHLUESSEL = "work_neuigkeiten_gesehen"


@cache
def neuigkeiten() -> tuple[portal.Neuigkeit, ...]:
    """Alle Einträge, neueste zuerst; leer, wenn die Datei fehlt oder fehlerhaft ist."""
    try:
        return tuple(portal.lies(DATEI))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        logger.exception("Neuigkeiten in Work nicht lesbar (%s)", DATEI)
        return ()


def gruppen() -> list[portal.Gruppe]:
    """Einträge je Tag und Version, neueste zuerst."""
    return portal.gruppiere(neuigkeiten())


def stand() -> str:
    """
    Kennung der neuesten Neuigkeit (Tag und Titel); leer ohne Einträge.

    Der Tag allein genügte nicht: Kommt am selben Tag ein weiterer Eintrag dazu, soll der Punkt wieder erscheinen.
    """
    eintraege = neuigkeiten()
    if not eintraege:
        return ""
    return f"{eintraege[0].datum.isoformat()}|{eintraege[0].titel}"


def _gesehen(user: Any) -> Any:
    einstellungen = getattr(user, "settings", None)
    return einstellungen.get(GESEHEN_SCHLUESSEL) if isinstance(einstellungen, dict) else None


def ungesehen(user: Any) -> bool:
    """Gibt es eine Neuigkeit, die die angemeldete Person noch nicht gesehen hat? Ohne Abfrage."""
    aktuell = stand()
    if not aktuell or not getattr(user, "is_authenticated", False):
        return False
    return bool(_gesehen(user) != aktuell)


def als_gesehen_merken(user: Any) -> None:
    """Die neueste Neuigkeit als gesehen merken; schreibt nur, wenn sich der Stand ändert."""
    aktuell = stand()
    if not aktuell or _gesehen(user) == aktuell:
        return
    # Frisch lesen: Andere Angaben in den Einstellungen (zurückgestellte Hinweise) dürfen nicht verloren gehen
    user.refresh_from_db(fields=["settings"])
    einstellungen = dict(user.settings or {})
    einstellungen[GESEHEN_SCHLUESSEL] = aktuell
    user.settings = einstellungen
    user.save(update_fields=["settings"])
