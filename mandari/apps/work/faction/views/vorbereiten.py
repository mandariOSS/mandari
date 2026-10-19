# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seite einer Fraktionssitzung vor der Sitzung nach dem Muster (Issue #1071, Schritt 2): Planung, Einladung, Rückmeldungen.

Über :class:`FactionMeetingDetailView`, wenn der Schalter der Organisation an ist und die Sitzung im Entwurf, geplant
oder eingeladen ist. Geschrieben wird über die bisherigen Aktionen (``FactionActionView``); deren Antworten für die
Tagesordnung rendert :func:`tagesordnung_html`, wenn die Anfrage von dieser Seite kommt (``X-Work-Gestaltung: neu``).
Fachlogik in :mod:`apps.work.faction.vorbereiten`.
"""

from __future__ import annotations

from typing import Any, cast

from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse

from .. import agenda, liste, ratssitzung, vorbereiten
from ..models import FactionMeeting
from . import _helpers

#: Platzhalter in der Panel-URL (wie PANEL_ITEM_PLACEHOLDER in meetings.py und frontend/alpine/faction-detail.ts)
_PLATZHALTER = "00000000-0000-0000-0000-000000000000"


def _detail_config(meeting: FactionMeeting) -> dict[str, str]:
    """Konfiguration der Alpine-Komponente ``factionDetail`` (Panel, Löschdialoge), wie auf der bisherigen Seite."""
    slug = meeting.organization.slug
    return {
        "actionUrl": reverse("work:faction_action", kwargs={"org_slug": slug, "meeting_id": meeting.id}),
        "panelUrlTemplate": reverse(
            "work:faction_item_panel",
            kwargs={"org_slug": slug, "meeting_id": meeting.id, "item_id": _PLATZHALTER},
        ),
    }


def _modus(view: Any, meeting: FactionMeeting, ansicht: str | None) -> str:
    """
    Was die Hauptspalte zeigt: „planung“ (Tagesordnung bearbeiten), „einladung“ (Versand und Rückmeldungen) oder
    „lesen“ (Mitglieder: Einladung, Tagesordnung, eigene Zu- oder Absage).
    """
    if not vorbereiten.darf_verwalten(view.membership, meeting):
        return "lesen"
    if vorbereiten.ist_eingeladen(meeting) and ansicht != "tagesordnung":
        return "einladung"
    return "planung"


def tagesordnung_kontext(
    view: Any, meeting: FactionMeeting, offen: Any = None, *, lesen: bool = False
) -> dict[str, Any]:
    """Kontext der Tagesordnung (auch für die Antworten der Aktionen): bisheriger Sitzungskontext plus Zeilen."""
    kontext: dict[str, Any] = dict(cast(Any, _helpers)._get_meeting_context(view, meeting))
    kontext |= vorbereiten.tagesordnung(meeting, view.membership, offen=offen, mit_verlauf=lesen)
    # „Aus Ratssitzung übernehmen …“ (Issue #1071): wer die Tagesordnung verwaltet, vor Beginn, mit Kommune
    kontext["ris_moeglich"] = (
        bool(kontext.get("can_edit"))
        and meeting.status in agenda.PROPOSAL_OPEN_STATUSES
        and ratssitzung.moeglich(meeting)
    )
    return kontext


def _to_config(view: Any, meeting: FactionMeeting) -> dict[str, str]:
    """Konfiguration der Komponente ``fsTagesordnung`` (Ziehen, sofort speichern, Sicherung im Browser)."""
    slug = meeting.organization.slug
    return {
        "actionUrl": reverse("work:faction_action", kwargs={"org_slug": slug, "meeting_id": meeting.id}),
        "felderUrl": reverse(
            "work:faction_item_fields", kwargs={"org_slug": slug, "meeting_id": meeting.id, "item_id": _PLATZHALTER}
        ),
        # Kennungen, nie Namen: trennen die Sicherung ungespeicherter Eingaben je Konto, Organisation und Sitzung
        "accountId": str(view.request.user.pk),
        "orgId": str(meeting.organization_id),
        "meetingId": str(meeting.id),
    }


def tagesordnung_html(view: Any, request: HttpRequest, meeting: FactionMeeting) -> str:
    """Tagesordnung der Planung nach einer Aktion (TOP anlegen, ändern, verschieben, löschen); der offene TOP bleibt offen."""
    offen = request.POST.get("offen") or request.GET.get("top")
    kontext = tagesordnung_kontext(view, meeting, offen)
    return render_to_string(vorbereiten.TAGESORDNUNG, kontext, request=request)


def seiten_kontext(view: Any, meeting: FactionMeeting, request: HttpRequest) -> dict[str, Any]:
    """Kontext der ganzen Seite: Kopf mit Ablaufleiste, Infoleiste, Hauptspalte je Modus, Nebenspalte, Dialoge."""
    ansicht = request.GET.get("ansicht")
    modus = _modus(view, meeting, ansicht)
    kontext = tagesordnung_kontext(view, meeting, request.GET.get("top"), lesen=modus == "lesen")
    membership = view.membership
    eingeladen = vorbereiten.ist_eingeladen(meeting)
    verwalten = vorbereiten.darf_verwalten(membership, meeting)

    personen = vorbereiten.personen(meeting) if verwalten and eingeladen else []
    rm = vorbereiten.rueckmeldungen(personen) if personen and meeting.rsvp_enabled else None
    versand = vorbereiten.versand(personen)
    darf_einladen = membership.has_permission("faction.invite")
    empfaenger = vorbereiten.empfaenger(meeting) if not eingeladen and (verwalten or darf_einladen) else None

    kontext.update(
        {
            "modus": modus,
            "eingeladen": eingeladen,
            "verwalten": verwalten,
            "darf_einladen": darf_einladen,
            "detail_config": _detail_config(meeting),
            "stufen": vorbereiten.stufen(meeting, kontext["to_gesamt"], rm),
            "info": vorbereiten.info(meeting, membership, verwalten=verwalten, versand_zahl=versand),
            "personen": personen,
            "gaeste": [a for a in kontext["attendances"] if a.is_guest],
            "rm": rm,
            "versand": versand,
            "empfaenger": empfaenger,
            "erinnerung_tops": vorbereiten.erinnerung_satz(meeting, mitglied=not verwalten),
            "erinnerung_sitzung": vorbereiten.erinnerung_sitzung(meeting),
            "ratssitzungen": vorbereiten.ratssitzungen(meeting, membership),
            "reihe_satz": liste._rhythmus(meeting.schedule) if meeting.schedule_id else "",
            "antworten": ANTWORTEN,
            "to_config": _to_config(view, meeting),
            "erinnern": (
                vorbereiten.offene_erinnern_stand(meeting, rm["zahl"]["invited"]) if rm and darf_einladen else None
            ),
        }
    )
    if empfaenger is not None:
        kontext["einladung_satz"], kontext["einladung_regel"] = vorbereiten.einladung_satz(
            meeting, empfaenger["nicht_vereidigt"]
        )
        kontext["vorschau"] = _vorschau(empfaenger)
        kontext["absender"] = vorbereiten.absender(meeting.organization)
        kontext["erstversand"] = vorbereiten.erstversand(meeting.organization)
        kontext |= vorbereiten.einladung_zeitpunkt(meeting)
    kontext["teilnehmende"] = _teilnehmende(meeting, personen or vorbereiten.personen(meeting), kontext["gaeste"])
    if modus == "lesen":
        kontext |= _lesen_kontext(meeting, kontext)
    return kontext


def _teilnehmende(meeting: FactionMeeting, alle: list[Any], gaeste: list[Any]) -> dict[str, Any] | None:
    """
    Teilnehmende wie die Teilnehmerliste der bisherigen Seite (für alle sichtbar): nach Rückmeldung gruppiert, wenn
    Zu- und Absagen an sind, sonst alle Eingeladenen; Gäste als eigene Gruppe.
    """
    gruppen: list[dict[str, Any]]
    rm = None
    if meeting.rsvp_enabled:
        rm = vorbereiten.rueckmeldungen(alle)
        gruppen = list(rm["gruppen"])
    else:
        gruppen = [{"name": "Eingeladen", "personen": alle}] if alle else []
    if gaeste:
        gruppen.append({"name": "Gäste", "personen": [{"name": a.get_display_name()} for a in gaeste]})
    return {"rm": rm, "gruppen": gruppen} if gruppen else None


#: Knöpfe der eigenen Antwort: Status, Beschriftung, Symbol (Muster antwortHtml)
ANTWORTEN = (("confirmed", "Zusagen", "check"), ("tentative", "Vielleicht", ""), ("declined", "Absagen", "x"))


def _lesen_kontext(meeting: FactionMeeting, kontext: dict[str, Any]) -> dict[str, Any]:
    """Sicht der Mitglieder: Überschrift, Antwortknöpfe, Teilnehmende (wie die Teilnehmerliste bisher), Unterlagen."""
    from datetime import timedelta

    from django.utils import timezone

    from ..invitations import get_invitation_settings, invitation_dispatch_at
    from ..services import meeting_reminder_hours

    versand_am = invitation_dispatch_at(meeting, get_invitation_settings(meeting.organization))
    folgt = (
        f"Einladung folgt am {liste._zeitpunkt(versand_am)}"
        if versand_am > timezone.now()
        else "Einladung folgt in Kürze"
    )
    stunden = meeting_reminder_hours(meeting.organization)
    erinnerung = meeting.start - timedelta(hours=stunden) if stunden is not None else None
    return {
        "einladung_folgt": folgt,
        "erinnerung_tag": liste._tag(erinnerung)
        if erinnerung is not None and erinnerung > timezone.now() and not meeting.reminder_sent_at
        else "",
        "eigene_unterlagen": vorbereiten.eigene_unterlagen(kontext),
    }


def _vorschau(empfaenger: dict[str, Any]) -> list[dict[str, Any]]:
    """Vorschau der Einladung: eine vereidigte Person und, falls vorhanden, eine nicht vereidigte."""
    vorschau = []
    vereidigt = next((e for e in empfaenger["alle"] if e["intern"]), None)
    nicht = next((e for e in empfaenger["alle"] if not e["intern"]), None)
    for person in (vereidigt, nicht):
        if person is not None:
            vorschau.append(person)
    return vorschau
