# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenzugriffe des Finanzchecks (Issue #1513, ``insight_ai.services.finanzcheck``): Auswahl, Speichern, Suchen.

Der Finanzcheck liegt an ``OParlPaper.finanzcheck``. Wie die Zusammenfassung (``summary_store``) wird er nur
gespeichert, wenn Vorgang und verwendete Anlagen noch gelten und seit Beginn kein neuer Text kam; verworfen wird er an
denselben Stellen wie die Zusammenfassung.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from ..models import OParlFile, OParlPaper


def ohne_finanzcheck(kommunen: Mapping[Any, date], *, limit: int, ohne: Iterable[Any] = ()) -> list[OParlPaper]:
    """
    Vorgänge ausgewählter Kommunen ohne Finanzcheck, mit Text, neueste zuerst: dieselbe Auswahl wie
    ``summary_store.fuer_tagesbatch`` (Stichtag je Kommune, gelistet, Veröffentlichung nicht pausiert), aber
    unabhängig davon, ob schon eine Zusammenfassung da ist. ``ohne``: Kennungen, die übersprungen werden.
    """
    from insight_core import publication

    if not kommunen:
        return []
    gesperrt = [uuid.UUID(k) for k, stand in publication.states().items() if stand.paused or stand.withdrawn]
    filter_kommunen = Q()
    for kommune, ab in kommunen.items():
        filter_kommunen |= Q(body_id=kommune, date__gte=ab)
    mit_text = (
        OParlFile.objects.filter(paper_id=OuterRef("pk"), deleted=False, source_missing_since__isnull=True)
        .filter(text_content__isnull=False)
        .exclude(text_content="")
    )
    return list(
        OParlPaper.objects.filter(filter_kommunen)
        .filter(finanzcheck__isnull=True, deleted=False, body__deleted=False, body__is_listed=True)
        .filter(Exists(mit_text))
        .exclude(body_id__in=gesperrt)
        .exclude(pk__in=list(ohne))
        .select_related("body", "body__source")
        .defer("raw_json", "summary_einfach")
        .order_by("-date", "-pk")[: max(0, limit)]
    )


def speichern(
    vorgang_id: Any, wert: dict[str, Any], *, dateien: list[str], seit: datetime, mit_text: Iterable[str] = ()
) -> bool:
    """
    Speichert den Finanzcheck, wenn der Vorgang noch gilt, keine der verwendeten Anlagen gelöscht oder in der Quelle
    verschwunden ist, seit ``seit`` kein neuer Text kam und der Text der Anlagen aus ``mit_text`` (die in die Anfrage
    eingingen) noch erkannt vorliegt, also nicht ersetzt oder zurückgesetzt wurde; Rückgabe: gespeichert? Der Vorgang
    ist dabei gesperrt wie bei ``summary_store.speichern``: Eine gleichzeitige Rücknahme wartet und verwirft ihn
    danach.
    """
    with transaction.atomic():
        vorgang = OParlPaper.objects.select_for_update(no_key=True).filter(pk=vorgang_id).only("pk", "deleted").first()
        if vorgang is None or vorgang.deleted:
            return False
        # Die Anlagen bleiben gesperrt, bis gespeichert ist: Neuer Text oder eine Rücknahme einer Anlage wartet und
        # verwirft den Finanzcheck danach. Ist eine Anlage schon gesperrt (sie wird gerade geändert oder
        # zurückgenommen, wobei die Rücknahme danach den Vorgang sperrt), wartet das Speichern nicht, sondern verwirft
        # das Ergebnis: Sonst sperrten beide Seiten in umgekehrter Reihenfolge (Deadlock). Der nächste Lauf prüft
        # neu. Gelöschte, verschwundene oder einem anderen Vorgang zugeordnete Anlagen verwerfen ihn ebenso.
        if dateien:
            gesperrt = list(
                OParlFile.objects.select_for_update(no_key=True, skip_locked=True)
                .filter(pk__in=dateien)
                .values_list("paper_id", "deleted", "source_missing_since")
            )
            if len(gesperrt) != len(set(dateien)) or any(
                str(vorgang_von) != str(vorgang.pk) or geloescht or verschwunden
                for vorgang_von, geloescht, verschwunden in gesperrt
            ):
                return False
        if OParlFile.objects.filter(
            paper_id=vorgang_id, text_extraction_status="completed", text_extracted_at__gt=seit
        ).exists():
            return False
        mit_text = list(mit_text)
        if (
            mit_text
            and OParlFile.objects.filter(pk__in=mit_text)
            .filter(
                Q(text_extraction_status__in=("pending", "processing"))
                | Q(text_content__isnull=True)
                | Q(text_content="")
            )
            .exists()
        ):
            return False
        OParlPaper.objects.filter(pk=vorgang_id).update(finanzcheck=wert)
    return True


def vorgang_finden(schluessel: str, kommune: str | None = None) -> OParlPaper:
    """Vorgang über Kennung (UUID) oder Vorlagen-Nr., ggf. in einer Kommune; ``LookupError``, wenn nicht eindeutig."""
    from . import summary_store

    try:
        return OParlPaper.objects.select_related("body").get(pk=uuid.UUID(schluessel), deleted=False)
    except (ValueError, OParlPaper.DoesNotExist):
        pass
    auswahl = OParlPaper.objects.select_related("body").filter(reference=schluessel, deleted=False)
    if kommune:
        auswahl = auswahl.filter(body=summary_store.kommune_finden(kommune))
    treffer = list(auswahl[:2])
    if len(treffer) != 1:
        raise LookupError(f"„{schluessel}“ passt auf {len(treffer)} Vorgänge; --kommune angeben")
    return treffer[0]
