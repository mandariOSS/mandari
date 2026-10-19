# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angaben der Seite Einstellungen › Fraktionssitzungen nach dem Muster (Issue #1071, Schritt 4).

Die Seite speichert jede Änderung sofort: Jeder Abschnitt des Hauptformulars schickt seine Felder einzeln
(``section=save_settings``, ``felder=…``, ``services.save_faction_settings(..., fields=…)``). ``ABSCHNITT_FELDER``
nennt, welcher Abschnitt welche Felder speichert; die Vorlage liest daraus ihre ``felder``-Angabe, damit Vorlage und
Server nicht auseinanderlaufen.

Dazu Sätze je Sitzungsreihe (Rhythmus, Zeit, Ort, kommende Pausen) wie in der Liste der Fraktionssitzungen und die
Übersicht „Wer TOPs einbringt“ aus den Rollen der Organisation (Rechte ``agenda.propose``, ``agenda.create``,
``agenda.approve``; geändert werden sie unter Rollen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from apps.common import formatting
from apps.work.faction import liste

#: Abschnitt der Seite → Felder, die er sofort speichert (alle aus services.FACTION_SETTINGS_FIELDS)
ABSCHNITT_FELDER: dict[str, tuple[str, ...]] = {
    "einladung": ("invitation_dispatch", "invitation_lead_hours", "invitation_mode"),
    "sitzungserinnerung": ("meeting_reminder_enabled", "meeting_reminder_hours"),
    "erinnerung": ("agenda_reminder_enabled", "agenda_reminder_hours"),
    "protokoll": (
        "protocol_dispatch",
        "protocol_dispatch_delay_hours",
        "require_protocol_approval",
        "protocol_revision_safe",
        "auto_lock_protocol_on_complete",
    ),
    "oeffentlich": ("publish_protocols",),
    "erster_top": (
        "auto_create_approval_item",
        "first_agenda_title_with_previous",
        "first_agenda_title_no_previous",
        "first_agenda_description",
    ),
    "ablauf": ("link_previous_meeting", "quorum_rule"),
}


def felder(abschnitt: str) -> str:
    """Felder eines Abschnitts für ``felder=…`` (kommagetrennt)."""
    return ",".join(ABSCHNITT_FELDER[abschnitt])


def zurueck_zur_liste(form: Any) -> bool:
    """Führt „Wiederherstellen“ aus der Liste der Fraktionssitzungen dorthin zurück (``zurueck=liste``)?"""
    return bool(form.get("zurueck") == "liste")


def felder_aus(form: Any) -> list[str]:
    """Felder einer Anfrage ``section=save_settings`` (``felder=a,b``)."""
    return [f.strip() for f in str(form.get("felder", "")).split(",")]


@dataclass
class ReiheAngaben:
    """Satz zu einer Sitzungsreihe: wann und wo, was in nächster Zeit ausfällt und welche Termine gelöscht sind."""

    satz: str
    pausen: list[str] = field(default_factory=list)
    #: Ausnahmen ohne gelöschte Termine (Pausen, Sondertermine) für „Pausen und Ausnahmen“
    ausnahmen: list[Any] = field(default_factory=list)
    #: Kommende gelöschte Termine (Ausnahme „Gelöscht“, Issue #1071), je mit ``satz``
    geloescht: list[Any] = field(default_factory=list)


def reihe_angaben(schedule: Any) -> ReiheAngaben:
    """„Wöchentlich montags von 18:00 bis 20:00 Uhr, Fraktionsbüro“, kommende Pausen und gelöschte Termine."""
    satz = f"{liste._rhythmus(schedule)} {liste._von_bis(schedule)} Uhr"
    if schedule.default_location:
        satz += f", {schedule.default_location}"
    heute = timezone.localdate()
    alle = sorted(schedule.exceptions.all(), key=lambda a: a.original_date)
    ausnahmen = [a for a in alle if a.exception_type != "deleted"]
    pausen = [liste._ausnahme(a) for a in ausnahmen if (a.end_date or a.new_date or a.original_date) >= heute]
    return ReiheAngaben(
        satz=f"{satz}.", pausen=pausen, ausnahmen=ausnahmen, geloescht=liste.geloeschte_termine(schedule)
    )


@dataclass
class NichtoeffentlichOrdner:
    """Ordner „Nichtöffentliche Vorgänge“ im Dokumentenspeicher (Issue #873), nur zur Information."""

    vereidigt: int
    mitglieder: int
    #: Zahl der Dokumente und Kennung des Ordners nur für Vereidigte (sonst ``None``)
    dateien: int | None = None
    ordner_id: Any = None


def nichtoeffentlich_ordner(organization: Any, membership: Any) -> NichtoeffentlichOrdner:
    """
    Wer den Ordner öffnen darf (vereidigte Mitglieder, ``visibility.is_sworn_member``) und, für Vereidigte, wie viele
    Dokumente darin liegen. Freigaben gibt es für diesen Ordner nicht; die Einstellungen ändern daran nichts.
    """
    from apps.tenants.models import Membership
    from apps.work.faction.visibility import is_sworn_member
    from apps.work.motions.models import DocumentFolder, Motion

    mitglieder = list(Membership.objects.filter(organization=organization, is_active=True, is_guest=False))
    ergebnis = NichtoeffentlichOrdner(
        vereidigt=sum(1 for m in mitglieder if is_sworn_member(m)), mitglieder=len(mitglieder)
    )
    if not is_sworn_member(membership):
        return ergebnis
    ordner = DocumentFolder.objects.filter(organization=organization, sworn_in_only=True).first()
    if ordner is not None:
        ergebnis.ordner_id = ordner.pk
        ergebnis.dateien = Motion.objects.filter(organization=organization, folder=ordner).count()
    else:
        ergebnis.dateien = 0
    return ergebnis


@dataclass
class TopRechte:
    """Rollen je Recht für Tagesordnungspunkte (nur Anzeige)."""

    vorschlagen: str
    eintragen: str
    genehmigen: str


def top_rechte(organization: Any) -> TopRechte:
    """
    Welche Rollen TOPs vorschlagen, direkt eintragen und Vorschläge genehmigen. Administratorrollen haben alle Rechte;
    wer direkt eintragen darf, braucht keinen Vorschlag und steht deshalb nicht unter „Vorschlagen“.
    """
    from apps.tenants.models import Role

    vorschlagen: list[str] = []
    eintragen: list[str] = []
    genehmigen: list[str] = []
    for rolle in Role.objects.filter(organization=organization).prefetch_related("permissions"):
        rechte = {p.codename for p in rolle.permissions.all()}
        if rolle.is_admin or "agenda.approve" in rechte:
            genehmigen.append(rolle.name)
        if rolle.is_admin or "agenda.create" in rechte:
            eintragen.append(rolle.name)
        elif "agenda.propose" in rechte:
            vorschlagen.append(rolle.name)

    def text(namen: list[str]) -> str:
        return formatting.join_parts(namen) if namen else "Keine Rolle"

    return TopRechte(vorschlagen=text(vorschlagen), eintragen=text(eintragen), genehmigen=text(genehmigen))
