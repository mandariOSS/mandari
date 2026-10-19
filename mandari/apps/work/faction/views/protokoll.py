# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seite einer Fraktionssitzung nach der Sitzung nach dem Muster (Issue #1071, Schritt 3): Protokoll, Abschluss, Absage.

Über :class:`FactionMeetingDetailView`, wenn der Schalter der Organisation an ist und die Sitzung abgeschlossen oder
abgesagt ist. Geschrieben wird über die bisherigen Aktionen (``FactionActionView``), das Panel des TOPs und die Notizen
der Sitzungsansicht (``FactionSessionActionView``). Antworten der Aktionen für das Dokument rendert
:func:`dokument_html`, wenn die Anfrage von dieser Seite kommt (``X-Work-Gestaltung: neu``).
Fachlogik in :mod:`apps.work.faction.protokoll`.
"""

from __future__ import annotations

from typing import Any, cast

from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .. import protokoll, sitzung
from ..models import FactionMeeting
from . import _helpers
from .vorbereiten import _detail_config


def _sitzung_urls(meeting: FactionMeeting) -> dict[str, str]:
    """Aktions-Adresse der Sitzungsansicht für die Notizen je TOP (Komponente ``sitzungsNotizen``)."""
    slug = meeting.organization.slug
    return {
        "aktion": reverse("work:faction_session_action", kwargs={"org_slug": slug, "meeting_id": meeting.pk}),
        "top": "",
        "seite": reverse("work:faction_detail", kwargs={"org_slug": slug, "meeting_id": meeting.pk}),
        "platzhalter": "",
    }


def dokument_kontext(view: Any, meeting: FactionMeeting) -> dict[str, Any]:
    """Kontext des Protokolls als Dokument (auch für die Antworten der Aktionen)."""
    membership = view.membership
    r = protokoll.rechte(membership, meeting)
    d = protokoll.dokument(meeting, membership)
    g = protokoll.genehmigung(meeting)
    editierbar = r.protokollieren and meeting.status == "completed"
    if editierbar:
        # @-Erwähnen auch nach der Sitzung (Issue #874); bei nichtöffentlichen TOPs nur Vereidigte
        erwaehnbar: dict[bool, list[dict[str, str]]] = {}
        for t in d.tops:
            if t.intern not in erwaehnbar:
                personen, _gruppen = sitzung.personen_und_gruppen(meeting.organization, nur_vereidigte=t.intern)
                erwaehnbar[t.intern] = sitzung.erwaehnbare_personen(personen)
            t.konfig = {
                "itemId": str(t.item.pk),
                "stand": t.stand,
                "editierbar": True,
                "platzhalter": "Notiz ergänzen",
                "leiste": False,
                "personen": erwaehnbar[t.intern],
                "ich": str(membership.pk),
            }
    return {
        "meeting": meeting,
        "organization": meeting.organization,
        "org_slug": meeting.organization.slug,
        "membership": membership,
        "rechte": r,
        "dok": d,
        "angaben": protokoll.angaben(meeting),
        "genehmigung": g,
        "notizen_editierbar": editierbar,
        "to_gesperrt": d.gesperrt,
    }


def dokument_html(view: Any, request: HttpRequest, meeting: FactionMeeting) -> str:
    """Protokoll nach einer Aktion (Abstimmung, Eintrag, Panel); ersetzt den Inhalt von ``#agenda-container``."""
    return render_to_string(protokoll.DOKUMENT, dokument_kontext(view, meeting), request=request)


def seiten_kontext(view: Any, meeting: FactionMeeting, request: HttpRequest) -> dict[str, Any]:
    """Kontext der ganzen Seite: Kopf mit Ablaufleiste, Infoleiste, Protokoll, Nebenspalte, Dialoge."""
    kontext: dict[str, Any] = dict(cast(Any, _helpers)._get_meeting_context(view, meeting))
    kontext |= dokument_kontext(view, meeting)
    membership = view.membership
    r: protokoll.Rechte = kontext["rechte"]
    d: protokoll.Dokument = kontext["dok"]
    g: protokoll.Genehmigung = kontext["genehmigung"]
    a: protokoll.Angaben = kontext["angaben"]
    v = protokoll.versand(meeting)
    tag = timezone.localtime(meeting.start).date()
    kontext.update(
        {
            "detail_config": _detail_config(meeting),
            "sitzung_urls": _sitzung_urls(meeting),
            "versand": v,
            "stufen": protokoll.stufen(meeting, len(d.tops) + d.gesperrt, g),
            "info": protokoll.info(meeting, r, g, v, alt=d.alt),
            "schritte": protokoll.schritte(meeting, a, g, v, genehmigen=r.genehmigen)
            if meeting.status == "completed"
            else [],
            "aufgaben_sitzung": protokoll.aufgaben(d),
            "eigene_aufgaben": protokoll.eigene_aufgaben(meeting, membership, d),
            "teilnahme": protokoll.eigene_teilnahme(meeting, membership),
            "nachweis_tag": tag,
            "can_export_attendance": _darf_sammel_export(membership),
            "darf_einstellungen": membership.has_permission("faction.manage"),
            "darf_intern": kontext["can_view_internal"],
        }
    )
    return kontext


def _darf_sammel_export(membership: Any) -> bool:
    from ..invitations import can_confirm_attendance

    return can_confirm_attendance(membership)


def ist_nach_der_sitzung(meeting: FactionMeeting) -> bool:
    return meeting.status in protokoll.NACH_DER_SITZUNG
