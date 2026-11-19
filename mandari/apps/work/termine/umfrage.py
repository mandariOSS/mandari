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
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from . import termine
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
    tage = [timezone.localtime(vorschlag.start).date() for vorschlag in vorschlaege]
    von = timezone.make_aware(datetime.combine(min(tage), time.min))
    bis = timezone.make_aware(datetime.combine(max(tage) + timedelta(days=1), time.min))
    je_tag: dict[date, list[str]] = defaultdict(list)
    for eintrag in kalender.termine(umfrage.organization, membership, von, bis, "sitzung,fraktion,termin"):
        beginn = str(eintrag.get("start") or "")
        try:
            wann = datetime.fromisoformat(beginn)
        except ValueError:
            continue
        if eintrag.get("allDay") or timezone.is_naive(wann):
            je_tag[wann.date()].append(str(eintrag["title"]))
        else:
            lokal = timezone.localtime(wann)
            je_tag[lokal.date()].append(f"{lokal.strftime('%H:%M')} {eintrag['title']}")
    return je_tag


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
    return {
        "zeilen": zeilen,
        "gefragt": len(gefragte),
        "beantwortet": len(beantwortet & set(namen)),
        "ohne_antwort": [name for mitglied_id, name in namen.items() if mitglied_id not in beantwortet],
    }


def antworten(umfrage: Terminumfrage, membership: Any, post: Any) -> int:
    """
    Antworten der gefragten Person speichern (``wahl-<Vorschlag>`` = ja, notfalls oder nein); gibt die Zahl der
    gespeicherten Antworten zurück, 0 bei einer abgeschlossenen Umfrage oder ohne Einladung.
    """
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


@transaction.atomic
def festlegen(umfrage: Terminumfrage, vorschlag_id: Any, membership: Any) -> Termin | None:
    """
    Einen Vorschlag festlegen: Daraus entsteht ein Termin für die Gefragten, die Umfrage ist abgeschlossen. ``None``,
    wenn die Umfrage schon abgeschlossen ist oder der Vorschlag nicht zu ihr gehört.
    """
    from django.core.exceptions import ValidationError

    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    # Zeile sperren und neu lesen: Zwei gleichzeitige Klicks legten sonst zwei Termine an
    umfrage = Terminumfrage.objects.select_for_update().get(pk=umfrage.pk)
    if umfrage.abgeschlossen_am is not None:
        return None
    try:
        vorschlag = umfrage.vorschlaege.filter(pk=vorschlag_id).first()
    except (ValueError, ValidationError):
        vorschlag = None  # keine gültige Kennung
    if vorschlag is None:
        return None
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
    for mitglied in gefragte:
        if mitglied.pk == getattr(membership, "pk", None):
            continue
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_EINLADUNG,
            title="Termin steht fest",
            message=f"Die Terminumfrage „{umfrage.title}“ ist entschieden: {zeit(vorschlag)}.",
            link=link,
            actor=membership,
            metadata={"termin_id": str(termin.pk), "umfrage_id": str(umfrage.pk)},
        )
    return termin


def _benachrichtigen(umfrage: Terminumfrage, gefragte: list[Any], fragend: Any) -> None:
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    link = reverse("work:terminumfrage", kwargs={"org_slug": umfrage.organization.slug, "umfrage_id": umfrage.pk})
    frist = f" Bitte antworten Sie bis {umfrage.antwort_bis.strftime('%d.%m.%Y')}." if umfrage.antwort_bis else ""
    for mitglied in gefragte:
        if mitglied.pk == getattr(fragend, "pk", None):
            continue
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_UMFRAGE,
            title="Terminumfrage",
            message=f"Wann passt es Ihnen? „{umfrage.title}“ steht zur Abstimmung.{frist}",
            link=link,
            actor=fragend,
            metadata={"umfrage_id": str(umfrage.pk)},
        )


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
                tag = date.fromisoformat(zeile["tag"])
                beginn = time.fromisoformat(zeile["beginn"])
                ende = time.fromisoformat(zeile["ende"]) if zeile["ende"] else None
            except ValueError:
                self.add_error(None, "Jeder Vorschlag braucht einen Tag und eine Uhrzeit für den Beginn.")
                break
            start = timezone.make_aware(datetime.combine(tag, beginn))
            schluss = timezone.make_aware(datetime.combine(tag, ende)) if ende else None
            if schluss is not None and schluss <= start:
                self.add_error(None, f"Am {tag.strftime('%d.%m.%Y')} liegt das Ende nicht nach dem Beginn.")
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
