# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kalender in Work (Issue #1489, Schritt 1): alle Termine, die es schon gibt, in einer Ansicht.

Der Kalender unter „Sitzungen“ zeigte nur die Sitzungen der Gremien. Jetzt laufen dort zusammen:

* **Sitzungen der Gremien** aus dem Ratsinformationssystem (wie bisher), getrennt nach eigenen und weiteren Gremien
  (Issue #1548, ``eigene_gremien``),
* **interne Sitzungen** (Fraktionssitzungen, Arbeitskreise und weitere Sitzungsarten), soweit das Mitglied sie
  sehen darf (Issue #1239),
* **eigene Termine** der Organisation, soweit das Mitglied sie sehen darf (``apps.work.termine``),
* **Fristen der Dokumente** (sichtbare, nicht abgeschlossene Dokumente mit Frist),
* **Aufgaben** mit Fälligkeit (sichtbare, offene Aufgaben).

Jede Art erscheint nur mit dem Recht, das ihre eigene Seite verlangt, und lässt sich im Kalender aus- und einblenden.
Sichtbarkeit und Schutz nichtöffentlicher Inhalte kommen aus den bestehenden Regeln der Bereiche; der Kalender zeigt
nur Titel, Zeit und den Weg zur Seite. Fristen und Fälligkeiten sind ganztägige Einträge. Sitzungen einer Sitzungsreihe
stehen immer zu ihrer festen Zeit, auch wenn jemand sie erst später beendet hat (``_ende_im_kalender``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from django.db.models import Max, Q
from django.urls import reverse
from django.utils import timezone

from . import selectors
from .serializers import serialize_calendar_event


@dataclass(frozen=True)
class Art:
    """Art eines Termins: Schlüssel (auch in der Adresse und als CSS-Klasse), Beschriftung, nötiges Recht."""

    key: str
    label: str
    recht: str


ARTEN: Final[tuple[Art, ...]] = (
    Art("sitzung", "Sitzungen eigener Gremien", "meetings.view"),
    Art("gremium", "Sitzungen weiterer Gremien", "meetings.view"),
    Art("fraktion", "Interne Sitzungen", "faction.view_public"),
    Art("termin", "Termine", "meetings.view"),
    Art("umfrage", "Terminvorschläge", "meetings.view"),
    Art("dokument", "Fristen der Dokumente", "motions.view"),
    Art("aufgabe", "Aufgaben", "tasks.view"),
)
#: Kennt die Organisation keine eigenen Gremien, stehen alle Sitzungen der Gremien unter einer Art
ALLE_GREMIEN: Final = Art("sitzung", "Sitzungen der Gremien", "meetings.view")
#: Höchstzahl je Art und Abruf: Ein Monatsraster zeigt ohnehin nur wenige Einträge je Tag
MAX_JE_ART: Final = 300
#: Dauer einer Sitzung aus einer Reihe, wenn die Reihe keine nennt (Vorgabe des Modells)
REIHE_DAUER_VORGABE: Final = 120


def eigene_gremien(organization: Any, membership: Any) -> set[Any]:
    """
    Gremien, die im Kalender als „eigene“ gelten (Issue #1548): die zugewiesenen Gremien aller aktiven Mitglieder
    (Einstellungen › Mitglieder), die Gremien, in denen eine mit einem Konto verknüpfte Person derzeit sitzt, die an
    der Organisation verknüpften Gremien und die Gremien, denen das Mitglied selbst folgt (Profil › Meine Gremien).
    Vier Abfragen; leer, wenn nichts davon gepflegt ist.
    """
    from apps.tenants.models import Membership
    from hub.ris import selectors as ris

    if organization is None or membership is None:
        return set()
    aktive = Membership.objects.filter(organization=organization, is_active=True, is_guest=False)
    ids: set[Any] = set(
        aktive.filter(oparl_committees__isnull=False).values_list("oparl_committees", flat=True).distinct()
    )
    ids |= ris.current_organization_ids_of_persons(aktive.exclude(oparl_person=None).values("oparl_person"))
    ids |= set(organization.oparl_organizations.values_list("pk", flat=True))
    ids |= set(membership.followed_organizations.values_list("pk", flat=True))
    return ids


def arten_fuer(membership: Any, *, mit_eigenen_gremien: bool = True) -> list[Art]:
    """
    Arten, die das Mitglied im Kalender sehen darf (Gäste: keine). Ohne eigene Gremien (``eigene_gremien`` leer)
    gibt es keine Trennung: eine Art „Sitzungen der Gremien“ für alle.
    """
    if membership is None or getattr(membership, "is_guest", False):
        return []
    arten = []
    for art in ARTEN:
        if not membership.has_permission(art.recht):
            continue
        if not mit_eigenen_gremien and art.key == "gremium":
            continue
        arten.append(ALLE_GREMIEN if not mit_eigenen_gremien and art.key == "sitzung" else art)
    return arten


def gewaehlt(wert: str | None, erlaubt: list[Art]) -> set[str]:
    """Arten aus dem Parameter ``arten`` (kommagetrennt); ohne Parameter alle erlaubten, nie mehr als erlaubt."""
    schluessel = {art.key for art in erlaubt}
    if wert is None:
        return schluessel
    return schluessel & {teil.strip() for teil in wert.split(",")}


def zeitfenster(parameter: Any) -> tuple[datetime, datetime, str | None] | None:
    """
    Beginn, Ende und gewählte Arten aus den Parametern des Abrufs (``start``, ``end``, ``arten``); ``None``, wenn das
    Zeitfenster fehlt, sich nicht lesen lässt oder keine Zeitzone trägt (FullCalendar sendet sie immer mit).
    """
    try:
        start = datetime.fromisoformat(str(parameter.get("start", "")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(parameter.get("end", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(start) or timezone.is_naive(end):
        return None
    return start, end, parameter.get("arten")


def _eintrag(art: str, kennung: Any, titel: str, url: str, **felder: Any) -> dict[str, Any]:
    return {
        "id": f"{art}-{kennung}",
        "title": titel,
        "url": url,
        "classNames": [f"termin-{art}"],
        "extendedProps": {"art": art, "fullTitle": titel},
        **felder,
    }


def _eigene_bedingung(eigene: set[Any], eigene_adressen: set[str]) -> Q:
    """
    Sitzung eines eigenen Gremiums: zugeordnet oder (ohne Zuordnung) mit dessen Adresse an erster oder zweiter Stelle
    im Rohdatensatz, wie ``OParlMeeting.get_display_name`` die Gremien liest.
    """
    bedingung = Q(organizations__in=list(eigene))
    if eigene_adressen:
        adressen = list(eigene_adressen)
        bedingung |= Q(raw_json__organization__0__in=adressen) | Q(raw_json__organization__1__in=adressen)
    return bedingung


def _sitzungen(
    organization: Any, start: datetime, end: datetime, eigene: set[Any], wahl: set[str]
) -> list[dict[str, Any]]:
    """
    Sitzungen der Gremien: ``sitzung`` für die eigenen Gremien, ``gremium`` für die weiteren. Ohne eigene Gremien
    gehören alle zu ``sitzung``. Der Titel nennt zuerst das Gremium (``OParlMeeting.bezeichnung``). Jede Art hat ihre
    eigene, schon in der Datenbank getrennte Abfrage mit eigenem Kontingent: Sitzungen der einen Art verdrängen die
    der anderen nicht.
    """
    from hub.ris import selectors as ris

    bodies = selectors.organization_bodies(organization)
    if bodies is None:
        return []
    alle = selectors.meetings_between(bodies, start, end).order_by("start", "pk")
    if eigene:
        eigene_sitzungen = alle.filter(_eigene_bedingung(eigene, ris.organization_external_ids(eigene))).distinct()
        abfragen = {"sitzung": eigene_sitzungen, "gremium": alle.exclude(pk__in=eigene_sitzungen.values("pk"))}
    else:
        abfragen = {"sitzung": alle}
    eintraege = []
    for art, abfrage in abfragen.items():
        if art not in wahl:
            continue
        for meeting in abfrage[:MAX_JE_ART]:
            eintrag = serialize_calendar_event(meeting, organization.slug)
            eintrag["classNames"] = [f"termin-{art}"]
            eintrag["extendedProps"]["art"] = art
            eintrag["extendedProps"]["fullTitle"] = eintrag["title"]
            eintraege.append(eintrag)
    return eintraege


def _ende_im_kalender(sitzung: Any) -> datetime | None:
    """
    Ende einer internen Sitzung im Kalender. Eine Sitzung aus einer Sitzungsreihe steht immer zu ihrer festen Zeit
    (Issue #1548): Beginn plus Dauer der Reihe, wenn sie beendet ist („Sitzung beenden“ setzt das Ende auf die Uhrzeit
    des Klicks, nach Mitternacht ragte sie sonst in den Folgetag) oder kein Ende hat. Sonst das gespeicherte Ende.
    """
    beginn: datetime = sitzung.start
    ende: datetime | None = sitzung.end
    reihe = sitzung.schedule if sitzung.schedule_id else None
    if reihe is not None and (sitzung.status == "completed" or ende is None):
        return beginn + timedelta(minutes=reihe.duration_minutes or REIHE_DAUER_VORGABE)
    return ende


def _fraktionssitzungen(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.faction.models import FactionMeeting, FactionMeetingSchedule
    from apps.work.faction.sitzungsarten import sichtbar_fuer

    # Auch Sitzungen, die vor dem Fenster begonnen haben und hineinreichen (über Mitternacht, mehrtägig); ohne Ende
    # zählt der Beginn. Das Fenster ist halboffen: Was es nur berührt (endet genau zu seinem Beginn, beginnt genau
    # an seinem Ende), gehört nicht hinein. Ausgewählt wird nach der Zeit, die der Kalender zeigt (_ende_im_kalender):
    # Dafür kommen Sitzungen einer Reihe ab der längsten Dauer einer Reihe vor dem Fenster in Frage (das Modell begrenzt
    # die Dauer nicht), entschieden wird unten.
    laengste = FactionMeetingSchedule.objects.filter(organization=organization).aggregate(m=Max("duration_minutes"))
    vorlauf = timedelta(minutes=max(laengste["m"] or 0, REIHE_DAUER_VORGABE))
    im_fenster = Q(start__lt=end) & (
        Q(end__gt=start) | Q(end__isnull=True, start__gte=start) | Q(schedule__isnull=False, start__gt=start - vorlauf)
    )
    sitzungen = (
        sichtbar_fuer(membership, FactionMeeting.objects.filter(im_fenster, organization=organization))
        .select_related("schedule")
        .order_by("start", "pk")
    )
    # Seitenweise laden, bis MAX_JE_ART gültige Einträge da sind: Verworfen werden nur Sitzungen aus dem Vorlauf, deren
    # gezeigte Zeit vor dem Fenster endet; eine Begrenzung vor dieser Prüfung könnte spätere gültige verdrängen. Kein
    # .iterator(): Hinter PgBouncer (ohne serverseitige Cursor) lüde er alles auf einmal.
    gueltige: list[tuple[Any, datetime | None]] = []
    versatz = 0
    while len(gueltige) < MAX_JE_ART:
        seite = list(sitzungen[versatz : versatz + MAX_JE_ART])
        if not seite:
            break
        versatz += len(seite)
        for sitzung in seite:
            ende = _ende_im_kalender(sitzung)
            if ende > start if ende is not None else sitzung.start >= start:
                gueltige.append((sitzung, ende))
    eintraege: list[dict[str, Any]] = []
    for sitzung, ende in gueltige[:MAX_JE_ART]:
        eintraege.append(
            _eintrag(
                "fraktion",
                sitzung.pk,
                f"{sitzung.title} (abgesagt)" if sitzung.status == "cancelled" else sitzung.title,
                reverse("work:faction_detail", kwargs={"org_slug": organization.slug, "meeting_id": sitzung.pk}),
                start=sitzung.start.isoformat(),
                end=ende.isoformat() if ende else None,
            )
        )
    return eintraege


def _termine(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.termine import termine as eigene

    # Auch Termine, die vor dem Fenster beginnen und hineinreichen (mehrtägig); ohne Ende zählt der Beginn
    im_fenster = Q(start__lt=end) & (Q(end__gt=start) | Q(end__isnull=True, start__gte=start))
    return [
        _eintrag(
            "termin",
            termin.pk,
            termin.title,
            reverse("work:termin", kwargs={"org_slug": organization.slug, "termin_id": termin.pk}),
            # Ganztägig: nur das Datum, das Ende ist wie in Kalenderprogrammen der Folgetag
            start=timezone.localtime(termin.start).date().isoformat() if termin.all_day else termin.start.isoformat(),
            end=(
                (timezone.localtime(termin.end).date().isoformat() if termin.all_day else termin.end.isoformat())
                if termin.end
                else None
            ),
            allDay=termin.all_day,
        )
        for termin in eigene.sichtbare(membership).filter(im_fenster).order_by("start")[:MAX_JE_ART]
    ]


def _vorschlaege(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Vorschläge offener Terminumfragen, die das Mitglied sieht; ein Klick führt zur Umfrage."""
    from apps.work.termine import umfrage
    from apps.work.termine.models import Terminvorschlag

    offene = umfrage.sichtbare(membership).filter(abgeschlossen_am__isnull=True)
    vorschlaege = (
        Terminvorschlag.objects.filter(umfrage__in=offene, start__gte=start, start__lt=end)
        .select_related("umfrage")
        .order_by("start")[:MAX_JE_ART]
    )
    return [
        _eintrag(
            "umfrage",
            vorschlag.pk,
            f"Vorschlag: {vorschlag.umfrage.title}",
            reverse("work:terminumfrage", kwargs={"org_slug": organization.slug, "umfrage_id": vorschlag.umfrage_id}),
            start=vorschlag.start.isoformat(),
            end=vorschlag.end.isoformat() if vorschlag.end else None,
        )
        for vorschlag in vorschlaege
    ]


def _dokumentfristen(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.motions.models import Motion

    dokumente = (
        Motion.visible_to(membership)  # type: ignore[no-untyped-call]  # Modell noch ohne Annotationen
        .filter(due_date__gte=start.date(), due_date__lte=end.date())
        .exclude(status__in=Motion.CLOSED_STATUSES)
        .order_by("due_date")
        .distinct()[:MAX_JE_ART]
    )
    return [
        _eintrag(
            "dokument",
            dokument.pk,
            f"Frist: {dokument.title}",
            reverse("work:document_editor", kwargs={"org_slug": organization.slug, "motion_id": dokument.pk}),
            start=dokument.due_date.isoformat(),
            allDay=True,
        )
        for dokument in dokumente
    ]


def _aufgaben(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.tasks.selectors import visible_tasks

    aufgaben = (
        visible_tasks(organization, membership)
        .filter(due_date__gte=start.date(), due_date__lte=end.date(), status__in=("todo", "in_progress"))
        .order_by("due_date")[:MAX_JE_ART]
    )
    board = reverse("work:tasks", kwargs={"org_slug": organization.slug})
    return [
        _eintrag(
            "aufgabe",
            aufgabe.pk,
            f"Aufgabe: {aufgabe.title}",
            f"{board}?open={aufgabe.pk}",
            start=aufgabe.due_date.isoformat(),
            allDay=True,
        )
        for aufgabe in aufgaben
        if aufgabe.due_date is not None
    ]


def termine(
    organization: Any, membership: Any, start: datetime, end: datetime, arten: str | None = None
) -> list[dict[str, Any]]:
    """Einträge für FullCalendar im Zeitfenster: nur Arten, die das Mitglied sehen darf und gewählt hat."""
    eigene = eigene_gremien(organization, membership)
    wahl = gewaehlt(arten, arten_fuer(membership, mit_eigenen_gremien=bool(eigene)))
    eintraege: list[dict[str, Any]] = []
    if wahl & {"sitzung", "gremium"}:
        eintraege += _sitzungen(organization, start, end, eigene, wahl)
    if "fraktion" in wahl:
        eintraege += _fraktionssitzungen(organization, membership, start, end)
    if "termin" in wahl:
        eintraege += _termine(organization, membership, start, end)
    if "umfrage" in wahl:
        eintraege += _vorschlaege(organization, membership, start, end)
    if "dokument" in wahl:
        eintraege += _dokumentfristen(organization, membership, start, end)
    if "aufgabe" in wahl:
        eintraege += _aufgaben(organization, membership, start, end)
    return eintraege
