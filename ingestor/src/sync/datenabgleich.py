# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenabgleich mit der Schnittstelle der Kommune (Issue #1525): ``totalElements`` der Hauptlisten gegen den Bestand.

Je Kommune einer Quelle holt der Abgleich die **erste Seite** der Listen Vorlagen, Sitzungen, Beratungen, TOPs und
Dateien (Adressen aus dem Body der Kommune, ohne Filter) und vergleicht ``pagination.totalElements`` mit der Zahl der
nicht gelöschten Objekte im Bestand. Das sind höchstens fünf Abrufe je Kommune, ein Versuch je Liste (keine
Wiederholung), über den ``OParlClient`` des Ingestors mit allen seinen Regeln: Takt je Server über alle Prozesse,
robots.txt (samt Ausnahme der Quelle), User-Agent der Quelle, Schutzschalter. Dazu:

- Abruffenster der Quelle (``sync_config["sync_window"]``, Issue #1186): außerhalb kein Abruf; im Fenster endet der
  Abgleich mit dem Fenster.
- Ausgelassen werden Listen aus ``skip_lists`` und als fehlend vermerkte Listen (``missing_lists``), Quellen in der
  Schonung nach Fehlversuchen und Scraper-Quellen (sie haben keine OParl-Listen).
- Takt: Der Daemon gleicht eine Quelle nach einem erfolgreichen Lauf ab, wenn ihr letzter Abgleich länger als
  ``ABSTAND`` zurückliegt (``faellig``), also höchstens einmal täglich. Von Hand: ``python -m src.main datenabgleich``.

Das Ergebnis steht je Kommune in ``sync_config["datenabgleich_quelle"]`` der Quelle (unter Zeilensperre). Bewertung
und Anzeige übernimmt die Anwendung (``mandari/insight_core/services/datenabgleich.py``, Betriebsmonitor, Alarm).
Server ohne ``totalElements`` (die Angabe ist in OParl optional) bekommen nur den Bestand, ohne Bewertung.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import text

from src.client.oparl_client import OParlClient
from src.client.source_options import SourceFetchOptions

logger = logging.getLogger(__name__)

#: Schlüssel in ``OParlSource.sync_config`` (Kennung der Kommune -> Befund), liest die Anwendung
SCHLUESSEL: Final = "datenabgleich_quelle"
#: Abstand zwischen zwei Abgleichen einer Quelle im Daemon (knapp ein Tag, damit der Termin nicht täglich wandert)
ABSTAND: Final = timedelta(hours=23)
#: Höchstdauer eines Abgleichs im Daemon; danach endet er, das Ergebnis bis dahin zählt nicht
FRIST_SEKUNDEN: Final = 15 * 60
#: Listen am Body (Typ wie in ``skip_lists``, Schlüssel im Body; einige Server nennen sie in der Mehrzahl)
LISTEN: Final[dict[str, tuple[str, ...]]] = {
    "paper": ("paper",),
    "meeting": ("meeting",),
    "consultation": ("consultation", "consultations"),
    "agendaitem": ("agendaItem", "agendaItems"),
    "file": ("file", "files"),
}

#: Bestand je Kommune und Liste (TOPs über ihre Sitzung): nicht gelöschte Objekte und alle samt Löschmarkierung. Ob
#: ``totalElements`` gelöschte Objekte mitzählt, legt OParl nicht fest; die Anwendung vergleicht mit beiden Zahlen
_BESTAND_SQL: Final = text(
    """
    SELECT
      (SELECT count(*) FILTER (WHERE NOT deleted) FROM oparl_papers WHERE body_id = :body) AS paper,
      (SELECT count(*) FROM oparl_papers WHERE body_id = :body) AS paper_alle,
      (SELECT count(*) FILTER (WHERE NOT deleted) FROM oparl_meetings WHERE body_id = :body) AS meeting,
      (SELECT count(*) FROM oparl_meetings WHERE body_id = :body) AS meeting_alle,
      (SELECT count(*) FILTER (WHERE NOT deleted) FROM oparl_consultations WHERE body_id = :body) AS consultation,
      (SELECT count(*) FROM oparl_consultations WHERE body_id = :body) AS consultation_alle,
      (SELECT count(*) FILTER (WHERE NOT a.deleted) FROM oparl_agenda_items a JOIN oparl_meetings m
         ON m.id = a.meeting_id WHERE m.body_id = :body) AS agendaitem,
      (SELECT count(*) FROM oparl_agenda_items a JOIN oparl_meetings m ON m.id = a.meeting_id
         WHERE m.body_id = :body) AS agendaitem_alle,
      (SELECT count(*) FILTER (WHERE NOT deleted) FROM oparl_files WHERE body_id = :body) AS file,
      (SELECT count(*) FROM oparl_files WHERE body_id = :body) AS file_alle
    """
)
_BODIES_SQL: Final = text(
    "SELECT id, external_id, name, raw_json FROM oparl_bodies WHERE source_id = :source AND NOT deleted ORDER BY name"
)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def letzter_abgleich(sync_config: Any) -> datetime | None:
    """Ältester Zeitpunkt der abgelegten Abgleiche der Quelle (``None``: noch keiner oder unvollständig)."""
    stored = sync_config.get(SCHLUESSEL) if isinstance(sync_config, Mapping) else None
    if not isinstance(stored, Mapping) or not stored:
        return None
    stamps = [_stamp(entry.get("at")) if isinstance(entry, Mapping) else None for entry in stored.values()]
    if any(stamp is None for stamp in stamps):
        return None
    return min(stamp for stamp in stamps if stamp is not None)


def ist_scraper(sync_config: Any) -> bool:
    return isinstance(sync_config, Mapping) and str(sync_config.get("source_type") or "").startswith("scraper:")


def faellig(source: Any, now: datetime | None = None) -> bool:
    """Ist der tägliche Abgleich der Quelle fällig (aktiv, OParl, letzter Abgleich älter als ``ABSTAND``)?"""
    sync_config = getattr(source, "sync_config", None)
    if not getattr(source, "is_active", False) or ist_scraper(sync_config):
        return False
    letzter = letzter_abgleich(sync_config)
    return letzter is None or (now or datetime.now(UTC)) - letzter >= ABSTAND


def listen_der_kommune(raw_json: Any) -> dict[str, str]:
    """Adressen der Hauptlisten am Body (Typ -> Adresse)."""
    raw = raw_json if isinstance(raw_json, Mapping) else {}
    listen: dict[str, str] = {}
    for typ, schluessel in LISTEN.items():
        for name in schluessel:
            wert = raw.get(name)
            if isinstance(wert, str) and wert.strip():
                listen[typ] = wert.strip()
                break
    return listen


def total_elements(data: Any) -> int | None:
    """``pagination.totalElements`` einer Listenseite, sonst ``None``."""
    pagination = data.get("pagination") if isinstance(data, Mapping) else None
    wert = pagination.get("totalElements") if isinstance(pagination, Mapping) else None
    return wert if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 0 else None


@dataclass
class Ergebnis:
    """Abgleich einer Quelle: Befund je Kommune (wie abgelegt) und ein Hinweis, falls er nicht oder nur teils lief."""

    quelle: str
    kommunen: dict[str, dict[str, Any]] = field(default_factory=dict)
    hinweis: str = ""
    abrufe: int = 0


def _vermerkt(sync_config: Any) -> set[str]:
    """Als fehlend vermerkte Listen der Quelle (``missing_lists``), Adressen ohne Schrägstrich am Ende."""
    stored = sync_config.get("missing_lists") if isinstance(sync_config, Mapping) else None
    if not isinstance(stored, Mapping):
        return set()
    return {str(url).rstrip("/") for url in stored}


async def _bestand(storage: Any, body_id: Any) -> dict[str, tuple[int, int]]:
    """Je Liste (nicht gelöschte, alle samt Löschmarkierung)."""
    async with storage.get_session() as session:
        row = (await session.execute(_BESTAND_SQL, {"body": body_id})).mappings().one()
    return {typ: (int(row[typ] or 0), int(row[f"{typ}_alle"] or 0)) for typ in LISTEN}


async def _kommunen(storage: Any, source_id: Any) -> list[Mapping[str, Any]]:
    async with storage.get_session() as session:
        return list((await session.execute(_BODIES_SQL, {"source": source_id})).mappings().all())


async def gleiche_ab(
    storage: Any,
    source: Any,
    *,
    kommune: str = "",
    speichern: bool = True,
    now: datetime | None = None,
    client_factory: Callable[..., Any] = OParlClient,
) -> Ergebnis:
    """
    Abgleich einer Quelle mit ihrer Schnittstelle (siehe Moduldokumentation); ``kommune`` beschränkt ihn auf Kommunen,
    deren Name oder Kennung den Text enthält. ``speichern``: Ergebnis an der Quelle ablegen (bei ``kommune`` zu den
    übrigen Kommunen dazu, sonst an ihrer Stelle).
    """
    from src.sync.orchestrator import source_backoff_until

    now = now or datetime.now(UTC)
    ergebnis = Ergebnis(quelle=str(getattr(source, "name", "") or source.url))
    sync_config = getattr(source, "sync_config", None)
    if ist_scraper(sync_config):
        ergebnis.hinweis = "Scraper-Quelle: keine OParl-Listen"
        return ergebnis
    options = SourceFetchOptions.from_sync_config(sync_config)
    if options.outside_sync_window(now):
        ergebnis.hinweis = "außerhalb des Abruffensters, nichts abgerufen"
        return ergebnis
    schonung = source_backoff_until(source, now)
    if schonung is not None:
        ergebnis.hinweis = f"Quelle in Schonung bis {schonung:%d.%m. %H:%M} UTC, nichts abgerufen"
        return ergebnis
    deadline = options.fetch_deadline(now)
    vermerkt = _vermerkt(sync_config)
    alle = await _kommunen(storage, source.id)
    kommunen = [
        k for k in alle if not kommune or kommune.lower() in str(k["name"]).lower() or kommune in str(k["external_id"])
    ]
    async with client_factory(
        max_concurrent=1,
        source_name=urlsplit(source.url).netloc or "unknown",
        user_agent=getattr(source, "user_agent", None),
        fetch_deadline=deadline,
        **options.client_kwargs(),
    ) as client:
        # Ein Versuch je Liste: Der Abgleich soll die Quelle nicht zusätzlich belasten, ein Fehler ist ein Befund
        client.max_retries = 1
        for body in kommunen:
            listen: dict[str, dict[str, Any]] = {}
            abgebrochen = False
            for typ, adresse in listen_der_kommune(body["raw_json"]).items():
                if typ in options.skip_lists or adresse.rstrip("/") in vermerkt:
                    continue
                rest = None if deadline is None else (deadline - datetime.now(UTC)).total_seconds()
                if rest is not None and rest <= 0:
                    abgebrochen = True
                    break
                try:
                    # Der Client hält das Ende des Fensters nur in Listen ein; hier gilt es samt Takt und Weiterleitung
                    async with asyncio.timeout(rest):
                        result = await client.fetch(adresse, use_cache=False)
                except TimeoutError:
                    abgebrochen = True
                    break
                ergebnis.abrufe += 1
                if result.data is None or result.error:
                    fehler = result.error or f"HTTP {result.status_code}"
                    listen[typ] = {"fehler": str(fehler)[:120]}
                else:
                    listen[typ] = {"quelle": total_elements(result.data)}
            if not listen and not abgebrochen:
                continue
            bestand = await _bestand(storage, body["id"])
            for typ, werte in listen.items():
                werte["bestand"], werte["mit_geloeschten"] = bestand[typ]
                if typ == "file" and len(alle) > 1:
                    # Eine Datei, die mehrere Kommunen der Quelle nennen, steht im Bestand nur bei einer davon
                    werte["nur_hinweis"] = True
            eintrag: dict[str, Any] = {"at": _iso(now), "name": body["name"], "listen": listen}
            if abgebrochen:
                eintrag["abgebrochen"] = "Abruffenster endete"
            ergebnis.kommunen[str(body["external_id"])] = eintrag
            if abgebrochen:
                ergebnis.hinweis = "Abruffenster endete, nicht alle Kommunen abgeglichen"
                break
    if speichern and ergebnis.kommunen:
        await _speichern(storage, source, ergebnis.kommunen, ersetzen=not kommune and not ergebnis.hinweis)
    return ergebnis


async def _speichern(storage: Any, source: Any, kommunen: dict[str, dict[str, Any]], *, ersetzen: bool) -> None:
    """
    Befunde unter ``sync_config["datenabgleich_quelle"]`` ablegen: Lesen, Zusammenführen und Schreiben unter
    Zeilensperre (wie ``DatabaseStorage._change_sync_config``), die übrigen Schlüssel bleiben. ``ersetzen``: Kommunen
    ohne neuen Befund fallen weg, sonst kommen die neuen zu den bisherigen dazu.
    """
    from sqlalchemy import select

    from src.storage.models import OParlSource

    async with storage.get_session() as session:
        row = (await session.execute(select(OParlSource).where(OParlSource.id == source.id).with_for_update())).scalar()
        if row is None:
            return
        config = dict(row.sync_config or {})
        bisher = config.get(SCHLUESSEL)
        neu: dict[str, Any] = {} if ersetzen or not isinstance(bisher, dict) else dict(bisher)
        neu.update(kommunen)
        config[SCHLUESSEL] = dict(sorted(neu.items()))
        row.sync_config = config
        await session.commit()


async def im_daemon(storage: Any, source: Any, now: datetime | None = None) -> Ergebnis | None:
    """
    Abgleich nach einem erfolgreichen Lauf im Daemon, wenn er fällig ist (``faellig``); höchstens ``FRIST_SEKUNDEN``.
    Fehler landen nur im Protokoll: Der Abgleich darf den Takt der Quelle nie stören.
    """
    if not faellig(source, now):
        return None
    try:
        async with asyncio.timeout(FRIST_SEKUNDEN):
            ergebnis = await gleiche_ab(storage, source, now=now)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - ein Befund weniger, der nächste Lauf versucht es wieder
        logger.warning("Datenabgleich der Quelle %s nicht möglich: %s", getattr(source, "name", "?"), type(e).__name__)
        return None
    if ergebnis.kommunen or ergebnis.hinweis:
        logger.info(
            "Datenabgleich %s: %d Kommunen, %d Abrufe%s",
            ergebnis.quelle,
            len(ergebnis.kommunen),
            ergebnis.abrufe,
            f" ({ergebnis.hinweis})" if ergebnis.hinweis else "",
        )
    return ergebnis
