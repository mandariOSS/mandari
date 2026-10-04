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
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from typing import Any, Final

from django.db.models import QuerySet

from insight_core.models import (
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services.search_documents import (
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
        return OParlFile.objects.filter(
            deleted=False,
            source_missing_since__isnull=True,
            text_content__isnull=False,
            text_extraction_status="completed",
        )
    raise ValueError(f"Unbekannter Suchindex: {index}")


def _vorgeladen(index: str, abfrage: QuerySet[Any]) -> QuerySet[Any]:
    if index == "meetings":
        return abfrage.prefetch_related("organizations")
    if index == "files":
        return abfrage.select_related("paper")
    return abfrage


def build(index: str, obj: Any) -> dict[str, Any]:
    """Suchdokument zu einem Objekt (Dokumentbauer aus ``search_documents``)."""
    if index == "papers":
        return paper_to_doc(obj)
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
    groesse = _CHUNK_FILES if index == "files" else _CHUNK
    for start in range(0, len(kennungen), groesse):
        abschnitt = kennungen[start : start + groesse]
        objekte = {obj.pk: obj for obj in _vorgeladen(index, indexable(index).filter(pk__in=abschnitt))}
        for kennung in abschnitt:
            obj = objekte.pop(kennung, None)
            yield kennung, (build(index, obj) if obj is not None else None)


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


def count_documents(index: str, body_ids: Iterable[uuid.UUID] = ()) -> int:
    """Zahl der Dokumente eines Index, auf Kommunen begrenzt (leer = alle)."""
    return _bestand(index, body_ids).count()


def iter_all_documents(index: str, body_ids: Iterable[uuid.UUID] = ()) -> Iterator[dict[str, Any]]:
    """Alle Dokumente eines Index für den Vollbau, auf Kommunen begrenzt (leer = alle), nach Kennung."""
    groesse = _CHUNK_FILES if index == "files" else _CHUNK
    for obj in _vorgeladen(index, _bestand(index, body_ids).order_by("pk")).iterator(chunk_size=groesse):
        yield build(index, obj)


def _bestand(index: str, body_ids: Iterable[uuid.UUID]) -> QuerySet[Any]:
    kommunen = list(body_ids)
    abfrage = indexable(index)
    return abfrage.filter(body_id__in=kommunen) if kommunen else abfrage
