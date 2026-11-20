# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenabgleich mit den Schnittstellen der Kommunen (Issue #1525).

Hintergrund: Mitte November 2026 fehlten in mehreren Kommunen die Verknüpfungen zwischen Tagesordnungspunkten und
Vorlagen. Die Abgleiche liefen „erfolgreich“, der Datenstand war frisch, der Betriebsmonitor blieb grün: Er bewertete
nur, ob abgeglichen wird, nicht, ob der Bestand zur Quelle passt. Dieses Modul prüft das je Kommune und bewertet es
mit einer Ampel (ok / auffällig / kritisch).

Zwei Teile, getrennt nach Zuständigkeit:

- **Interne Konsistenz** (hier, nur Datenbank, Zeitplan ``befehl:datenabgleich`` täglich): Für die Sitzungen von
  ``FENSTER_VOR`` Tagen zurück bis ``FENSTER_NACH`` Tage voraus zählt ``pruefe_kommune``

  - TOPs mit Verweis auf eine Beratung (``raw_json.consultation``), deren Beratung im Bestand fehlt,
  - TOPs mit Verweis, für die Work keine Vorlage findet (Work sucht über ``OParlConsultation.agenda_item_external_id``
    und ``paper``, ``apps/work/meetings/selectors.py``),
  - Beratungen dieser TOPs ohne Vorlage,
  - Vorlagen dieser Beratungen ohne Dateien, obwohl ihre Rohdaten Dateien nennen,
  - als lückenhaft vermerkte Listen der Kommune (ausgesetzt, Fortsetzung offen, Zeitscheiben offen, nicht
    freigegeben).

  Das Ergebnis steht je Kommune in ``sync_config["datenabgleich"]`` der Quelle (unter Zeilensperre geschrieben).
- **Abgleich mit der Quelle** (Ingestor, ``ingestor/src/sync/datenabgleich.py``, höchstens einmal täglich je Quelle
  im Abruffenster): erste Seite der Hauptlisten, ``pagination.totalElements`` gegen den Bestand, abgelegt in
  ``sync_config["datenabgleich_quelle"]``.

Die Bewertung (``bewerte_quelle``) liest nur diese beiden Einträge und die Felder der Quelle, ohne weitere Abfrage.
``evaluate_source`` im Betriebsmonitor übernimmt sie: Eine kritische Kommune macht die Quelle kritisch, mit Alarm-Mail
wie bei anderen Ingestor-Problemen; die Prüfung ``datenabgleich`` unter ``/health/worker/`` scheitert dann (Statusseite).
Dazu der „Datenstand“ der Kommune: Bis wann ist der Bestand nachweislich vollständig? Der spätere Zeitpunkt aus dem
Beginn des Änderungsfensters der Kommune (``sync_config["body_sync_started"]``, rückt nur nach einem lückenlosen
Abgleich vor) und dem letzten lückenlosen Vollabgleich der Quelle. Liegt er mehr als sieben Tage zurück, fragt der
Änderungsabgleich nicht mehr bis dorthin zurück (``MODIFIED_SINCE_FALLBACK_DAYS`` im Ingestor): Ältere Änderungen
holt nur noch ein Vollabgleich.

Schwellen: ``SCHWELLEN`` (geprüft am Bestand vom 20.11.2026, siehe PR zu #1525). Einträge, die älter sind als
``GUELTIG_INTERN`` bzw. ``GUELTIG_QUELLE``, zählen nicht mehr; der Monitor zeigt dann den Hinweis, dass der Abgleich
nicht läuft, ohne alte Befunde weiter zu melden.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from django.db import transaction
from django.db.models import Q
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

#: Schlüssel in ``OParlSource.sync_config``: interne Konsistenz je Kommune (Kennung der Kommune -> Befund)
SCHLUESSEL_INTERN: Final = "datenabgleich"
#: Schlüssel in ``OParlSource.sync_config``: Abgleich mit der Quelle je Kommune, schreibt der Ingestor
SCHLUESSEL_QUELLE: Final = "datenabgleich_quelle"
#: Messfenster: Sitzungen ab so vielen Tagen zurück bis so viele Tage voraus
FENSTER_VOR: Final = 14
FENSTER_NACH: Final = 60
#: Befunde älter als das zählen nicht mehr (der tägliche Lauf fiel aus)
GUELTIG_INTERN: Final = timedelta(hours=48)
GUELTIG_QUELLE: Final = timedelta(days=3)
#: Bis zu so vielen Beispielen (Kennungen der TOPs) je Befund, für die Suche nach der Ursache
BEISPIELE: Final = 5
#: Größe der IN-Listen
_HAPPEN: Final = 500

#: Schwellen der Ampel: (Mindestzahl, Mindestanteil) je Stufe
SCHWELLEN: Final[dict[str, dict[str, tuple[int, float]]]] = {
    # TOPs mit Beratungsverweis, deren Beratung fehlt oder für die Work keine Vorlage findet
    "top_luecke": {"critical": (10, 0.10), "warning": (3, 0.03)},
    # Beratungen der TOPs im Fenster ohne Vorlage
    "beratung_ohne_vorlage": {"warning": (5, 0.03)},
    # Vorlagen der Beratungen im Fenster ohne Dateien, obwohl die Rohdaten welche nennen
    "vorlage_ohne_dateien": {"critical": (10, 0.10), "warning": (3, 0.03)},
    # Abgleich mit der Quelle: Objekte, die uns fehlen (``totalElements`` minus Bestand)
    "fehlbestand": {"critical": (100, 0.20), "warning": (20, 0.10)},
}
#: Datenstand der Kommune: auffällig bzw. kritisch ab diesem Alter (kritisch: Fenster des Änderungsabgleichs)
STAND_AUFFAELLIG: Final = timedelta(days=3)
STAND_KRITISCH: Final = timedelta(days=7)

STUFEN: Final = {"ok": 0, "warning": 1, "critical": 2}
AMPEL: Final = {"ok": "ok", "warning": "auffällig", "critical": "kritisch"}
LISTEN: Final = ("paper", "meeting", "consultation", "agendaitem", "file")
LISTEN_NAMEN: Final = {
    "paper": "Vorlagen",
    "meeting": "Sitzungen",
    "consultation": "Beratungen",
    "agendaitem": "TOPs",
    "file": "Dateien",
}
#: Schlüssel der Listen am Body (OParl; einige Server nennen sie in der Mehrzahl)
_BODY_LISTEN: Final = {
    "paper": ("paper",),
    "meeting": ("meeting",),
    "consultation": ("consultation", "consultations"),
    "agendaitem": ("agendaItem", "agendaItems"),
    "file": ("file", "files"),
    "person": ("person",),
    "organization": ("organization",),
    "membership": ("membership",),
    "location": ("location", "locationList"),
    "legislativeterm": ("legislativeTerm", "legislativeTermList"),
}
EMPFEHLUNG: Final = (
    "Einzelheiten zeigt „python manage.py datenabgleich --kommune <Name>“ (Beispiele, Listen), den Abgleich mit der "
    "Quelle der Ingestor („datenabgleich --source <URL>“). Ursachen und Abhilfe: docs/MONITORING.md, Abschnitt "
    "„Datenabgleich mit den Kommunen“."
)


# =============================================================================
# Interne Konsistenz
# =============================================================================


def _happen[T](werte: list[T]) -> Iterator[list[T]]:
    for start in range(0, len(werte), _HAPPEN):
        yield werte[start : start + _HAPPEN]


def _verweis(wert: Any) -> str:
    """Kennung der Beratung aus ``raw_json.consultation``: Adresse, eingebettetes Objekt (als JSON-Text) oder nichts."""
    if not isinstance(wert, str):
        return ""
    text = wert.strip()
    if text.startswith("{"):
        try:
            objekt = json.loads(text)
        except ValueError:
            return ""
        kennung = objekt.get("id") if isinstance(objekt, dict) else None
        return kennung if isinstance(kennung, str) else ""
    return text


def _nennt_dateien(haupt: Any, weitere: Any) -> bool:
    """Nennen die Rohdaten einer Vorlage Dateien (``mainFile`` oder nicht leere ``auxiliaryFile``)?"""
    for wert in (haupt, weitere):
        if isinstance(wert, str):
            try:
                wert = json.loads(wert)
            except ValueError:
                if wert.strip():
                    return True
                continue
        if isinstance(wert, dict | list) and wert:
            return True
    return False


def _body_listen(raw_json: Any) -> dict[str, str]:
    """Adressen der Listen am Body: Typ -> Adresse (ohne Schrägstrich am Ende)."""
    raw = raw_json if isinstance(raw_json, dict) else {}
    listen: dict[str, str] = {}
    for typ, schluessel in _BODY_LISTEN.items():
        for name in schluessel:
            wert = raw.get(name)
            if isinstance(wert, str) and wert.strip():
                listen[typ] = wert.strip().rstrip("/")
                break
    return listen


def _typ_der_liste(adresse: str, listen: dict[str, str]) -> str | None:
    """Typ der Liste dieser Kommune unter ``adresse`` (Vermerke des Ingestors), sonst ``None``."""
    ohne_anfrage = adresse.strip().rstrip("/")
    for typ, liste in listen.items():
        if ohne_anfrage == liste:
            return typ
    return None


def lueckenhafte_listen(sync_config: Any, listen: dict[str, str]) -> list[str]:
    """
    Als lückenhaft vermerkte Listen der Kommune aus den Vermerken des Ingestors (``sync_config``): ausgesetzte langsame
    Listen, offene Fortsetzung eines abgebrochenen Vollabgleichs, offene Zeitscheiben, nicht freigegebene Listen.
    """
    config = sync_config if isinstance(sync_config, dict) else {}
    hinweise: list[str] = []

    def eintraege(schluessel: str) -> Iterable[tuple[str, Any]]:
        wert = config.get(schluessel)
        return wert.items() if isinstance(wert, dict) else ()

    for adresse, eintrag in eintraege("slow_lists"):
        typ = _typ_der_liste(str(adresse), listen)
        if typ is not None and isinstance(eintrag, dict):
            grund = str(eintrag.get("reason") or "Zeitüberschreitung")
            seit = str(eintrag.get("since") or "")[:10]
            hinweise.append(
                f"{LISTEN_NAMEN.get(typ, typ)} ausgesetzt ({grund}" + (f", Lücke ab {seit})" if seit else ")")
            )
    for adresse, eintrag in eintraege("full_sync_resume"):
        typ = _typ_der_liste(str(adresse), listen)
        if typ is not None and isinstance(eintrag, dict) and not eintrag.get("done"):
            hinweise.append(f"{LISTEN_NAMEN.get(typ, typ)}: Vollabgleich abgebrochen, Fortsetzung offen")
    for adresse, eintrag in eintraege("full_sync_progress"):
        typ = _typ_der_liste(str(adresse), listen)
        if typ is not None:
            hinweise.append(f"{LISTEN_NAMEN.get(typ, typ)}: Vollabgleich in Zeitscheiben, offen ab {str(eintrag)[:10]}")
    for adresse, eintrag in eintraege("missing_lists"):
        typ = _typ_der_liste(str(adresse), listen)
        vermerk = eintrag.get("reason") if isinstance(eintrag, dict) else None
        if typ is not None and isinstance(vermerk, str) and "nicht freigegeben" in vermerk:
            hinweise.append(f"{LISTEN_NAMEN.get(typ, typ)} nicht freigegeben (HTTP 403)")
    return sorted(hinweise)


def pruefe_kommune(body: Any, now: datetime | None = None) -> dict[str, Any]:
    """
    Interne Konsistenz einer Kommune im Messfenster (siehe Moduldokumentation). Wenige Abfragen über Indizes, IN-Listen
    in Happen; die TOPs des Fensters liegen je Kommune im Bereich von Hunderten bis wenigen Tausend.
    """
    from ..models import OParlAgendaItem, OParlConsultation, OParlFile, OParlPaper, withdrawn_q

    now = now or timezone.now()
    von, bis = now - timedelta(days=FENSTER_VOR), now + timedelta(days=FENSTER_NACH)
    zeilen = (
        OParlAgendaItem.objects.filter(
            meeting__body_id=body.id,
            meeting__deleted=False,
            meeting__start__gte=von,
            meeting__start__lt=bis,
            deleted=False,
        )
        .annotate(verweis=KeyTextTransform("consultation", "raw_json"))
        .values_list("external_id", "verweis")
    )
    tops: list[str] = []
    verweise: dict[str, str] = {}
    for kennung, verweis in zeilen:
        tops.append(kennung)
        beratung = _verweis(verweis)
        if beratung:
            verweise[kennung] = beratung

    # Beratung dieser Quelle oder ohne Kommune (Altbestand), wie ``hub.ris.selectors.consultation_agenda_item_in_source``
    eigene = Q(body__isnull=True) | Q(body__source_id=body.source_id)
    vorhanden: set[str] = set()
    for happen in _happen(sorted(set(verweise.values()))):
        vorhanden.update(
            OParlConsultation.objects.filter(external_id__in=happen, deleted=False).values_list(
                "external_id", flat=True
            )
        )
    mit_vorlage: set[str] = set()
    beratungen: dict[Any, Any] = {}
    for happen in _happen(sorted(verweise)):
        mit_vorlage.update(
            kennung
            for kennung in OParlConsultation.objects.filter(
                eigene, agenda_item_external_id__in=happen, paper__isnull=False
            )
            # Wie ``OParlAgendaItem.get_consultations``: von mandari Session zurückgenommene zählen nicht
            .exclude(withdrawn_q())
            .exclude(withdrawn_q("paper"))
            .values_list("agenda_item_external_id", flat=True)
            .distinct()
            if kennung
        )
    for happen in _happen(tops):
        beratungen.update(
            OParlConsultation.objects.filter(eigene, agenda_item_external_id__in=happen, deleted=False).values_list(
                "id", "paper_id"
            )
        )
    for happen in _happen(sorted(set(verweise.values()))):
        beratungen.update(
            OParlConsultation.objects.filter(eigene, external_id__in=happen, deleted=False).values_list(
                "id", "paper_id"
            )
        )

    fehlt = sorted(top for top, beratung in verweise.items() if beratung not in vorhanden)
    work_ohne = sorted(top for top in verweise if top not in mit_vorlage)
    luecke = sorted(set(fehlt) | set(work_ohne))

    vorlagen = sorted({paper for paper in beratungen.values() if paper is not None})
    nennen: set[Any] = set()
    mit_dateien: set[Any] = set()
    for ids in _happen(vorlagen):
        for kennung, haupt, weitere in OParlPaper.objects.filter(id__in=ids, deleted=False).values_list(
            "id", KeyTransform("mainFile", "raw_json"), KeyTransform("auxiliaryFile", "raw_json")
        ):
            if _nennt_dateien(haupt, weitere):
                nennen.add(kennung)
        mit_dateien.update(
            OParlFile.objects.filter(paper_id__in=ids, deleted=False).values_list("paper_id", flat=True).distinct()
        )
    ohne_dateien = sorted(nennen - mit_dateien)

    sync_config = body.source.sync_config if body.source_id else {}
    return {
        "at": now.isoformat(timespec="seconds"),
        "name": body.name,
        "fenster": [FENSTER_VOR, FENSTER_NACH],
        "tops": len(tops),
        "mit_beratung": len(verweise),
        "beratung_fehlt": len(fehlt),
        "work_ohne_vorlage": len(work_ohne),
        "top_luecke": len(luecke),
        "beratungen": len(beratungen),
        "beratung_ohne_vorlage": sum(1 for paper in beratungen.values() if paper is None),
        "vorlagen": len(vorlagen),
        "vorlage_ohne_dateien": len(ohne_dateien),
        "listen": lueckenhafte_listen(sync_config, _body_listen(body.raw_json)),
        "beispiele": luecke[:BEISPIELE],
    }


def zusammenfuehren(
    bisher: Any, befunde: dict[str, dict[str, Any]], gescheitert: dict[str, dict[str, Any]], *, ersetzen: bool
) -> dict[str, dict[str, Any]]:
    """
    Neuer Stand von ``sync_config["datenabgleich"]``. ``ersetzen`` (Lauf über alle Kommunen): Kommunen ohne neuen Befund
    fallen weg, sonst bleiben sie stehen (Lauf mit ``--kommune``). Eine Kommune, deren Prüfung scheiterte
    (``gescheitert``), behält ihren bisherigen Befund samt Zeitpunkt und bekommt den Vermerk ``fehlgeschlagen``: Ein
    kritischer Befund verschwindet so nicht mit einem Fehler der Prüfung, und der Fehler bleibt sichtbar.
    """
    alt = bisher if isinstance(bisher, dict) else {}
    neu: dict[str, dict[str, Any]] = {} if ersetzen else {k: v for k, v in alt.items() if isinstance(v, dict)}
    neu.update(befunde)
    for kennung, vermerk in gescheitert.items():
        vorher = alt.get(kennung)
        neu[kennung] = {**(vorher if isinstance(vorher, dict) else {}), **vermerk}
    return dict(sorted(neu.items()))


def speichere(
    source_id: Any,
    befunde: dict[str, dict[str, Any]],
    *,
    ersetzen: bool,
    gescheitert: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    Befunde je Kommune in ``sync_config["datenabgleich"]`` schreiben (``zusammenfuehren``), unter Zeilensperre: Der
    Ingestor schreibt in dieselbe ``sync_config``.
    """
    from ..models import OParlSource

    with transaction.atomic():
        source = OParlSource.objects.select_for_update().filter(pk=source_id).first()
        if source is None:
            return
        config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
        neu = zusammenfuehren(config.get(SCHLUESSEL_INTERN), befunde, gescheitert or {}, ersetzen=ersetzen)
        if neu:
            config[SCHLUESSEL_INTERN] = neu
        else:
            config.pop(SCHLUESSEL_INTERN, None)
        source.sync_config = config
        source.save(update_fields=["sync_config"])


def pruefe_alle(kommune: str = "", *, speichern: bool = True, now: datetime | None = None) -> list[Any]:
    """
    Interne Konsistenz aller Kommunen aktiver Quellen (``kommune``: nur Kommunen, deren Name oder Kennung den Text
    enthält, ohne Groß-/Kleinschreibung) und Ergebnis speichern (``speichern``). Liefert die Quellen mit Befund; ihre
    ``sync_config`` trägt die neuen Befunde auch ohne Speichern (nur im Speicher), für ``bewerte_kommunen``.
    """
    from ..models import OParlBody, OParlSource

    now = now or timezone.now()
    bodies = OParlBody.objects.filter(deleted=False, source__is_active=True).select_related("source").order_by("name")
    if kommune:
        bodies = bodies.filter(Q(name__icontains=kommune) | Q(external_id__icontains=kommune))
    je_quelle: dict[Any, dict[str, dict[str, Any]]] = {}
    gescheitert: dict[Any, dict[str, dict[str, Any]]] = {}
    for body in bodies:
        try:
            befund = pruefe_kommune(body, now)
        except Exception as exc:  # noqa: BLE001 – eine Kommune darf den Lauf der übrigen nicht verhindern
            logger.exception("Datenabgleich: Kommune %s nicht geprüft", body.name)
            gescheitert.setdefault(body.source_id, {})[body.external_id] = {
                "name": body.name,
                "fehlgeschlagen": now.isoformat(timespec="seconds"),
                "fehler": type(exc).__name__,
            }
            continue
        je_quelle.setdefault(body.source_id, {})[body.external_id] = befund
    betroffen = set(je_quelle) | set(gescheitert)
    if speichern:
        for source_id in betroffen:
            speichere(
                source_id, je_quelle.get(source_id, {}), ersetzen=not kommune, gescheitert=gescheitert.get(source_id)
            )
    quellen = list(OParlSource.objects.filter(pk__in=list(betroffen)).order_by("name"))
    if not speichern:
        for source in quellen:
            config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
            config[SCHLUESSEL_INTERN] = zusammenfuehren(
                config.get(SCHLUESSEL_INTERN),
                je_quelle.get(source.pk, {}),
                gescheitert.get(source.pk, {}),
                ersetzen=False,
            )
            source.sync_config = config
    return quellen


# =============================================================================
# Bewertung
# =============================================================================


@dataclass
class Kommune:
    """Bewertung einer Kommune: Ampel, Gründe (bewertet) und Hinweise (ohne Einfluss auf die Ampel)."""

    kennung: str
    name: str
    status: str = "ok"
    gruende: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    intern: dict[str, Any] | None = None
    quelle: dict[str, Any] | None = None
    stand: datetime | None = None

    @property
    def ampel(self) -> str:
        return AMPEL[self.status]

    def melde(self, status: str, grund: str) -> None:
        """Befund mit Stufe: ``ok`` als Hinweis, sonst als Grund, der die Ampel höchstens anhebt."""
        if status == "ok":
            self.hinweise.append(grund)
            return
        if STUFEN[status] > STUFEN[self.status]:
            self.status = status
        self.gruende.append(grund)


def _zeitpunkt(wert: Any) -> datetime | None:
    if isinstance(wert, datetime):
        return wert
    if not isinstance(wert, str):
        return None
    stamp = parse_datetime(wert)
    if stamp is not None and timezone.is_naive(stamp):
        stamp = timezone.make_aware(stamp, UTC)
    return stamp


def _stufe(art: str, anzahl: int, gesamt: int) -> str:
    """Stufe nach ``SCHWELLEN[art]`` (Anzahl und Anteil an ``gesamt`` müssen beide erreicht sein)."""
    anteil = anzahl / gesamt if gesamt else 0.0
    for stufe in ("critical", "warning"):
        schwelle = SCHWELLEN[art].get(stufe)
        if schwelle is not None and anzahl >= schwelle[0] and anteil >= schwelle[1]:
            return stufe
    return "ok"


def _prozent(anzahl: int, gesamt: int) -> str:
    return f"{anzahl / gesamt:.0%}" if gesamt else "–"


def _dauer(alter: timedelta) -> str:
    stunden = int(alter.total_seconds() // 3600)
    return f"{stunden} Stunden" if stunden < 48 else f"{stunden // 24} Tagen"


def datenstand(source: Any, kennung: str) -> datetime | None:
    """
    Bis wann der Bestand der Kommune nachweislich vollständig ist: der spätere Zeitpunkt aus dem Beginn ihres
    Änderungsfensters (``sync_config["body_sync_started"]``) und dem letzten lückenlosen Vollabgleich der Quelle; ohne
    beides (Scraper-Quellen) der letzte lückenlose Abgleich der Quelle.
    """
    config = source.sync_config if isinstance(source.sync_config, dict) else {}
    fenster = config.get("body_sync_started")
    kandidaten = [
        _zeitpunkt(fenster.get(kennung) if isinstance(fenster, dict) else None),
        getattr(source, "last_successful_full_sync", None),
    ]
    vorhanden = [stamp for stamp in kandidaten if stamp is not None]
    if vorhanden:
        return max(vorhanden)
    return getattr(source, "last_successful_sync", None)


def _bewerte_intern(kommune: Kommune, intern: dict[str, Any]) -> None:
    mit_beratung = int(intern.get("mit_beratung") or 0)
    luecke = int(intern.get("top_luecke") or 0)
    if luecke:
        stufe = _stufe("top_luecke", luecke, mit_beratung)
        text = (
            f"{luecke} von {mit_beratung} TOPs mit Beratungsverweis ohne Vorlage ({_prozent(luecke, mit_beratung)}; "
            f"Beratung fehlt {int(intern.get('beratung_fehlt') or 0)}, Work findet keine Vorlage "
            f"{int(intern.get('work_ohne_vorlage') or 0)})"
        )
        kommune.melde(stufe, text)
    beratungen = int(intern.get("beratungen") or 0)
    ohne = int(intern.get("beratung_ohne_vorlage") or 0)
    if ohne:
        stufe = _stufe("beratung_ohne_vorlage", ohne, beratungen)
        text = f"{ohne} von {beratungen} Beratungen ohne Vorlage"
        kommune.melde(stufe, text)
    vorlagen = int(intern.get("vorlagen") or 0)
    ohne_dateien = int(intern.get("vorlage_ohne_dateien") or 0)
    if ohne_dateien:
        stufe = _stufe("vorlage_ohne_dateien", ohne_dateien, vorlagen)
        text = f"{ohne_dateien} von {vorlagen} Vorlagen ohne Dateien, obwohl die Quelle welche nennt"
        kommune.melde(stufe, text)
    for hinweis in intern.get("listen") or []:
        kommune.hinweise.append(str(hinweis))


def _bewerte_stand(kommune: Kommune, now: datetime) -> None:
    if kommune.stand is None:
        kommune.hinweise.append("noch kein lückenloser Abgleich")
        return
    alter = now - kommune.stand
    text = f"letzter lückenloser Abgleich vor {_dauer(alter)}"
    if alter > STAND_KRITISCH:
        kommune.melde(
            "critical", f"{text}: Änderungen davor holt nur noch ein Vollabgleich (Änderungsfenster sieben Tage)"
        )
    elif alter > STAND_AUFFAELLIG:
        kommune.melde("warning", text)


def _bewerte_quelle(kommune: Kommune, eintrag: dict[str, Any]) -> None:
    listen = eintrag.get("listen")
    if not isinstance(listen, dict):
        return
    for typ in LISTEN:
        werte = listen.get(typ)
        if not isinstance(werte, dict):
            continue
        name = LISTEN_NAMEN[typ]
        if werte.get("fehler"):
            kommune.hinweise.append(f"{name}: Liste der Quelle nicht abrufbar ({werte['fehler']})")
            continue
        quelle, bestand = werte.get("quelle"), werte.get("bestand")
        if not isinstance(quelle, int) or not isinstance(bestand, int):
            continue
        # Ob ``totalElements`` gelöschte Objekte mitzählt, legt OParl nicht fest: Als fehlend gilt nur, was auch mit den
        # als gelöscht markierten nicht im Bestand steht (kein Alarm durch Löschmarkierungen)
        geloescht = werte.get("mit_geloeschten")
        fehlt = quelle - max(bestand, geloescht if isinstance(geloescht, int) else bestand)
        if fehlt >= SCHWELLEN["fehlbestand"]["warning"][0]:
            stufe = _stufe("fehlbestand", fehlt, quelle)
            text = f"{name}: Quelle {quelle}, Bestand {bestand} ({fehlt} fehlen, {_prozent(fehlt, quelle)})"
            if werte.get("nur_hinweis"):
                # Dateien einer Quelle mit mehreren Kommunen: Eine Datei gehört im Bestand nur einer davon
                kommune.hinweise.append(f"{text}; Zuordnung zur Kommune unsicher, nur Hinweis")
            else:
                kommune.melde(stufe, text)
        elif bestand > quelle and bestand - quelle >= SCHWELLEN["fehlbestand"]["warning"][0]:
            kommune.hinweise.append(f"{name}: Bestand {bestand} größer als Quelle {quelle} (Löschungen nachziehen?)")


def bewerte_kommunen(source: Any, now: datetime | None = None) -> list[Kommune]:
    """Bewertung je Kommune der Quelle aus den abgelegten Befunden (keine Abfrage), kritische zuerst."""
    now = now or timezone.now()
    config = source.sync_config if isinstance(source.sync_config, dict) else {}
    intern = config.get(SCHLUESSEL_INTERN)
    intern = intern if isinstance(intern, dict) else {}
    quelle = config.get(SCHLUESSEL_QUELLE)
    quelle = quelle if isinstance(quelle, dict) else {}
    kommunen: list[Kommune] = []
    for kennung in sorted(set(intern) | set(quelle)):
        eintrag_intern = intern.get(kennung) if isinstance(intern.get(kennung), dict) else None
        eintrag_quelle = quelle.get(kennung) if isinstance(quelle.get(kennung), dict) else None
        name = str((eintrag_intern or eintrag_quelle or {}).get("name") or kennung)
        kommune = Kommune(kennung=kennung, name=name, intern=eintrag_intern, quelle=eintrag_quelle)
        if eintrag_intern is not None:
            if eintrag_intern.get("fehlgeschlagen"):
                kommune.melde(
                    "warning",
                    f"interne Prüfung gescheitert ({eintrag_intern.get('fehler') or 'Fehler'}, "
                    f"{str(eintrag_intern['fehlgeschlagen'])[:16]}), bisheriger Befund gilt weiter",
                )
            at = _zeitpunkt(eintrag_intern.get("at"))
            if at is None or now - at > GUELTIG_INTERN:
                kommune.hinweise.append("interne Prüfung veraltet (Zeitplan befehl:datenabgleich läuft nicht?)")
            else:
                _bewerte_intern(kommune, eintrag_intern)
                kommune.stand = datenstand(source, kennung)
                _bewerte_stand(kommune, now)
        if eintrag_quelle is not None:
            at = _zeitpunkt(eintrag_quelle.get("at"))
            if at is None or now - at > GUELTIG_QUELLE:
                kommune.hinweise.append("Abgleich mit der Quelle veraltet")
            else:
                _bewerte_quelle(kommune, eintrag_quelle)
        kommunen.append(kommune)
    kommunen.sort(key=lambda k: (-STUFEN[k.status], k.name))
    return kommunen


def bewerte_quelle(source: Any, now: datetime | None = None) -> dict[str, Any]:
    """
    Zusammenfassung für ``evaluate_source``: schlechteste Ampel der Kommunen, je auffälliger Kommune ein Grund. Ohne
    abgelegte Befunde ``ok`` ohne Gründe.
    """
    kommunen = bewerte_kommunen(source, now)
    status = "ok"
    gruende: list[str] = []
    for kommune in kommunen:
        if kommune.status == "ok":
            continue
        if STUFEN[kommune.status] > STUFEN[status]:
            status = kommune.status
        gruende.append(f"Datenabgleich {kommune.name} {kommune.ampel}: " + "; ".join(kommune.gruende))
    return {"status": status, "reasons": gruende, "kommunen": kommunen}


def check_datenabgleich() -> Any:
    """Prüfung ``datenabgleich`` für ``/health/worker/``: keine Kommune kritisch. Nur Zahlen, keine Namen."""
    from apps.events.status import Check

    from ..models import OParlSource

    zaehler = {"ok": 0, "warning": 0, "critical": 0}
    now = timezone.now()
    for source in OParlSource.objects.filter(is_active=True).only(
        "id", "sync_config", "last_successful_sync", "last_successful_full_sync"
    ):
        for kommune in bewerte_kommunen(source, now):
            zaehler[kommune.status] += 1
    return Check(
        zaehler["critical"] == 0,
        f"{zaehler['critical']} Kommunen kritisch, {zaehler['warning']} auffällig, {zaehler['ok']} ok",
    )
