# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Derselbe Tagesordnungspunkt, dieselbe Vorlage nach einer Neuveröffentlichung (Issue #547).

Die kanonische Kennung (``uuid5`` der Quell-Adresse, ADR ``docs/adr/20260929-kanonisches-modell.md``) bleibt
gleich, solange die Quelle ihre Adressen hält. Nicht jede Quelle tut das. Beobachtet im Bestand:

- **Löschmarkierung und Neuanlage:** Die Quelle meldet den alten Punkt als gelöscht und veröffentlicht ihn unter
  neuer Adresse (eigenes Session-RIS: Punkt entfernt und wieder eingefügt).
- **Neue Adressen ohne Löschmeldung:** Die Sitzung listet den Punkt unter neuer Adresse; der alte steht noch im
  Bestand, kommt in der Tagesordnung der Quelle aber nicht mehr vor.
- **Umnummerierung bei Quellen mit Nummer in der Adresse** (Abruf aus Sitzungsseiten): Die Zeile von TOP 5 trägt
  nach einer Einfügung den Inhalt des früheren TOP 4. Die Kennung bleibt, der fachliche Punkt wechselt.

Dieses Modul beschreibt einen Punkt bzw. eine Vorlage fachlich (``TopKennung``, ``VorlagenKennung``) und
entscheidet, wo ein früher beschriebener Punkt heute steht (``top_zuordnen``, ``vorlage_zuordnen``). Es liest nur
den RIS-Bestand; was an den Objekten hängt (Notizen, Positionen), verschiebt das jeweilige Fachmodul selbst.

Regeln, im Zweifel keine Zuordnung:

- Ein Punkt bleibt derselbe, wenn er dieselbe Vorlage berät (Kennung oder Drucksachennummer). Ohne Vorlage zählt
  der Name, dazu öffentlich/nichtöffentlich; leichte Korrekturen des Namens erkennt nur der Punkt selbst.
- Ein Nachfolger kommt nur aus derselben Sitzung und muss eindeutig sein; mehrere Treffer grenzen Name und Nummer
  ein, sonst ``mehrdeutig``.
- Eine Vorlage hat einen Nachfolger nur, wenn sie gelöscht ist und genau eine andere Vorlage derselben Kommune
  dieselbe Drucksachennummer trägt.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from django.db.models import Max
from django.db.models.fields.json import KeyTransform

from insight_core.models import OParlAgendaItem, OParlConsultation, OParlMeeting, OParlPaper

#: Ergebnisse einer Zuordnung
BESTAETIGT = "bestaetigt"  # das Objekt ist weiterhin derselbe fachliche Punkt bzw. dieselbe Vorlage
NACHFOLGER = "nachfolger"  # ein anderes Objekt ist jetzt dieser Punkt bzw. diese Vorlage
ABWEICHEND = "abweichend"  # das Objekt steht noch, ist aber ein anderer Punkt; kein Nachfolger
ENTFALLEN = "entfallen"  # das Objekt ist gelöscht oder nicht mehr auf der Tagesordnung; kein Nachfolger
MEHRDEUTIG = "mehrdeutig"  # mehrere mögliche Nachfolger

#: Ab dieser Ähnlichkeit gilt ein geänderter Name desselben Punkts als Korrektur (Tippfehler, Ergänzung).
AEHNLICH_AB = 0.8
#: Länge des gespeicherten Titels (Anzeige „früherer Stand“)
TITEL_LAENGE = 300


def name_key(text: str | None) -> str:
    """Name zum Vergleichen: Unicode-normalisiert, ohne Groß/klein, Satzzeichen und doppelte Leerzeichen."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.findall(r"\w+", text))


def reference_key(text: str | None) -> str:
    """Drucksachennummer zum Vergleichen: ohne Leerraum und Groß/klein, Trennzeichen bleiben („1/23“ ≠ „12/3“)."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").casefold())


def _titel(text: str | None) -> str:
    return " ".join((text or "").split())[:TITEL_LAENGE]


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# =============================================================================
# Tagesordnungspunkte
# =============================================================================


@dataclass(frozen=True)
class TopKennung:
    """Fachliche Beschreibung eines Tagesordnungspunkts, unabhängig von seiner Kennung in der Quelle."""

    meeting: str
    number: str = ""
    name: str = ""
    title: str = ""
    public: bool = True
    papers: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    @property
    def mit_vorlage(self) -> bool:
        return bool(self.papers or self.references)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meeting": self.meeting,
            "number": self.number,
            "name": self.name,
            "title": self.title,
            "public": self.public,
            "papers": list(self.papers),
            "references": list(self.references),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> TopKennung | None:
        """Aus gespeicherter Form; ``None`` ohne Sitzung (noch nicht erfasst)."""
        if not data or not data.get("meeting"):
            return None
        return cls(
            meeting=str(data["meeting"]),
            number=str(data.get("number") or ""),
            name=str(data.get("name") or ""),
            title=str(data.get("title") or ""),
            public=bool(data.get("public", True)),
            papers=tuple(sorted({str(p) for p in data.get("papers") or ()})),
            references=tuple(sorted({str(r) for r in data.get("references") or () if r})),
        )

    def mit_vorlagen_von(self, frueher: TopKennung) -> TopKennung:
        """Behält die Vorlagen einer früheren Beschreibung, solange die Quelle (noch) keine Beratung liefert."""
        if self.mit_vorlage or not frueher.mit_vorlage:
            return self
        return replace(self, papers=frueher.papers, references=frueher.references)


@dataclass(frozen=True)
class TopStand:
    """Ein Tagesordnungspunkt einer Sitzung, wie er heute im Bestand steht."""

    id: uuid.UUID
    kennung: TopKennung
    #: nicht gelöscht und – sofern die Quelle ihre Tagesordnung mitliefert – darin enthalten
    auf_tagesordnung: bool
    geloescht: bool


def _vorlagen_je_top(external_ids: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """Beratene Vorlagen je Tagesordnungspunkt (Adresse): ``[(Kennung, Drucksachennummer), …]``."""
    result: dict[str, list[tuple[str, str]]] = {}
    ids = [e for e in external_ids if e]
    if not ids:
        return result
    rows = OParlConsultation.objects.filter(
        agenda_item_external_id__in=ids, paper__isnull=False, deleted=False
    ).values_list("agenda_item_external_id", "paper_id", "paper__reference")
    for external_id, paper_id, reference in rows:
        if external_id:
            result.setdefault(external_id, []).append((str(paper_id), reference_key(reference)))
    return result


def _kennung(row: Mapping[str, Any], vorlagen: list[tuple[str, str]]) -> TopKennung:
    return TopKennung(
        meeting=str(row["meeting_id"]),
        number=(row["number"] or "").strip(),
        name=name_key(row["name"]),
        title=_titel(row["name"]),
        public=row["public"] is not False,
        papers=tuple(sorted({paper for paper, _ in vorlagen})),
        references=tuple(sorted({ref for _, ref in vorlagen if ref})),
    )


_TOP_FELDER = ("id", "external_id", "meeting_id", "number", "name", "public", "deleted")


def top_kennungen(agenda_item_ids: Iterable[object]) -> dict[uuid.UUID, TopKennung]:
    """Heutige Beschreibung der Tagesordnungspunkte (fehlende Kennungen fehlen im Ergebnis)."""
    ids = [pk for pk in (_uuid(v) for v in agenda_item_ids) if pk]
    if not ids:
        return {}
    rows = list(OParlAgendaItem.objects.filter(pk__in=ids).values(*_TOP_FELDER))
    vorlagen = _vorlagen_je_top(row["external_id"] for row in rows)
    return {row["id"]: _kennung(row, vorlagen.get(row["external_id"], [])) for row in rows}


def _adressen_der_tagesordnung(value: Any) -> frozenset[str] | None:
    """Adressen der Punkte, die die Quelle in der Sitzung listet; ``None``, wenn sie keine Liste mitliefert."""
    if not isinstance(value, list):
        return None
    ids = set()
    for entry in value:
        address = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(address, str) and address:
            ids.add(address)
    return frozenset(ids) or None


def tagesordnungen(meeting_ids: Iterable[object]) -> dict[uuid.UUID, list[TopStand]]:
    """
    Alle Punkte der Sitzungen (auch gelöschte) mit ihrer heutigen Beschreibung.

    Ein Punkt steht nicht mehr auf der Tagesordnung, wenn er gelöscht ist oder die Quelle in der Sitzung eine
    Liste liefert, die ihn nicht enthält. Die Liste zählt nur, wenn sie mindestens einen Punkt des Bestands
    enthält – nach einem Umzug der Quelle mit neuer Adressform passt sie sonst zu keinem Punkt.
    """
    ids = [pk for pk in (_uuid(v) for v in meeting_ids) if pk]
    if not ids:
        return {}
    listen = dict(
        OParlMeeting.objects.filter(pk__in=ids)
        .annotate(_tagesordnung=KeyTransform("agendaItem", "raw_json"))
        .values_list("id", "_tagesordnung")
    )
    rows = list(OParlAgendaItem.objects.filter(meeting_id__in=ids).values(*_TOP_FELDER))
    vorlagen = _vorlagen_je_top(row["external_id"] for row in rows)

    adressen: dict[uuid.UUID, frozenset[str] | None] = {}
    for meeting_id in ids:
        liste = _adressen_der_tagesordnung(listen.get(meeting_id))
        eigene = {row["external_id"] for row in rows if row["meeting_id"] == meeting_id}
        adressen[meeting_id] = liste if liste and liste & eigene else None

    result: dict[uuid.UUID, list[TopStand]] = {pk: [] for pk in ids}
    for row in rows:
        liste = adressen.get(row["meeting_id"])
        geloescht = bool(row["deleted"])
        auf_tagesordnung = not geloescht and (liste is None or row["external_id"] in liste)
        result[row["meeting_id"]].append(
            TopStand(
                id=row["id"],
                kennung=_kennung(row, vorlagen.get(row["external_id"], [])),
                auf_tagesordnung=auf_tagesordnung,
                geloescht=geloescht,
            )
        )
    return result


def sitzungen_geaendert(seit: Mapping[Any, datetime | None], *, ruhig_seit: datetime | None = None) -> set[uuid.UUID]:
    """
    Sitzungen, an denen sich seit dem jeweiligen Zeitpunkt etwas geändert hat (Sitzung oder einer ihrer Punkte).

    ``None`` als Zeitpunkt: noch nie geprüft. Mit ``ruhig_seit`` fallen Sitzungen weg, die sich danach noch
    geändert haben – ein Abgleich mitten im Abruf einer Tagesordnung sähe einen halben Stand.
    """
    zeitpunkte = {pk: since for pk, since in ((_uuid(k), v) for k, v in seit.items()) if pk}
    if not zeitpunkte:
        return set()
    ids = list(zeitpunkte)
    sitzung = dict(OParlMeeting.objects.filter(pk__in=ids).values_list("id", "updated_at"))
    punkte = dict(
        OParlAgendaItem.objects.filter(meeting_id__in=ids)
        .values("meeting_id")
        .annotate(zuletzt=Max("updated_at"))
        .values_list("meeting_id", "zuletzt")
    )
    result = set()
    for pk, since in zeitpunkte.items():
        if pk not in sitzung:
            continue
        stempel = [s for s in (sitzung.get(pk), punkte.get(pk)) if s is not None]
        zuletzt = max(stempel) if stempel else None
        if ruhig_seit is not None and zuletzt is not None and zuletzt > ruhig_seit:
            continue
        if since is None or (zuletzt is not None and zuletzt > since):
            result.add(pk)
    return result


@dataclass(frozen=True)
class Zuordnung:
    """Ergebnis einer Zuordnung: ``ergebnis``, ggf. das Zielobjekt und dessen heutige Beschreibung."""

    ergebnis: str
    ziel: uuid.UUID | None = None
    kennung: Any = None


def _gleich_ueber_vorlage(anker: TopKennung, kennung: TopKennung) -> bool | None:
    """Dieselbe Vorlage? ``None``, wenn eine Seite keine Vorlage hat (dann entscheidet der Name)."""
    if not anker.mit_vorlage or not kennung.mit_vorlage:
        return None
    return bool(set(anker.papers) & set(kennung.papers)) or bool(set(anker.references) & set(kennung.references))


def gleicher_top(anker: TopKennung, kennung: TopKennung) -> bool:
    """Beschreiben beide denselben fachlichen Punkt (strenge Regel für Nachfolger)?"""
    ueber_vorlage = _gleich_ueber_vorlage(anker, kennung)
    if ueber_vorlage is not None:
        return ueber_vorlage
    return bool(anker.name) and anker.name == kennung.name and anker.public == kennung.public


def _korrigiert(anker: TopKennung, kennung: TopKennung) -> bool:
    """Leicht geänderter Name desselben Punkts (gilt nur für den Punkt selbst, nicht für Nachfolger)."""
    if _gleich_ueber_vorlage(anker, kennung) is False or anker.public != kennung.public:
        return False
    if not anker.name or not kennung.name:
        return False
    return SequenceMatcher(None, anker.name, kennung.name).ratio() >= AEHNLICH_AB


def _eingrenzen(anker: TopKennung, treffer: list[TopStand]) -> list[TopStand]:
    """Mehrere Treffer: zuerst gleicher Name, dann gleiche Nummer (jeweils nur, wenn danach noch einer bleibt)."""
    for merkmal in ("name", "number"):
        if len(treffer) <= 1:
            break
        enger = [s for s in treffer if getattr(s.kennung, merkmal) == getattr(anker, merkmal)]
        if enger:
            treffer = enger
    return treffer


def top_zuordnen(anker: TopKennung, objekt: object, stand: Iterable[TopStand]) -> Zuordnung:
    """
    Wo steht der mit ``anker`` beschriebene Punkt heute? ``objekt`` ist die Zeile, an der die Daten hängen,
    ``stand`` die Tagesordnung seiner Sitzung (``tagesordnungen``).
    """
    pk = _uuid(objekt)
    staende = list(stand)
    selbst = next((s for s in staende if s.id == pk), None)
    steht = selbst is not None and selbst.auf_tagesordnung

    if selbst is not None and steht and gleicher_top(anker, selbst.kennung):
        return Zuordnung(BESTAETIGT, selbst.id, selbst.kennung.mit_vorlagen_von(anker))

    treffer = _eingrenzen(
        anker, [s for s in staende if s.id != pk and s.auf_tagesordnung and gleicher_top(anker, s.kennung)]
    )
    if len(treffer) == 1:
        return Zuordnung(NACHFOLGER, treffer[0].id, treffer[0].kennung.mit_vorlagen_von(anker))
    if len(treffer) > 1:
        return Zuordnung(MEHRDEUTIG)
    if selbst is not None and steht and _korrigiert(anker, selbst.kennung):
        return Zuordnung(BESTAETIGT, selbst.id, selbst.kennung.mit_vorlagen_von(anker))
    return Zuordnung(ABWEICHEND if steht else ENTFALLEN)


# =============================================================================
# Vorlagen
# =============================================================================


@dataclass(frozen=True)
class VorlagenKennung:
    """Fachliche Beschreibung einer Vorlage: Kommune und Drucksachennummer."""

    body: str
    reference: str = ""
    name: str = ""
    title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"body": self.body, "reference": self.reference, "name": self.name, "title": self.title}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> VorlagenKennung | None:
        if not data or not data.get("body"):
            return None
        return cls(
            body=str(data["body"]),
            reference=str(data.get("reference") or ""),
            name=str(data.get("name") or ""),
            title=str(data.get("title") or ""),
        )


@dataclass(frozen=True)
class VorlagenStand:
    """Eine Vorlage, wie sie heute im Bestand steht."""

    id: uuid.UUID
    kennung: VorlagenKennung
    geloescht: bool
    geaendert: datetime | None


_VORLAGEN_FELDER = ("id", "body_id", "reference", "name", "deleted", "updated_at")


def _vorlagen_stand(row: Mapping[str, Any]) -> VorlagenStand:
    return VorlagenStand(
        id=row["id"],
        kennung=VorlagenKennung(
            body=str(row["body_id"] or ""),
            reference=(row["reference"] or "").strip(),
            name=name_key(row["name"]),
            title=_titel(row["name"]),
        ),
        geloescht=bool(row["deleted"]),
        geaendert=row["updated_at"],
    )


def vorlagen(paper_ids: Iterable[object]) -> dict[uuid.UUID, VorlagenStand]:
    """Heutiger Stand der Vorlagen (fehlende Kennungen fehlen im Ergebnis)."""
    ids = [pk for pk in (_uuid(v) for v in paper_ids) if pk]
    if not ids:
        return {}
    return {row["id"]: _vorlagen_stand(row) for row in OParlPaper.objects.filter(pk__in=ids).values(*_VORLAGEN_FELDER)}


def vorlagen_mit_nummer(anker: VorlagenKennung) -> list[VorlagenStand]:
    """Nicht gelöschte Vorlagen derselben Kommune mit derselben Drucksachennummer."""
    gesucht = reference_key(anker.reference)
    body = _uuid(anker.body)
    if body is None or not gesucht:
        return []
    # Leerraum an beliebiger Stelle zulassen (wie ``reference_key``), Groß/klein egal
    muster = r"^\s*" + r"\s*".join(re.escape(zeichen) for zeichen in gesucht) + r"\s*$"
    rows = OParlPaper.objects.filter(body_id=body, deleted=False, reference__iregex=muster).values(*_VORLAGEN_FELDER)
    return [s for s in map(_vorlagen_stand, rows) if reference_key(s.kennung.reference) == gesucht]


def vorlage_zuordnen(
    anker: VorlagenKennung, objekt: object, stand: VorlagenStand | None, kandidaten: Iterable[VorlagenStand]
) -> Zuordnung:
    """Wo steht die mit ``anker`` beschriebene Vorlage heute? ``kandidaten`` aus ``vorlagen_mit_nummer``."""
    pk = _uuid(objekt)
    if stand is not None and not stand.geloescht:
        return Zuordnung(BESTAETIGT, stand.id, stand.kennung)
    gesucht = reference_key(anker.reference)
    treffer = [
        k
        for k in kandidaten
        if k.id != pk
        and not k.geloescht
        and gesucht
        and k.kennung.body == anker.body
        and reference_key(k.kennung.reference) == gesucht
    ]
    if len(treffer) > 1:
        treffer = [k for k in treffer if k.kennung.name == anker.name] or treffer
    if len(treffer) == 1:
        return Zuordnung(NACHFOLGER, treffer[0].id, treffer[0].kennung)
    if len(treffer) > 1:
        return Zuordnung(MEHRDEUTIG)
    return Zuordnung(ENTFALLEN)
