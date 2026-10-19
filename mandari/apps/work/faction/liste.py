# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Liste der Fraktionssitzungen nach dem Muster (Issue #1071, Schritt 1): Stand je Zeile, Satz im Kopfband und die
Nebenspalte mit Sitzungsreihe, Automatik und Pausen.

Alles wird aus vorhandenen Daten abgeleitet (Sitzungsreihen, Ausnahmen, RIS-Ausfallregeln, Einladungs- und
Protokollversand-Einstellungen); nichts davon schreibt. Gelöschte Termine speichert Work nicht einzeln (die Reihe merkt
sich nur ``generated_until``), deshalb nennt die Nebenspalte nur die Regel und die Liste keine gelöschten Termine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings as django_settings
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.formats import date_format

from apps.common import formatting
from apps.common.formatting import count_label

from .generation import DEFAULT_HORIZON_DAYS
from .invitations import (
    RELEASE_NOTICE_FINAL_HOURS,
    RELEASE_NOTICE_FIRST_HOURS,
    dispatches_automatically,
    get_invitation_settings,
    invitation_dispatch_at,
)
from .protocol_dispatch import get_protocol_dispatch_settings
from .services import FACTION_REMINDER_WINDOW_HOURS

WOCHENTAGE = dict(formatting.WEEKDAY_CHOICES)


def _uhr(wert: time | datetime) -> str:
    return str(wert.strftime("%H:%M"))


def _tag(wann: datetime | date) -> str:
    """„Fr 09.10.“ in der Ortszeit."""
    if isinstance(wann, datetime):
        wann = timezone.localtime(wann)
    return str(date_format(wann, "D d.m."))


def _zeitpunkt(wann: datetime) -> str:
    """„Fr 09.10. um 18:00“."""
    lokal = timezone.localtime(wann)
    return f"{_tag(lokal)} um {_uhr(lokal)}"


def _dauer(stunden: int, nach: str) -> str:
    """„3 Tage vor Beginn“, „48 Stunden vor Beginn“."""
    if stunden and stunden % 24 == 0 and stunden >= 72:
        return f"{stunden // 24} Tage {nach}"
    return f"{count_label(stunden, 'Stunde', 'Stunden')} {nach}"


# ---------------------------------------------------------------------------
# Zeilen der Liste
# ---------------------------------------------------------------------------


@dataclass
class Stand:
    """Stand einer Sitzung in der Liste: Punkt (Farbe wf-punkt), Wort und eine Zeile Erläuterung."""

    punkt: str
    text: str
    detail: str = ""
    entfaellt: bool = False
    #: Erläuterung auch am Handy zeigen (Ausfallgrund, offenes Protokoll, überfällige Einladung)
    hervorheben: bool = False


def stand(meeting: Any, einladung: dict[str, Any]) -> Stand:
    """Stand je Zeile wie im Muster: Entwurf, Eingeladen, Abgeschlossen mit Protokollstand, Abgesagt mit Grund."""
    status = meeting.status
    if status == "cancelled":
        return Stand("andere", "Entfällt", meeting.cancellation_reason, entfaellt=True, hervorheben=True)
    if status == "ongoing":
        return Stand("zustimmung", "Läuft")
    if status == "completed":
        if meeting.protocol_approved:
            return Stand("zustimmung", "Abgeschlossen", "Protokoll genehmigt")
        if meeting.protocol_status == "pending":
            return Stand("enthaltung", "Abgeschlossen", "Protokoll zur Genehmigung", hervorheben=True)
        return Stand("enthaltung", "Abgeschlossen", "Protokoll offen", hervorheben=True)
    if status == "invited" or meeting.invitation_sent:
        detail = f"am {_tag(meeting.invitation_sent_at)}" if meeting.invitation_sent_at else ""
        return Stand("zustimmung", "Eingeladen", detail)
    # Entwurf oder geplant: wann die Einladung rausgeht
    punkt = "offen"
    wort = meeting.get_status_display()
    if meeting.start <= timezone.now():
        return Stand(punkt, wort)
    versand = invitation_dispatch_at(meeting, einladung)
    if versand <= timezone.now():
        # Versandzeit vorbei, aber nichts verschickt (Freigabe fehlt oder Versand fehlgeschlagen)
        return Stand("enthaltung", wort, "Einladung überfällig", hervorheben=True)
    if dispatches_automatically(meeting, einladung):
        return Stand(punkt, wort, f"Einladung automatisch am {_zeitpunkt(versand)}")
    return Stand(punkt, wort, f"Einladung nach Freigabe, geplant am {_zeitpunkt(versand)}")


def mit_zahlen(meetings: Any) -> Any:
    """TOPs je Sitzung, getrennt nach öffentlich und nichtöffentlich (für die breite Spalte ab 1600 px)."""
    return meetings.annotate(
        tops_oeffentlich=Count("agenda_items", filter=Q(agenda_items__visibility="public"), distinct=True),
        tops_intern=Count("agenda_items", filter=~Q(agenda_items__visibility="public"), distinct=True),
    )


def zeilen(meetings: Any, organization: Any, *, intern: bool) -> list[dict[str, Any]]:
    """Zeilen für die Vorlage: Sitzung, Stand und die TOP-Zahl, die diese Person sehen darf."""
    einladung = get_invitation_settings(organization)
    ergebnis = []
    for meeting in meetings:
        oeffentlich = getattr(meeting, "tops_oeffentlich", 0)
        nicht = getattr(meeting, "tops_intern", 0) if intern else 0
        tops = ""
        if oeffentlich + nicht:
            tops = count_label(oeffentlich + nicht, "TOP", "TOPs")
            if nicht:
                tops += f", {nicht} nichtöffentlich"
        ergebnis.append({"meeting": meeting, "stand": stand(meeting, einladung), "tops": tops})
    return ergebnis


def naechste_satz(organization: Any, stats_satz: str) -> str:
    """
    Satz im Kopfband: „Die nächste Fraktionssitzung ist am Mo 12.10. um 18:00. Work hat am Fr 09.10. eingeladen.“
    Offene Protokolle hängen an. Ohne kommende Sitzung bleibt der Zahlensatz.
    """
    from .models import FactionMeeting

    naechste = (
        FactionMeeting.objects.filter(organization=organization, start__gte=timezone.now())
        .exclude(status="cancelled")
        .select_related("schedule")
        .order_by("start")
        .first()
    )
    if naechste is None:
        return stats_satz
    satz = f"Die nächste Fraktionssitzung ist am {_zeitpunkt(naechste.start)}."
    einladung = get_invitation_settings(organization)
    if naechste.invitation_sent:
        if naechste.invitation_sent_at:
            # Automatisch: Versand ohne Klick vorgesehen und nicht vor dem geplanten Zeitpunkt (sonst „Jetzt einladen“)
            versand = invitation_dispatch_at(naechste, einladung)
            von_selbst = dispatches_automatically(naechste, einladung) and naechste.invitation_sent_at >= versand
            automatisch = " automatisch" if von_selbst else ""
            satz += f" Work hat am {_zeitpunkt(naechste.invitation_sent_at)}{automatisch} eingeladen."
    elif naechste.status in ("draft", "planned"):
        versand = invitation_dispatch_at(naechste, einladung)
        if versand > timezone.now():
            if dispatches_automatically(naechste, einladung):
                satz += f" Work lädt am {_zeitpunkt(versand)} automatisch ein."
            else:
                satz += f" Die Einladung ist für {_zeitpunkt(versand)} geplant und braucht eine Freigabe."
    offen = FactionMeeting.objects.filter(
        organization=organization, status="completed", protocol_approved=False
    ).count()
    if offen:
        satz += f" Offene Protokolle: {formatting.format_count(offen)}."
    return satz


# ---------------------------------------------------------------------------
# Nebenspalte
# ---------------------------------------------------------------------------


@dataclass
class Reihe:
    name: str
    satz: str
    aktiv: bool
    hinweise: list[str] = field(default_factory=list)


def _rhythmus(schedule: Any) -> str:
    tag = WOCHENTAGE.get(schedule.weekday, "")
    texte: dict[str, str] = {
        "weekly": f"Wöchentlich {tag.lower()}s",
        "biweekly": f"Alle zwei Wochen {tag.lower()}s",
        "monthly": f"Am ersten {tag} im Monat",
        "monthly_first": f"Am ersten {tag} im Monat",
        "monthly_last": f"Am letzten {tag} im Monat",
    }
    return texte.get(schedule.recurrence) or str(schedule.get_recurrence_display())


def _von_bis(schedule: Any) -> str:
    beginn = datetime.combine(date.today(), schedule.time)
    ende = beginn + timedelta(minutes=schedule.duration_minutes or 0)
    if ende == beginn:
        return f"um {_uhr(beginn)}"
    return f"von {_uhr(beginn)} bis {_uhr(ende)}"


def _tag_kurz(tag: date) -> str:
    return str(date_format(tag, "d.m."))


def _ausnahme(ausnahme: Any) -> str:
    grund = f" ({ausnahme.reason})" if ausnahme.reason else ""
    if ausnahme.exception_type == "cancelled":
        if ausnahme.end_date and ausnahme.end_date != ausnahme.original_date:
            wofuer = f" {ausnahme.reason}," if ausnahme.reason else ""
            return f"Pause:{wofuer} {_tag_kurz(ausnahme.original_date)} bis {_tag_kurz(ausnahme.end_date)}"
        return f"Entfällt am {_tag_kurz(ausnahme.original_date)}{grund}"
    if ausnahme.exception_type == "rescheduled" and ausnahme.new_date:
        return f"Verschoben vom {_tag_kurz(ausnahme.original_date)} auf den {_tag_kurz(ausnahme.new_date)}{grund}"
    return f"{ausnahme.get_exception_type_display()} am {_tag_kurz(ausnahme.original_date)}{grund}"


def reihen(organization: Any) -> list[Reihe]:
    """Sitzungsreihen als Satz wie im Muster, mit kommenden Pausen und Ausfallregeln."""
    from apps.work.organization import selectors

    from .models import FactionStandardAgendaItem

    vorlauf = max(1, int(getattr(django_settings, "FACTION_SCHEDULE_HORIZON_DAYS", DEFAULT_HORIZON_DAYS)))
    mit_standard = FactionStandardAgendaItem.objects.filter(organization=organization).exists()
    heute = timezone.localdate()
    ergebnis = []
    for schedule in selectors.faction_schedules(organization):
        satz = f"{_rhythmus(schedule)} {_von_bis(schedule)}"
        if schedule.default_location:
            satz += f", {schedule.default_location}"
        if schedule.default_video_link:
            satz += ", auch per Video"
        satz += "."
        if schedule.is_active:
            satz += f" Work legt die Termine {vorlauf} Tage im Voraus an"
            satz += ", jeweils mit der Standard-Tagesordnung." if mit_standard else "."
        else:
            satz += " Pausiert: Work legt keine neuen Termine an."
        hinweise = [
            _ausnahme(a)
            for a in sorted(schedule.exceptions.all(), key=lambda a: a.original_date)
            if (a.end_date or a.new_date or a.original_date) >= heute
        ]
        regeln = [
            r.ris_organization.name for r in schedule.suspension_rules.all() if r.is_active and r.ris_organization.name
        ]
        if regeln:
            hinweise.append(f"Entfällt nach Sitzungen von {formatting.join_parts(regeln)}")
        hinweise = [h if h.endswith(".") else f"{h}." for h in hinweise]
        ergebnis.append(Reihe(schedule.name, satz, schedule.is_active, hinweise))
    return ergebnis


def _wochenzeit_minus(wochentag: int, uhrzeit: Any, stunden: int) -> str:
    """Wochentag und Uhrzeit, ``stunden`` vor einem festen Wochenzeitpunkt („Mittwoch 12:00“)."""
    minuten = (wochentag * 24 * 60 + uhrzeit.hour * 60 + uhrzeit.minute - stunden * 60) % (7 * 24 * 60)
    tag, rest = divmod(minuten, 24 * 60)
    return f"{WOCHENTAGE[tag]} {rest // 60:02d}:{rest % 60:02d}"


def automatik(organization: Any) -> list[tuple[str, str]]:
    """Was Work von selbst tut, als Paare (wann, was) in der Reihenfolge des Ablaufs."""
    einladung = get_invitation_settings(organization)
    protokoll = get_protocol_dispatch_settings(organization)
    feste = [s for s in organization.meeting_schedules.filter(is_active=True, auto_invite=True) if s.auto_invite_ready]
    eine = feste[0] if len(feste) == 1 else None
    punkte: list[tuple[str, str]] = []

    if einladung["agenda_reminder_enabled"]:
        stunden = einladung["agenda_reminder_hours"]
        if eine is not None and stunden < 7 * 24:
            wann = _wochenzeit_minus(eine.auto_invite_weekday, eine.auto_invite_time, stunden)
        else:
            wann = _dauer(stunden, "vor der Einladung")
        punkte.append((wann, "Erinnerung zum Eintragen von TOPs"))

    for reihe in feste:
        zusatz = "für diese Reihe eingeschaltet" if len(feste) == 1 else f"Reihe „{reihe.name}“"
        punkte.append(
            (
                f"{WOCHENTAGE[reihe.auto_invite_weekday]} {_uhr(reihe.auto_invite_time)}",
                f"Einladung ohne Freigabe, mit der Tagesordnung, die dann gilt (fester Zeitpunkt, {zusatz})",
            )
        )
    if not feste:
        wann = _dauer(einladung["invitation_lead_hours"], "vor Beginn")
        if einladung["invitation_dispatch"] == "automatic":
            punkte.append((wann, "Einladung mit der Tagesordnung, die dann gilt"))
        else:
            punkte.append(
                (
                    wann,
                    f"Einladung nach Freigabe durch den Vorsitz; Work erinnert {RELEASE_NOTICE_FIRST_HOURS} und "
                    f"{RELEASE_NOTICE_FINAL_HOURS} Stunden vorher an die Freigabe",
                )
            )

    punkte.append((_dauer(FACTION_REMINDER_WINDOW_HOURS, "vorher"), "Erinnerung an die Sitzung an alle Eingeladenen"))

    verzoegerung = protokoll["protocol_dispatch_delay_hours"]
    if protokoll["protocol_dispatch"] == "after_approval":
        wann = "Nach der Genehmigung" if not verzoegerung else _dauer(verzoegerung, "nach der Genehmigung")
        punkte.append((wann, "Protokoll per E-Mail"))
    elif protokoll["protocol_dispatch"] == "after_completion":
        wann = "Nach der Sitzung" if not verzoegerung else _dauer(verzoegerung, "nach der Sitzung")
        punkte.append((wann, "Protokoll per E-Mail"))
    return punkte
