# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzung nach der Sitzung nach dem Muster (Issue #1071, Schritt 3): Protokoll, Abschluss, abgesagte Sitzung.

Fachlogik der neuen Seite für abgeschlossene und abgesagte Sitzungen (``templates/work/faction/neu/protokoll*``).
Alles wird aus vorhandenen Daten abgeleitet; geschrieben wird nur über die bisherigen Wege: Aktionen
(``FactionActionView``: Anwesenheit bestätigen, genehmigen, Nachtrag, Abstimmung), Panel des TOPs
(``FactionItemPanelActionView``: Einträge, Aufgaben, Anhänge) und Notizen je TOP (``FactionSessionActionView``,
Aktion ``notizen`` mit Konflikterkennung). Die Views rufen nur hier hinein.

Grenzen wie in der bisherigen Seite (``detail.html``) und im PDF der Niederschrift:

- Nichtöffentliche TOPs (auch als Unterpunkt), ihre Notizen, Einträge, Beschlüsse und Aufgaben sehen nur Vereidigte
  (:mod:`apps.work.faction.visibility`). Für alle anderen enthält der Kontext keine nichtöffentlichen Objekte, nur die
  Anzahl. Einträge ohne TOP gehören wie im PDF zur internen Fassung.
- Notizen bearbeiten darf, wer protokollieren darf (:func:`apps.work.faction.sitzung.darf_protokollieren`), solange
  das Protokoll nicht genehmigt ist. Danach gibt es nur noch Nachträge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from django.utils import timezone

from apps.common.formatting import count_label
from apps.work.rahmen import neues_design

from . import sitzung
from .liste import _tag
from .visibility import can_view_internal, is_item_internal

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem, FactionMeeting

#: Zustände nach der Sitzung, die die neue Seite zeigt
NACH_DER_SITZUNG = ("completed", "cancelled")

SEITE = "work/faction/neu/protokoll.html"
DOKUMENT = "work/faction/neu/_pr_dokument.html"

#: Ergebnis einer Abstimmung als Satzanfang (FactionDecision.RESULT_CHOICES)
ERGEBNIS = {
    "accepted": "Angenommen",
    "rejected": "Abgelehnt",
    "postponed": "Vertagt",
    "modified": "Geändert angenommen",
}

#: Bisherige Einträge: Bezeichnung je Art (FactionProtocolEntry.ENTRY_TYPE_CHOICES)
EINTRAG_ART = {
    "speech": "Wortbeitrag",
    "note": "Notiz",
    "decision": "Beschluss",
    "action": "Aufgabe",
    "vote": "Abstimmung",
    "addendum": "Nachtrag",
}


def zeigt_protokoll(meeting: FactionMeeting, ansicht: str | None) -> bool:
    """Neue Seite nach der Sitzung? Nur mit dem Schalter der Organisation; ``?ansicht=bisher`` führt zurück."""
    return meeting.status in NACH_DER_SITZUNG and ansicht != "bisher" and neues_design(meeting.organization)


# =============================================================================
# Rechte
# =============================================================================


@dataclass
class Rechte:
    """Was das Mitglied auf der Seite darf (Prüfungen wie bisher in ``_get_meeting_context`` und den Aktionen)."""

    bestaetigen: bool = False
    genehmigen: bool = False
    protokollieren: bool = False
    nachtrag: bool = False
    verwalten: bool = False
    pdf_oeffentlich: bool = False
    pdf_intern: bool = False

    @property
    def schritte(self) -> bool:
        """Nebenspalte mit den Schritten (Vorsitz, Schriftführung) statt „Ihre Teilnahme“."""
        return self.bestaetigen or self.genehmigen or self.protokollieren or self.verwalten


def rechte(membership: Membership, meeting: FactionMeeting) -> Rechte:
    from .invitations import can_confirm_attendance

    abgeschlossen = meeting.status == "completed"
    darf_intern = can_view_internal(membership)
    darf_nachtrag = meeting.protocol_approved and (
        sitzung.darf_verwalten(membership, meeting)
        or membership.has_permission("protocols.create")
        or membership.has_permission("protocols.edit")
    )
    return Rechte(
        bestaetigen=abgeschlossen and meeting.attendance_confirmed_at is None and can_confirm_attendance(membership),
        genehmigen=abgeschlossen and not meeting.protocol_approved and membership.has_permission("protocols.approve"),
        protokollieren=sitzung.darf_protokollieren(membership, meeting),
        nachtrag=abgeschlossen and darf_nachtrag,
        verwalten=sitzung.darf_verwalten(membership, meeting),
        pdf_oeffentlich=abgeschlossen and membership.has_permission("protocols.view_public"),
        pdf_intern=abgeschlossen and darf_intern and membership.has_permission("protocols.view_full"),
    )


# =============================================================================
# Protokoll als Dokument
# =============================================================================


@dataclass
class Eintrag:
    """Ein bisheriger Protokolleintrag, unverändert: Art, wer, wann, Inhalt."""

    art: str
    typ: str
    wer: str
    wann: Any
    inhalt: str
    zusatz: str = ""


@dataclass
class ProtTop:
    """Ein TOP im Protokoll: Notizen (ein Dokument je TOP), bisherige Einträge, Aufgaben, Beschluss."""

    item: FactionAgendaItem
    unter: bool
    intern: bool
    notiz_html: str = ""
    stand: str = ""
    eintraege: list[Eintrag] = field(default_factory=list)
    aufgaben: list[sitzung.AufgabenZeile] = field(default_factory=list)
    beschluss: str = ""
    beschluss_text: str = ""
    #: Konfiguration des Editors der Notizen (nur, wenn bearbeitbar)
    konfig: dict[str, Any] | None = None

    @property
    def konfig_id(self) -> str:
        return f"fs-notiz-{self.item.pk}"

    @property
    def leer(self) -> bool:
        return not (self.notiz_html or self.eintraege or self.aufgaben or self.beschluss)


def beschluss_satz(decision: Any) -> tuple[str, str]:
    """„Angenommen mit 5 Stimmen dafür, 0 dagegen, 0 Enthaltungen.“ und der Beschlusstext (Muster beschlussSatz)."""
    if decision is None:
        return "", ""
    satz = ERGEBNIS.get(decision.result, decision.get_result_display())
    if decision.result != "postponed":
        enth = "Enthaltung" if decision.votes_abstain == 1 else "Enthaltungen"
        satz += (
            f" mit {count_label(decision.votes_yes, 'Stimme', 'Stimmen')} dafür, {decision.votes_no} dagegen, "
            f"{decision.votes_abstain} {enth}"
        )
    return f"{satz}.", (decision.decision_text or "").strip()


def _eintrag(entry: Any) -> Eintrag:
    typ = entry.entry_type
    if typ == "speech" and entry.speaker_name:
        wer = entry.speaker_name
    elif entry.created_by_id and entry.created_by:
        wer = entry.created_by.user.get_display_name()
    else:
        wer = ""
    zusatz = ""
    if typ == "action":
        teile = [entry.action_assignee_name] if entry.action_assignee_name else []
        if entry.action_due_date:
            teile.append(f"bis {entry.action_due_date:%d.%m.}")
        if entry.action_completed:
            teile.append("erledigt")
        zusatz = ", ".join(teile)
    return Eintrag(
        art=EINTRAG_ART.get(typ, entry.get_entry_type_display()),
        typ=typ,
        wer=wer,
        wann=entry.created_at,
        inhalt=entry.get_content_decrypted() or "",
        zusatz=zusatz,
    )


@dataclass
class Dokument:
    oeffentlich: list[ProtTop]
    intern: list[ProtTop]
    #: nichtöffentliche TOPs, die das Mitglied nicht sehen darf (nur die Zahl)
    gesperrt: int
    #: Einträge ohne TOP (wie im PDF nur in der internen Fassung, also nur für Vereidigte)
    allgemein: list[Eintrag]
    #: Bisheriges Protokoll aus Einzeleinträgen (keine Notizen, aber Einträge)
    alt: bool
    zahlen: dict[str, int]

    @property
    def tops(self) -> list[ProtTop]:
        return [*self.oeffentlich, *self.intern]


def dokument(meeting: FactionMeeting, membership: Membership) -> Dokument:
    """
    Protokoll aller sichtbaren TOPs: öffentlicher Teil, dann nichtöffentlicher Teil (nur für Vereidigte), Unterpunkte
    unter ihrem TOP. Je TOP die Notizen, die bisherigen Einträge in ihrer Reihenfolge, Aufgaben und der Beschluss.
    """
    ordnung = sitzung.tagesordnung(meeting, membership)
    zeilen = ordnung.zeilen
    ids = [z.item.pk for z in zeilen]
    eintraege: dict[Any, list[Eintrag]] = {pk: [] for pk in ids}
    allgemein: list[Eintrag] = []
    zahlen = {"alle": 0, "speech": 0, "note": 0, "action": 0, "decision": 0}
    darf_intern = can_view_internal(membership)
    qs = meeting.protocol_entries.select_related(
        "speaker__user", "action_assignee__user", "created_by__user", "agenda_item"
    ).order_by("order", "created_at")
    for entry in qs:
        if entry.agenda_item_id is None:
            if darf_intern:
                allgemein.append(_eintrag(entry))
            continue
        if entry.agenda_item_id not in eintraege:
            # TOP nicht sichtbar (nichtöffentlich) oder kein angenommener TOP
            continue
        eintraege[entry.agenda_item_id].append(_eintrag(entry))
        zahlen["alle"] += 1
        if entry.entry_type in zahlen:
            zahlen[entry.entry_type] += 1

    def top(z: sitzung.TopZeile) -> ProtTop:
        item = z.item
        notiz = item.notes_html if item.notes_encrypted else ""
        satz, text = beschluss_satz(item.recorded_decision)
        return ProtTop(
            item=item,
            unter=z.unter,
            intern=z.intern,
            notiz_html=notiz,
            stand=sitzung.fingerabdruck(sitzung.notizen(item)) if item.notes_encrypted else sitzung.fingerabdruck(""),
            eintraege=eintraege.get(item.pk, []),
            aufgaben=sitzung.aufgaben_des_tops(item, membership),
            beschluss=satz,
            beschluss_text=text,
        )

    oeffentlich = [top(z) for z in ordnung.oeffentlich]
    intern = [top(z) for z in ordnung.intern]
    alle = [*oeffentlich, *intern]
    alt = zahlen["alle"] > 0 and not any(t.notiz_html for t in alle)
    return Dokument(oeffentlich, intern, ordnung.gesperrt, allgemein, alt, zahlen)


# =============================================================================
# Angaben im Kopf des Protokolls: Zeit, Leitung, Anwesenheit, Beschlussfähigkeit
# =============================================================================


@dataclass
class Angaben:
    zeit: str
    leitung: str
    schrift: str
    vor_ort: list[str]
    online: list[str]
    entschuldigt: list[str]
    abwesend: list[str]
    gaeste: list[str]
    quorum: dict[str, Any] | None
    #: jemand ohne Stimmrecht ist anwesend (Stern erklären)
    ohne_stimme: bool
    anwesend: int

    @property
    def namen_satz(self) -> str:
        """Für den Dialog „Anwesenheit bestätigen“: „Vera Beispiel, Paula Probe (online). Entschuldigt: Nora Norm.“"""
        da = [n.rstrip("*") for n in self.vor_ort] + [f"{n.rstrip('*')} (online)" for n in self.online]
        satz = f"{self.anwesend} anwesend" + (f": {', '.join(da)}." if da else ".")
        if self.gaeste:
            satz += f" Gäste: {', '.join(self.gaeste)}."
        if self.entschuldigt:
            satz += f" Entschuldigt: {', '.join(self.entschuldigt)}."
        if self.abwesend:
            satz += f" Abwesend: {', '.join(self.abwesend)}."
        return satz

    @property
    def satz(self) -> str:
        """„8 anwesend, davon 2 online, 1 entschuldigt.“ (Muster, Schritt „Anwesenheit bestätigen“)."""
        teile = [f"{self.anwesend} anwesend"]
        if self.online:
            teile[0] += f", davon {len(self.online)} online"
        if self.entschuldigt:
            teile.append(f"{len(self.entschuldigt)} entschuldigt")
        if self.abwesend:
            teile.append(f"{len(self.abwesend)} abwesend")
        return ", ".join(teile)


def _uhr(wann: Any) -> str:
    return f"{timezone.localtime(wann):%H:%M}"


def zeitraum(meeting: FactionMeeting) -> str:
    """„18:04 bis 19:51 Uhr“: tatsächlicher Beginn und Ende, sonst der geplante Beginn."""
    beginn = meeting.started_at or meeting.start
    if meeting.end and meeting.end > beginn:
        return f"{_uhr(beginn)} bis {_uhr(meeting.end)} Uhr"
    return f"ab {_uhr(beginn)} Uhr"


def angaben(meeting: FactionMeeting) -> Angaben:
    from apps.common.permissions import PermissionChecker

    from .quorum import faction_quorum_status

    teilnahmen = sitzung.anwesenheit(meeting).teilnahmen
    vor_ort: list[str] = []
    online: list[str] = []
    entschuldigt: list[str] = []
    abwesend: list[str] = []
    gaeste: list[str] = []
    ohne_stimme = False
    for a in teilnahmen:
        name = a.get_display_name()
        if a.is_guest:
            if a.status == "present":
                gaeste.append(name)
            continue
        if a.status == "present":
            stimme = a.membership is not None and cast(Any, PermissionChecker)(a.membership).has_permission(
                "voting.participate"
            )
            if not stimme:
                ohne_stimme = True
                name = f"{name}*"
            (online if a.participation_type == "online" else vor_ort).append(name)
        elif a.status in ("excused", "declined"):
            entschuldigt.append(name)
        else:
            abwesend.append(name)
    ort = meeting.location or ""
    zeit = zeitraum(meeting)
    if ort:
        zeit += f", {ort}"
    if meeting.is_virtual:
        zeit += ", auch per Video" if ort else ", per Video"
    quorum = faction_quorum_status(meeting) if meeting.status == "completed" else None
    return Angaben(
        zeit=zeit,
        leitung=meeting.chaired_by_name,
        schrift=meeting.minute_taker_name,
        vor_ort=vor_ort,
        online=online,
        entschuldigt=entschuldigt,
        abwesend=abwesend,
        gaeste=gaeste,
        quorum=quorum if quorum and quorum.get("voting_total") else None,
        ohne_stimme=ohne_stimme,
        anwesend=len(vor_ort) + len(online),
    )


# =============================================================================
# Genehmigung und Versand
# =============================================================================


@dataclass
class Genehmigung:
    """Wo und wann das Protokoll genehmigt wird bzw. wurde."""

    genehmigt: bool
    #: Sitzung mit dem Genehmigungs-TOP (geplant oder erfolgt), sonst ``None``
    sitzung: Any = None
    top_nr: str = ""
    am: Any = None
    von: str = ""
    direkt: bool = False

    @property
    def wo(self) -> str:
        """„Nr. 25 am Mo 26.10. unter TOP 1“ bzw. „der nächsten Sitzung unter TOP 1“."""
        if self.sitzung is None:
            return "der nächsten Sitzung unter TOP 1"
        nr = f"Nr. {self.sitzung.meeting_number}" if self.sitzung.meeting_number else "der Sitzung"
        top = f" unter TOP {self.top_nr}" if self.top_nr else ""
        return f"{nr} am {_tag(self.sitzung.start)}{top}"


def genehmigung(meeting: FactionMeeting) -> Genehmigung:
    from .models import FactionAgendaItem

    if meeting.protocol_approved:
        g = Genehmigung(
            genehmigt=True,
            am=meeting.protocol_approved_at,
            von=meeting.protocol_approved_by.user.get_display_name() if meeting.protocol_approved_by else "",
        )
        if meeting.protocol_approved_in_id:
            g.sitzung = meeting.protocol_approved_in
            top = FactionAgendaItem.objects.filter(meeting=g.sitzung, approves_meeting=meeting).first()
            g.top_nr = top.number if top else ""
        else:
            g.direkt = True
        return g
    top = (
        FactionAgendaItem.objects.filter(approves_meeting=meeting, is_approval_item=True)
        .select_related("meeting")
        .exclude(meeting__status="cancelled")
        .order_by("meeting__start")
        .first()
    )
    if top is None:
        return Genehmigung(genehmigt=False)
    return Genehmigung(genehmigt=False, sitzung=top.meeting, top_nr=top.number or "")


@dataclass
class Versand:
    modus: str
    satz: str
    verschickt: bool = False


def versand(meeting: FactionMeeting) -> Versand:
    """Protokollversand per E-Mail (Issue #871) nach der Einstellung der Organisation, mit Stand dieser Sitzung."""
    from .models import FactionAuditLog
    from .protocol_dispatch import get_protocol_dispatch_settings, protocol_due_at

    einstellungen = get_protocol_dispatch_settings(meeting.organization)
    modus = einstellungen["protocol_dispatch"]
    fassung = "an Vereidigte vollständig, an alle anderen den öffentlichen Teil"
    if meeting.protocol_sent_at:
        log = (
            FactionAuditLog.objects.filter(model_name="FactionMeeting", object_id=meeting.pk, action="protocol_sent")
            .order_by("-created_at")
            .values_list("changes", flat=True)
            .first()
        )
        zahl = log.get("empfaenger") if isinstance(log, dict) else None
        an = f" an {count_label(zahl, 'Mitglied', 'Mitglieder')}" if isinstance(zahl, int) and zahl else ""
        lokal = timezone.localtime(meeting.protocol_sent_at)
        return Versand(modus, f"Am {lokal:%d.%m.} um {lokal:%H:%M} automatisch{an} verschickt: {fassung}.", True)
    if modus == "off":
        return Versand(
            modus, "Work verschickt Protokolle nicht automatisch. Mitglieder lesen es hier oder laden es als PDF."
        )
    stunden = einstellungen["protocol_dispatch_delay_hours"]
    spaeter = f"{count_label(stunden, 'Stunde', 'Stunden')} " if stunden else ""
    faellig = protocol_due_at(meeting, einstellungen)
    seit = einstellungen["protocol_dispatch_since"]
    if faellig is not None and seit is not None and faellig < seit:
        return Versand(modus, "Dieses Protokoll war vor dem Einschalten des Versands fällig und wird nicht verschickt.")
    if modus == "after_completion":
        wann = f"{spaeter}nach dem Ende der Sitzung" if spaeter else "nach dem Ende der Sitzung"
    else:
        wann = f"{spaeter}nach der Genehmigung" if spaeter else "nach der Genehmigung"
    return Versand(modus, f"Work verschickt das Protokoll {wann} automatisch per E-Mail: {fassung}.")


# =============================================================================
# Kopf, Infoleiste und Schritte
# =============================================================================


def stufen(meeting: FactionMeeting, to_zahl: int, g: Genehmigung) -> list[dict[str, Any]]:
    """Ablaufleiste Planung → Einladung → Sitzung → Protokoll nach der Sitzung (Muster unter())."""
    tops = count_label(to_zahl, "TOP", "TOPs")
    if meeting.invitation_sent_at:
        einladung = f"am {timezone.localtime(meeting.invitation_sent_at):%d.%m.} versendet"
    elif meeting.invitation_sent:
        einladung = "versendet"
    else:
        einladung = "ohne Einladung"
    if meeting.status == "cancelled":
        return [
            {"nr": 1, "name": "Planung", "unter": tops, "stand": "erledigt"},
            {"nr": 2, "name": "Einladung", "unter": einladung, "stand": "erledigt" if meeting.invitation_sent else ""},
            {"nr": 3, "name": "Sitzung", "unter": "abgesagt", "stand": "aktuell"},
            {"nr": 4, "name": "Protokoll", "unter": "entfällt", "stand": ""},
        ]
    if g.genehmigt:
        protokoll = f"genehmigt am {timezone.localtime(g.am):%d.%m.}" if g.am else "genehmigt"
        stand = "erledigt"
    else:
        protokoll = "Entwurf"
        if meeting.attendance_confirmed_at and g.sitzung is not None and g.sitzung.meeting_number:
            protokoll = f"Entwurf, Genehmigung in Nr. {g.sitzung.meeting_number}"
        stand = "aktuell"
    return [
        {"nr": 1, "name": "Planung", "unter": tops, "stand": "erledigt"},
        {"nr": 2, "name": "Einladung", "unter": einladung, "stand": "erledigt"},
        {"nr": 3, "name": "Sitzung", "unter": zeitraum(meeting).replace(" Uhr", ""), "stand": "erledigt"},
        {"nr": 4, "name": "Protokoll", "unter": protokoll, "stand": stand},
    ]


@dataclass
class Info:
    """Infoleiste: ein Satz (fett beginnend) und höchstens eine Hauptaktion (bestaetigen, genehmigen, nachtrag)."""

    fett: str = ""
    text: str = ""
    aktion: str = ""


def _genehmigt_satz(g: Genehmigung) -> str:
    am = f" am {_tag(g.am)}" if g.am else ""
    if g.direkt:
        return f"Protokoll direkt genehmigt{am}" + (f" von {g.von}" if g.von else "")
    return f"Protokoll genehmigt{am} in {g.wo}"


def info(meeting: FactionMeeting, r: Rechte, g: Genehmigung, v: Versand, *, alt: bool) -> Info:
    """Satz und Aktion der Infoleiste je Zustand und Rolle (Muster zeichneInfo, Zustände protokoll/abgeschlossen)."""
    if meeting.status == "cancelled":
        grund = f" {meeting.cancellation_reason}" if meeting.cancellation_reason else ""
        return Info("Abgesagt.", f"Diese Sitzung findet nicht statt.{grund}".strip())
    if g.genehmigt:
        if alt:
            text = f"{_genehmigt_satz(g)}. Bisheriges Protokoll aus Einzeleinträgen, nur zum Lesen."
        else:
            versendet = " und automatisch per E-Mail verschickt" if v.verschickt else ""
            text = f"{_genehmigt_satz(g)}{versendet}." + (
                " Änderungen sind nur noch als Nachtrag möglich." if r.nachtrag else ""
            )
        return Info("Abgeschlossen.", text, "nachtrag" if r.nachtrag else "")
    danach = " Danach verschickt Work es automatisch per E-Mail." if v.modus == "after_approval" else ""
    if r.bestaetigen:
        ende = f" um {_uhr(meeting.end)}" if meeting.end else ""
        return Info(
            f"Beendet{ende}.",
            "Nächster Schritt: Anwesenheit bestätigen, sie ist die Grundlage der Teilnahmenachweise.",
            "bestaetigen",
        )
    if r.schritte:
        fett = "Anwesenheit bestätigt." if meeting.attendance_confirmed_at else "Protokoll im Entwurf."
        return Info(
            fett, f"Die Fraktion genehmigt das Protokoll in {g.wo}.{danach}", "genehmigen" if r.genehmigen else ""
        )
    return Info("Protokoll im Entwurf.", f"Die Fraktion genehmigt es in {g.wo}.")


def schritte(
    meeting: FactionMeeting, a: Angaben, g: Genehmigung, v: Versand, *, genehmigen: bool = False
) -> list[dict[str, str]]:
    """Schritte nach der Sitzung (Muster „Nach der Sitzung“): bestätigen, genehmigen, versenden, Nachweise."""
    bestaetigt = meeting.attendance_confirmed_at is not None
    if bestaetigt:
        von = meeting.attendance_confirmed_by.user.get_display_name() if meeting.attendance_confirmed_by else ""
        text1 = f"Bestätigt am {timezone.localtime(meeting.attendance_confirmed_at):%d.%m.}" + (
            f" von {von}" if von else ""
        )
        text1 += f": {a.satz}."
    else:
        text1 = (
            f"{a.satz}. Die bestätigte Liste ist die Grundlage der Teilnahmenachweise und lässt sich danach nicht mehr"
            " ändern."
        )
    if g.genehmigt:
        titel2 = "Protokoll genehmigt"
        text2 = f"{_genehmigt_satz(g)}."
    else:
        titel2 = (
            f"Genehmigung in Nr. {g.sitzung.meeting_number}"
            if g.sitzung is not None and g.sitzung.meeting_number
            else "Genehmigung"
        )
        if g.sitzung is not None:
            text2 = (
                f"Work hat in {g.wo} „Tagesordnung und Protokoll beschließen“ angelegt; dort stimmt die Fraktion ab."
            )
        else:
            text2 = "Die Fraktion genehmigt das Protokoll in der nächsten Sitzung unter TOP 1."
        if genehmigen:
            text2 += " Als Vorsitz können Sie es auch direkt genehmigen."
    return [
        {
            "nr": "1",
            "stand": "erledigt" if bestaetigt else "aktuell",
            "titel": "Anwesenheit bestätigt" if bestaetigt else "Anwesenheit bestätigen",
            "text": text1,
        },
        {
            "nr": "2",
            "stand": "erledigt" if g.genehmigt else ("aktuell" if bestaetigt else ""),
            "titel": titel2,
            "text": text2,
        },
        {
            "nr": "3",
            "stand": "erledigt" if v.verschickt else "",
            "titel": "Per E-Mail verschickt" if v.verschickt else "Versand per E-Mail",
            "text": v.satz,
        },
    ]


# =============================================================================
# Aufgaben aus der Sitzung, eigene Teilnahme
# =============================================================================


def aufgaben(d: Dokument) -> list[tuple[sitzung.AufgabenZeile, ProtTop]]:
    """Aufgaben aller sichtbaren TOPs (Nebenspalte „Aufgaben aus der Sitzung“)."""
    return [(a, t) for t in d.tops for a in t.aufgaben]


def eigene_aufgaben(meeting: FactionMeeting, membership: Membership, d: Dokument) -> list[Any]:
    """Aufgaben aus den sichtbaren TOPs, für die das Mitglied zuständig ist."""
    from apps.work.tasks.models import Task

    ids = [t.item.pk for t in d.tops]
    if not ids:
        return []
    return list(
        cast(Any, Task)
        .objects.filter(related_faction_agenda_item_id__in=ids, assigned_to=membership)
        .order_by("is_completed", "due_date", "created_at")
    )


def eigene_teilnahme(meeting: FactionMeeting, membership: Membership) -> dict[str, Any] | None:
    """„Anwesend, online. Bestätigt am 13.10.“ für die Nebenspalte der Mitglieder."""
    teilnahme = meeting.attendances.filter(membership=membership).first()
    if teilnahme is None:
        return None
    if teilnahme.status == "present":
        satz = "Anwesend, online." if teilnahme.participation_type == "online" else "Anwesend, vor Ort."
    elif teilnahme.status in ("excused", "declined"):
        satz = "Entschuldigt."
    else:
        satz = "Nicht anwesend."
    if meeting.attendance_confirmed_at:
        satz += f" Bestätigt am {timezone.localtime(meeting.attendance_confirmed_at):%d.%m.}"
    else:
        satz += " Noch nicht bestätigt."
    return {"satz": satz, "nachweis": bool(meeting.attendance_confirmed_at) and teilnahme.status == "present"}


def ist_top_intern(item: FactionAgendaItem) -> bool:
    return is_item_internal(item)
