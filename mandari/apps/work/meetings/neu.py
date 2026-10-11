# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen der Fraktion im neuen Erscheinungsbild (Issue #951, Teilpaket 2): zusätzlicher Kontext für „Meine
Sitzungen“, den Kalender und das Sitzungsdetail der Vorbereitung.

Die Views bleiben dieselben (Filter, Rechte); im neuen Erscheinungsbild (Schalter je Organisation,
``apps.work.rahmen.neues_design``) wählen sie eine Vorlage aus ``work/meetings/neu/`` und ergänzen hier, was die
gemeinsamen Bausteine brauchen: die Liste von Insight (``c-liste.sitzungen``) mit dem Vorbereitungsstand der
Organisation und Zielen in Work, den Satz im Kopfband statt Hinweiskasten und Gremien-Chips. Nichts hier schreibt.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.urls import reverse

from apps.common.formatting import count_label, join_parts
from apps.work.ris import neu as ris_neu
from apps.work.ris.links import WorkRisLinks

#: Zeitfilter der Liste wie bisher (Parameter ``time``)
ZEITRAEUME = (("upcoming", "Anstehend"), ("past", "Vergangen"), ("all", "Alle"))
#: So viele Gremien nennt der Satz im Kopfband, die übrigen als Zahl
GREMIEN_IM_SATZ = 3
_WORT_ZEITRAUM = {"upcoming": "anstehende", "past": "vergangene"}


class VorbereitungLinks(WorkRisLinks):
    """Wie die Recherche, nur öffnet eine Sitzung das Sitzungsdetail der Vorbereitung (bisheriges Ziel der Liste)."""

    def url(self, art: str, kennung: Any) -> str:
        if art == "sitzung":
            return reverse("work:meeting_detail", kwargs={"org_slug": self.org_slug, "meeting_id": kennung})
        return super().url(art, kennung)


def _gremien_text(gremien: Sequence[Any]) -> str:
    """„Rat, Hauptausschuss und Bauausschuss“ bzw. „Rat, Hauptausschuss, Bauausschuss und 2 weiteren“."""
    namen = [g.name for g in gremien if getattr(g, "name", "")]
    if len(namen) <= GREMIEN_IM_SATZ:
        return join_parts(namen)
    rest = len(namen) - GREMIEN_IM_SATZ
    return join_parts([*namen[:GREMIEN_IM_SATZ], f"{rest} weiteren"])


def sitzungen_satz(context: dict[str, Any]) -> str:
    """
    Der Satz im Kopfband: wie viele Sitzungen welcher Gremien die Liste zeigt. Ersetzt den Untertitel, den
    Hinweiskasten „Keine Gremien ausgewählt“ und die Gremien-Chips unter der Liste (alle Angaben bleiben im Satz).
    """
    if not context.get("has_body"):
        return "Ihre Organisation ist noch mit keiner Kommune verknüpft."
    anzahl = len(context.get("meetings") or [])
    zeitraum = _WORT_ZEITRAUM.get(context.get("time_filter", ""), "")
    sitzungen = count_label(anzahl, f"{zeitraum} Sitzung".strip(), f"{zeitraum} Sitzungen".strip())
    if context.get("view_mode") != "my":
        return f"{sitzungen} aller Gremien."
    gremien = list(context.get("my_committees") or [])
    if not gremien:
        return (
            f"{sitzungen} aller Gremien: Sie folgen noch keinen Gremien, und Ihnen sind keine zugewiesen. Wählen Sie "
            "Ihre Gremien, damit hier nur deren Sitzungen stehen."
        )
    art = "denen Sie folgen" if context.get("my_committees_followed") else "die Ihnen zugewiesen sind"
    return f"{sitzungen} der Gremien, {art}: {_gremien_text(gremien)}."


def sitzungen(organization: Any, membership: Any, meetings: Iterable[Any], context: dict[str, Any]) -> dict[str, Any]:
    """„Meine Sitzungen“: Vorbereitungsstand je Sitzung (Punkte, Vorlagen, Positionen) und der Satz im Kopfband."""
    erlaubt = ris_neu.rechte(membership)
    liste = list(meetings)
    if liste and erlaubt["darf_sitzungen"]:
        from apps.work.dashboard.selectors import vorbereitungsstand

        stand = vorbereitungsstand(organization, [m.pk for m in liste])
        for sitzung in liste:
            sitzung.stand = stand.get(sitzung.pk)
            sitzung.anzahl_tops = sitzung.stand.tops if sitzung.stand else 0
    return {
        **erlaubt,
        "sitzung_links": VorbereitungLinks(organization.slug),
        "zeitraeume": ZEITRAEUME,
        "sitzungen_satz": sitzungen_satz(context),
    }


def sitzung(agenda_items: Sequence[Any]) -> dict[str, Any]:
    """
    Sitzungsdetail: je Tagesordnungspunkt alle Vorlagen (eine Abfrage). Die bisherige Seite wollte die erste Vorlage
    samt Dateien zeigen, las sie aber aus einem Feld, das es am Tagesordnungspunkt nicht gibt; die Vorlagen führen
    jetzt in die Recherche, ihre Dateien stehen dort.
    """
    from hub.ris import selectors as ris

    vorlagen = ris.papers_of_agenda_items(agenda_items)
    return {"tagesordnung": [{"item": item, "papers": vorlagen.get(item.pk, [])} for item in agenda_items]}
