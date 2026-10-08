# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Seiten von Work im neuen Erscheinungsbild (Issues #852, #853): zusätzlicher Kontext je Seite.

Die Views bleiben dieselben (Filter, Seiten, Rechte); im neuen Erscheinungsbild (Schalter je Organisation,
``apps.work.rahmen.neues_design``) wählen sie eine Vorlage aus ``work/ris/neu/`` und ergänzen hier, was die
Bausteine von Insight brauchen – Stand-Satz und Zeitstrahl aus dem Beratungsverlauf
(``insight_core.services.paper_status``), Dokumentzeilen – und die Spalte bzw. Zeile „Für die Fraktion“
(``apps.work.ris.fraktion``). RIS-Daten nur über die Lese-Fassade ``hub.ris.selectors``; nichts hier schreibt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from django.urls import reverse
from django.utils import timezone

from hub.ris import selectors as ris

from . import fraktion
from .links import WorkRisLinks
from .selectors import _natural_key


def _ids(objekte: Iterable[Any]) -> list[uuid.UUID]:
    return [objekt.pk for objekt in objekte]


def _zahl(wert: int) -> str:
    """Zahl mit Tausenderpunkt („12.345“)."""
    return f"{wert:,}".replace(",", ".")


def _vorbereitungsstand(organization: Any, sitzungen: Sequence[Any]) -> dict[uuid.UUID, Any]:
    from apps.work.dashboard.selectors import vorbereitungsstand

    return vorbereitungsstand(organization, _ids(sitzungen))


def rechte(membership: Any) -> dict[str, bool]:
    """Was „Für die Fraktion“ zeigen darf (siehe ``apps.work.ris.fraktion``)."""
    return {
        "darf_vorbereiten": fraktion.darf(membership, "meetings.prepare"),
        "darf_sitzungen": fraktion.darf(membership, "meetings.view") or fraktion.darf(membership, "meetings.prepare"),
        "darf_dokument_anlegen": fraktion.darf(membership, "motions.create"),
        "darf_mitglieder": fraktion.darf(membership, "members.view"),
    }


def basis(organization: Any, membership: Any) -> dict[str, Any]:
    """Rechte und die Ziele der gemeinsamen Listen von Insight (``c-liste.*``): Einträge öffnen die Recherche."""
    return {**rechte(membership), "ris_links": WorkRisLinks(organization.slug)}


def _angaben(personen: Iterable[Any]) -> dict[Any, Any]:
    """Fraktion, Funktion und Gremien je Person wie in Insight (Fraktion auch aus der Zuordnung, Issue #916)."""
    from insight_core.services.personen_liste import angaben_fuer

    return angaben_fuer(personen)


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------


def uebersicht_satz(stats: dict[str, int], body: Any) -> str:
    """Die Kennzahlen der bisherigen Kacheln als ein Satz (Konzept W11: Zahlen im Satz statt Zählerkacheln)."""
    ort = f"aus {body.get_display_name()}" if body is not None else ""
    return (
        f"Ratsinformationen {ort}: {_zahl(stats['papers_total'])} Vorgänge, davon "
        f"{_zahl(stats['papers_this_year'])} aus diesem Jahr; {_zahl(stats['meetings_total'])} Sitzungen, davon "
        f"{_zahl(stats['meetings_upcoming'])} angesetzt; {_zahl(stats['organizations_active'])} aktive von "
        f"{_zahl(stats['organizations_total'])} Gremien und {_zahl(stats['persons_total'])} Personen."
    ).replace("Ratsinformationen :", "Ratsinformationen:")


def uebersicht(organization: Any, membership: Any, bodies: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Übersicht: Satz mit den Kennzahlen, nächste Sitzungen mit Vorbereitungsstand, neue Vorgänge mit Stand,
    für die Fraktion die eigenen Gremien und zuletzt eingereichte Dokumente."""
    from apps.work.organization.selectors import my_committees
    from insight_core.services.search_presentation import statuses_for_papers

    erlaubt = basis(organization, membership)
    sitzungen = list(context.get("upcoming_meetings") or [])
    vorgaenge = list(context.get("recent_papers") or [])
    if erlaubt["darf_sitzungen"]:
        stand = _vorbereitungsstand(organization, sitzungen)
        for sitzung in sitzungen:
            sitzung.stand = stand.get(sitzung.pk)
    staende = statuses_for_papers(str(v.pk) for v in vorgaenge)
    for vorgang in vorgaenge:
        vorgang.stand = staende.get(str(vorgang.pk))
    gremien = my_committees(membership).within(bodies)
    return {
        **erlaubt,
        "uebersicht_satz": uebersicht_satz(context["stats"], context.get("body")),
        "upcoming_meetings": sitzungen,
        "recent_papers": vorgaenge,
        "meine_gremien": list(gremien.committees),
        "eingereichte_dokumente": fraktion.eingereichte_dokumente(organization, membership),
    }


# ---------------------------------------------------------------------------
# Sitzungen
# ---------------------------------------------------------------------------


#: Zeiträume der Sitzungsliste (Parameter ``view`` wie bisher)
ANSICHTEN = (("upcoming", "Kommende"), ("past", "Vergangene"), ("all", "Alle"))


def sitzungen(organization: Any, membership: Any, seite: Iterable[Any]) -> dict[str, Any]:
    """Sitzungsliste: je Sitzung der Vorbereitungsstand der Organisation (Punkte, Vorlagen, Positionen)."""
    erlaubt = basis(organization, membership)
    liste = list(seite)
    if erlaubt["darf_sitzungen"]:
        stand = _vorbereitungsstand(organization, liste)
        for sitzung in liste:
            sitzung.stand = stand.get(sitzung.pk)
    return {**erlaubt, "ansichten": ANSICHTEN}


def sitzung(organization: Any, membership: Any, meeting: Any) -> dict[str, Any]:
    """
    Sitzung: Tagesordnung natürlich sortiert, je Punkt alle Vorlagen (eine Abfrage) und – mit ``meetings.prepare`` –
    Position und Zahl der Notizen der Organisation; dazu Vorbereitungsstand und Dokumente zur Sitzung, und wie in
    Insight die Niederschrift, die Teilnahme der Öffentlichkeit (Übertragung) und die übrigen Sitzungsdateien.
    """
    from insight_core.services import file_reconcile

    erlaubt = basis(organization, membership)
    punkte = sorted(ris.agenda_items(meeting), key=lambda punkt: _natural_key(punkt.number))
    vorlagen = ris.papers_of_agenda_items(punkte)
    positionen: dict[uuid.UUID, fraktion.Position] = {}
    notizen: dict[uuid.UUID, int] = {}
    if erlaubt["darf_vorbereiten"]:
        positionen = fraktion.positionen(organization, _ids(punkte))
        notizen = fraktion.notizen_je_punkt(organization, _ids(punkte))
    stand = _vorbereitungsstand(organization, [meeting]).get(meeting.pk) if erlaubt["darf_sitzungen"] else None
    niederschrift = ris.protocol_file(meeting)
    dateien = ris.files_of_meeting(meeting, ohne=[niederschrift.pk] if niederschrift is not None else [])
    return {
        **erlaubt,
        "tagesordnung": [
            {
                "item": punkt,
                "papers": vorlagen.get(punkt.pk, []),
                "position": positionen.get(punkt.pk),
                "notizen": notizen.get(punkt.pk, 0),
            }
            for punkt in punkte
        ],
        "stand": stand,
        "dokumente": fraktion.dokumente_zur_sitzung(organization, membership, meeting),
        "protocol_file": niederschrift,
        "broadcast": ris.broadcast_info(meeting),
        "sitzungsdateien": [d for d in dateien if not file_reconcile.is_blocked(d)],
        "sitzungsdateien_gesperrt": [d for d in dateien if file_reconcile.is_blocked(d)],
        "ist_kommend": bool(meeting.start and meeting.start > timezone.now()),
    }


# ---------------------------------------------------------------------------
# Vorgänge
# ---------------------------------------------------------------------------


def _letzte_position(
    item_ids: Iterable[uuid.UUID], positionen: dict[uuid.UUID, fraktion.Position]
) -> fraktion.Position | None:
    gesetzt = [positionen[item_id] for item_id in item_ids if item_id in positionen]
    return max(gesetzt, key=lambda position: position.geaendert) if gesetzt else None


def vorgaenge(organization: Any, membership: Any, seite: Iterable[Any]) -> dict[str, Any]:
    """
    Vorgangsliste: je Vorgang der Stand-Satz (eine Abfrage für die Seite) und für die Fraktion die zuletzt gesetzte
    Position (mit ``meetings.prepare``) und die Dokumente der Organisation zur Vorlage.
    """
    from insight_core.services.search_presentation import statuses_for_papers

    erlaubt = basis(organization, membership)
    liste = list(seite)
    ids = _ids(liste)
    staende = statuses_for_papers(str(pk) for pk in ids)
    punkte: dict[uuid.UUID, set[uuid.UUID]] = {}
    positionen: dict[uuid.UUID, fraktion.Position] = {}
    if erlaubt["darf_vorbereiten"]:
        punkte = ris.agenda_items_of_papers(ids)
        positionen = fraktion.positionen(organization, {i for menge in punkte.values() for i in menge})
    dokumente = fraktion.dokumente_zu_vorlagen(organization, membership, ids)
    for vorgang in liste:
        vorgang.stand = staende.get(str(vorgang.pk))
        vorgang.position = _letzte_position(punkte.get(vorgang.pk, ()), positionen)
        vorgang.dokumente = dokumente.get(vorgang.pk, [])
    return erlaubt


def _dateien(paper: Any) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """(abrufbare Dateien, nicht mehr abrufbare, Dateien nur aus den Rohdaten). Zurückgenommene fehlen ganz."""
    from insight_core.services import file_reconcile

    from .selectors import paper_files

    dateien: Any
    dateien, aus_rohdaten = paper_files(paper)
    if aus_rohdaten:
        return [], [], list(dateien)
    sichtbar = [datei for datei in dateien if not datei.withdrawn_by_publisher]
    return (
        [d for d in sichtbar if not file_reconcile.is_blocked(d)],
        [d for d in sichtbar if file_reconcile.is_blocked(d)],
        [],
    )


def _quelle(paper: Any) -> str:
    """Seite des Vorgangs im Ratsinformationssystem (OParl ``web``), nur als http(s)-Adresse."""
    web = (paper.raw_json or {}).get("web") if isinstance(paper.raw_json, dict) else None
    return web if isinstance(web, str) and web.startswith(("https://", "http://")) else ""


def vorgang(organization: Any, membership: Any, paper: Any) -> dict[str, Any]:
    """
    Vorgang: Stand-Satz und Beratungsverlauf als Zeitstrahl (wie Insight), Dokumentzeilen, Angaben – und für die
    Fraktion die Positionen je Beratung mit Begründung und Ergebnis (Übergabe-Infos), Notizen, Kommentare,
    Dokumente und der Weg in die Vorbereitung der nächsten bzw. letzten Beratung.
    """
    from insight_core.services.paper_status import paper_status, timeline

    erlaubt = basis(organization, membership)
    jetzt = timezone.now()
    verlauf = ris.consultation_history(paper)
    status = paper_status(verlauf, jetzt)
    zeitstrahl = timeline(verlauf, status, jetzt)
    item_ids = [eintrag["agenda_item"].pk for eintrag in verlauf if eintrag["agenda_item"] is not None]
    positionen: dict[uuid.UUID, fraktion.Position] = {}
    notizen = kommentare = 0
    if erlaubt["darf_vorbereiten"]:
        positionen = fraktion.positionen(organization, item_ids)
        notizen = sum(fraktion.notizen_je_punkt(organization, item_ids).values())
        kommentare = fraktion.kommentare_zur_vorlage(paper, membership)
    for eintrag in zeitstrahl:
        punkt = eintrag["agenda_item"]
        eintrag["position"] = positionen.get(punkt.pk) if punkt is not None else None
    bezug = status.upcoming or status.last
    vorbereitung_url = ""
    if erlaubt["darf_vorbereiten"] and bezug is not None and bezug.get("meeting") is not None:
        vorbereitung_url = reverse(
            "work:meeting_prepare", kwargs={"org_slug": organization.slug, "meeting_id": bezug["meeting"].pk}
        )
    federfuehrend = next((e for e in verlauf if e["authoritative"] and e["organization_name"]), None)
    dateien, gesperrt, rohdateien = _dateien(paper)
    orte = [o for o in (paper.locations or []) if isinstance(o, dict) and (o.get("name") or o.get("lat"))]
    return {
        **erlaubt,
        "paper_status": status,
        "zeitstrahl": zeitstrahl,
        "uebergaben": [e for e in zeitstrahl if e["position"] is not None],
        "notizen": notizen,
        "kommentare": kommentare,
        "dokumente": fraktion.dokumente_zu_vorlagen(organization, membership, [paper.pk]).get(paper.pk, []),
        "vorbereitung_url": vorbereitung_url,
        "vorbereitung_datum": bezug["date"] if vorbereitung_url and bezug is not None else None,
        "federfuehrend": federfuehrend["organization_name"] if federfuehrend else "",
        "viewer_committee": bezug.get("organization_name") if bezug else "",
        "dateien": dateien,
        "dateien_gesperrt": gesperrt,
        "dateien_roh": rohdateien,
        "orte": orte,
        "source_url": _quelle(paper),
    }


# ---------------------------------------------------------------------------
# Gremien und Personen
# ---------------------------------------------------------------------------


def gremien(organization: Any, membership: Any, seite: Iterable[Any]) -> dict[str, Any]:
    """
    Gremienliste: „Ihr Gremium“ für die Gremien, denen das Mitglied folgt bzw. die ihm zugewiesen sind, und die Art
    des Gremiums für die Spalte der gemeinsamen Liste (Klassifikation, sonst OParl-Typ, wie in Insight).
    """
    from apps.work.organization.selectors import my_committees

    meine = my_committees(membership).ids
    for gremium in seite:
        gremium.ihr_gremium = gremium.pk in meine
        gremium.art = (gremium.classification or "").strip() or (gremium.organization_type or "").strip()
    return basis(organization, membership)


def gremium(
    organization: Any, membership: Any, org: Any, mitglieder: Iterable[Any], kommende: Any, vergangene: Any
) -> dict[str, Any]:
    """
    Gremium: ob es eines der eigenen Gremien ist, welche Mitglieder der Organisation (über die verknüpfte Person im
    RIS, mit ``members.view``) darin sitzen, und der Vorbereitungsstand der nächsten Sitzung.
    """
    from apps.work.organization.selectors import my_committees

    erlaubt = basis(organization, membership)
    aktive = list(mitglieder)
    aus_fraktion: dict[uuid.UUID, str] = {}
    if erlaubt["darf_mitglieder"]:
        aus_fraktion = fraktion.mitglieder_je_person(organization, [m.person_id for m in aktive if m.person_id])
    angaben = _angaben(m.person for m in aktive if m.person_id)
    for mitgliedschaft in aktive:
        mitgliedschaft.konto = aus_fraktion.get(mitgliedschaft.person_id, "")
        mitgliedschaft.angaben = angaben.get(mitgliedschaft.person_id)
    # Gremien der Sitzungen vorladen: die Zeile nennt sie (get_display_name) ohne eine Abfrage je Sitzung
    kommende = list(kommende.prefetch_related("organizations"))
    vergangene = list(vergangene.prefetch_related("organizations"))
    naechste = next(iter(kommende), None)
    stand = None
    if naechste is not None and erlaubt["darf_sitzungen"]:
        stand = _vorbereitungsstand(organization, [naechste]).get(naechste.pk)
    return {
        **erlaubt,
        "ihr_gremium": org.pk in my_committees(membership).ids,
        "aktive_mitglieder": aktive,
        "aus_fraktion": sorted(set(aus_fraktion.values())),
        "naechste_sitzung": naechste,
        "naechste_stand": stand,
        "upcoming_meetings": kommende,
        "past_meetings": vergangene,
    }


def personen(organization: Any, membership: Any, seite: Iterable[Any]) -> dict[str, Any]:
    """
    Personenliste: Fraktion, Funktion und Gremien wie in Insight (``angaben``, eine Abfrage für die Seite) und das
    Konto in der Organisation, wenn ein Mitglied mit der Person verknüpft ist (``members.view``).
    """
    from insight_core.services.personen_liste import PersonAngaben

    erlaubt = basis(organization, membership)
    liste = list(seite)
    konten = fraktion.mitglieder_je_person(organization, _ids(liste)) if erlaubt["darf_mitglieder"] else {}
    angaben = _angaben(liste)
    for person in liste:
        person.konto = konten.get(person.pk, "")
        person.angaben = angaben.get(person.pk) or PersonAngaben()
    return erlaubt


def person(organization: Any, membership: Any, person_obj: Any) -> dict[str, Any]:
    """
    Person: Funktion und Fraktion wie in Insight (Fraktion auch aus der bestätigten Zuordnung, Issue #916) und das
    Konto in der Organisation, wenn ein Mitglied mit der Person verknüpft ist (``members.view``).
    """
    erlaubt = basis(organization, membership)
    konto = ""
    if erlaubt["darf_mitglieder"]:
        konto = fraktion.mitglieder_je_person(organization, [person_obj.pk]).get(person_obj.pk, "")
    return {**erlaubt, "konto": konto, "angaben": _angaben([person_obj]).get(person_obj.pk)}
