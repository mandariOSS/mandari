# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aktueller Stand der Suchdokumente aus dem RIS-Bestand (Abonnement ``suchindex``, Issue #526).

Das Abonnement (``insight_search.abonnement``) und der Vollbau des Schattenindex
(``manage.py suchindex_schatten aufbauen``) fragen hier, welche Dokumente zu einer Liste von
Kennungen gehören. Gebaut wird mit den Dokumentbauern aus ``search_documents`` (dieselben wie für
``reindex_elasticsearch``); was in den Index gehört, steht in ``indexable``:

- Vorgänge, Sitzungen, Personen, Gremien: ohne Löschmarkierung der Quelle;
- Dateien: zusätzlich mit erkanntem Text und in der Quelle weiter abrufbar (Löschabgleich, #787).

Ein Objekt, das es nicht mehr gibt oder das nicht mehr in den Index gehört, liefert ``None``: Das
Abonnement löscht dann sein Dokument. So ergibt jedes Ereignis den aktuellen Stand, unabhängig davon,
was das Ereignis selbst meldet, und ein wiederholtes oder verspätetes Ereignis schadet nicht.

Vorgänge werden mit ihren Beratungen und der Textvorschau ihrer Dateien vorgeladen (nur die ersten
Zeichen, die ``paper_to_doc`` nutzt), damit ein Batch nicht je Vorgang mehrere Abfragen stellt. Dateien
lösen ihren Kontext (Sitzung, Gremien, Tagesordnungspunkt) je Block von bis zu 200 Dateien gemeinsam auf
(``search_documents.file_contexts``, Issue #821); ihre Texte werden danach in kleinen Abschnitten geladen.

Welche Dokumente eine Änderung außer dem eigenen betrifft, bestimmen ``related_paper_ids``,
``related_file_ids``, ``context_file_ids``, ``meetings_of_organizations`` und ``papers_of_organizations``.
Sie liefern eine Obermenge: Jedes Dokument wird ohnehin aus dem aktuellen Bestand gebaut, ein Dokument zu
viel kostet nur Arbeit.

**Ablage (Übergang):** Laut Schichtenmodell (``docs/adr/20260929-schichtenmodell.md``) gehört die
Suchindex-Projektion nach ``hub/projections`` und liest den RIS-Bestand über die Lese-Fassade
``hub/ris/selectors.py``; ``insight_core`` behält nur den Bestand. Das Modul zieht spätestens mit dem
Umschalten (Issue #527) dorthin um; bis dahin liegen die Lesezugriffe gebündelt hier.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from itertools import batched
from typing import Any, Final, NamedTuple

from django.db.models import Prefetch, Q, QuerySet
from django.db.models.functions import Substr

from insight_core.models import (
    OParlAgendaItem,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services.search_documents import (
    PREVIEW_CHARS_PER_FILE,
    file_contexts,
    file_to_doc,
    meeting_to_doc,
    organization_to_doc,
    paper_to_doc,
    person_to_doc,
)

INDEXES: Final = ("papers", "meetings", "persons", "organizations", "files")
#: Kennungen je Datenbankabfrage; Dateien tragen ihren ganzen Text, deshalb weniger
_CHUNK: Final = 200
_CHUNK_FILES: Final = 10


#: Vorgeladene Textvorschau der Dateien eines Vorgangs (Attribut am Vorgang)
_VORSCHAU: Final = "suchindex_dateien"


class _DateiVorschau(NamedTuple):
    """Was ``paper_to_doc`` von einer Datei liest: Dateiname und die ersten Zeichen des Texts."""

    file_name: str | None
    text_content: str | None


def _indexierbare_dateien() -> QuerySet[OParlFile]:
    return OParlFile.objects.filter(
        deleted=False,
        source_missing_since__isnull=True,
        text_content__isnull=False,
        text_extraction_status="completed",
    )


def indexable(index: str) -> QuerySet[Any]:
    """Alle Objekte, die in den Suchindex ``index`` gehören (ohne Vorladen)."""
    if index == "papers":
        return OParlPaper.objects.filter(deleted=False)
    if index == "meetings":
        return OParlMeeting.objects.filter(deleted=False)
    if index == "persons":
        return OParlPerson.objects.filter(deleted=False)
    if index == "organizations":
        return OParlOrganization.objects.filter(deleted=False)
    if index == "files":
        return _indexierbare_dateien()
    raise ValueError(f"Unbekannter Suchindex: {index}")


def _vorgeladen(index: str, abfrage: QuerySet[Any]) -> QuerySet[Any]:
    if index == "papers":
        # Dieselbe Auswahl wie paper_to_doc ohne Vorladen; vom Text nur, was die Vorschau nutzt
        dateien = (
            _indexierbare_dateien()
            .only("id", "paper_id", "file_name")
            .annotate(text_anfang=Substr("text_content", 1, PREVIEW_CHARS_PER_FILE))
        )
        return abfrage.prefetch_related("consultations", Prefetch("files", queryset=dateien, to_attr=_VORSCHAU))
    if index == "meetings":
        return abfrage.prefetch_related("organizations")
    if index == "files":
        return abfrage.select_related("paper")
    return abfrage


def build(index: str, obj: Any) -> dict[str, Any]:
    """Suchdokument zu einem Objekt (Dokumentbauer aus ``search_documents``)."""
    if index == "papers":
        vorgeladen = getattr(obj, _VORSCHAU, None)
        if vorgeladen is None:
            return paper_to_doc(obj)
        return paper_to_doc(obj, files=[_DateiVorschau(datei.file_name, datei.text_anfang) for datei in vorgeladen])
    if index == "meetings":
        return meeting_to_doc(obj)
    if index == "persons":
        return person_to_doc(obj)
    if index == "organizations":
        return organization_to_doc(obj)
    if index == "files":
        return file_to_doc(obj)
    raise ValueError(f"Unbekannter Suchindex: {index}")


def iter_documents(index: str, ids: Iterable[uuid.UUID]) -> Iterator[tuple[uuid.UUID, dict[str, Any] | None]]:
    """Je Kennung das aktuelle Dokument oder ``None`` (Dokument gehört nicht in den Index).

    Gelesen wird in Abschnitten; ein Dokument entsteht erst, wenn der Aufrufer es abholt, damit nie
    viele Dateitexte gleichzeitig im Speicher liegen.
    """
    kennungen = list(dict.fromkeys(ids))
    if index == "files":
        yield from _iter_files(kennungen)
        return
    for start in range(0, len(kennungen), _CHUNK):
        abschnitt = kennungen[start : start + _CHUNK]
        objekte = {obj.pk: obj for obj in _vorgeladen(index, indexable(index).filter(pk__in=abschnitt))}
        for kennung in abschnitt:
            obj = objekte.pop(kennung, None)
            yield kennung, (build(index, obj) if obj is not None else None)


def _iter_files(kennungen: list[uuid.UUID]) -> Iterator[tuple[uuid.UUID, dict[str, Any] | None]]:
    """
    Dateien in Blöcken von ``_CHUNK``: der Kontext je Block mit wenigen Abfragen (``file_contexts``), die
    Texte danach in Abschnitten von ``_CHUNK_FILES``. Je Block also 1 + höchstens 4 + Block/``_CHUNK_FILES``
    Abfragen, gleich an wie vielen Vorgängen und Sitzungen die Dateien hängen.
    """
    for start in range(0, len(kennungen), _CHUNK):
        block = kennungen[start : start + _CHUNK]
        koepfe = {
            kopf.pk: kopf for kopf in indexable("files").filter(pk__in=block).only("id", "paper_id", "meeting_id")
        }
        kontexte = file_contexts(koepfe.values())
        for teil in range(0, len(block), _CHUNK_FILES):
            abschnitt = block[teil : teil + _CHUNK_FILES]
            objekte = {obj.pk: obj for obj in _vorgeladen("files", indexable("files").filter(pk__in=abschnitt))}
            for kennung in abschnitt:
                obj = objekte.pop(kennung, None)
                if obj is None:
                    yield kennung, None
                    continue
                kopf = koepfe.get(kennung)
                if kopf is None or (kopf.paper_id, kopf.meeting_id) != (obj.paper_id, obj.meeting_id):
                    # Zwischen beiden Abfragen neu zugeordnet: Kontext dieser Datei einzeln
                    kontext = file_contexts([obj]).get(kennung, {})
                else:
                    kontext = kontexte.get(kennung, {})
                yield kennung, file_to_doc(obj, context_info=kontext)


def related_paper_ids(aggregate_type: str, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Vorgang je Datei bzw. Beratung (Objekt → Vorgang); sein Dokument hängt von ihnen ab (Textvorschau, Gremien).

    Gelöschte Objekte zählen mit: Auch ihr Wegfall ändert das Dokument des Vorgangs.
    """
    kennungen = list(ids)
    if not kennungen:
        return {}
    modell: type[OParlFile] | type[OParlConsultation]
    if aggregate_type == "File":
        modell = OParlFile
    elif aggregate_type == "Consultation":
        modell = OParlConsultation
    else:
        return {}
    ergebnis: dict[uuid.UUID, uuid.UUID] = {}
    for start in range(0, len(kennungen), _CHUNK):
        abschnitt = kennungen[start : start + _CHUNK]
        for objekt, vorgang in modell.objects.filter(pk__in=abschnitt, paper_id__isnull=False).values_list(
            "pk", "paper_id"
        ):
            ergebnis[objekt] = vorgang
    return ergebnis


def related_file_ids(aggregate_type: str, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Indexierbare Dateien eines Vorgangs bzw. einer Sitzung (Datei → Objekt).

    Ihr Dokument übernimmt Name und Aktenzeichen des Vorgangs bzw. Name und Datum der Sitzung, an der
    sie direkt hängen. Dateien, die nicht in den Index gehören, betrifft die Änderung nicht.
    """
    kennungen = list(ids)
    if aggregate_type == "Paper":
        feld = "paper_id"
    elif aggregate_type == "Meeting":
        feld = "meeting_id"
    else:
        return {}
    ergebnis: dict[uuid.UUID, uuid.UUID] = {}
    for start in range(0, len(kennungen), _CHUNK):
        abschnitt = kennungen[start : start + _CHUNK]
        for datei, objekt in _indexierbare_dateien().filter(**{f"{feld}__in": abschnitt}).values_list("pk", feld):
            ergebnis[datei] = objekt
    return ergebnis


def _pairs(
    abfrage: QuerySet[Any], feld: str, werte: Iterable[Any], spalten: tuple[str, str]
) -> Iterator[tuple[Any, Any]]:
    """``values_list(*spalten)`` für ``feld__in`` in Abschnitten von ``_CHUNK``."""
    for abschnitt in batched(dict.fromkeys(werte), _CHUNK):
        yield from abfrage.filter(**{f"{feld}__in": abschnitt}).values_list(*spalten)


def _files_of_papers(objekt_und_vorgang: Iterable[tuple[uuid.UUID, uuid.UUID]]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(Datei, Objekt) für die indexierbaren Dateien der Vorgänge aus Paaren (Objekt, Vorgang)."""
    objekte_je_vorgang: dict[uuid.UUID, set[uuid.UUID]] = {}
    for objekt, vorgang in objekt_und_vorgang:
        objekte_je_vorgang.setdefault(vorgang, set()).add(objekt)
    return [
        (datei, objekt)
        for datei, vorgang in _pairs(_indexierbare_dateien(), "paper_id", objekte_je_vorgang, ("pk", "paper_id"))
        for objekt in objekte_je_vorgang[vorgang]
    ]


def context_file_ids(aggregate_type: str, ids: Iterable[uuid.UUID]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """
    Indexierbare Dateien, deren Kontext (``meeting_name``, ``meeting_date``, ``organization_names``,
    ``agenda_number``) über eine Beratung vom Objekt abhängt, als (Datei, Objekt), Issue #821:

    - Sitzung: Dateien der Vorgänge, die in ihr beraten werden (Beratung mit ihrer Kennung);
    - Tagesordnungspunkt: Dateien der Vorgänge, die unter ihm beraten werden;
    - Beratung: Dateien ihres Vorgangs (welche Beratung den Kontext stellt, kann sich ändern).

    Dateien, die direkt an einer Sitzung hängen, liefert ``related_file_ids``.
    """
    kennungen = list(ids)
    if not kennungen:
        return []
    beratungen = OParlConsultation.objects.filter(paper_id__isnull=False)
    if aggregate_type == "Consultation":
        return _files_of_papers(_pairs(beratungen, "pk", kennungen, ("pk", "paper_id")))
    modell: type[OParlMeeting] | type[OParlAgendaItem]
    if aggregate_type == "Meeting":
        modell, feld = OParlMeeting, "meeting_external_id"
    elif aggregate_type == "AgendaItem":
        modell, feld = OParlAgendaItem, "agenda_item_external_id"
    else:
        return []
    objekt_je_kennung = dict(_pairs(modell.objects.all(), "pk", kennungen, ("external_id", "pk")))
    return _files_of_papers(
        (objekt_je_kennung[kennung], vorgang)
        for kennung, vorgang in _pairs(beratungen, feld, objekt_je_kennung, (feld, "paper_id"))
    )


def meetings_of_organizations(ids: Iterable[uuid.UUID]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Sitzungen der Gremien als (Sitzung, Gremium): Ihr Dokument nennt die Gremien (``organization_names``)."""
    zuordnung = OParlMeeting.organizations.through.objects.all()
    return list(_pairs(zuordnung, "oparlorganization_id", ids, ("oparlmeeting_id", "oparlorganization_id")))


def papers_of_organizations(ids: Iterable[uuid.UUID]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """
    Vorgänge, deren Beratungen das Gremium nennen, als (Vorgang, Gremium): ``paper_to_doc`` löst
    ``organization_names`` über ``raw_json["organization"]`` der Beratungen auf.

    Gesucht wird im Text der Verweise, eine Abfrage je Kommune für alle ihre Gremien des Batches und nur
    unter den Beratungen dieser Kommune (Index auf ``body_id``): Beim ersten Abgleich einer Kommune
    kommen viele Gremien auf einmal. Ein Verweis, der die Kennung nur enthält (``…/organization/12`` für
    ``…/organization/1``), ergibt eine harmlose Obermenge.
    """
    gremien_je_kommune: dict[uuid.UUID | None, dict[str, uuid.UUID]] = {}
    for abschnitt in batched(dict.fromkeys(ids), _CHUNK):
        for gremium, kennung, kommune in OParlOrganization.objects.filter(pk__in=abschnitt).values_list(
            "pk", "external_id", "body_id"
        ):
            if kennung:
                gremien_je_kommune.setdefault(kommune, {})[kennung] = gremium
    ergebnis: list[tuple[uuid.UUID, uuid.UUID]] = []
    for kommune, gremien in gremien_je_kommune.items():
        nennt = Q()
        for kennung in gremien:
            nennt |= Q(raw_json__organization__icontains=kennung)
        beratungen = OParlConsultation.objects.filter(nennt, paper_id__isnull=False)
        if kommune is not None:
            beratungen = beratungen.filter(Q(body_id=kommune) | Q(body_id__isnull=True))
        for vorgang, verweise in beratungen.values_list("paper_id", "raw_json__organization").distinct():
            text = (verweise if isinstance(verweise, str) else json.dumps(verweise, ensure_ascii=False)).lower()
            ergebnis.extend((vorgang, gremium) for kennung, gremium in gremien.items() if kennung.lower() in text)
    return ergebnis


def count_documents(index: str, body_ids: Iterable[uuid.UUID] = ()) -> int:
    """Zahl der Dokumente eines Index, auf Kommunen begrenzt (leer = alle)."""
    return _bestand(index, body_ids).count()


def iter_all_documents(index: str, body_ids: Iterable[uuid.UUID] = ()) -> Iterator[dict[str, Any]]:
    """Alle Dokumente eines Index für den Vollbau, auf Kommunen begrenzt (leer = alle), nach Kennung."""
    if index == "files":
        # Erst die Kennungen, dann wie im Abonnement: Kontext je Block gebündelt, Texte in kleinen Abschnitten
        kennungen = _bestand(index, body_ids).order_by("pk").values_list("pk", flat=True)
        for block in batched(kennungen.iterator(chunk_size=_CHUNK * 10), _CHUNK):
            for _, dokument in _iter_files(list(block)):
                if dokument is not None:
                    yield dokument
        return
    for obj in _vorgeladen(index, _bestand(index, body_ids).order_by("pk")).iterator(chunk_size=_CHUNK):
        yield build(index, obj)


def _bestand(index: str, body_ids: Iterable[uuid.UUID]) -> QuerySet[Any]:
    kommunen = list(body_ids)
    abfrage = indexable(index)
    return abfrage.filter(body_id__in=kommunen) if kommunen else abfrage
