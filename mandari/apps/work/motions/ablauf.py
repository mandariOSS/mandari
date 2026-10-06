# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ablauf eines Antrags für den neuen Editor (Teil von #856): Entwurf → Abstimmung → Freigabe → Einreichung.

Die vier Stufen bilden nur ab, was es schon gibt; neue Schritte oder Regeln entstehen hier nicht:

- **Entwurf**: Status „Entwurf“ (auch der Altstatus „In Prüfung“).
- **Abstimmung**: Status „Interne Absprache“ bzw. „Externe Absprache“; abgestimmt wird über die vorhandenen
  Freigabe-Anfragen (``MotionApproval``: Zustimmen oder Ablehnen).
- **Freigabe**: Statuswechsel nach „Freigegeben“ (``Motion.VALID_TRANSITIONS``); danach ist der Text gesperrt
  (``Motion.EDITABLE_STATUSES``).
- **Einreichung**: Einreichung bei der Verwaltung (mandari Session oder E-Mail, ``submit_ris``) oder Status
  „Eingereicht“; spätere Stände (Bei Verwaltung, Auf Tagesordnung, Beschlossen …) zählen als eingereicht.

Abgelehnte, zurückgezogene und archivierte Dokumente stehen bei „Einreichung“, wenn sie eingereicht waren, sonst bei
„Entwurf“; der Stand-Knopf zeigt immer den tatsächlichen Status. Alle Übergänge der Übergangsmatrix bleiben im
Menü „Ablauf“ erreichbar.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q
from django.utils import formats

#: Stufen des Ablaufs (Schlüssel, Beschriftung, Untertitel solange sie noch nicht dran sind)
STUFEN: tuple[tuple[str, str, str], ...] = (
    ("entwurf", "Entwurf", "Schreiben und kommentieren"),
    ("abstimmung", "Abstimmung", "in der Fraktion"),
    ("freigabe", "Freigabe", "Fassung festlegen"),
    ("einreichung", "Einreichung", "bei der Verwaltung"),
)
ENTWURF, ABSTIMMUNG, FREIGABE, EINREICHUNG = range(4)
#: Alle Stufen erledigt (eingereicht oder später)
FERTIG = 4

ENTWURF_STATUS = frozenset({"draft", "review"})
ABSTIMMUNG_STATUS = frozenset({"internal_review", "external_review"})
EINGEREICHT_STATUS = frozenset({"submitted", "at_admin", "on_agenda", "adopted", "completed"})
#: Ende ohne Pipeline: eingereicht gewesen → Einreichung erledigt, sonst Entwurf
ABSEITS_STATUS = frozenset({"rejected", "withdrawn", "archived", "deleted"})

#: Schritt (Dialog) und die Stufe, die ihn auslöst
AKTION_STUFE: dict[str, int] = {"abstimmung": ABSTIMMUNG, "freigeben": FREIGABE, "einreichen": EINREICHUNG}

#: Recht „Stimmrecht bei Abstimmungen“ (apps/common/permissions.py)
STIMMRECHT = "voting.participate"


@dataclass(frozen=True)
class Stufe:
    key: str
    label: str
    unter: str
    zustand: str  # erledigt | aktuell | offen
    #: Dialog, den ein Klick auf die Stufe öffnet (abstimmung, freigeben, einreichen) oder leer
    dialog: str = ""


@dataclass(frozen=True)
class Zustimmung:
    ja: int = 0
    nein: int = 0
    offen: int = 0

    @property
    def gesamt(self) -> int:
        return self.ja + self.nein + self.offen

    def satz(self) -> str:
        """„3 von 5 haben zugestimmt, 1 abgelehnt, 1 offen“ – nur Teile, die vorkommen."""
        if not self.gesamt:
            return "Noch niemand um Zustimmung gebeten"
        teile = [f"{self.ja} von {self.gesamt} haben zugestimmt"]
        if self.nein:
            teile.append(f"{self.nein} abgelehnt")
        if self.offen:
            teile.append(f"{self.offen} offen")
        return ", ".join(teile)


@dataclass(frozen=True)
class Ablauf:
    stufen: list[Stufe]
    #: Index der Stufe, an der der Antrag steht (0–3), 4 = alle erledigt
    aktuell: int
    #: Nächster Schritt für die Person (Dialogschlüssel und Beschriftung) oder leer
    aktion: str = ""
    aktion_label: str = ""
    #: Infozeile unter der Werkzeugleiste: fett gesetzter Anfang und Rest; leer = keine Zeile
    info_titel: str = ""
    info_text: str = ""
    zustimmung: Zustimmung = field(default_factory=Zustimmung)

    @property
    def aktuelle_stufe(self) -> Stufe | None:
        return self.stufen[self.aktuell] if self.aktuell < len(self.stufen) else None

    @property
    def jetzt(self) -> int:
        """Stufe, die in der Ablaufleiste ausgeschrieben bleibt: die aktuelle, nach der Einreichung die letzte"""
        return min(self.aktuell, len(self.stufen) - 1)


def zustimmung_aus(approvals: Iterable[Any]) -> Zustimmung:
    ja = nein = offen = 0
    for approval in approvals:
        if approval.approved is True:
            ja += 1
        elif approval.approved is False:
            nein += 1
        else:
            offen += 1
    return Zustimmung(ja=ja, nein=nein, offen=offen)


def stufe_des_status(status: str, *, eingereicht: bool) -> int:
    if status in ENTWURF_STATUS:
        return ENTWURF
    if status in ABSTIMMUNG_STATUS:
        return ABSTIMMUNG
    if status == "approved":
        # Freigegeben: Die Freigabe ist erledigt, die Einreichung ist dran
        return EINREICHUNG
    if status in EINGEREICHT_STATUS:
        return FERTIG
    if status in ABSEITS_STATUS:
        return FERTIG if eingereicht else ENTWURF
    return ENTWURF


def _datum(value: Any) -> str:
    return str(formats.date_format(value, "d.m.Y")) if value else ""


def _plural(anzahl: int, einzahl: str, mehrzahl: str) -> str:
    return f"{anzahl} {einzahl if anzahl == 1 else mehrzahl}"


def offen_text(kommentare: int, vorschlaege: int = 0) -> str:
    """„2 Kommentare, 1 Vorschlag“ – nur Teile, die vorkommen; leer, wenn nichts offen ist."""
    teile = []
    if kommentare:
        teile.append(_plural(kommentare, "Kommentar", "Kommentare"))
    if vorschlaege:
        teile.append(_plural(vorschlaege, "Vorschlag", "Vorschläge"))
    return ", ".join(teile)


def ablauf_fuer(
    motion: Any,
    *,
    approvals: Iterable[Any],
    offene_kommentare: int,
    darf_steuern: bool,
    offene_vorschlaege: int = 0,
) -> Ablauf:
    """
    Ablauf eines Dokuments für Kopfzeile, Infozeile und Menü „Ablauf“.

    ``darf_steuern``: Die Person darf den Status ändern (Bearbeitungsrecht, kein Gast) – nur dann gibt es einen
    nächsten Schritt als Knopf. Ob der Übergang erlaubt ist, entscheidet die Übergangsmatrix des Dokuments.
    """
    status = str(motion.status)
    eingereicht = bool(motion.submitted_at) or status in EINGEREICHT_STATUS
    aktuell = stufe_des_status(status, eingereicht=eingereicht)
    zustimmung = zustimmung_aus(approvals)
    erlaubt = set(motion.VALID_TRANSITIONS.get(status, []))

    unter_aktuell = {
        ENTWURF: (
            f"Offen: {offen_text(offene_kommentare, offene_vorschlaege)}"
            if offene_kommentare or offene_vorschlaege
            else "bereit zur Abstimmung"
        ),
        ABSTIMMUNG: f"{zustimmung.ja} von {zustimmung.gesamt} zugestimmt"
        if zustimmung.gesamt
        else motion.get_status_display(),
        FREIGABE: "Fassung festlegen",
        EINREICHUNG: f"Frist {_datum(motion.due_date)}" if motion.due_date else "bei der Verwaltung",
    }
    unter_erledigt = {
        ENTWURF: "abgeschlossen",
        ABSTIMMUNG: f"{zustimmung.ja} von {zustimmung.gesamt} zugestimmt" if zustimmung.gesamt else "abgeschlossen",
        FREIGABE: "erteilt",
        EINREICHUNG: f"am {_datum(motion.submitted_at)}" if motion.submitted_at else motion.get_status_display(),
    }

    aktion, aktion_label = "", ""
    if darf_steuern:
        if aktuell == ENTWURF and "internal_review" in erlaubt:
            aktion, aktion_label = "abstimmung", "Zur Abstimmung geben …"
        elif aktuell == ABSTIMMUNG and "approved" in erlaubt:
            aktion, aktion_label = "freigeben", "Freigeben …"
        elif aktuell == EINREICHUNG and status == "approved" and "submitted" in erlaubt:
            aktion, aktion_label = "einreichen", "Einreichen …"

    stufen: list[Stufe] = []
    for index, (key, label, unter_offen) in enumerate(STUFEN):
        if index < aktuell:
            zustand, unter = "erledigt", unter_erledigt[index]
        elif index == aktuell:
            zustand, unter = "aktuell", unter_aktuell[index]
        else:
            zustand, unter = "offen", unter_offen
        # Die Stufe, deren Schritt jetzt dran ist, öffnet ihren Dialog direkt
        dialog = aktion if aktion and AKTION_STUFE[aktion] == index else ""
        stufen.append(Stufe(key=key, label=label, unter=unter, zustand=zustand, dialog=dialog))

    info_titel, info_text = "", ""
    if aktuell == ABSTIMMUNG:
        info_titel = f"{motion.get_status_display()}."
        info_text = f"{zustimmung.satz()}."
    elif aktuell == EINREICHUNG and status == "approved":
        info_titel = "Freigegeben."
        frist = f", Frist {_datum(motion.due_date)}" if motion.due_date else ""
        info_text = f"Nächster Schritt: bei der Verwaltung einreichen{frist}."
    elif aktuell == FERTIG and status == "submitted" and motion.submitted_at:
        info_titel = "Eingereicht"
        info_text = f"am {_datum(motion.submitted_at)}."
    elif aktuell == FERTIG:
        info_titel = f"{motion.get_status_display()}."
        if motion.submitted_at:
            info_text = f"Eingereicht am {_datum(motion.submitted_at)}."

    return Ablauf(
        stufen=stufen,
        aktuell=aktuell,
        aktion=aktion,
        aktion_label=aktion_label,
        info_titel=info_titel,
        info_text=info_text,
        zustimmung=zustimmung,
    )


def stimmberechtigte_ids(organization: Any) -> set[Any]:
    """
    Aktive Mitglieder (keine Gäste) mit Stimmrecht – Vorauswahl im Dialog „Zur Abstimmung geben“.

    Dieselbe Regel wie ``PermissionChecker``: Admin-Rolle, Rollen- oder Einzelrecht, ausdrücklich verweigert geht
    vor. Eine Abfrage statt einer Rechteprüfung je Mitglied.
    """
    from apps.tenants.models import Membership

    return set(
        Membership.objects.filter(organization=organization, is_active=True, is_guest=False)
        .filter(
            Q(roles__is_admin=True)
            | Q(roles__permissions__codename=STIMMRECHT)
            | Q(individual_permissions__codename=STIMMRECHT)
        )
        .exclude(denied_permissions__codename=STIMMRECHT)
        .values_list("id", flat=True)
        .distinct()
    )
