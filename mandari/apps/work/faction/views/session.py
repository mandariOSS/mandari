# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsansicht der laufenden Fraktionssitzung (Issue #874).

Seite (über :class:`FactionMeetingDetailView`, wenn der Schalter der Organisation an ist), TOP-Wechsel und
Unterlagen als HTMX-Teile sowie ein Aktions-Endpunkt für Notizen, Beschluss, Aufgaben, Unterlagen und
Anwesenheit. Fachlogik und Datenzugriffe in :mod:`apps.work.faction.sitzung`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View

from apps.common.mixins import WorkViewMixin
from apps.common.uploads import DOCUMENTS, MB, is_embeddable, validate_upload
from apps.work.rahmen import neues_design
from apps.work.sanitize import safe_editor_html

from .. import sitzung
from ..models import FactionAgendaItem, FactionDecision, FactionMeeting
from ..quorum import faction_quorum_status
from ..visibility import can_view_internal, is_item_internal

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization

logger = logging.getLogger(__name__)

SEITE = "work/faction/sitzung/seite.html"
TOP = "work/faction/sitzung/_top.html"
UNTERLAGE = "work/faction/sitzung/_unterlage.html"
UNTERLAGEN = "work/faction/sitzung/_unterlagen.html"
UNTEN = "work/faction/sitzung/_unten.html"
AUFGABEN = "work/faction/sitzung/_aufgaben.html"
ANWESENHEIT = "work/faction/sitzung/_anwesenheit.html"

#: Feste Meldungen (nie Ausnahmetexte an den Browser)
KEINE_BERECHTIGUNG = "Keine Berechtigung für diese Aktion."
UNGUELTIG = "Ungültige Eingabe."

ERGEBNISSE = dict(FactionDecision.RESULT_CHOICES)
PUNKT_ERGEBNIS = {"accepted": "zustimmung", "modified": "zustimmung", "rejected": "ablehnung", "postponed": "andere"}


def _wert(request: HttpRequest, name: str) -> str:
    """Formularwert ohne umgebende Leerzeichen (fehlend: leer)."""
    return str(request.POST.get(name) or "").strip()


# =============================================================================
# Kontext
# =============================================================================


def _urls(meeting: FactionMeeting) -> dict[str, str]:
    org_slug = meeting.organization.slug
    platzhalter = "00000000-0000-0000-0000-000000000000"
    return {
        "aktion": reverse("work:faction_session_action", kwargs={"org_slug": org_slug, "meeting_id": meeting.pk}),
        "top": reverse(
            "work:faction_session_item", kwargs={"org_slug": org_slug, "meeting_id": meeting.pk, "item_id": platzhalter}
        ),
        "seite": reverse("work:faction_detail", kwargs={"org_slug": org_slug, "meeting_id": meeting.pk}),
        "platzhalter": platzhalter,
    }


def _stufen(
    meeting: FactionMeeting, ordnung: sitzung.Tagesordnung, quorum: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Ablaufleiste Planung → Einladung → Sitzung → Protokoll; die laufende Sitzung ist Stufe 3."""
    tops = len(ordnung.zeilen) + ordnung.gesperrt
    if meeting.invitation_sent_at:
        einladung = f"am {timezone.localtime(meeting.invitation_sent_at):%d.%m.} versendet"
    else:
        einladung = "nicht versendet"
    lage = "läuft"
    if quorum and quorum["has_list"]:
        lage = "beschlussfähig" if quorum["met"] else "nicht beschlussfähig"
    return [
        {"nr": 1, "name": "Planung", "unter": f"{tops} TOP" if tops == 1 else f"{tops} TOPs", "stand": "erledigt"},
        {
            "nr": 2,
            "name": "Einladung",
            "unter": einladung,
            "stand": "erledigt" if meeting.invitation_sent_at else "offen",
        },
        {"nr": 3, "name": "Sitzung", "unter": lage, "stand": "aktuell"},
        {"nr": 4, "name": "Protokoll", "unter": "nach der Sitzung", "stand": "offen"},
    ]


def _rechte(membership: Any, meeting: FactionMeeting) -> dict[str, bool]:
    return {
        "protokollieren": sitzung.darf_protokollieren(membership, meeting),
        "anwesenheit": sitzung.darf_anwesenheit_pflegen(membership, meeting),
        "rollen": sitzung.darf_rollen_setzen(membership, meeting),
        "anhaengen": sitzung.darf_unterlagen_anhaengen(membership, meeting),
        "beenden": membership.has_permission("faction.start"),
        "intern": can_view_internal(membership),
    }


def _basis(view: Any, meeting: FactionMeeting) -> dict[str, Any]:
    """Was jeder Teil der Seite braucht: Sitzung, Organisation, Mitglied, Rechte, Beschlussfähigkeit, Adressen."""
    return {
        "meeting": meeting,
        "organization": view.organization,
        "org_slug": view.organization.slug,
        "membership": view.membership,
        "rechte": _rechte(view.membership, meeting),
        "quorum": faction_quorum_status(meeting),
        "sitzung_urls": _urls(meeting),
    }


def _kopf_kontext(meeting: FactionMeeting, ordnung: sitzung.Tagesordnung, quorum: dict[str, Any]) -> dict[str, Any]:
    """Kopf und Anwesenheit: Ablaufleiste, Anwesenheit, Auswahl für Sitzungsleitung und Schriftführung."""
    return {
        "ordnung": ordnung,
        "stufen": _stufen(meeting, ordnung, quorum),
        "anw": sitzung.anwesenheit(meeting),
        "rollen_auswahl": sitzung.rollen_auswahl(meeting.organization),
    }


def seiten_kontext(view: Any, meeting: FactionMeeting, top_id: Any = None) -> dict[str, Any]:
    """Kontext der ganzen Seite: Kopf, Tagesordnung, aktueller TOP, Leiste unten, Anwesenheit."""
    ordnung = sitzung.tagesordnung(meeting, view.membership)
    zeile = sitzung.aktueller_top(ordnung, top_id)
    kontext = _basis(view, meeting)
    kontext |= _kopf_kontext(meeting, ordnung, kontext["quorum"])
    if zeile is not None:
        kontext |= top_kontext(view, meeting, zeile.item, ordnung=ordnung)
    return kontext


def top_kontext(
    view: Any, meeting: FactionMeeting, item: FactionAgendaItem, *, ordnung: sitzung.Tagesordnung | None = None
) -> dict[str, Any]:
    """Kontext eines TOPs: Kopf mit Beratungsverlauf, Unterlagen, Protokoll (Notizen, Einträge, Aufgaben), Leiste."""
    membership = view.membership
    if ordnung is None:
        ordnung = sitzung.tagesordnung(meeting, membership)
    vor, nach = ordnung.nachbarn(item.pk)
    unterlagen = sitzung.unterlagen(item, membership)
    intern = is_item_internal(item)
    personen, gruppen = sitzung.personen_und_gruppen(meeting.organization, nur_vereidigte=intern)
    notizen = sitzung.notizen(item)
    decision = item.recorded_decision
    kontext = {
        "item": item,
        "intern": intern,
        "vor": vor,
        "nach": nach,
        "unterlagen": unterlagen,
        "verlauf": sitzung.beratungsverlauf(item, meeting.organization)
        if sitzung.darf_beratungsverlauf_sehen(membership)
        else [],
        "herkunft": sitzung.herkunft(item),
        "beschreibung": cast(Any, item).get_description_decrypted() or "",
        "notizen_html": safe_editor_html(notizen),
        "eintraege": sitzung.protokolleintraege(item),
        "aufgaben": sitzung.aufgaben_des_tops(item, membership),
        "decision": decision,
        "beschluss_punkt": PUNKT_ERGEBNIS.get(decision.result, "andere") if decision else "",
        "ergebnisse": ERGEBNISSE,
        "aktuelle_zeile": ordnung.finde(item.pk),
        "notizen_config": {
            "itemId": str(item.pk),
            "stand": sitzung.fingerabdruck(notizen),
            "editierbar": sitzung.darf_protokollieren(membership, meeting),
            "platzhalter": f"Notizen zu TOP {item.number}",
        },
        "zustaendig_config": {
            "personen": [{"id": str(m.pk), "name": m.user.get_display_name()} for m in personen],
            "gruppen": [
                {"id": g.schluessel, "name": g.name, "art": g.art, "anzahl": len(g.mitglieder)} for g in gruppen
            ],
        },
        "heute_plus_woche": (timezone.localdate() + timedelta(days=7)).isoformat(),
    }
    if unterlagen:
        kontext |= unterlage_kontext(unterlagen[0])
        kontext["aktive_unterlage"] = unterlagen[0].schluessel
    return kontext


def unterlage_kontext(unterlage: sitzung.Unterlage) -> dict[str, Any]:
    """Inhalt eines Reiters: Text der RIS-Datei, Dokumentinhalt, Vorschau der Anlage oder Links."""
    kontext: dict[str, Any] = {"unterlage": unterlage, "text": "", "html": "", "vorschau_url": ""}
    objekt = unterlage.objekt
    if unterlage.art == "ris_datei":
        # Der Text ist beim Laden der Reiter zurückgestellt und kommt erst hier (eine Abfrage für den gezeigten Reiter)
        kontext["text"] = (objekt.text_content or "").strip()
        kontext["vorschau_url"] = reverse("insight_core:insight:file_proxy", args=[objekt.pk])
        # Ohne Text: PDFs (und Dateien ohne Angabe) in der Vorschau, andere Formate als Dokumentzeile zum Herunterladen
        art = (objekt.mime_type or "").lower()
        dateiname = (objekt.file_name or objekt.name or "").lower()
        kontext["ist_pdf"] = not art or "pdf" in art or dateiname.endswith(".pdf")
    elif unterlage.art == "dokument":
        kontext["html"] = safe_editor_html(objekt.get_content_decrypted())
    elif unterlage.art == "anlage":
        name = (objekt.filename or "").lower()
        kontext["ist_pdf"] = name.endswith(".pdf")
        kontext["ist_bild"] = is_embeddable(name)
    return kontext


# =============================================================================
# Seite und Teile
# =============================================================================


def _sitzung_oder_404(view: Any, meeting_id: Any) -> FactionMeeting:
    meeting = sitzung.sitzung_laden(view.organization, meeting_id)
    if meeting is None or not neues_design(meeting.organization):
        raise Http404
    return meeting


def _top_oder_404(view: Any, meeting: FactionMeeting, item_id: Any) -> FactionAgendaItem:
    item = sitzung.top_laden(meeting, item_id)
    if item is None:
        raise Http404
    # Nichtöffentlich strikt (Issue #64): auch über die Teile der neuen Ansicht nichts für Nicht-Vereidigte
    if not sitzung.darf_top_sehen(item, view.membership):
        raise PermissionDenied("Gesperrte Information")
    return item


class _SitzungsView(WorkViewMixin, View):
    """Gemeinsame Basis: Mitgliedschaft und Organisation sind nach der Prüfung des WorkViewMixin gesetzt."""

    permission_required = "faction.view_public"

    @property
    def mitglied(self) -> Membership:
        return cast("Membership", self.membership)

    @property
    def org(self) -> Organization:
        return cast("Organization", self.organization)


class FactionSessionItemView(_SitzungsView):
    """GET: ein TOP der laufenden Sitzung als HTMX-Teil (Mitte, rechts und Leiste unten); sonst zur Seite."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = _sitzung_oder_404(self, kwargs["meeting_id"])
        item = _top_oder_404(self, meeting, kwargs["item_id"])
        if not self.is_htmx:
            url = reverse("work:faction_detail", kwargs={"org_slug": self.org.slug, "meeting_id": meeting.pk})
            return redirect(f"{url}?top={item.pk}")
        kontext = _basis(self, meeting) | top_kontext(self, meeting, item) | {"oob": True}
        return HttpResponse(render_to_string(TOP, kontext, request=request))


class FactionSessionDocumentView(_SitzungsView):
    """GET: Inhalt eines Reiters unter „Unterlagen“ (HTMX-Teil)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = _sitzung_oder_404(self, kwargs["meeting_id"])
        item = _top_oder_404(self, meeting, kwargs["item_id"])
        unterlage = sitzung.unterlage_finden(item, self.mitglied, kwargs["schluessel"])
        if unterlage is None:
            raise Http404
        kontext = {"meeting": meeting, "item": item, "org_slug": self.org.slug} | unterlage_kontext(unterlage)
        return HttpResponse(render_to_string(UNTERLAGE, kontext, request=request))


# =============================================================================
# Aktionen
# =============================================================================


def _mit_meldung(antwort: HttpResponse, meldung: str) -> HttpResponse:
    antwort["HX-Trigger"] = json.dumps({"showToast": {"message": meldung, "type": "success"}})
    return antwort


class FactionSessionActionView(_SitzungsView):
    """POST: Notizen, Beschluss, Aufgaben, Unterlagen, Anwesenheit und Rollen der laufenden Sitzung."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = _sitzung_oder_404(self, kwargs["meeting_id"])
        handlers = {
            "notizen": self._notizen,
            "beschluss": self._beschluss,
            "beschluss_aendern": self._beschluss_aendern,
            "aufgaben": self._aufgaben,
            "aufgabe_erledigt": self._aufgabe_erledigt,
            "datei": self._datei,
            "vorlage_suchen": self._vorlage_suchen,
            "vorlage": self._vorlage,
            "dokument_suchen": self._dokument_suchen,
            "dokument": self._dokument,
            "anwesenheit": self._anwesenheit,
            "gast": self._gast,
            "rollen": self._rollen,
        }
        handler = handlers.get(_wert(request, "action"))
        if handler is None:
            return HttpResponse(UNGUELTIG, status=400)
        return handler(request, meeting)

    # -- Hilfen ----------------------------------------------------------

    def _top(self, request: HttpRequest, meeting: FactionMeeting) -> FactionAgendaItem:
        return _top_oder_404(self, meeting, _wert(request, "item_id"))

    def _render(self, request: HttpRequest, template: str, kontext: dict[str, Any]) -> HttpResponse:
        return HttpResponse(render_to_string(template, kontext, request=request))

    def _top_basis(self, meeting: FactionMeeting, item: FactionAgendaItem) -> dict[str, Any]:
        return _basis(self, meeting) | top_kontext(self, meeting, item)

    # -- Notizen ---------------------------------------------------------

    def _notizen(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not sitzung.darf_protokollieren(self.mitglied, meeting):
            return JsonResponse({"fehler": KEINE_BERECHTIGUNG}, status=403)
        html = str(request.POST.get("html") or "")
        if len(html) > sitzung.NOTIZEN_MAX_ZEICHEN:
            return JsonResponse({"fehler": "Die Notizen sind zu lang."}, status=400)
        basis = request.POST.get("stand")
        ergebnis = sitzung.notizen_speichern(item, html, basis=basis, membership=self.mitglied)
        antwort = {
            "stand": ergebnis.stand,
            "gespeichert_um": timezone.localtime(ergebnis.geaendert_um).strftime("%H:%M")
            if ergebnis.geaendert_um
            else "",
        }
        if not ergebnis.gespeichert:
            # Jemand hat inzwischen gespeichert: aktueller Stand zurück, der Browser behält beide Fassungen
            return JsonResponse({**antwort, "konflikt": True, "html": str(safe_editor_html(ergebnis.html))}, status=409)
        return JsonResponse(antwort)

    # -- Beschluss -------------------------------------------------------

    def _beschluss(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not sitzung.darf_protokollieren(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        try:
            stimmen = [max(0, int(_wert(request, k) or 0)) for k in ("votes_yes", "votes_no", "votes_abstain")]
        except ValueError:
            return HttpResponse("Ungültige Stimmzahlen.", status=400)
        result = _wert(request, "result") or "accepted"
        if result not in ERGEBNISSE:
            return HttpResponse(UNGUELTIG, status=400)
        from .. import services as faction_services

        faction_services.record_decision(
            item,
            self.mitglied,
            votes_yes=stimmen[0],
            votes_no=stimmen[1],
            votes_abstain=stimmen[2],
            result=result,
            decision_text=_wert(request, "decision_text"),
        )
        item.refresh_from_db()
        kontext = self._top_basis(meeting, item) | {"oob_zeile": True}
        return _mit_meldung(self._render(request, UNTEN, kontext), "Beschluss erfasst")

    def _beschluss_aendern(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        """Erfasste Abstimmung zum Ändern öffnen: Sie bleibt gespeichert, bis eine neue erfasst ist."""
        item = self._top(request, meeting)
        if not sitzung.darf_protokollieren(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        return self._render(request, UNTEN, self._top_basis(meeting, item) | {"aendern": True})

    # -- Aufgaben ----------------------------------------------------------

    def _aufgaben(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not sitzung.darf_protokollieren(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        titel = _wert(request, "titel")
        if not titel:
            return HttpResponse("Bitte beschreiben Sie die Aufgabe.", status=400)
        faellig: date | None = None
        if _wert(request, "faellig"):
            try:
                faellig = date.fromisoformat(_wert(request, "faellig"))
            except ValueError:
                return HttpResponse("Ungültiges Datum.", status=400)
        personen, gruppen = request.POST.getlist("personen"), request.POST.getlist("gruppen")
        empfaenger = sitzung.empfaenger_aufloesen(
            meeting.organization, personen, gruppen, nur_vereidigte=is_item_internal(item)
        )
        if not empfaenger and (personen or gruppen):
            return HttpResponse("Die Auswahl enthält niemanden, der die Aufgabe bekommen kann.", status=400)
        angelegt = sitzung.aufgaben_anlegen(item, self.mitglied, titel=titel, faellig=faellig, empfaenger=empfaenger)
        antwort = self._render(request, AUFGABEN, self._top_basis(meeting, item) | {"nach_anlegen": True})
        return _mit_meldung(antwort, "Aufgabe angelegt" if len(angelegt) == 1 else f"{len(angelegt)} Aufgaben angelegt")

    def _aufgabe_erledigt(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        """Aufgabe aus dem TOP abhaken oder wieder öffnen, mit dem Recht des Aufgabenboards."""
        item = self._top(request, meeting)
        task = sitzung.aufgabe_umschalten(item, self.mitglied, _wert(request, "task_id"))
        if task is None:
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        antwort = self._render(request, AUFGABEN, self._top_basis(meeting, item))
        return _mit_meldung(antwort, "Aufgabe erledigt" if task.is_completed else "Aufgabe wieder offen")

    # -- Unterlagen ----------------------------------------------------------

    def _unterlagen_antwort(
        self, request: HttpRequest, meeting: FactionMeeting, item: FactionAgendaItem, meldung: str, schluessel: str
    ) -> HttpResponse:
        kontext = self._top_basis(meeting, item)
        neu = next((u for u in kontext["unterlagen"] if u.schluessel == schluessel), None)
        if neu is not None:
            kontext |= unterlage_kontext(neu) | {"aktive_unterlage": neu.schluessel}
        # Erste Unterlage eines TOPs: die Spalte „Unterlagen“ entsteht erst, deshalb der ganze TOP
        vorlage = TOP if _wert(request, "ziel") == "fokus" else UNTERLAGEN
        return _mit_meldung(self._render(request, vorlage, kontext), meldung)

    def _darf_anhaengen(self, meeting: FactionMeeting) -> bool:
        return sitzung.darf_unterlagen_anhaengen(self.mitglied, meeting)

    def _datei(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not self._darf_anhaengen(meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        datei = request.FILES.get("file")
        try:
            validate_upload(datei, allowed=DOCUMENTS, max_bytes=20 * MB, bezeichnung="Anlage")
        except ValidationError as exc:
            return HttpResponse(exc.messages[0], status=400)
        anlage = sitzung.anlage_speichern(item, datei, self.mitglied)
        return self._unterlagen_antwort(request, meeting, item, "Datei angehängt", f"a-{anlage.pk}")

    def _vorlage_suchen(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not self._darf_anhaengen(meeting):
            return JsonResponse({"treffer": []}, status=403)
        return JsonResponse({"treffer": sitzung.vorlagen_suchen(item, self.org, _wert(request, "q"))})

    def _vorlage(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not self._darf_anhaengen(meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        paper = sitzung.vorlage_verknuepfen(item, self.org, _wert(request, "paper_id"))
        if paper is None:
            return HttpResponse("Vorlage nicht gefunden.", status=404)
        reiter = sitzung.unterlagen(item, self.mitglied)
        schluessel = next((u.schluessel for u in reiter if getattr(u.vorlage or u.objekt, "pk", None) == paper.pk), "")
        return self._unterlagen_antwort(request, meeting, item, "RIS-Vorlage verknüpft", schluessel)

    def _dokument_suchen(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not self._darf_anhaengen(meeting):
            return JsonResponse({"treffer": []}, status=403)
        return JsonResponse({"treffer": sitzung.dokumente_suchen(item, self.mitglied, _wert(request, "q"))})

    def _dokument(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        item = self._top(request, meeting)
        if not self._darf_anhaengen(meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        motion = sitzung.dokument_verknuepfen(item, self.mitglied, _wert(request, "motion_id"))
        if motion is None:
            return HttpResponse("Dokument nicht gefunden.", status=404)
        return self._unterlagen_antwort(request, meeting, item, "Dokument verknüpft", f"d-{motion.pk}")

    # -- Anwesenheit und Rollen ------------------------------------------------

    def _anwesenheit_antwort(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        meeting.refresh_from_db()
        kontext = _basis(self, meeting)
        ordnung = sitzung.tagesordnung(meeting, self.mitglied)
        kontext |= _kopf_kontext(meeting, ordnung, kontext["quorum"]) | {"oob_kopf": True}
        return self._render(request, ANWESENHEIT, kontext)

    def _anwesenheit(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        if not sitzung.darf_anwesenheit_pflegen(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        gesetzt = sitzung.anwesenheit_setzen(
            meeting,
            _wert(request, "attendance_id"),
            status=_wert(request, "status"),
            art=_wert(request, "participation_type"),
        )
        if not gesetzt:
            return HttpResponse(UNGUELTIG, status=400)
        return self._anwesenheit_antwort(request, meeting)

    def _gast(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        if not sitzung.darf_anwesenheit_pflegen(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        name = _wert(request, "guest_name")
        if not name:
            return HttpResponse("Bitte einen Namen angeben.", status=400)
        sitzung.gast_hinzufuegen(meeting, name)
        return self._anwesenheit_antwort(request, meeting)

    def _rollen(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        if not sitzung.darf_rollen_setzen(self.mitglied, meeting):
            return HttpResponse(KEINE_BERECHTIGUNG, status=403)
        werte = {k: _wert(request, k) for k in ("leitung", "schriftfuehrung") if k in request.POST}
        if not sitzung.rollen_setzen(meeting, werte):
            return HttpResponse(UNGUELTIG, status=400)
        return self._anwesenheit_antwort(request, meeting)
