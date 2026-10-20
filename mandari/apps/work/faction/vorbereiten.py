# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzung vor der Sitzung nach dem Muster (Issue #1071, Schritt 2): Planung, Einladung und Rückmeldungen.

Fachlogik der neuen Seite für Sitzungen im Entwurf, geplant oder eingeladen (``templates/work/faction/neu/vorbereiten*``).
Alles wird aus vorhandenen Daten abgeleitet; geschrieben wird nur über die bisherigen Aktionen
(``FactionActionView``, ``FactionItemPanelActionView``). Die Views rufen nur hier hinein.

Grenzen wie in der bisherigen Seite (``detail.html``):

- Nichtöffentliche TOPs (auch als Unterpunkt) und ihre Unterlagen sehen nur Vereidigte
  (:mod:`apps.work.faction.visibility`). Für alle anderen enthält der Kontext keine nichtöffentlichen Objekte, nur die
  Anzahl (gesperrter Teil mit Erklärung).
- Verwalten (Tagesordnung bearbeiten, Termin ändern, Rückmeldungen und Versandergebnis aller) darf, wer die Sitzung
  angelegt hat oder ``faction.manage`` hat (``can_edit`` der bisherigen Seite). Einladen verlangt ``faction.invite``,
  Freigeben das Recht aus :func:`apps.work.faction.invitations.can_release_invitations`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.common.formatting import count_label
from apps.work.rahmen import neues_design

from . import invitations, sitzung
from .invitations import (
    can_release_invitations,
    dispatches_automatically,
    get_invitation_settings,
    invitation_dispatch_at,
)
from .liste import _dauer, _tag, _zeitpunkt
from .services import FACTION_REMINDER_WINDOW_HOURS, invitation_attendances
from .visibility import can_view_internal, is_item_internal

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem, FactionMeeting

#: Zustände vor der Sitzung, die die neue Seite zeigt; laufend, abgeschlossen und abgesagt bleiben, wo sie sind
VOR_DER_SITZUNG = ("draft", "planned", "invited")

SEITE = "work/faction/neu/vorbereiten.html"
TAGESORDNUNG = "work/faction/neu/_vb_tagesordnung.html"


def zeigt_vorbereitung(meeting: FactionMeeting, ansicht: str | None) -> bool:
    """Neue Seite vor der Sitzung? Nur mit dem Schalter der Organisation; ``?ansicht=bisher`` führt zurück."""
    return meeting.status in VOR_DER_SITZUNG and ansicht != "bisher" and neues_design(meeting.organization)


def ist_eingeladen(meeting: FactionMeeting) -> bool:
    return bool(meeting.invitation_sent or meeting.status == "invited")


def darf_verwalten(membership: Membership, meeting: FactionMeeting) -> bool:
    """Wie ``can_edit`` der bisherigen Seite: Ersteller oder ``faction.manage``, solange die Sitzung offen ist."""
    return meeting.status in ("draft", "planned", "invited", "ongoing") and sitzung.darf_verwalten(membership, meeting)


# =============================================================================
# Tagesordnung
# =============================================================================


@dataclass
class Verweis:
    """Eine Unterlage als Link: Titel, Adresse, ob nichtöffentlich (Schloss) und ob außerhalb von Work."""

    titel: str
    href: str
    gesperrt: bool = False
    extern: bool = False


@dataclass
class Zeile:
    """Ein TOP der Tagesordnung auf der Seite vor der Sitzung."""

    item: FactionAgendaItem
    unter: bool
    intern: bool
    unterlagen: list[Verweis] = field(default_factory=list)
    bezug: str = ""
    verlauf: list[sitzung.VerlaufEintrag] = field(default_factory=list)
    von_mir: bool = False
    #: Verschieben wie bisher nur für TOPs der obersten Ebene (nicht den Genehmigungs-TOP)
    verschiebbar: bool = False
    erster: bool = False
    letzter: bool = False
    offen: bool = False
    beschreibung: str = ""


def _org_slug(item: FactionAgendaItem) -> str:
    return str(item.meeting.organization.slug)


def _unterlagen_alle(items: list[FactionAgendaItem], membership: Membership) -> dict[Any, list[sitzung.Unterlage]]:
    """
    Unterlagen aller TOPs wie :func:`apps.work.faction.sitzung.unterlagen` (gleiche Reihenfolge und Sichtbarkeit), aber
    mit einer festen Zahl an Abfragen statt mehrerer je TOP: Vorlagen, Dateien, Dokumente und Anlagen gesammelt.
    """
    from hub.ris import selectors as ris

    from . import services as faction_services
    from .models import FactionAgendaItem, FactionAgendaItemAttachment

    ids = [item.pk for item in items]
    ergebnis: dict[Any, list[sitzung.Unterlage]] = {pk: [] for pk in ids}
    if not ids:
        return ergebnis
    papiere: dict[Any, list[Any]] = {pk: [] for pk in ids}
    durch = FactionAgendaItem.related_papers.through.objects.filter(factionagendaitem_id__in=ids)
    for zeile in durch.select_related("oparlpaper"):
        papiere[zeile.factionagendaitem_id].append(zeile.oparlpaper)
    papier_ids = {paper.pk for liste in papiere.values() for paper in liste}
    dateien: dict[Any, list[Any]] = {}
    if papier_ids:
        for datei in ris.files_of_papers(papier_ids).defer("text_content", "raw_json"):
            dateien.setdefault(datei.paper_id, []).append(datei)
    motion_durch = FactionAgendaItem.related_motions.through.objects.filter(factionagendaitem_id__in=ids)
    motion_zuordnung = list(motion_durch.values_list("factionagendaitem_id", "motion_id"))
    sichtbar: dict[Any, Any] = {}
    if motion_zuordnung:
        from apps.work.motions.models import Motion

        dokumente = cast(Any, Motion).visible_to(membership).filter(pk__in={mid for _i, mid in motion_zuordnung})
        sichtbar = {m.pk: m for m in dokumente}
    anlagen: dict[Any, list[Any]] = {}
    for anlage in FactionAgendaItemAttachment.objects.filter(agenda_item_id__in=ids).order_by("created_at"):
        anlagen.setdefault(anlage.agenda_item_id, []).append(anlage)

    fassung: str | None = None
    for item in items:
        liste = ergebnis[item.pk]
        vorige = item.approves_meeting if item.is_approval_item and item.approves_meeting_id else None
        if vorige is not None:
            if fassung is None:
                fassung = sitzung.protokoll_fassung(membership)
            if fassung:
                titel = f"Protokoll vom {timezone.localtime(vorige.start):%d.%m.%Y}" if vorige.start else "Protokoll"
                liste.append(sitzung.Unterlage(f"p-{vorige.pk}", "protokoll", titel, vorige, fassung=fassung))
        mit_datum = sorted((p for p in papiere[item.pk] if p.date), key=lambda p: p.date, reverse=True)
        for paper in [*mit_datum, *(p for p in papiere[item.pk] if not p.date)]:
            eigene = list(dateien.get(paper.pk, []))
            haupt = sitzung._haupt_datei_kennung(paper)
            eigene.sort(key=lambda f: (0 if haupt and f.external_id == haupt else 1, (f.name or f.file_name or "")))
            if not eigene:
                name = paper.reference or paper.name or "Vorlage"
                liste.append(sitzung.Unterlage(f"v-{paper.pk}", "ris_vorlage", name, paper))
                continue
            for datei in eigene:
                ist_haupt = len(eigene) == 1 or bool(haupt and datei.external_id == haupt)
                titel = (paper.reference or paper.name or "Vorlage") if ist_haupt else (datei.name or datei.file_name)
                liste.append(sitzung.Unterlage(f"r-{datei.pk}", "ris_datei", titel or "Datei", datei, paper))
        motions = [sichtbar[mid] for iid, mid in motion_zuordnung if iid == item.pk and mid in sichtbar]
        for motion in sorted(motions, key=lambda m: m.title or ""):
            liste.append(sitzung.Unterlage(f"d-{motion.pk}", "dokument", motion.title or "Dokument", motion))
        for anlage in anlagen.get(item.pk, []):
            liste.append(sitzung.Unterlage(f"a-{anlage.pk}", "anlage", anlage.filename or "Anlage", anlage))
        links = [
            link
            for link in (item.reference_links or [])
            if isinstance(link, dict) and cast(Any, faction_services).safe_link_url(str(link.get("url") or ""))
        ]
        if links:
            liste.append(sitzung.Unterlage("links", "links", "Links", links))
    return ergebnis


def _verweise(item: FactionAgendaItem, unterlagen: list[sitzung.Unterlage]) -> list[Verweis]:
    """Unterlagen eines TOPs als Links: Protokoll zur Genehmigung, RIS-Dateien und -Vorlagen, Dokumente, Anlagen."""
    slug = _org_slug(item)
    intern = is_item_internal(item)
    verweise: list[Verweis] = []
    for u in unterlagen:
        if u.art == "protokoll":
            variante = "intern" if u.fassung == "intern" else "oeffentlich"
            href = reverse(
                "work:faction_protocol_pdf",
                kwargs={"org_slug": slug, "meeting_id": u.objekt.pk, "variant": variante},
            )
        elif u.art == "ris_datei":
            href = reverse("insight_core:insight:file_proxy", args=[u.objekt.pk])
        elif u.art == "ris_vorlage":
            href = reverse("work:ris_paper_detail", kwargs={"org_slug": slug, "paper_id": u.objekt.pk})
        elif u.art == "dokument":
            href = reverse("work:motion_detail", kwargs={"org_slug": slug, "motion_id": u.objekt.pk})
        elif u.art == "anlage":
            href = reverse(
                "work:faction_attachment_download",
                kwargs={
                    "org_slug": slug,
                    "meeting_id": item.meeting_id,
                    "item_id": item.pk,
                    "attachment_id": u.objekt.pk,
                },
            )
        elif u.art == "links":
            for link in u.objekt:
                url = str(link.get("url") or "")
                verweise.append(Verweis(str(link.get("title") or url), url, gesperrt=intern, extern=True))
            continue
        else:  # pragma: no cover – neue Arten zeigt die Seite erst, wenn sie hier stehen
            continue
        verweise.append(Verweis(u.titel, href, gesperrt=intern))
    return verweise


def _bezug(item: FactionAgendaItem, verweise: list[Verweis]) -> str:
    """Zweite Spalte der Planungsliste: wofür der TOP steht (Genehmigung, Vorschlag, Unterlagen)."""
    vorige = item.approves_meeting if item.is_approval_item and item.approves_meeting_id else None
    if vorige is not None and vorige.start:
        return f"genehmigt das Protokoll vom {timezone.localtime(vorige.start):%d.%m.}"
    if item.proposed_by_id and item.proposed_by is not None:
        return f"Vorschlag von {item.proposed_by.user.get_display_name()}"
    if verweise:
        return ", ".join(v.titel for v in verweise)
    if item.standard_item_id:
        return "Standard-Tagesordnung"
    return ""


def tagesordnung(
    meeting: FactionMeeting, membership: Membership, *, offen: Any = None, mit_verlauf: bool = False
) -> dict[str, Any]:
    """
    Tagesordnung für Planung und Lesen: öffentlicher Teil, nichtöffentlicher Teil (nur Vereidigte), sonst die Zahl
    der gesperrten TOPs. ``offen``: TOP, dessen Angaben aufgeklappt sind. ``mit_verlauf``: „Im Beratungsverlauf“ je
    TOP (lesende Tagesordnung), sonst nur für den geöffneten.
    """
    ordnung = sitzung.tagesordnung(meeting, membership)
    darf_verlauf = sitzung.darf_beratungsverlauf_sehen(membership)
    offen_id = str(offen or "")
    teile: dict[str, list[Zeile]] = {"oeffentlich": [], "intern": []}
    alle_unterlagen = _unterlagen_alle([z.item for z in ordnung.zeilen], membership)
    for teil, quelle in (("oeffentlich", ordnung.oeffentlich), ("intern", ordnung.intern)):
        oben = [z for z in quelle if not z.unter and not z.item.is_approval_item]
        for z in quelle:
            item = z.item
            verweise = _verweise(item, alle_unterlagen[item.pk])
            zeile = Zeile(
                item=item,
                unter=z.unter,
                intern=z.intern,
                unterlagen=verweise,
                bezug=_bezug(item, verweise),
                von_mir=bool(item.proposed_by_id and item.proposed_by_id == membership.pk),
                verschiebbar=not z.unter and not item.is_approval_item,
                erster=bool(oben) and oben[0].item.pk == item.pk,
                letzter=bool(oben) and oben[-1].item.pk == item.pk,
                offen=str(item.pk) == offen_id,
            )
            # Beratungsverlauf nur für TOPs mit Bezug zum RIS (verknüpfter TOP oder Vorlage), sonst keine Abfrage
            mit_bezug = bool(item.related_agenda_item_id) or any(
                u.art in ("ris_datei", "ris_vorlage") for u in alle_unterlagen[item.pk]
            )
            if darf_verlauf and mit_bezug and (mit_verlauf or zeile.offen):
                zeile.verlauf = sitzung.beratungsverlauf(item, meeting.organization)
            if zeile.offen:
                zeile.beschreibung = cast(Any, item).get_description_decrypted() or ""
            teile[teil].append(zeile)
    oeffentlich, intern = teile["oeffentlich"], teile["intern"]
    gesamt = len(oeffentlich) + len(intern) + ordnung.gesperrt
    nicht_oeffentlich = len(intern) + ordnung.gesperrt
    zahl = count_label(gesamt, "TOP", "TOPs")
    if nicht_oeffentlich:
        zahl += f", davon {nicht_oeffentlich} nichtöffentlich"
    return {
        "to_oeffentlich": oeffentlich,
        "to_intern": intern,
        "to_gesperrt": ordnung.gesperrt,
        "to_zahl": zahl,
        "to_gesamt": gesamt,
        "to_offen": offen_id,
        "to_eltern": [z for z in oeffentlich if not z.unter and not z.item.is_approval_item],
        "to_eltern_intern": [z for z in intern if not z.unter and not z.item.is_approval_item],
    }


def eigene_unterlagen(teile: dict[str, Any]) -> list[tuple[Verweis, str]]:
    """Alle Unterlagen der sichtbaren TOPs mit ihrer Nummer, für die Nebenspalte der Mitglieder."""
    liste: list[tuple[Verweis, str]] = []
    for zeile in [*teile["to_oeffentlich"], *teile["to_intern"]]:
        for verweis in zeile.unterlagen:
            nummer = (
                f"NÖ {zeile.item.number}" if zeile.intern and zeile.item.visibility == "internal" else zeile.item.number
            )
            liste.append((verweis, f"TOP {nummer}" if nummer else ""))
    return liste


def ratssitzungen(meeting: FactionMeeting, membership: Membership) -> list[dict[str, Any]]:
    """
    Sitzungen des Rats und der Ausschüsse, die diese Fraktionssitzung vorbereitet (verknüpfte TOPs): Name, Datum und der
    Weg zur Vorbereitung (mit ``meetings.prepare``) bzw. zur Sitzung (mit ``meetings.view``).
    """
    from .models import FactionAgendaItem

    intern = can_view_internal(membership)
    qs = FactionAgendaItem.objects.filter(
        meeting=meeting, proposal_status="active", related_agenda_item__isnull=False
    ).select_related("related_agenda_item__meeting", "parent")
    gesehen: dict[Any, dict[str, Any]] = {}
    for item in qs:
        if is_item_internal(item) and not intern:
            continue
        ris = item.related_agenda_item.meeting if item.related_agenda_item else None
        if ris is None or ris.pk in gesehen:
            continue
        name = ris.name or "Sitzung"
        datum = date_format(timezone.localtime(ris.start), "D d.m.Y") if ris.start else ""
        href = ""
        vorbereiten = membership.has_permission("meetings.prepare")
        adresse = "work:meeting_prepare" if vorbereiten else "work:meeting_detail"
        if vorbereiten or membership.has_permission("meetings.view"):
            try:
                href = reverse(adresse, kwargs={"org_slug": meeting.organization.slug, "meeting_id": ris.pk})
            except NoReverseMatch:  # pragma: no cover
                href = ""
        gesehen[ris.pk] = {"name": name, "datum": datum, "href": href, "vorbereiten": vorbereiten}
    return list(gesehen.values())


# =============================================================================
# Einladung und Rückmeldungen
# =============================================================================

#: Gruppen der Rückmeldungen in der Reihenfolge des Musters
RUECKMELDE_GRUPPEN = (
    ("invited", "Noch offen", "offen"),
    ("tentative", "Vielleicht", "enthaltung"),
    ("confirmed", "Zugesagt", "zustimmung"),
    ("declined", "Abgesagt", "ablehnung"),
)
RUECKMELDE_WORT = {"invited": "offen", "tentative": "vielleicht", "confirmed": "zugesagt", "declined": "abgesagt"}


@dataclass
class Person:
    """Eine eingeladene Person: Rückmeldung und Versandergebnis der Einladung."""

    attendance: Any
    name: str
    kuerzel: str
    rollen: str
    status: str
    wort: str
    punkt: str
    antwort_am: str
    nachricht: str
    #: Versandergebnis: verschickt (Zeitpunkt), Fehler mit Grund oder nicht erfasst (Versand vor dieser Erfassung)
    versendet: str
    fehler: str
    ohne_noe: bool
    #: Erinnerung an die offene Rückmeldung („Offene erinnern“): wann bzw. warum nicht
    erinnert: str = ""
    erinnert_fehler: str = ""

    @property
    def versand_text(self) -> str:
        """Ergebnis für die Versandliste, wenn nichts schiefging."""
        if self.versendet:
            return f"verschickt {self.versendet}" + (", ohne nichtöffentlichen Teil" if self.ohne_noe else "")
        return "kein Ergebnis erfasst"


def _kurz(wann: Any) -> str:
    return f"{timezone.localtime(wann):%d.%m., %H:%M}" if wann else ""


def personen(meeting: FactionMeeting) -> list[Person]:
    """Eingeladene Mitglieder (ohne Gäste) mit Rolle, Rückmeldung und Versandergebnis, sortiert nach Namen."""
    punkte = {status: punkt for status, _name, punkt in RUECKMELDE_GRUPPEN}
    teilnahmen = (
        meeting.attendances.filter(is_guest=False)
        .select_related("membership__user")
        .prefetch_related("membership__roles")
    )
    liste: list[Person] = []
    for a in teilnahmen:
        m = a.membership
        name = a.get_display_name()
        kuerzel = m.user.get_initials() if m is not None else "".join(t[:1] for t in name.split()[:2]).upper()
        rollen = ", ".join(r.name for r in m.roles.all()) if m is not None else ""
        liste.append(
            Person(
                attendance=a,
                name=name,
                kuerzel=kuerzel,
                rollen=rollen,
                status=a.status,
                wort=RUECKMELDE_WORT.get(a.status, a.get_status_display()),
                punkt=punkte.get(a.status, "andere"),
                antwort_am=_kurz(a.responded_at),
                nachricht=a.response_message or "",
                versendet=_kurz(a.invitation_mailed_at),
                fehler=a.invitation_error or "",
                ohne_noe=bool(a.invitation_without_internal),
                erinnert=_kurz(a.rsvp_reminder_mailed_at),
                erinnert_fehler=a.rsvp_reminder_error or "",
            )
        )
    return liste


def rueckmeldungen(liste: list[Person]) -> dict[str, Any]:
    """Zahlen und Gruppen der Rückmeldungen („5 Zusagen, 1 Absage und 3 offen“)."""
    zahl = {status: 0 for status, *_ in RUECKMELDE_GRUPPEN}
    for p in liste:
        if p.status in zahl:
            zahl[p.status] += 1
    teile = [count_label(zahl["confirmed"], "Zusage", "Zusagen"), count_label(zahl["declined"], "Absage", "Absagen")]
    if zahl["tentative"]:
        teile.append(f"{zahl['tentative']} vielleicht")
    satz = ", ".join(teile) + f" und {zahl['invited']} offen"
    gruppen = []
    for status, name, _punkt in RUECKMELDE_GRUPPEN:
        mitglieder = [p for p in liste if p.status == status]
        if mitglieder:
            gruppen.append({"name": name, "personen": mitglieder})
    weitere = [p for p in liste if p.status not in zahl]
    if weitere:
        gruppen.append({"name": "Weitere", "personen": weitere})
    return {"satz": satz, "gruppen": gruppen, "zahl": zahl}


def versand(liste: list[Person]) -> dict[str, Any]:
    """Versandergebnis: wie viele verschickt, wie viele nicht, wie viele ohne Erfassung (älterer Versand)."""
    ok = sum(1 for p in liste if p.versendet)
    nicht = sum(1 for p in liste if p.fehler)
    return {"ok": ok, "nicht": nicht, "gesamt": len(liste), "unbekannt": len(liste) - ok - nicht}


# -----------------------------------------------------------------------------
# Offene erinnern (Issue #1071)
# -----------------------------------------------------------------------------

#: Sperrfrist zwischen zwei Erinnerungen von Hand an alle ohne Rückmeldung (Schutz gegen Mehrfachklicks und Spam)
ERINNERN_SPERRE = timedelta(hours=2)


class ErinnernNichtMoeglichError(Exception):
    """Offene erinnern geht gerade nicht; die Meldung ist ein Satz für die Oberfläche."""


def offene_erinnern_stand(meeting: FactionMeeting, offen: int) -> dict[str, Any]:
    """Knopf „Offene erinnern“: möglich, zuletzt erinnert am, wieder möglich ab."""
    jetzt = timezone.now()
    zuletzt = meeting.rsvp_reminder_sent_at
    wieder_ab = zuletzt + ERINNERN_SPERRE if zuletzt else None
    gesperrt = bool(wieder_ab and wieder_ab > jetzt)
    return {
        "offen": offen,
        "moeglich": bool(offen) and not gesperrt and meeting.start > jetzt and meeting.status in ("planned", "invited"),
        "zuletzt": _zeitpunkt(zuletzt) if zuletzt else "",
        "wieder_ab": f"{timezone.localtime(wieder_ab):%H:%M}" if gesperrt and wieder_ab else "",
    }


def offene_erinnern(meeting: FactionMeeting, now: Any = None) -> tuple[int, int]:
    """
    Erinnerung an alle Eingeladenen ohne Rückmeldung senden (vorhandener Erinnerungsversand). Nur nach dem Versand
    der Einladung, mit Zu- und Absagen, vor Beginn und höchstens einmal je Sperrfrist: Der Zeitpunkt wird vorher
    atomar beansprucht, ein zweiter Klick oder ein zweiter Tab verschickt nichts. Liefert (verschickt, nicht).
    """
    from django.db.models import Q

    from .models import FactionMeeting as Sitzung
    from .services import FactionMeetingEmailService

    jetzt = now or timezone.now()
    if not ist_eingeladen(meeting) or not meeting.rsvp_enabled:
        raise ErinnernNichtMoeglichError("Erinnern geht erst nach der Einladung und mit Zu- und Absagen.")
    if meeting.status not in ("planned", "invited") or meeting.start <= jetzt:
        raise ErinnernNichtMoeglichError("Die Sitzung hat schon begonnen.")
    if not invitation_attendances(meeting).exclude(membership__is_guest=True).exists():
        raise ErinnernNichtMoeglichError("Alle Eingeladenen haben schon geantwortet.")
    beansprucht = (
        Sitzung.objects.filter(pk=meeting.pk)
        .filter(Q(rsvp_reminder_sent_at__isnull=True) | Q(rsvp_reminder_sent_at__lte=jetzt - ERINNERN_SPERRE))
        .update(rsvp_reminder_sent_at=jetzt)
    )
    if not beansprucht:
        meeting.refresh_from_db(fields=["rsvp_reminder_sent_at"])
        wieder = meeting.rsvp_reminder_sent_at + ERINNERN_SPERRE if meeting.rsvp_reminder_sent_at else jetzt
        raise ErinnernNichtMoeglichError(
            f"Work hat gerade erst erinnert. Erneut erinnern können Sie ab {timezone.localtime(wieder):%H:%M}."
        )
    meeting.rsvp_reminder_sent_at = jetzt
    return FactionMeetingEmailService().send_rsvp_reminder(meeting)


def erstversand(organization: Any) -> bool:
    """Erste Einladung der Organisation über ihren Versandweg? Dann rät der Dialog zur Testmail (Muster)."""
    from .models import FactionMeeting as Sitzung

    return not Sitzung.objects.filter(organization=organization, invitation_sent=True).exists()


def empfaenger(meeting: FactionMeeting) -> dict[str, Any]:
    """Empfänger des Erstversands: eingeladene aktive Mitglieder ohne Gastzugang; wer nicht vereidigt ist."""
    teilnahmen = list(
        invitation_attendances(meeting).exclude(membership__is_guest=True).select_related("membership__user")
    )
    alle = []
    nicht = []
    for a in teilnahmen:
        name = a.membership.user.get_display_name()
        intern = can_view_internal(a.membership)
        alle.append({"name": name, "mail": a.membership.user.email or "", "intern": intern})
        if not intern:
            nicht.append(name)
    return {"alle": alle, "nicht_vereidigt": nicht, "anzahl": len(alle)}


def absender(organization: Any) -> str:
    """Absender der Einladung für den Dialog: Absender des Versandwegs (``Name <adresse>``) und der Weg."""
    from apps.common.mail import config

    try:
        adresse = config.resolve(organization).from_email() or ""
    except Exception:  # Anzeige; ein fehlerhafter Versandweg zeigt sich beim Versand selbst
        adresse = ""
    weg = (
        "über den Mailserver der Fraktion"
        if getattr(organization, "smtp_from_email", "")
        else "über den Versand von mandari"
    )
    return f"{adresse or organization.name}, {weg}"


def einladung_zeitpunkt(meeting: FactionMeeting) -> dict[str, Any]:
    """Geplanter Versand für den Dialog „Jetzt einladen“: Zeitpunkt, automatisch oder nach Freigabe, schon vorbei."""
    einstellungen = get_invitation_settings(meeting.organization)
    versand_am = invitation_dispatch_at(meeting, einstellungen)
    return {
        "einladung_zeitpunkt": _zeitpunkt(versand_am),
        "einladung_automatisch": dispatches_automatically(meeting, einstellungen),
        "einladung_vorbei": versand_am <= timezone.now(),
    }


def tagesordnung_geaendert(meeting: FactionMeeting) -> bool:
    """Seit dem letzten Versand TOPs angelegt oder geändert? Dann gehört eine Aktualisierung an alle."""
    stand = meeting.invitation_updated_at or meeting.invitation_sent_at
    if not stand:
        return False
    return meeting.agenda_items.filter(proposal_status="active", updated_at__gt=stand).exists()


# =============================================================================
# Kopf: Ablaufleiste und Infoleiste
# =============================================================================


def _start_ab(meeting: FactionMeeting) -> Any:
    return meeting.start - timedelta(minutes=30)


def stufen(meeting: FactionMeeting, to_zahl: int, rm: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Ablaufleiste Planung → Einladung → Sitzung → Protokoll vor der Sitzung (wie im Muster)."""
    einstellungen = get_invitation_settings(meeting.organization)
    tops = count_label(to_zahl, "TOP", "TOPs")
    lokal = timezone.localtime(meeting.start)
    sitzung_unter = f"{_tag(lokal)}, {lokal:%H:%M}"
    if ist_eingeladen(meeting):
        if rm is not None:
            einladung = f"{rm['zahl']['confirmed']} zugesagt, {rm['zahl']['invited']} offen"
        elif meeting.invitation_sent_at:
            einladung = f"am {timezone.localtime(meeting.invitation_sent_at):%d.%m.} versendet"
        else:
            einladung = "versendet"
        return [
            {"nr": 1, "name": "Planung", "unter": tops, "stand": "erledigt"},
            {"nr": 2, "name": "Einladung", "unter": einladung, "stand": "aktuell"},
            {"nr": 3, "name": "Sitzung", "unter": sitzung_unter, "stand": ""},
            {"nr": 4, "name": "Protokoll", "unter": "nach der Sitzung", "stand": ""},
        ]
    versand_am = invitation_dispatch_at(meeting, einstellungen)
    lokal_v = timezone.localtime(versand_am)
    if dispatches_automatically(meeting, einstellungen):
        einladung = f"automatisch {_tag(lokal_v)}, {lokal_v:%H:%M}"
    else:
        einladung = f"nach Freigabe, {_tag(lokal_v)}"
    planung = "Tagesordnung anlegen" if to_zahl == 0 else f"{tops}, Entwurf"
    return [
        {"nr": 1, "name": "Planung", "unter": planung, "stand": "aktuell"},
        {"nr": 2, "name": "Einladung", "unter": einladung, "stand": ""},
        {"nr": 3, "name": "Sitzung", "unter": sitzung_unter, "stand": ""},
        {"nr": 4, "name": "Protokoll", "unter": "nach der Sitzung", "stand": ""},
    ]


@dataclass
class Info:
    """Infoleiste unter dem Kopf: ein Satz (fett beginnend) und höchstens eine Hauptaktion daneben."""

    fett: str = ""
    text: str = ""
    #: Hauptaktion: einladen, freigeben, aktualisieren, starten oder leer
    aktion: str = ""
    #: Knopf „Versandergebnis“ neben der Aktion
    versand: bool = False
    starten_ab: str = ""
    #: „Sitzung starten“ zusätzlich anbieten (ab 30 Minuten vor Beginn mit faction.start, unabhängig von der Einladung)
    starten: bool = False


def info(meeting: FactionMeeting, membership: Membership, *, verwalten: bool, versand_zahl: dict[str, Any]) -> Info:
    """
    Satz und Aktion der Infoleiste je Zustand und Rolle (Muster: zeichneInfo). „Sitzung starten“ gibt es wie bisher
    (``can_start``) für alle mit ``faction.start`` ab 30 Minuten vor Beginn, auch ohne Einladung und ohne Verwaltung.
    """
    darf_starten = membership.has_permission("faction.start") and meeting.status in ("planned", "invited")
    start_ab = _start_ab(meeting)
    ergebnis = _info(meeting, membership, verwalten=verwalten, versand_zahl=versand_zahl)
    if darf_starten and not ergebnis.fett:
        # Eingeladen, ohne Verwaltung: nur der Hinweis zum Start
        if start_ab <= timezone.now():
            ergebnis = Info("Eingeladen.", "Sie können die Sitzung jetzt starten.", "starten")
        else:
            ergebnis = Info("Eingeladen.", f"Starten können Sie die Sitzung ab {_zeitpunkt(start_ab)}.", "starten")
        ergebnis.starten_ab = _zeitpunkt(start_ab)
    ergebnis.starten = darf_starten and start_ab <= timezone.now() and ergebnis.aktion != "starten"
    return ergebnis


def _info(meeting: FactionMeeting, membership: Membership, *, verwalten: bool, versand_zahl: dict[str, Any]) -> Info:
    einstellungen = get_invitation_settings(meeting.organization)
    jetzt = timezone.now()
    darf_einladen = membership.has_permission("faction.invite")
    start_ab = _start_ab(meeting)
    if not ist_eingeladen(meeting):
        versand_am = invitation_dispatch_at(meeting, einstellungen)
        automatisch = dispatches_automatically(meeting, einstellungen)
        entwurf = "Entwurf. " if meeting.status == "draft" else ""
        if not verwalten and not darf_einladen:
            if versand_am > jetzt:
                return Info("In Planung.", f"Die Einladung kommt am {_zeitpunkt(versand_am)}.")
            return Info("In Planung.", "Die Einladung kommt in Kürze.")
        aktion = "einladen" if darf_einladen else ""
        if versand_am <= jetzt:
            return Info(
                "Noch nicht eingeladen.",
                f"{entwurf}Der geplante Versand am {_zeitpunkt(versand_am)} ist vorbei. Laden Sie jetzt ein.",
                aktion,
            )
        if automatisch:
            if meeting.invitation_released_at and cast(Any, invitations).auto_invite_schedule(meeting) is None:
                satz = f"Versand freigegeben. Work lädt am {_zeitpunkt(versand_am)} ein, mit der Tagesordnung, die dann gilt."
            else:
                satz = f"Work lädt am {_zeitpunkt(versand_am)} automatisch ein, mit der Tagesordnung, die dann gilt."
            return Info("Noch nicht eingeladen.", f"{entwurf}{satz}", aktion)
        if can_release_invitations(membership):
            aktion = "freigeben"
        return Info(
            "Noch nicht eingeladen.",
            f"{entwurf}Die Einladung geht nach Freigabe durch den Vorsitz raus, geplant am {_zeitpunkt(versand_am)}.",
            aktion,
        )
    # Eingeladen
    if not verwalten:
        return Info()
    darf_starten = membership.has_permission("faction.start")
    if darf_einladen and tagesordnung_geaendert(meeting):
        return Info(
            "Tagesordnung geändert.",
            "Senden Sie eine Aktualisierung, damit alle die neue Fassung und den Kalendereintrag erhalten.",
            "aktualisieren",
            versand=True,
        )
    gesendet = meeting.invitation_sent_at
    wann = f"am {_zeitpunkt(gesendet)}" if gesendet else ""
    v = versand_zahl
    if v["gesamt"] and v["unbekannt"] < v["gesamt"]:
        if v["nicht"]:
            zahl = (
                f"{v['ok']} von {v['gesamt']} Einladungen versendet, {'eine' if v['nicht'] == 1 else v['nicht']} nicht."
            )
        else:
            zahl = "Alle Einladungen sind versendet." if not v["unbekannt"] else f"{v['ok']} Einladungen versendet."
    else:
        zahl = ""
    if start_ab <= jetzt:
        start = f"Beginn {'heute ' if timezone.localdate(meeting.start) == timezone.localdate() else ''}um {timezone.localtime(meeting.start):%H:%M}, Sie können die Sitzung jetzt starten."
    else:
        start = f"Starten können Sie die Sitzung ab {_zeitpunkt(start_ab)}."
    text = " ".join(t for t in (f"{wann}." if wann else "", zahl, start if darf_starten else "") if t)
    return Info(
        "Eingeladen",
        text,
        "starten" if darf_starten else "",
        versand=True,
        starten_ab=_zeitpunkt(start_ab),
    )


def erinnerung_satz(meeting: FactionMeeting, *, mitglied: bool) -> str:
    """„Erinnerung zum Eintragen von TOPs“: verschickt oder geplant, nur wenn eingeschaltet."""
    einstellungen = get_invitation_settings(meeting.organization)
    if meeting.agenda_reminder_sent_at:
        wann = _zeitpunkt(meeting.agenda_reminder_sent_at)
        return (
            f"Work hat am {wann} daran erinnert, Punkte einzutragen oder vorzuschlagen."
            if mitglied
            else f"Am {wann} an alle Mitglieder verschickt, die TOPs eintragen oder vorschlagen dürfen."
        )
    if not einstellungen["agenda_reminder_enabled"] or ist_eingeladen(meeting):
        return ""
    versand_am = invitation_dispatch_at(meeting, einstellungen)
    wann_dt = versand_am - timedelta(hours=einstellungen["agenda_reminder_hours"])
    if wann_dt <= timezone.now():
        return ""
    return f"Am {_zeitpunkt(wann_dt)} erinnert Work alle daran, Punkte einzutragen oder vorzuschlagen."


def einladung_satz(meeting: FactionMeeting, nicht_vereidigt: list[str]) -> tuple[str, str]:
    """Abschnitt „Einladung“ der Nebenspalte in der Planung: Satz und Erläuterung zur Regel (Muster)."""
    einstellungen = get_invitation_settings(meeting.organization)
    versand_am = invitation_dispatch_at(meeting, einstellungen)
    reihe = cast(Any, invitations).auto_invite_schedule(meeting)
    if dispatches_automatically(meeting, einstellungen):
        satz = f"Automatisch am {_zeitpunkt(versand_am)}, ohne Freigabe, mit der Tagesordnung, die dann gilt."
    else:
        satz = f"Nach Freigabe durch den Vorsitz, geplant am {_zeitpunkt(versand_am)}, mit der Tagesordnung, die dann gilt."
    if nicht_vereidigt:
        namen = ", ".join(nicht_vereidigt)
        einzahl = len(nicht_vereidigt) == 1
        satz += (
            f" {namen} {'ist' if einzahl else 'sind'} nicht vereidigt und {'erhält' if einzahl else 'erhalten'} sie "
            "ohne nichtöffentlichen Teil."
        )
    if reihe is not None:
        regel = "Fester Zeitpunkt der Sitzungsreihe, von der Fraktion eingeschaltet."
    else:
        regel = (
            f"{_dauer(einstellungen['invitation_lead_hours'], 'vor Beginn')}, wie in den Einstellungen der Fraktion."
        )
    return satz, regel


def erinnerung_sitzung(meeting: FactionMeeting) -> str:
    """Erinnerung an die Sitzung (48 Stunden vorher, an alle Eingeladenen bzw. Zusagen)."""
    if meeting.reminder_sent_at:
        return f"Work hat am {_zeitpunkt(meeting.reminder_sent_at)} an die Sitzung erinnert."
    wann = meeting.start - timedelta(hours=FACTION_REMINDER_WINDOW_HOURS)
    dauer = _dauer(FACTION_REMINDER_WINDOW_HOURS, "vor Beginn")
    if wann <= timezone.now():
        return ""
    if meeting.rsvp_enabled:
        return (
            f"Work erinnert alle, die zugesagt oder „vielleicht“ geantwortet haben, automatisch am {_zeitpunkt(wann)},"
            f" {dauer}."
        )
    return f"Work erinnert alle Eingeladenen am {_zeitpunkt(wann)} an die Sitzung, {dauer}."
