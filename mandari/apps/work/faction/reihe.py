# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestehende Sitzungsreihe bearbeiten (Issue #1071, Muster einstellungen.html „Sitzungsreihe bearbeiten“).

Geändert werden Name, Wiederholung, Wochentag, Beginn, Dauer, Ort und Videolink. Die Änderung wirkt nur auf künftige,
noch nicht eingeladene Termine der Reihe; eingeladene, laufende, abgeschlossene und vergangene Sitzungen bleiben, wie
sie sind. Vorher zeigt die Seite, wie viele Termine sich ändern (:func:`plan` und :meth:`Plan.satz`, dieselbe
Rechnung wie beim Übernehmen in :func:`apply`).

Regeln je betroffenem Termin:

- Liegt sein Datum auch im neuen Rhythmus, bleibt er an diesem Tag. Beginn, Dauer, Ort, Videolink und Titel übernimmt
  er nur, wenn sie noch dem bisherigen Stand der Reihe entsprechen; was jemand am Termin selbst geändert hat, bleibt.
- Bearbeitete Termine (alles, was nicht genau dem automatisch angelegten Stand entspricht, :func:`_unveraendert`)
  wandern auf den nächstgelegenen freien Tag des neuen Rhythmus. Gibt es keinen, bleiben sie unverändert.
- Unveränderte Termine wandern der Reihe nach auf die freien Tage. Was ohne Tag bleibt, sagt Work ab („Entfällt:
  Rhythmus der Sitzungsreihe geändert“) statt zu löschen; liegt sein Tag nach einer weiteren Änderung wieder im
  Rhythmus, setzt Work ihn wieder an. Gelöscht werden nur automatisch als „Entfällt“ angelegte Einträge, die nie
  jemand berührt hat und die keine Daten tragen.
- Fehlende Tage legt die Reihe danach wie gewohnt an (``generation.generate_meetings_now``); gelöschte Termine
  (Ausnahme „Gelöscht“) kommen auch dann nicht wieder. Danach ordnet Work die Nummern der nicht eingeladenen
  künftigen Sitzungen nach Datum (``generation.renumber_upcoming``).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

from apps.common.formatting import count_label

from . import generation

if TYPE_CHECKING:
    from .models import FactionMeeting, FactionMeetingSchedule

#: Felder der Reihe, die sich bearbeiten lassen
FELDER = ("name", "recurrence", "weekday", "time", "duration_minutes", "default_location", "default_video_link")

#: Was ein leerer, automatisch angelegter Termin beim Löschen höchstens mitnimmt (deletion.DELETED_LABELS)
_LEER_ERLAUBT = {"Tagesordnungspunkte", "Anwesenheiten und Zusagen"}

#: Ausfallgrund für Termine, die nach einem Rhythmuswechsel keinen Tag mehr haben
RHYTHMUS_GRUND = "Entfällt: Rhythmus der Sitzungsreihe geändert"

#: Feste Meldungen
GESPERRT = "Die Reihe legt gerade Termine an. Bitte versuchen Sie es in einer Minute noch einmal."


class ReiheError(ValueError):
    """Ungültige Eingabe; die Meldung ist ein Satz für die Oberfläche."""


@dataclass(frozen=True)
class Werte:
    """Neue Werte der Reihe (geprüft)."""

    name: str
    recurrence: str
    weekday: int
    time: time
    duration_minutes: int
    default_location: str
    default_video_link: str


def werte_aus(form: Any) -> Werte:
    """Eingaben prüfen: Name, Rhythmus, Wochentag und Beginn Pflicht, Videolink nur als http(s)-Adresse."""
    from .models import FactionMeetingSchedule
    from .services import safe_link_url

    name = (form.get("name") or "").strip()
    if not name:
        raise ReiheError("Bitte einen Namen für die Sitzungsreihe angeben.")
    recurrence = form.get("recurrence") or ""
    if recurrence not in dict(FactionMeetingSchedule.RECURRENCE_CHOICES):
        raise ReiheError("Bitte eine Wiederholung wählen.")
    try:
        weekday = int(form.get("weekday", ""))
    except (TypeError, ValueError):
        weekday = -1
    if weekday not in dict(FactionMeetingSchedule.WEEKDAY_CHOICES):
        raise ReiheError("Bitte einen Wochentag wählen.")
    try:
        beginn = time.fromisoformat((form.get("time") or "").strip())
    except ValueError:
        raise ReiheError("Bitte den Beginn als Uhrzeit angeben, z. B. 18:00.") from None
    try:
        dauer = int(form.get("duration_minutes") or 120)
    except (TypeError, ValueError):
        dauer = 0
    if not 15 <= dauer <= 24 * 60:
        raise ReiheError("Bitte eine Dauer von 15 bis 1440 Minuten angeben.")
    video = (form.get("default_video_link") or "").strip()
    if video and not safe_link_url(video):
        raise ReiheError("Bitte den Videolink als vollständige Adresse mit https:// angeben.")
    if len(video) > 200:
        raise ReiheError("Der Videolink ist zu lang (höchstens 200 Zeichen).")
    return Werte(
        name=name[:200],
        recurrence=recurrence,
        weekday=weekday,
        time=beginn.replace(second=0, microsecond=0),
        duration_minutes=dauer,
        default_location=(form.get("default_location") or "").strip()[:500],
        default_video_link=video,
    )


@dataclass
class Schritt:
    """Ein betroffener Termin: neues Datum und die Felder, die sich ändern."""

    meeting: FactionMeeting
    datum: Any
    felder: dict[str, Any] = field(default_factory=dict)

    @property
    def verschoben(self) -> bool:
        return bool(self.datum != self.meeting.scheduled_date)


@dataclass
class Plan:
    """Was eine Änderung der Reihe mit den kommenden Terminen macht."""

    reihe_felder: dict[str, Any]
    rhythmus_geaendert: bool
    angepasst: list[Schritt] = field(default_factory=list)
    verschoben: list[Schritt] = field(default_factory=list)
    #: Geplante Termine ohne Tag im neuen Rhythmus: abgesagt („Entfällt“, ``RHYTHMUS_GRUND``), Inhalte bleiben
    abgesagt: list[Any] = field(default_factory=list)
    #: Wegen eines früheren Rhythmuswechsels abgesagte Termine, deren Tag wieder im Rhythmus liegt
    wieder: list[Schritt] = field(default_factory=list)
    #: Automatisch als „Entfällt“ angelegte, nie berührte Platzhalter ohne Tag im neuen Rhythmus (ohne Daten)
    entfernt: list[Any] = field(default_factory=list)
    #: Termine mit Inhalten ohne freien Tag im neuen Rhythmus: bleiben, wie sie sind
    bleiben: list[Any] = field(default_factory=list)
    #: Tage, die die Reihe danach neu anlegt
    neu: int = 0
    #: Künftige Sitzungen der Reihe, die schon eingeladen sind oder laufen (bleiben unverändert)
    eingeladen: int = 0

    @property
    def zahl(self) -> int:
        """Termine, die sich ändern."""
        return (
            len(self.angepasst)
            + len(self.verschoben)
            + len(self.abgesagt)
            + len(self.wieder)
            + len(self.entfernt)
            + self.neu
        )

    def satz(self) -> str:
        """„Ändert 6 kommende Termine: 4 angepasst, 2 verschoben. …“ für Vorschau und Meldung."""
        teile = []
        if self.angepasst:
            teile.append(f"{len(self.angepasst)} angepasst")
        if self.verschoben:
            teile.append(f"{len(self.verschoben)} mit Inhalten verschoben")
        if self.wieder:
            teile.append(f"{len(self.wieder)} wieder angesetzt")
        if self.abgesagt:
            teile.append(f"{len(self.abgesagt)} ohne Tag im neuen Rhythmus abgesagt, mit allen Inhalten")
        if self.entfernt:
            teile.append(f"{len(self.entfernt)} leere „Entfällt“-Einträge entfernt")
        if self.neu:
            teile.append(f"{self.neu} neu angelegt")
        if teile:
            satz = f"Ändert {count_label(self.zahl, 'kommenden Termin', 'kommende Termine')}: {', '.join(teile)}."
        elif self.reihe_felder:
            satz = "Die kommenden Termine bleiben, wie sie sind; die Änderung gilt für neu angelegte Termine."
        else:
            satz = "Keine Änderung."
        if self.bleiben:
            satz += (
                f" {count_label(len(self.bleiben), 'Termin', 'Termine')} mit Inhalten "
                f"{'bleibt' if len(self.bleiben) == 1 else 'bleiben'} am bisherigen Tag, weil es im neuen Rhythmus "
                "keinen freien Tag gibt."
            )
        if self.eingeladen:
            satz += (
                f" {count_label(self.eingeladen, 'eingeladene Sitzung bleibt', 'eingeladene Sitzungen bleiben')} "
                "unverändert."
            )
        return satz


#: Felder der Sitzung, die nach dem Anlegen durch die Reihe leer sind; ist eines gesetzt, hat jemand den Termin bearbeitet
_LEER_FELDER = (
    "chaired_by_id",
    "chaired_by_name_snapshot",
    "minute_taker_id",
    "minute_taker_name_snapshot",
    "related_meeting_id",
    "started_at",
    "invitation_released_at",
    "release_notice_first_sent_at",
    "release_notice_final_sent_at",
    "attendance_confirmed_at",
    "agenda_reminder_sent_at",
    "rsvp_reminder_sent_at",
    "reminder_sent_at",
    "protocol_sent_at",
    "protocol_approved",
    "protocol_approved_at",
)
#: Felder eines TOPs, die nach dem Anlegen leer sind
_TOP_LEER_FELDER = (
    "parent_id",
    "has_decision",
    "notes_updated_at",
    "notes_encrypted",
    "decision_encrypted",
    "proposed_by_id",
    "reviewed_by_id",
    "related_agenda_item_id",
    "reference_links",
    "votes_for",
    "votes_against",
    "votes_abstain",
)
#: Audit-Einträge, die beim automatischen Anlegen entstehen (Sitzung, TOPs, Teilnahmen)
_AUDIT_ANLAGE = {"generated", "auto_cancelled", "create"}
_AUDIT_ANLAGE_MODELLE = {"FactionMeeting", "FactionAgendaItem", "FactionAttendance"}


def _unveraendert(meeting: FactionMeeting, alt: FactionMeetingSchedule) -> bool:
    """
    Steht der Termin noch genau so da, wie die Reihe ihn angelegt hat? Nur dann zählt er als leer. Geprüft werden
    Titel, Beschreibung, Zeit, Ort, Videolink, Vorsitz, Protokollführung und alle Abläufe der Sitzung, jeder TOP gegen
    seine Vorlage (Standard-TOP bzw. Genehmigungs-TOP aus den Einstellungen), Teilnahmen, Anhänge, Protokoll,
    verknüpfte Dokumente, die Löschkaskade (etwa Aufgaben) und die Änderungshistorie. Im Zweifel nicht leer.
    """
    from apps.work.faction import deletion

    from .models import FactionAgendaItemAttachment, FactionAuditLog

    if meeting.description or meeting.title != alt.name or meeting.protocol_status != "draft":
        return False
    if meeting.location != alt.default_location or meeting.video_link != alt.default_video_link:
        return False
    if meeting.is_virtual != bool(alt.default_video_link) or meeting.rsvp_enabled != alt.rsvp_enabled:
        return False
    if not _folgt(meeting, alt) or meeting.end is None:
        return False
    if meeting.end - meeting.start != timedelta(minutes=alt.duration_minutes or 120):
        return False
    if any(getattr(meeting, name, None) for name in _LEER_FELDER):
        return False
    for item in meeting.agenda_items.select_related("standard_item"):
        if any(getattr(item, name, None) for name in _TOP_LEER_FELDER) or item.proposal_status != "active":
            return False
        if item.is_approval_item:
            if not cast(Any, generation).approval_item_unchanged(meeting):
                return False
            continue
        standard = item.standard_item
        if standard is None or item.title != standard.title or item.description_encrypted:
            return False
    if FactionAgendaItemAttachment.objects.filter(agenda_item__meeting=meeting).exists():
        return False
    if meeting.agenda_items.filter(related_motions__isnull=False).exists():
        return False
    if meeting.agenda_items.filter(related_papers__isnull=False).exists():
        return False
    if meeting.protocol_entries.exists():
        return False
    if meeting.attendances.exclude(
        status="invited",
        responded_at__isnull=True,
        is_guest=False,
        checked_in_at__isnull=True,
        confirmed_final_at__isnull=True,
        response_message="",
    ).exists():
        return False
    try:
        folgen = deletion.impact_of(meeting)
    except deletion.DeletionBlockedError:
        return False
    if folgen.kept or {zeile.label for zeile in folgen.deleted} - _LEER_ERLAUBT:
        return False
    historie = FactionAuditLog.objects.filter(meeting_id_ref=meeting.pk)
    if historie.exclude(action__in=_AUDIT_ANLAGE).exists():
        return False
    return not historie.exclude(model_name__in=_AUDIT_ANLAGE_MODELLE).exists()


def _folgt(meeting: FactionMeeting, alt: FactionMeetingSchedule) -> bool:
    """Beginnt der Termin noch zur bisherigen Zeit der Reihe (niemand hat ihn einzeln verlegt)?"""
    lokal = timezone.localtime(meeting.start)
    return lokal.date() == meeting.scheduled_date and lokal.time().replace(second=0, microsecond=0) == alt.time


def _felder(
    meeting: FactionMeeting, alt: FactionMeetingSchedule, neu: Any, datum: Any, now: datetime
) -> dict[str, Any]:
    """
    Felder des Termins, die sich mit der Reihe ändern (nur was noch dem bisherigen Stand der Reihe entspricht). Ein
    heutiger Termin, dessen neuer Beginn schon vorbei wäre, behält seinen Beginn.
    """
    felder: dict[str, Any] = {}
    verschoben = datum != meeting.scheduled_date
    if verschoben:
        felder["scheduled_date"] = datum
    folgt = _folgt(meeting, alt)
    if verschoben or (folgt and neu.time != alt.time):
        uhrzeit = neu.time if folgt else timezone.localtime(meeting.start).time()
        start = datetime.combine(datum, uhrzeit)
        if timezone.is_naive(start):
            start = timezone.make_aware(start, timezone.get_default_timezone())
        if start > now or verschoben:
            felder["start"] = start
    start = felder.get("start", meeting.start)
    if meeting.end is not None:
        dauer = meeting.end - meeting.start
        if dauer == timedelta(minutes=alt.duration_minutes or 120):
            dauer = timedelta(minutes=neu.duration_minutes)
        if start + dauer != meeting.end:
            felder["end"] = start + dauer
    if meeting.location == alt.default_location and neu.default_location != alt.default_location:
        felder["location"] = neu.default_location
    if meeting.video_link == alt.default_video_link and neu.default_video_link != alt.default_video_link:
        felder["video_link"] = neu.default_video_link
        if meeting.is_virtual == bool(alt.default_video_link):
            felder["is_virtual"] = bool(neu.default_video_link)
    if meeting.title == alt.name and neu.name != alt.name:
        felder["title"] = neu.name
    return felder


def _naechster_tag(frei: set[Any], datum: Any) -> Any:
    """Nächstgelegener freier Tag; bei Gleichstand der spätere."""

    def abstand(tag: Any) -> tuple[int, int]:
        return abs((tag - datum).days), -tag.toordinal()

    return min(frei, key=abstand)


def _art(meeting: FactionMeeting, alt: FactionMeetingSchedule) -> str | None:
    """
    Wie der Termin bei einer Änderung der Reihe behandelt wird: ``inhalt`` (geplant, bearbeitet), ``leer`` (geplant,
    unverändert), ``rhythmus`` (von einem früheren Rhythmuswechsel abgesagt), ``platzhalter`` (automatisch als
    „Entfällt“ angelegt und nie berührt) oder ``None`` (bleibt außen vor, etwa von Hand abgesagt).
    """
    if meeting.status in ("draft", "planned"):
        return "leer" if _unveraendert(meeting, alt) else "inhalt"
    if meeting.cancellation_reason == RHYTHMUS_GRUND:
        return "rhythmus"
    if (
        meeting.cancellation_reason.startswith("Entfällt")
        and meeting.meeting_number is None
        and not meeting.attendances.exists()
        and _unveraendert(meeting, alt)
    ):
        return "platzhalter"
    return None


def plan(schedule: FactionMeetingSchedule, werte: Werte, now: datetime | None = None) -> Plan:
    """Was die Änderung mit den kommenden Terminen macht (ohne zu schreiben)."""
    from .models import FactionMeeting

    now = now or timezone.now()
    reihe_felder = {name: getattr(werte, name) for name in FELDER if getattr(schedule, name) != getattr(werte, name)}
    rhythmus = "recurrence" in reihe_felder or "weekday" in reihe_felder
    ergebnis = Plan(reihe_felder=reihe_felder, rhythmus_geaendert=rhythmus)

    neu = copy.copy(schedule)
    for name in FELDER:
        setattr(neu, name, getattr(werte, name))

    kuenftig = FactionMeeting.objects.filter(schedule=schedule, start__gt=now)
    kandidaten = list(
        kuenftig.filter(
            invitation_sent=False,
            invitation_claimed_at__isnull=True,
            scheduled_date__isnull=False,
            status__in=["draft", "planned", "cancelled"],
        ).order_by("scheduled_date")
    )
    betroffen: list[FactionMeeting] = []
    art: dict[Any, str] = {}
    for meeting in kandidaten:
        einordnung = _art(meeting, schedule)
        if einordnung is not None:
            betroffen.append(meeting)
            art[meeting.pk] = einordnung
    ergebnis.eingeladen = kuenftig.exclude(pk__in=[m.pk for m in betroffen]).exclude(status="cancelled").count()

    if not reihe_felder:
        return ergebnis

    heute = timezone.localtime(now, timezone.get_default_timezone()).date()
    ende = heute + timedelta(days=generation._horizon_days())
    belegt = set(
        kuenftig.exclude(pk__in=[m.pk for m in betroffen])
        .filter(scheduled_date__isnull=False)
        .values_list("scheduled_date", flat=True)
    )
    geloescht = generation.deleted_occurrence_dates(list(schedule.exceptions.all()))
    # Tage betroffener Termine zählen mit, auch wenn der neue Beginn heute schon vorbei wäre (sie bleiben dann)
    eigene = {m.scheduled_date for m in betroffen}
    erzeugung = cast(Any, generation)
    tage = [
        tag
        for tag in erzeugung.occurrence_dates(neu, heute, ende)
        if tag not in belegt and tag not in geloescht and (tag in eigene or erzeugung._occurrence_start(neu, tag) > now)
    ]
    frei = set(tage)

    schritte: list[Schritt] = []
    offen: list[FactionMeeting] = []
    for meeting in betroffen:
        if meeting.scheduled_date in frei:
            frei.discard(meeting.scheduled_date)
            schritt = Schritt(meeting, meeting.scheduled_date)
            (ergebnis.wieder if art[meeting.pk] == "rhythmus" else schritte).append(schritt)
        else:
            offen.append(meeting)
    # Termine mit Inhalten auf den nächstgelegenen freien Tag (bei Gleichstand den späteren), sonst bleiben sie
    for meeting in [m for m in offen if art[m.pk] == "inhalt"]:
        if not frei:
            ergebnis.bleiben.append(meeting)
            continue
        ziel = _naechster_tag(frei, meeting.scheduled_date)
        frei.discard(ziel)
        schritte.append(Schritt(meeting, ziel))
    # Unveränderte Termine der Reihe nach auf die übrigen Tage (Nummern bleiben in Folge); ohne Tag: absagen, nie löschen
    for meeting in [m for m in offen if art[m.pk] == "leer"]:
        if frei:
            ziel = min(frei)
            frei.discard(ziel)
            schritte.append(Schritt(meeting, ziel))
        else:
            ergebnis.abgesagt.append(meeting)
    # Nie berührte „Entfällt“-Platzhalter ohne Tag verschwinden; von einem Rhythmuswechsel abgesagte bleiben abgesagt
    ergebnis.entfernt = [m for m in offen if art[m.pk] == "platzhalter"]

    for schritt in [*schritte, *ergebnis.wieder]:
        schritt.felder = _felder(schritt.meeting, schedule, neu, schritt.datum, now)
    for schritt in schritte:
        if schritt.verschoben:
            (ergebnis.verschoben if art[schritt.meeting.pk] == "inhalt" else ergebnis.angepasst).append(schritt)
        elif schritt.felder:
            ergebnis.angepasst.append(schritt)
    # Nur nach einem Rhythmuswechsel beginnt die Reihe von vorn; sonst bleibt der Wasserstand (gelöschte Termine)
    if rhythmus and schedule.is_active:
        ergebnis.neu = len(frei)
    return ergebnis


def _speichern(meeting: FactionMeeting, felder: dict[str, Any], extra: dict[str, Any]) -> None:
    """Felder setzen und speichern; ein unbearbeiteter Genehmigungs-TOP bekommt den Titel zum neuen Datum."""
    beschriften = cast(Any, generation).approval_item_unchanged(meeting)
    for name, wert in {**felder, **extra}.items():
        setattr(meeting, name, wert)
    meeting.save(update_fields=sorted({*felder, *extra, "updated_at"}))
    if beschriften and ("start" in felder or "title" in felder):
        meeting.create_approval_agenda_item()


def apply(schedule: FactionMeetingSchedule, werte: Werte, membership: Any = None) -> Plan:
    """
    Änderung übernehmen: Reihe speichern, Termine nach :func:`plan` anpassen, verschieben, absagen oder wieder
    ansetzen, danach fehlende Tage anlegen und die Nummern ordnen. Gelöscht werden nur nie berührte
    „Entfällt“-Platzhalter. Unter der Sperre der Reihe und mit gesperrten Terminen: Was gerade eingeladen wird, ist nach
    der Sperre nicht mehr betroffen.
    """
    from django.core.cache import cache

    from . import audit, deletion
    from .models import FactionMeeting

    key = generation._schedule_lock_key(schedule.pk)
    if not cache.add(key, "1", timeout=generation._SCHEDULE_LOCK_TIMEOUT):
        raise ReiheError(GESPERRT)
    try:
        with transaction.atomic():
            schedule.refresh_from_db()
            # Künftige Termine sperren; plan() liest danach den gesperrten, aktuellen Stand (Einladung, Status)
            list(
                FactionMeeting.objects.select_for_update()
                .filter(schedule=schedule, start__gt=timezone.now())
                .values_list("pk", flat=True)
            )
            ergebnis = plan(schedule, werte)
            if not ergebnis.reihe_felder:
                return ergebnis
            for meeting in ergebnis.entfernt:
                deletion._delete_with_audit(meeting, deletion.impact_of(meeting))
            schritte = ergebnis.angepasst + ergebnis.verschoben
            # Erst alle wandernden Termine vom alten Tag lösen (eindeutig je Reihe und Tag), dann neu setzen
            wandernd = [s.meeting.pk for s in schritte if s.verschoben]
            if wandernd:
                FactionMeeting.objects.filter(pk__in=wandernd).update(scheduled_date=None)
            for name, wert in ergebnis.reihe_felder.items():
                setattr(schedule, name, wert)
            update_fields = list(ergebnis.reihe_felder)
            if ergebnis.rhythmus_geaendert:
                schedule.generated_until = None
                update_fields.append("generated_until")
            schedule.save(update_fields=update_fields)
            ausnahmen = list(schedule.exceptions.all())
            regeln = list(schedule.suspension_rules.filter(is_active=True).select_related("ris_organization"))

            def grund(tag: Any) -> str:
                return (generation.evaluate_cancellation(schedule, tag, exceptions=ausnahmen, rules=regeln) or "")[:300]

            for schritt in schritte:
                extra: dict[str, Any] = {}
                if schritt.verschoben:
                    extra["scheduled_date"] = schritt.datum
                    if schritt.meeting.status in ("draft", "planned") and grund(schritt.datum):
                        extra.update(status="cancelled", cancellation_reason=grund(schritt.datum))
                _speichern(schritt.meeting, schritt.felder, extra)
            for schritt in ergebnis.wieder:
                ausfall = grund(schritt.datum)
                extra = (
                    {"cancellation_reason": ausfall} if ausfall else {"status": "planned", "cancellation_reason": ""}
                )
                _speichern(schritt.meeting, schritt.felder, extra)
            for meeting in ergebnis.abgesagt:
                meeting.status = "cancelled"
                meeting.cancellation_reason = RHYTHMUS_GRUND
                meeting.save(update_fields=["status", "cancellation_reason", "updated_at"])
            audit.log_event(
                "schedule_changed",
                schedule,
                membership=membership,
                changes={
                    "Termine angepasst": {"alt": 0, "neu": len(ergebnis.angepasst)},
                    "Termine mit Inhalten verschoben": {"alt": 0, "neu": len(ergebnis.verschoben)},
                    "Termine wieder angesetzt": {"alt": 0, "neu": len(ergebnis.wieder)},
                    "Termine abgesagt (Rhythmuswechsel)": {"alt": 0, "neu": len(ergebnis.abgesagt)},
                    "Leere Entfällt-Einträge entfernt": {"alt": 0, "neu": len(ergebnis.entfernt)},
                    "Termine neu angelegt": {"alt": 0, "neu": ergebnis.neu},
                },
            )
    finally:
        cache.delete(key)
    organization = schedule.organization

    def danach() -> None:
        generation.generate_meetings_now(schedule)
        cast(Any, generation).renumber_upcoming(organization)

    transaction.on_commit(danach)
    return ergebnis
