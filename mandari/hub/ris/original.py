# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Das Original im Ratsinformationssystem der Kommune (Issue #1259): Wo steht ein Vorgang, eine Sitzung, ein Dokument
dort? Das Bürgerportal verlinkt die Adresse als „Im Ratsinformationssystem ansehen“; maßgeblich ist das Original.

- **OParl-Quellen** nennen die Seite eines Objekts im Attribut ``web`` (``raw_json``). Viele liefern es nicht, dann
  gibt es keinen Link. Eine Adresse wird nicht geraten.
- **Scraper-Quellen** (``scraper:*``): Die Kennung (``external_id``) ist die Adresse der Detailseite, mit https und nur
  den kennzeichnenden Parametern (``normalize_external_id`` in ``ingestor/src/scrapers/base.py``). Sie gilt, solange
  sie unter der Basisadresse der Quelle liegt (``sync_config["scraper"]["base_url"]``, deren Schema gilt); nach einem
  Umzug mit festgeschriebener Basis zeigt die Kennung auf die alte Adresse und gilt nicht mehr. Sitzungen nur mit
  gelesener Tagesordnung: Ohne sie ist ihre Seite im Ratsinformationssystem nicht öffentlich.
- **Dokumente:** die Abrufadresse der Quelle (OParl ``accessUrl``, sonst ``downloadUrl``).
- **mandari Session:** Für eigene Daten eines Session-Mandanten ist das Bürgerportal selbst das Original, kein Link.

Nur http(s)-Adressen; alles andere ergibt einen leeren Text.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from insight_core.models import SESSION_OPARL_MARKERS, OParlBody, OParlMeeting

_HTTP = ("https://", "http://")


def _http(adresse: object) -> bool:
    return isinstance(adresse, str) and adresse.startswith(_HTTP)


def _aus_session(external_id: str) -> bool:
    """Eigene Daten eines Session-Mandanten (wie ``withdrawn_by_publisher``)."""
    return bool(external_id) and all(marker in external_id for marker in SESSION_OPARL_MARKERS)


def _quelle(objekt: Any) -> Any:
    """``OParlSource`` der Kommune des Objekts; ``None`` für verwaiste Objekte."""
    try:
        return objekt.body.source
    except (AttributeError, OParlBody.DoesNotExist):
        return None


def _scraper_seite(external_id: str, quelle: Any) -> str:
    """Detailseite einer Scraper-Quelle aus ihrer Kennung, wenn diese unter der Basisadresse der Quelle liegt."""
    konfig = getattr(quelle, "sync_config", None)
    scraper = konfig.get("scraper") if isinstance(konfig, dict) else None
    basis = scraper.get("base_url") if isinstance(scraper, dict) else None
    if not isinstance(basis, str) or not _http(basis) or not _http(external_id) or "#" in external_id:
        return ""
    teile = urlsplit(basis)
    pfad = teile.path if teile.path.endswith("/") else f"{teile.path}/"
    # Wie normalize_external_id: https, Host in Kleinbuchstaben
    kanonisch = f"https://{teile.netloc.lower()}{pfad}"
    if not external_id.startswith(kanonisch) or len(external_id) == len(kanonisch):
        return ""
    return f"{teile.scheme}://{teile.netloc}{pfad}{external_id[len(kanonisch) :]}"


def seite_im_ris(objekt: Any, quelle: Any = None) -> str:
    """
    Seite eines Vorgangs oder einer Sitzung im Ratsinformationssystem der Kommune; leer, wenn die Quelle keine nennt.

    ``quelle`` ist die ``OParlSource`` der Kommune. Gebraucht wird sie nur, wenn ``web`` fehlt; ohne Angabe kommt sie
    aus ``objekt.body.source`` (eine Abfrage, sofern nicht vorgeladen).
    """
    external_id = str(getattr(objekt, "external_id", "") or "")
    if _aus_session(external_id):
        return ""
    roh = getattr(objekt, "raw_json", None)
    roh = roh if isinstance(roh, dict) else {}
    web = roh.get("web")
    if _http(web):
        return str(web)
    if quelle is None:
        quelle = _quelle(objekt)
    if quelle is None or not getattr(quelle, "is_scraper_source", False):
        return ""
    if isinstance(objekt, OParlMeeting) and not roh.get("agendaItem"):
        return ""
    return _scraper_seite(external_id, quelle)


def dokument_im_ris(datei: Any) -> str:
    """Abrufadresse eines Dokuments im Ratsinformationssystem (``access_url``, sonst ``download_url``); sonst leer."""
    if _aus_session(str(getattr(datei, "external_id", "") or "")):
        return ""
    for adresse in (getattr(datei, "access_url", None), getattr(datei, "download_url", None)):
        if _http(adresse):
            return str(adresse)
    return ""
