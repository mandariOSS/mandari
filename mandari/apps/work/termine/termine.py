# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Termine: wer sie sieht, wer sie ändert, Einladungen und das Formular dazu (Issue #1489, Schritt 1).

* **Sehen.** „Alle Mitglieder“ sieht jedes aktive Mitglied der Organisation, „Teilnehmende einer Sitzungsart“ nur,
  wer zu dieser Art gehört (dieselbe Zuordnung wie bei den Fraktionssitzungen, Issue #1239), „Nur ich und die
  Eingeladenen“ allein die Person, die den Termin angelegt hat, und wer eingeladen ist. Eingeladene sehen ihren
  Termin immer. Wer alle Termine verwalten darf (``calendar.manage``), sieht auch die Termine einer Gruppe, der er
  nicht angehört; private Termine anderer sieht niemand sonst. Gäste sehen keine Termine.
* **Anlegen** darf, wer ``calendar.create`` hat. **Ändern und löschen** darf, wer den Termin angelegt hat und das
  Recht noch besitzt, oder wer ``calendar.manage`` hat (nicht bei privaten Terminen anderer).
* **Einladen.** Eingeladen werden aktive Mitglieder der Organisation. Wer neu eingeladen wird, bekommt eine
  Benachrichtigung (und je nach eigener Einstellung eine E-Mail) und antwortet auf der Seite des Termins mit Zusage,
  „vielleicht“ oder Absage. Gäste ohne Konto folgen in einem eigenen Schritt.

Fachlogik steht hier, die Ansichten (``views.py``) lesen nur die Anfrage und rufen auf.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Final

from django import forms
from django.db import transaction
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from .models import Termin, TerminTeilnahme

ANLEGEN: Final = "calendar.create"
VERWALTEN: Final = "calendar.manage"
#: Dauer, die das Formular bei einem neuen Termin vorschlägt
STANDARD_DAUER: Final = timedelta(hours=1)
#: Fraktionssitzungen zur Auswahl im Formular: so weit zurück und voraus
SITZUNGEN_ZURUECK: Final = timedelta(days=14)
SITZUNGEN_VORAUS: Final = timedelta(days=120)
ANTWORTEN: Final = frozenset({TerminTeilnahme.ZUGESAGT, TerminTeilnahme.VIELLEICHT, TerminTeilnahme.ABGESAGT})


def darf_anlegen(membership: Any) -> bool:
    return (
        membership is not None
        and not getattr(membership, "is_guest", False)
        and bool(membership.has_permission(ANLEGEN))
    )


def sichtbare(membership: Any) -> QuerySet[Termin]:
    """Termine der Organisation, die das Mitglied sehen darf (leer für Gäste und ohne Mitgliedschaft)."""
    from apps.work.faction.sitzungsarten import arten_mit_teilnahme

    if membership is None or getattr(membership, "is_guest", False) or not membership.is_active:
        return Termin.objects.none()
    qs = Termin.objects.filter(organization_id=membership.organization_id)
    # Eingeladene als Unterabfrage statt als Verbund: Ein Verbund lieferte den Termin je Einladung einmal
    eingeladen = TerminTeilnahme.objects.filter(membership=membership).values("termin_id")
    eigene = Q(created_by=membership) | Q(pk__in=eingeladen)
    if membership.has_permission(VERWALTEN):
        return qs.filter(eigene | ~Q(visibility=Termin.PRIVAT))
    meine_arten = arten_mit_teilnahme(membership).values("pk")
    return qs.filter(eigene | Q(visibility=Termin.ALLE) | Q(visibility=Termin.ART, meeting_type__in=meine_arten))


def finden(membership: Any, termin_id: Any) -> Termin | None:
    """Ein sichtbarer Termin oder ``None`` (auch für Termine anderer Organisationen)."""
    return (
        sichtbare(membership)
        .select_related("meeting_type", "created_by__user", "faction_meeting")
        .filter(pk=termin_id)
        .first()
    )


def darf_aendern(membership: Any, termin: Termin) -> bool:
    """Ändern und Löschen: wer angelegt hat (und weiter anlegen darf) oder alle Termine verwaltet."""
    if membership is None or termin.organization_id != membership.organization_id:
        return False
    if termin.created_by_id == membership.pk:
        return darf_anlegen(membership) or bool(membership.has_permission(VERWALTEN))
    return termin.visibility != Termin.PRIVAT and bool(membership.has_permission(VERWALTEN))


def mitglieder(organization_id: Any) -> QuerySet[Any]:
    """Wer sich einladen lässt: aktive Mitglieder der Organisation ohne Gastzugang."""
    from apps.tenants.models import Membership

    return (
        Membership.objects.filter(organization_id=organization_id, is_active=True, is_guest=False)
        .select_related("user")
        .order_by("user__first_name", "user__last_name", "user__email")
    )


def sitzungen_zur_auswahl(membership: Any) -> QuerySet[Any]:
    """Fraktionssitzungen, auf die ein Termin verweisen kann: sichtbare aus den letzten Wochen und der nächsten Zeit."""
    from apps.work.faction.models import FactionMeeting
    from apps.work.faction.sitzungsarten import sichtbar_fuer

    jetzt = timezone.now()
    return sichtbar_fuer(
        membership,
        FactionMeeting.objects.filter(
            organization_id=membership.organization_id,
            start__gte=jetzt - SITZUNGEN_ZURUECK,
            start__lte=jetzt + SITZUNGEN_VORAUS,
        ),
    ).order_by("start")


def teilnahmen(termin: Termin) -> list[TerminTeilnahme]:
    """Einladungen eines Termins mit Namen, nach Namen sortiert."""
    return list(
        termin.teilnahmen.select_related("membership__user").order_by(
            "membership__user__first_name", "membership__user__last_name"
        )
    )


def meine_teilnahme(termin: Termin, membership: Any) -> TerminTeilnahme | None:
    return termin.teilnahmen.filter(membership=membership).first()


def antworten(termin: Termin, membership: Any, status: str) -> bool:
    """Antwort der eingeladenen Person festhalten; ``False`` ohne Einladung oder bei unbekannter Antwort."""
    if status not in ANTWORTEN:
        return False
    geaendert = TerminTeilnahme.objects.filter(termin=termin, membership=membership).update(
        status=status, geantwortet_am=timezone.now()
    )
    return bool(geaendert)


def _einladen(termin: Termin, eingeladene: list[Any], einladend: Any) -> None:
    """
    Einladungen an die Auswahl angleichen: Neue anlegen und benachrichtigen, Abgewählte entfernen. Wer einlädt,
    steht nicht selbst auf der Liste (er hat den Termin angelegt und sieht ihn ohnehin).
    """
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    gewuenscht = {mitglied.pk: mitglied for mitglied in eingeladene if mitglied.pk != termin.created_by_id}
    vorhanden = set(termin.teilnahmen.values_list("membership_id", flat=True))
    termin.teilnahmen.exclude(membership_id__in=gewuenscht.keys()).delete()
    neue = [mitglied for pk, mitglied in gewuenscht.items() if pk not in vorhanden]
    TerminTeilnahme.objects.bulk_create(TerminTeilnahme(termin=termin, membership=mitglied) for mitglied in neue)
    link = reverse("work:termin", kwargs={"org_slug": termin.organization.slug, "termin_id": termin.pk})
    wann = timezone.localtime(termin.start)
    zeit = wann.strftime("%d.%m.%Y") if termin.all_day else wann.strftime("%d.%m.%Y, %H:%M Uhr")
    for mitglied in neue:
        if mitglied.pk == getattr(einladend, "pk", None):
            continue
        NotificationHub.send(
            recipient=mitglied,
            notification_type=NotificationType.TERMIN_EINLADUNG,
            title="Einladung zu einem Termin",
            message=f"Sie sind zu „{termin.title}“ am {zeit} eingeladen. Bitte sagen Sie zu oder ab.",
            link=link,
            actor=einladend,
            metadata={"termin_id": str(termin.pk)},
        )


def _lokal(tag: date, uhrzeit: time) -> datetime:
    return timezone.make_aware(datetime.combine(tag, uhrzeit))


class TerminForm(forms.Form):
    """
    Formular für einen Termin. Tag und Uhrzeit sind getrennte Felder (gut mit Tastatur und am Handy); bei „ganztägig“
    zählt nur der Tag. ``speichern`` setzt die Werte am Termin, sichert ihn und gleicht die Einladungen an.
    """

    title = forms.CharField(max_length=200, label="Titel")
    tag = forms.DateField(label="Tag")
    beginn = forms.TimeField(label="Beginn", required=False)
    ende = forms.TimeField(label="Ende", required=False)
    bis_tag = forms.DateField(label="Letzter Tag", required=False)
    all_day = forms.BooleanField(label="Ganztägig", required=False)
    location = forms.CharField(max_length=500, label="Ort", required=False)
    video_url = forms.URLField(max_length=500, label="Link zur Videokonferenz", required=False, assume_scheme="https")
    description = forms.CharField(max_length=5000, label="Beschreibung", required=False, widget=forms.Textarea)
    faction_meeting: Any = forms.ModelChoiceField(queryset=None, label="Interne Sitzung", required=False)
    eingeladene: Any = forms.ModelMultipleChoiceField(queryset=None, label="Eingeladene", required=False)
    visibility = forms.ChoiceField(choices=Termin.SICHTBARKEIT, label="Wer sieht den Termin?")
    meeting_type: Any = forms.ModelChoiceField(queryset=None, label="Sitzungsart", required=False)

    def __init__(self, *args: Any, membership: Any, **kwargs: Any) -> None:
        from apps.work.faction.sitzungsarten import arten_mit_teilnahme

        super().__init__(*args, **kwargs)
        self.membership = membership
        felder: Any = self.fields
        # Nur Arten, zu denen die Person selbst gehört: Sie soll ihren eigenen Termin danach noch sehen
        arten = arten_mit_teilnahme(membership).filter(is_active=True).order_by("position", "name")
        felder["meeting_type"].queryset = arten
        felder["faction_meeting"].queryset = sitzungen_zur_auswahl(membership)
        felder["eingeladene"].queryset = mitglieder(membership.organization_id).exclude(pk=membership.pk)
        self.arten = list(arten)

    def _roh(self, name: str) -> Any:
        """Wert eines Felds, wie ihn die Seite wieder zeigt: die Eingabe, sonst der Anfangswert."""
        return self.data.get(name, "") if self.is_bound else self.initial.get(name)

    @property
    def werte(self) -> dict[str, str]:
        """Tag und Uhrzeiten als Text für ``<input type="date">`` und ``<input type="time">``."""
        werte: dict[str, str] = {}
        for name in ("tag", "bis_tag", "beginn", "ende"):
            wert = self._roh(name)
            if isinstance(wert, time):
                wert = wert.strftime("%H:%M")
            elif isinstance(wert, date):
                wert = wert.isoformat()
            werte[name] = str(wert or "")
        return werte

    @property
    def gewaehlt(self) -> dict[str, Any]:
        """Auswahlen für die Seite: Sichtbarkeit, Sitzungsart, Sitzung, ganztägig und die Eingeladenen (als Text)."""
        if self.is_bound:
            daten: Any = self.data  # QueryDict der Anfrage
            eingeladene = set(daten.getlist("eingeladene"))
        else:
            eingeladene = {str(pk) for pk in self.initial.get("eingeladene") or []}
        return {
            "visibility": str(self._roh("visibility") or Termin.ALLE),
            "meeting_type": str(self._roh("meeting_type") or ""),
            "faction_meeting": str(self._roh("faction_meeting") or ""),
            "all_day": bool(self._roh("all_day")),
            "eingeladene": eingeladene,
        }

    def clean(self) -> dict[str, Any]:
        daten = super().clean() or {}
        tag = daten.get("tag")
        if tag is None:
            return daten
        if daten.get("all_day"):
            letzter = daten.get("bis_tag") or tag
            if letzter < tag:
                self.add_error("bis_tag", "Der letzte Tag liegt vor dem ersten.")
            daten["start"] = _lokal(tag, time.min)
            # Ende ist der Beginn des Folgetags (wie in Kalenderprogrammen): Ein eintägiger Termin endet um 0 Uhr
            daten["end"] = _lokal(letzter + timedelta(days=1), time.min)
        else:
            beginn = daten.get("beginn")
            if beginn is None:
                self.add_error("beginn", "Bitte eine Uhrzeit angeben oder „Ganztägig“ wählen.")
                return daten
            daten["start"] = _lokal(tag, beginn)
            ende = daten.get("ende")
            daten["end"] = _lokal(tag, ende) if ende else None
            if ende and daten["end"] <= daten["start"]:
                self.add_error("ende", "Das Ende liegt nicht nach dem Beginn.")
        if daten.get("visibility") == Termin.ART and daten.get("meeting_type") is None:
            self.add_error("meeting_type", "Bitte die Sitzungsart wählen, deren Teilnehmende den Termin sehen.")
        if daten.get("visibility") != Termin.ART:
            daten["meeting_type"] = None
        return daten

    @transaction.atomic
    def speichern(self, termin: Termin) -> Termin:
        daten = self.cleaned_data
        termin.title = daten["title"].strip()
        termin.start = daten["start"]
        termin.end = daten["end"]
        termin.all_day = bool(daten.get("all_day"))
        termin.location = daten.get("location", "").strip()
        termin.video_url = daten.get("video_url") or ""
        termin.faction_meeting = daten.get("faction_meeting")
        termin.visibility = daten["visibility"]
        termin.meeting_type = daten.get("meeting_type")
        ablage: Any = termin  # Verschlüsselungsmethoden ohne Typangaben
        ablage.set_description_encrypted(daten.get("description", "").strip())
        termin.save()
        _einladen(termin, list(daten.get("eingeladene") or []), self.membership)
        return termin


def anfangswerte(termin: Termin | None, tag: date | None = None) -> dict[str, Any]:
    """Werte für das Formular: aus dem Termin, sonst ein Vorschlag (für ``tag`` 18 Uhr, sonst die nächste volle Stunde)."""
    if termin is None:
        jetzt = timezone.localtime()
        beginn = (jetzt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        ende = beginn + STANDARD_DAUER
        return {
            "tag": tag or beginn.date(),
            "beginn": beginn.time() if tag is None else time(18, 0),
            "ende": ende.time() if tag is None else time(19, 0),
            "visibility": Termin.ALLE,
        }
    start = timezone.localtime(termin.start)
    schluss = timezone.localtime(termin.end) if termin.end else None
    ablage: Any = termin
    werte: dict[str, Any] = {
        "title": termin.title,
        "tag": start.date(),
        "all_day": termin.all_day,
        "location": termin.location,
        "video_url": termin.video_url,
        "description": ablage.get_description_decrypted() or "",
        "faction_meeting": termin.faction_meeting_id,
        "eingeladene": list(termin.teilnahmen.values_list("membership_id", flat=True)),
        "visibility": termin.visibility,
        "meeting_type": termin.meeting_type_id,
    }
    if termin.all_day:
        # Gespeichert ist der Beginn des Folgetags; das Formular zeigt den letzten Tag
        letzter = (schluss - timedelta(days=1)).date() if schluss else start.date()
        werte["bis_tag"] = letzter if letzter != start.date() else None
    else:
        werte["beginn"] = start.time()
        werte["ende"] = schluss.time() if schluss else None
    return werte


def zeitangabe(termin: Termin) -> str:
    """Zeit eines Termins als Satzteil: „Di 24.11.2026, 18:00 bis 19:00 Uhr“ oder „Di 24.11. bis Do 26.11.2026“."""
    tage = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

    def tag(zeitpunkt: datetime, *, jahr: bool = True) -> str:
        return f"{tage[zeitpunkt.weekday()]} {zeitpunkt.strftime('%d.%m.%Y' if jahr else '%d.%m.')}"

    start = timezone.localtime(termin.start)
    ende = timezone.localtime(termin.end) if termin.end else None
    if termin.all_day:
        letzter = (ende - timedelta(days=1)) if ende else start
        if letzter.date() <= start.date():
            return f"{tag(start)}, ganztägig"
        return f"{tag(start, jahr=False)} bis {tag(letzter)}, ganztägig"
    if ende is None:
        return f"{tag(start)}, {start.strftime('%H:%M')} Uhr"
    if ende.date() == start.date():
        return f"{tag(start)}, {start.strftime('%H:%M')} bis {ende.strftime('%H:%M')} Uhr"
    return f"{tag(start)}, {start.strftime('%H:%M')} Uhr bis {tag(ende)}, {ende.strftime('%H:%M')} Uhr"


def neu(organization: Any, membership: Any) -> Termin:
    """Ein noch nicht gespeicherter Termin der Organisation, angelegt von ``membership``."""
    return Termin(organization=organization, created_by=membership)
