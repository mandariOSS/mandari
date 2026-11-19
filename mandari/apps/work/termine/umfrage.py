# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminumfrage: einen Termin gemeinsam finden (Issue #1489, Schritt 4).

Wer Termine anlegen darf, schlägt mehrere Zeitpunkte vor und fragt alle Mitglieder, die Teilnehmenden einer
Sitzungsart oder ausgewählte Personen. Jede gefragte Person antwortet je Vorschlag mit „Ja“, „Wenn nötig“ oder
„Nein“ und sieht dabei, was an dem Tag schon in ihrem Kalender steht. Wer die Umfrage angelegt hat, legt am Ende
einen Vorschlag fest: Daraus wird ein Termin (``apps.work.termine.termine``), die Gefragten sind eingeladen und ihre
Antwort aus der Umfrage steht schon als Zusage, „vielleicht“ oder Absage am Termin.

* **Sehen und antworten** dürfen die Gefragten und wer die Umfrage angelegt hat; wer alle Termine verwaltet
  (``calendar.manage``), sieht jede Umfrage der Organisation. Gäste sehen keine.
* **Anlegen** verlangt ``calendar.create``. **Festlegen und löschen** darf, wer angelegt hat, oder ``calendar.manage``.

Fachlogik steht hier, die Ansichten (``umfrage_views.py``) lesen nur die Anfrage und rufen auf.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from django import forms
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from . import termine
from .meldungen import melden
from .models import Termin, TerminTeilnahme, Terminumfrage, Terminvorschlag, UmfrageTeilnahme, VorschlagAntwort

#: So viele Zeilen für Vorschläge zeigt das Formular mindestens, so viele nimmt eine Umfrage höchstens auf
ZEILEN: Final = 4
MAX_VORSCHLAEGE: Final = 20
WAHLEN: Final = frozenset({VorschlagAntwort.JA, VorschlagAntwort.NOTFALLS, VorschlagAntwort.NEIN})
#: Antwort in der Umfrage → Antwort am Termin, der daraus entsteht
ANTWORT_AM_TERMIN: Final = {
    VorschlagAntwort.JA: TerminTeilnahme.ZUGESAGT,
    VorschlagAntwort.NOTFALLS: TerminTeilnahme.VIELLEICHT,
    VorschlagAntwort.NEIN: TerminTeilnahme.ABGESAGT,
}
#: Wen die Umfrage fragt (Auswahl im Formular; gespeichert werden die Personen)
ALLE: Final = "alle"
ART: Final = "art"
AUSWAHL: Final = "auswahl"
WEN: Final = [(ALLE, "Alle Mitglieder"), (ART, "Teilnehmende einer Sitzungsart"), (AUSWAHL, "Ausgewählte Mitglieder")]
TAGE: Final = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONATE: Final = ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez.")


def sichtbare(membership: Any) -> QuerySet[Terminumfrage]:
    """Umfragen der Organisation, die das Mitglied sehen darf (leer für Gäste und ohne Mitgliedschaft)."""
    if membership is None or getattr(membership, "is_guest", False) or not membership.is_active:
        return Terminumfrage.objects.none()
    qs = Terminumfrage.objects.filter(organization_id=membership.organization_id)
    if membership.has_permission(termine.VERWALTEN):
        return qs
    gefragt = UmfrageTeilnahme.objects.filter(membership=membership).values("umfrage_id")
    return qs.filter(Q(created_by=membership) | Q(pk__in=gefragt))


def finden(membership: Any, umfrage_id: Any) -> Terminumfrage | None:
    return sichtbare(membership).select_related("created_by__user", "termin").filter(pk=umfrage_id).first()


def darf_aendern(membership: Any, umfrage: Terminumfrage) -> bool:
    """Festlegen und Löschen: wer angelegt hat (und weiter anlegen darf) oder alle Termine verwaltet."""
    if membership is None or umfrage.organization_id != membership.organization_id:
        return False
    if membership.has_permission(termine.VERWALTEN):
        return True
    return umfrage.created_by_id == membership.pk and termine.darf_anlegen(membership)


def ist_gefragt(umfrage: Terminumfrage, membership: Any) -> bool:
    return umfrage.teilnahmen.filter(membership=membership).exists()


def offene(membership: Any) -> list[dict[str, Any]]:
    """Offene Umfragen für den Kalender: Titel, Adresse und ob die eigene Antwort noch fehlt."""
    umfragen = list(sichtbare(membership).filter(abgeschlossen_am__isnull=True).order_by("created_at")[:20])
    if not umfragen:
        return []
    gefragt = set(
        UmfrageTeilnahme.objects.filter(membership=membership, umfrage__in=umfragen).values_list(
            "umfrage_id", flat=True
        )
    )
    beantwortet = set(
        VorschlagAntwort.objects.filter(membership=membership, vorschlag__umfrage__in=umfragen).values_list(
            "vorschlag__umfrage_id", flat=True
        )
    )
    return [
        {
            "titel": umfrage.title,
            "url": reverse(
                "work:terminumfrage", kwargs={"org_slug": membership.organization.slug, "umfrage_id": umfrage.pk}
            ),
            "antwort_bis": umfrage.antwort_bis,
            "fehlt": umfrage.pk in gefragt and umfrage.pk not in beantwortet,
        }
        for umfrage in umfragen
    ]


def ohne_antwort(membership: Any, *, limit: int = 6) -> list[Terminumfrage]:
    """
    Offene Umfragen, in denen das Mitglied gefragt ist und noch auf keinen Vorschlag geantwortet hat, früheste Frist
    zuerst (Startseite „Für Sie“). Eine Abfrage.
    """
    if membership is None or getattr(membership, "is_guest", False):
        return []
    beantwortet = VorschlagAntwort.objects.filter(membership=membership).values("vorschlag__umfrage_id")
    return list(
        Terminumfrage.objects.filter(
            organization_id=membership.organization_id,
            abgeschlossen_am__isnull=True,
            teilnahmen__membership=membership,
        )
        .exclude(pk__in=beantwortet)
        .order_by(F("antwort_bis").asc(nulls_last=True), "created_at")[:limit]
    )


#: Erinnerung, wenn die Frist morgen oder heute endet
ERINNERN_VOR: Final = timedelta(days=1)


def erinnern(heute: date | None = None) -> dict[str, int]:
    """
    Erinnerung vor der Antwortfrist (Zeitplan ``terminumfragen_erinnern``): Endet die Frist einer offenen Umfrage
    morgen oder heute, bekommen die Gefragten ohne Antwort eine Benachrichtigung. Einmal je Umfrage
    (``erinnert_am``); die Zeile ist dabei gesperrt, ein zweiter Lauf zur selben Zeit überspringt sie.
    """
    heute = heute or timezone.localdate()
    faellig = Terminumfrage.objects.filter(
        abgeschlossen_am__isnull=True,
        erinnert_am__isnull=True,
        antwort_bis__gte=heute,
        antwort_bis__lte=heute + ERINNERN_VOR,
    ).values_list("pk", flat=True)
    umfragen = erinnert = 0
    for umfrage_id in list(faellig):
        with transaction.atomic():
            umfrage = (
                Terminumfrage.objects.select_for_update(skip_locked=True)
                .filter(pk=umfrage_id, erinnert_am__isnull=True, abgeschlossen_am__isnull=True)
                .select_related("organization")
                .first()
            )
            if umfrage is None:
                continue
            beantwortet = VorschlagAntwort.objects.filter(vorschlag__umfrage=umfrage).values("membership_id")
            offen = [
                teilnahme.membership
                for teilnahme in umfrage.teilnahmen.exclude(membership_id__in=beantwortet).select_related("membership")
            ]
            frist = "heute" if umfrage.antwort_bis == heute else "morgen"
            text = f"Erinnerung: Die Antwortfrist für „{umfrage.title}“ endet {frist}. Bitte antworten Sie noch."
            _benachrichtigen(umfrage, offen, umfrage.created_by, text=text)
            umfrage.erinnert_am = timezone.now()
            umfrage.save(update_fields=["erinnert_am", "updated_at"])
            umfragen += 1
            erinnert += len([person for person in offen if person.pk != umfrage.created_by_id])
    return {"umfragen": umfragen, "erinnert": erinnert}


def zeit(vorschlag: Terminvorschlag) -> str:
    """Zeitpunkt eines Vorschlags als Satzteil: „Di 24.11.2026, 18:00 bis 19:00 Uhr“."""
    start = timezone.localtime(vorschlag.start)
    tag = f"{TAGE[start.weekday()]} {start.strftime('%d.%m.%Y')}"
    if vorschlag.end is None:
        return f"{tag}, {start.strftime('%H:%M')} Uhr"
    return f"{tag}, {start.strftime('%H:%M')} bis {timezone.localtime(vorschlag.end).strftime('%H:%M')} Uhr"


def _am_selben_tag(
    umfrage: Terminumfrage, membership: Any, vorschlaege: list[Terminvorschlag]
) -> dict[date, list[str]]:
    """Was an den Tagen der Vorschläge schon im Kalender des Mitglieds steht (Sitzungen und Termine), je Tag."""
    from apps.work.meetings import kalender

    if not vorschlaege:
        return {}
    tage = {timezone.localtime(vorschlag.start).date() for vorschlag in vorschlaege}
    von = timezone.make_aware(datetime.combine(min(tage), time.min))
    bis = timezone.make_aware(datetime.combine(max(tage) + timedelta(days=1), time.min))
    je_tag: dict[date, list[str]] = defaultdict(list)
    for eintrag in kalender.termine(umfrage.organization, membership, von, bis, "sitzung,fraktion,termin"):
        beginn = _ortszeit(eintrag.get("start"))
        if beginn is None:
            continue
        titel = str(eintrag["title"])
        ende = _ortszeit(eintrag.get("end"))
        # Letzter Tag, den der Eintrag berührt. Ein Ende um 0 Uhr gehört noch zum Vortag; das gilt auch für
        # ganztägige Einträge, deren Ende der Folgetag ist.
        letzter = (ende - timedelta(microseconds=1)).date() if ende is not None and ende > beginn else beginn.date()
        erster = beginn.date()
        for tag in sorted(tage):
            if tag == erster:
                je_tag[tag].append(titel if eintrag.get("allDay") else f"{beginn.strftime('%H:%M')} {titel}")
            elif erster < tag <= letzter:
                # Läuft über Mitternacht oder mehrere Tage: auch an den Folgetagen nennen
                je_tag[tag].append(f"{titel} (seit {beginn.strftime('%d.%m.')})")
    return je_tag


def _ortszeit(wert: Any) -> datetime | None:
    """Zeitpunkt eines Kalendereintrags (ISO-Text; ganztägig nur der Tag) in der Zeit der Anwendung, ohne Zone."""
    try:
        wann = datetime.fromisoformat(str(wert or ""))
    except ValueError:
        return None
    return timezone.localtime(wann).replace(tzinfo=None) if timezone.is_aware(wann) else wann


def beteiligt(membership: Any, umfrage_id: Any) -> bool:
    """Ist das Mitglied in der Umfrage gefragt oder hat es sie angelegt? Sehen allein (alle Termine verwalten) zählt nicht."""
    eigene = Terminumfrage.objects.filter(pk=umfrage_id, organization_id=membership.organization_id)
    return eigene.filter(Q(created_by=membership) | Q(teilnahmen__membership=membership)).exists()


def uebersicht(umfrage: Terminumfrage, membership: Any) -> dict[str, Any]:
    """
    Alles für die Seite der Umfrage: je Vorschlag die Zeit, wer wie geantwortet hat, die eigene Antwort und was an
    dem Tag schon ansteht; dazu, wer noch nicht geantwortet hat. ``beste`` markiert den Vorschlag mit den meisten
    Zusagen (bei Gleichstand den mit den wenigsten Absagen), sobald jemand geantwortet hat.
    """
    vorschlaege = list(umfrage.vorschlaege.order_by("start"))
    gefragte = list(
        umfrage.teilnahmen.select_related("membership__user").order_by(
            "membership__user__first_name", "membership__user__last_name"
        )
    )
    namen = {teilnahme.membership_id: teilnahme.membership.user.get_display_name() for teilnahme in gefragte}
    antworten: dict[Any, dict[Any, str]] = defaultdict(dict)
    for antwort in VorschlagAntwort.objects.filter(vorschlag__in=vorschlaege):
        antworten[antwort.vorschlag_id][antwort.membership_id] = antwort.wahl
    am_tag = _am_selben_tag(umfrage, membership, vorschlaege)

    zeilen: list[dict[str, Any]] = []
    for vorschlag in vorschlaege:
        je_wahl: dict[str, list[str]] = {
            wahl: [] for wahl in (VorschlagAntwort.JA, VorschlagAntwort.NOTFALLS, VorschlagAntwort.NEIN)
        }
        for mitglied_id, wahl in antworten[vorschlag.pk].items():
            if mitglied_id in namen and wahl in je_wahl:
                je_wahl[wahl].append(namen[mitglied_id])
        zeilen.append(
            {
                "vorschlag": vorschlag,
                "zeit": zeit(vorschlag),
                "ja": sorted(je_wahl[VorschlagAntwort.JA]),
                "notfalls": sorted(je_wahl[VorschlagAntwort.NOTFALLS]),
                "nein": sorted(je_wahl[VorschlagAntwort.NEIN]),
                "meine": antworten[vorschlag.pk].get(getattr(membership, "pk", None), ""),
                "am_tag": am_tag.get(timezone.localtime(vorschlag.start).date(), []),
                "festgelegt": vorschlag.festgelegt,
                "beste": False,
            }
        )
    mit_zusage = [zeile for zeile in zeilen if zeile["ja"] or zeile["notfalls"]]
    if mit_zusage:
        max(mit_zusage, key=lambda zeile: (len(zeile["ja"]), -len(zeile["nein"]), len(zeile["notfalls"])))["beste"] = (
            True
        )
    beantwortet = {mitglied_id for je_vorschlag in antworten.values() for mitglied_id in je_vorschlag}
    ich = getattr(membership, "pk", None)
    for zeile in zeilen:
        start = timezone.localtime(zeile["vorschlag"].start)
        ende = timezone.localtime(zeile["vorschlag"].end) if zeile["vorschlag"].end else None
        # Kopf der Spalte wie ein Kalenderblatt: Monat, Tag, Wochentag, Uhrzeit
        zeile.update(
            monat=MONATE[start.month - 1],
            tag=start.day,
            wochentag=TAGE[start.weekday()],
            uhrzeit=f"{start:%H:%M}–{ende:%H:%M}" if ende else f"{start:%H:%M}",
        )
    # Raster Person mal Vorschlag; die eigene Zeile zeigt die Seite für sich (zum Ankreuzen)
    personen = [
        {
            "name": name,
            "kuerzel": "".join(teil[0] for teil in name.split()[:2]).upper(),
            "wahlen": [antworten[zeile["vorschlag"].pk].get(mitglied_id, "") for zeile in zeilen],
        }
        for mitglied_id, name in namen.items()
        if mitglied_id != ich
    ]
    return {
        "zeilen": zeilen,
        "personen": personen,
        "ich_gefragt": ich in namen,
        "gefragt": len(gefragte),
        "beantwortet": len(beantwortet & set(namen)),
        "ohne_antwort": [name for mitglied_id, name in namen.items() if mitglied_id not in beantwortet],
    }


@transaction.atomic
def antworten(umfrage: Terminumfrage, membership: Any, post: Any) -> int:
    """
    Antworten der gefragten Person speichern (``wahl-<Vorschlag>`` = ja, notfalls oder nein); gibt die Zahl der
    gespeicherten Antworten zurück, 0 bei einer abgeschlossenen Umfrage oder ohne Einladung.
    """
    # Unter derselben Zeilensperre wie das Festlegen: Nach dem Abschluss wird keine Antwort mehr gespeichert, die
    # der Termin nicht mehr übernähme
    umfrage = Terminumfrage.objects.select_for_update().get(pk=umfrage.pk)
    if umfrage.abgeschlossen_am is not None or not ist_gefragt(umfrage, membership):
        return 0
    gespeichert = 0
    for vorschlag in umfrage.vorschlaege.all():
        wahl = str(post.get(f"wahl-{vorschlag.pk}") or "")
        if wahl not in WAHLEN:
            continue
        VorschlagAntwort.objects.update_or_create(vorschlag=vorschlag, membership=membership, defaults={"wahl": wahl})
        gespeichert += 1
    return gespeichert


def _offen_mit_vorschlag(umfrage: Terminumfrage, vorschlag_id: Any) -> tuple[Terminumfrage, Terminvorschlag] | None:
    """
    Umfrage gesperrt neu lesen (zwei gleichzeitige Klicks legten sonst zweimal an) und den Vorschlag finden; ``None``,
    wenn sie schon abgeschlossen ist oder der Vorschlag nicht zu ihr gehört. Nur in einer Transaktion aufrufen.
    """
    from django.core.exceptions import ValidationError

    umfrage = Terminumfrage.objects.select_for_update().get(pk=umfrage.pk)
    if umfrage.abgeschlossen_am is not None:
        return None
    try:
        vorschlag = umfrage.vorschlaege.filter(pk=vorschlag_id).first()
    except (ValueError, ValidationError):
        vorschlag = None  # keine gültige Kennung
    return (umfrage, vorschlag) if vorschlag is not None else None


#: Antwort in der Umfrage → Anwesenheit in der internen Sitzung, die daraus entsteht
ANTWORT_IN_DER_SITZUNG: Final = {
    VorschlagAntwort.JA: "confirmed",
    VorschlagAntwort.NOTFALLS: "tentative",
    VorschlagAntwort.NEIN: "declined",
}


def darf_sitzung_anlegen(membership: Any) -> bool:
    """Darf aus einer Umfrage eine interne Sitzung werden? Wie beim Planen von Hand: ``faction.create``."""
    return (
        membership is not None
        and not getattr(membership, "is_guest", False)
        and bool(membership.has_permission("faction.create"))
    )


def sitzungsarten_zur_wahl(membership: Any) -> list[Any]:
    """Sitzungsarten, in denen die Sitzung entstehen kann: aktive, die das Mitglied sieht, die Standardart zuerst."""
    from apps.work.faction.sitzungsarten import sichtbare_arten

    return list(sichtbare_arten(membership).filter(is_active=True).order_by("-is_default", "position", "name"))


@transaction.atomic
def festlegen_als_sitzung(umfrage: Terminumfrage, vorschlag_id: Any, membership: Any, art_id: Any = None) -> Any:
    """
    Einen Vorschlag als interne Sitzung festlegen (mit Tagesordnung und Protokoll statt eines Termins). Die Sitzung
    entsteht wie beim Planen von Hand (``faction.anlegen.neue_sitzung``) in der gewählten Sitzungsart, mit Zu- und
    Absagen. Die Gefragten stehen auf der Anwesenheitsliste; ihre Antwort aus der Umfrage steht schon als Zusage,
    „vielleicht“ oder Absage dort. Die Einladung mit Tagesordnung verschickt wie gewohnt die Sitzung. ``None`` wie
    bei ``festlegen``, außerdem bei einer Sitzungsart, die das Mitglied nicht wählen kann.
    """
    from apps.work.faction.anlegen import neue_sitzung
    from apps.work.faction.models import FactionAttendance
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    arten = {str(art.pk): art for art in sitzungsarten_zur_wahl(membership)}
    art = arten.get(str(art_id)) if art_id else next(iter(arten.values()), None)
    if art is None:
        return None
    offen = _offen_mit_vorschlag(umfrage, vorschlag_id)
    if offen is None:
        return None
    umfrage, vorschlag = offen
    # Ohne Beschreibung: Sie ist an der Umfrage verschlüsselt, die Beschreibung einer Sitzung nicht. Die Umfrage
    # bleibt mit der Sitzung verbunden und behält sie.
    sitzung = neue_sitzung(
        umfrage.organization,
        membership,
        title=umfrage.title,
        start=vorschlag.start,
        end=vorschlag.end,
        location=umfrage.location,
        rsvp_enabled=True,
        meeting_type=art,
    )
    sitzung_id = sitzung.pk

    antworten = {
        mitglied_id: (wahl, beantwortet)
        for mitglied_id, wahl, beantwortet in vorschlag.antworten.values_list("membership_id", "wahl", "updated_at")
    }
    jetzt = timezone.now()
    gefragte = [teilnahme.membership for teilnahme in umfrage.teilnahmen.select_related("membership")]
    for mitglied in gefragte:
        anwesenheit, _ = FactionAttendance.objects.get_or_create(
            meeting_id=sitzung_id, membership=mitglied, defaults={"status": "invited"}
        )
        if mitglied.pk in antworten:
            wahl, beantwortet = antworten[mitglied.pk]
            anwesenheit.status = ANTWORT_IN_DER_SITZUNG[wahl]
            anwesenheit.responded_at = beantwortet
            anwesenheit.save(update_fields=["status", "responded_at"])

    vorschlag.festgelegt = True
    vorschlag.save(update_fields=["festgelegt"])
    umfrage.faction_meeting = sitzung
    umfrage.abgeschlossen_am = jetzt
    umfrage.save(update_fields=["faction_meeting", "abgeschlossen_am", "updated_at"])

    link = reverse("work:faction_detail", kwargs={"org_slug": umfrage.organization.slug, "meeting_id": sitzung_id})
    nachricht = f"Die Terminumfrage „{umfrage.title}“ ist entschieden: {zeit(vorschlag)}. Daraus wird eine Sitzung."
    metadata = {"meeting_id": str(sitzung_id), "umfrage_id": str(umfrage.pk)}

    def senden(mitglied: Any) -> None:
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_EINLADUNG,
            title="Sitzung steht fest",
            message=nachricht,
            link=link,
            actor=membership,
            metadata=metadata,
        )

    melden([mitglied for mitglied in gefragte if mitglied.pk != getattr(membership, "pk", None)], senden)
    return sitzung


def sitzung_kontext(umfrage: Terminumfrage, membership: Any) -> dict[str, Any]:
    """Für die Seite der Umfrage: ob eine Sitzung entstehen kann, in welchen Arten, und ob die entstandene sichtbar ist."""
    from apps.work.faction.sitzungsarten import darf_sehen

    darf = umfrage.abgeschlossen_am is None and darf_sitzung_anlegen(membership)
    return {
        "darf_sitzung": darf,
        "sitzungsarten": sitzungsarten_zur_wahl(membership) if darf else [],
        "sitzung_sichtbar": bool(umfrage.faction_meeting_id and darf_sehen(membership, umfrage.faction_meeting)),
    }


def festlegen_aus(umfrage: Terminumfrage, post: Any, membership: Any) -> tuple[str, Any]:
    """
    Festlegen aus dem Formular der Umfrage: ``vorschlag``, dazu ``als`` (``termin`` oder ``sitzung``) und bei einer
    Sitzung ``meeting_type``. Gibt die Art des Ergebnisses und das Ergebnis zurück, ``("", None)`` ohne Ergebnis.
    """
    from django.core.exceptions import PermissionDenied

    vorschlag_id = str(post["vorschlag"]) if "vorschlag" in post else ""
    if (str(post["als"]) if "als" in post else "") == "sitzung":
        if not darf_sitzung_anlegen(membership):
            raise PermissionDenied("Keine Berechtigung, interne Sitzungen anzulegen.")
        art_id = str(post["meeting_type"]) if "meeting_type" in post else ""
        sitzung = festlegen_als_sitzung(umfrage, vorschlag_id, membership, art_id)
        return ("sitzung", sitzung) if sitzung is not None else ("", None)
    termin = festlegen(umfrage, vorschlag_id, membership)
    return ("termin", termin) if termin is not None else ("", None)


@transaction.atomic
def festlegen(umfrage: Terminumfrage, vorschlag_id: Any, membership: Any) -> Termin | None:
    """
    Einen Vorschlag festlegen: Daraus entsteht ein Termin für die Gefragten, die Umfrage ist abgeschlossen. ``None``,
    wenn die Umfrage schon abgeschlossen ist oder der Vorschlag nicht zu ihr gehört.
    """
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    offen = _offen_mit_vorschlag(umfrage, vorschlag_id)
    if offen is None:
        return None
    umfrage, vorschlag = offen
    termin = Termin(
        organization=umfrage.organization,
        title=umfrage.title,
        start=vorschlag.start,
        end=vorschlag.end,
        location=umfrage.location,
        visibility=Termin.PRIVAT,
        created_by=umfrage.created_by or membership,
    )
    quelle: Any = umfrage
    ablage: Any = termin  # Verschlüsselungsmethoden ohne Typangaben
    ablage.set_description_encrypted(quelle.get_description_decrypted() or "")
    termin.save()

    wahl = dict(vorschlag.antworten.values_list("membership_id", "wahl"))
    jetzt = timezone.now()
    # Wer die Umfrage angelegt hat, bekommt den Termin als eigenen (``created_by``) und steht wie bei jedem Termin
    # nicht unter den Eingeladenen: ``termine._einladen`` nähme ihn beim nächsten Speichern wieder von der Liste,
    # und zusagen muss sich niemand selbst. Seine Antworten bleiben in der abgeschlossenen Umfrage nachlesbar.
    gefragte = [
        teilnahme.membership
        for teilnahme in umfrage.teilnahmen.select_related("membership")
        if teilnahme.membership_id != termin.created_by_id
    ]
    TerminTeilnahme.objects.bulk_create(
        TerminTeilnahme(
            termin=termin,
            membership=mitglied,
            status=ANTWORT_AM_TERMIN.get(wahl.get(mitglied.pk, ""), TerminTeilnahme.OFFEN),
            geantwortet_am=jetzt if mitglied.pk in wahl else None,
        )
        for mitglied in gefragte
    )
    vorschlag.festgelegt = True
    vorschlag.save(update_fields=["festgelegt"])
    umfrage.termin = termin
    umfrage.abgeschlossen_am = jetzt
    umfrage.save(update_fields=["termin", "abgeschlossen_am", "updated_at"])

    link = reverse("work:termin", kwargs={"org_slug": umfrage.organization.slug, "termin_id": termin.pk})
    nachricht = f"Die Terminumfrage „{umfrage.title}“ ist entschieden: {zeit(vorschlag)}."
    metadata = {"termin_id": str(termin.pk), "umfrage_id": str(umfrage.pk)}

    def senden(mitglied: Any) -> None:
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_EINLADUNG,
            title="Termin steht fest",
            message=nachricht,
            link=link,
            actor=membership,
            metadata=metadata,
        )

    melden([mitglied for mitglied in gefragte if mitglied.pk != getattr(membership, "pk", None)], senden)
    return termin


def _zeitpunkt(tag: str, beginn: str, ende: str) -> tuple[datetime, datetime | None]:
    """Tag, Beginn und Ende aus dem Formular als Zeitpunkte; ``ValueError`` trägt den Satz für die Seite."""
    try:
        am = date.fromisoformat(tag)
        von = time.fromisoformat(beginn)
        bis = time.fromisoformat(ende) if ende else None
    except ValueError:
        raise ValueError("Jeder Vorschlag braucht einen Tag und eine Uhrzeit für den Beginn.") from None
    start = timezone.make_aware(datetime.combine(am, von))
    schluss = timezone.make_aware(datetime.combine(am, bis)) if bis else None
    if schluss is not None and schluss <= start:
        raise ValueError(f"Am {am.strftime('%d.%m.%Y')} liegt das Ende nicht nach dem Beginn.")
    return start, schluss


@transaction.atomic
def vorschlag_ergaenzen(umfrage: Terminumfrage, post: Any, membership: Any) -> str:
    """
    Einen weiteren Zeitpunkt in eine offene Umfrage aufnehmen (``tag``, ``beginn``, ``ende``) und die Gefragten
    benachrichtigen, damit sie ihre Antwort ergänzen. Gibt den Satz für die Seite zurück, wenn es nicht geht
    (abgeschlossen, unvollständig, schon vorhanden, zu viele Vorschläge), sonst einen leeren Text.
    """
    # Zeile sperren und neu lesen wie beim Festlegen: kein Vorschlag an eine eben abgeschlossene Umfrage, und zwei
    # gleichzeitige Ergänzungen kommen nicht beide an der Höchstzahl vorbei
    umfrage = Terminumfrage.objects.select_for_update().get(pk=umfrage.pk)
    if umfrage.abgeschlossen_am is not None:
        return "Die Umfrage ist abgeschlossen."
    try:
        start, schluss = _zeitpunkt(*(str(post.get(name) or "") for name in ("tag", "beginn", "ende")))
    except ValueError as fehler:
        return str(fehler)
    if umfrage.vorschlaege.filter(start=start).exists():
        return "Diesen Zeitpunkt gibt es in der Umfrage schon."
    if umfrage.vorschlaege.count() >= MAX_VORSCHLAEGE:
        return f"Eine Umfrage nimmt höchstens {MAX_VORSCHLAEGE} Vorschläge auf."
    vorschlag = Terminvorschlag.objects.create(umfrage=umfrage, start=start, end=schluss)
    gefragte = [teilnahme.membership for teilnahme in umfrage.teilnahmen.select_related("membership")]
    text = f"Neuer Vorschlag in „{umfrage.title}“: {zeit(vorschlag)}. Bitte ergänzen Sie Ihre Antwort."
    _benachrichtigen(umfrage, gefragte, membership, text=text)
    return ""


def _benachrichtigen(umfrage: Terminumfrage, gefragte: list[Any], fragend: Any, *, text: str = "") -> None:
    """
    Die Gefragten benachrichtigen (wer fragt, bekommt nichts). Zeitpunkt und Fehlerbehandlung: ``meldungen.melden``
    (nichts geht hinaus, bevor die Änderung gespeichert ist; ein Fehler bei einer Person hält die übrigen nicht auf).
    """
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    link = reverse("work:terminumfrage", kwargs={"org_slug": umfrage.organization.slug, "umfrage_id": umfrage.pk})
    frist = f" Bitte antworten Sie bis {umfrage.antwort_bis.strftime('%d.%m.%Y')}." if umfrage.antwort_bis else ""
    nachricht = text or f"Wann passt es Ihnen? „{umfrage.title}“ steht zur Abstimmung.{frist}"
    metadata = {"umfrage_id": str(umfrage.pk)}

    def senden(mitglied: Any) -> None:
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_UMFRAGE,
            title="Terminumfrage",
            message=nachricht,
            link=link,
            actor=fragend,
            metadata=metadata,
        )

    melden([mitglied for mitglied in gefragte if mitglied.pk != getattr(fragend, "pk", None)], senden)


class UmfrageForm(forms.Form):
    """
    Formular für eine neue Terminumfrage. Die Vorschläge kommen als Zeilen aus Tag, Beginn und Ende
    (``vorschlag_tag``, ``vorschlag_beginn``, ``vorschlag_ende``, je Zeile ein Wert); leere Zeilen zählen nicht.
    """

    title = forms.CharField(max_length=200, label="Titel")
    location = forms.CharField(max_length=500, label="Ort", required=False)
    description = forms.CharField(max_length=5000, label="Beschreibung", required=False, widget=forms.Textarea)
    antwort_bis = forms.DateField(label="Antwort bis", required=False)
    wen = forms.ChoiceField(choices=WEN, label="Wen fragen Sie?")
    meeting_type: Any = forms.ModelChoiceField(queryset=None, label="Sitzungsart", required=False)
    gefragte: Any = forms.ModelMultipleChoiceField(queryset=None, label="Mitglieder", required=False)

    def __init__(self, *args: Any, membership: Any, **kwargs: Any) -> None:
        from apps.work.faction.sitzungsarten import arten_mit_teilnahme

        super().__init__(*args, **kwargs)
        self.membership = membership
        felder: Any = self.fields
        arten = arten_mit_teilnahme(membership).filter(is_active=True).order_by("position", "name")
        felder["meeting_type"].queryset = arten
        felder["gefragte"].queryset = termine.mitglieder(membership.organization_id).exclude(pk=membership.pk)
        self.arten = list(arten)
        self.vorschlaege: list[tuple[datetime, datetime | None]] = []

    @property
    def zeilen(self) -> list[dict[str, str]]:
        """Zeilen der Vorschläge, wie die Seite sie zeigt: die Eingaben, aufgefüllt auf ``ZEILEN``, sonst leere."""
        zeilen: list[dict[str, str]] = []
        if self.is_bound:
            daten: Any = self.data  # QueryDict der Anfrage
            tage, beginne, enden = (daten.getlist(f"vorschlag_{name}") for name in ("tag", "beginn", "ende"))
            for nummer, tag in enumerate(tage[:MAX_VORSCHLAEGE]):
                beginn = beginne[nummer] if nummer < len(beginne) else ""
                ende = enden[nummer] if nummer < len(enden) else ""
                if tag or beginn or ende:
                    zeilen.append({"tag": tag, "beginn": beginn, "ende": ende})
        while len(zeilen) < ZEILEN:
            zeilen.append({"tag": "", "beginn": "", "ende": ""})
        return zeilen

    @property
    def gewaehlt(self) -> dict[str, Any]:
        if self.is_bound:
            daten: Any = self.data
            return {
                "wen": str(daten.get("wen") or ALLE),
                "meeting_type": str(daten.get("meeting_type") or ""),
                "gefragte": set(daten.getlist("gefragte")),
            }
        return {"wen": ALLE, "meeting_type": "", "gefragte": set()}

    def clean(self) -> dict[str, Any]:
        daten = super().clean() or {}
        self.vorschlaege = []
        gesehen: set[datetime] = set()
        for zeile in self.zeilen:
            if not (zeile["tag"] or zeile["beginn"] or zeile["ende"]):
                continue
            try:
                start, schluss = _zeitpunkt(zeile["tag"], zeile["beginn"], zeile["ende"])
            except ValueError as fehler:
                self.add_error(None, str(fehler))
                break
            if start not in gesehen:
                gesehen.add(start)
                self.vorschlaege.append((start, schluss))
        if len(self.vorschlaege) < 2 and not self.non_field_errors():
            self.add_error(None, "Bitte mindestens zwei Zeitpunkte vorschlagen.")
        if daten.get("wen") == ART and daten.get("meeting_type") is None:
            self.add_error("meeting_type", "Bitte die Sitzungsart wählen, deren Teilnehmende Sie fragen.")
        if daten.get("wen") == AUSWAHL and not daten.get("gefragte"):
            self.add_error("gefragte", "Bitte mindestens ein Mitglied auswählen.")
        return daten

    def _gefragte(self) -> list[Any]:
        """Die Personen hinter der Auswahl; wer fragt, stimmt immer mit ab."""
        from apps.work.faction.sitzungsarten import teilnehmende

        daten = self.cleaned_data
        alle = termine.mitglieder(self.membership.organization_id)
        if daten["wen"] == ART:
            personen = list(alle.filter(pk__in=teilnehmende(daten["meeting_type"]).values("pk")))
        elif daten["wen"] == AUSWAHL:
            personen = list(daten["gefragte"])
        else:
            personen = list(alle)
        if all(person.pk != self.membership.pk for person in personen):
            personen.append(self.membership)
        return personen

    @transaction.atomic
    def speichern(self, organization: Any) -> Terminumfrage:
        daten = self.cleaned_data
        umfrage = Terminumfrage(
            organization=organization,
            title=daten["title"].strip(),
            location=daten.get("location", "").strip(),
            antwort_bis=daten.get("antwort_bis"),
            created_by=self.membership,
        )
        ablage: Any = umfrage  # Verschlüsselungsmethoden ohne Typangaben
        ablage.set_description_encrypted(daten.get("description", "").strip())
        umfrage.save()
        Terminvorschlag.objects.bulk_create(
            Terminvorschlag(umfrage=umfrage, start=start, end=schluss) for start, schluss in self.vorschlaege
        )
        gefragte = self._gefragte()
        UmfrageTeilnahme.objects.bulk_create(
            UmfrageTeilnahme(umfrage=umfrage, membership=person) for person in gefragte
        )
        _benachrichtigen(umfrage, gefragte, self.membership)
        return umfrage
