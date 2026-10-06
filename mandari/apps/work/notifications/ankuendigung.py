# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ankündigungen in Work (Issue #857).

Eine Ankündigung ist eine Benachrichtigung der Art ``announcement`` mit Titel, kurzem Text und Link. Der Betrieb legt
sie mit ``manage.py work_ankuendigung`` an. Sie erscheint in der Glocke und im Hinweisband auf Start (die neueste,
solange sie ungelesen ist). Per E-Mail geht sie nie hinaus.

Kennzeichen an der Benachrichtigung:

- ``metadata["ankuendigung"]``: Schlüssel der Ankündigung, etwa ``work-update-2026-11``
- ``event_key`` = ``ankuendigung:<schlüssel>:<mitgliedschaft>``: eindeutig (Teilindex), deshalb legt ein zweiter
  Aufruf nichts doppelt an, auch nicht bei zwei gleichzeitigen Läufen
- ``metadata["zurueckgezogen_am"]``: zurückgezogen. Glocke, Zähler und Band blenden sie aus, die Zeilen bleiben.

Gelesen gilt für die Person, nicht je Organisation: Wer die Ankündigung in einer Organisation wegklickt oder in der
Glocke öffnet, hat sie in allen Organisationen gelesen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.tenants.models import Membership

from .models import ANNOUNCEMENT_WITHDRAWN_KEY, Notification, NotificationPreference, NotificationType
from .services import NotificationHub

if TYPE_CHECKING:
    from apps.tenants.models import Organization

PRAEFIX = "ankuendigung"
METADATEN_SCHLUESSEL = "ankuendigung"
METADATEN_ZURUECKGEZOGEN = ANNOUNCEMENT_WITHDRAWN_KEY

#: Kleinbuchstaben, Ziffern und Bindestriche; höchstens 60 Zeichen, damit ``event_key`` (120) reicht
SCHLUESSEL_MUSTER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
TITEL_MAX = 120
TEXT_MAX = 500
LINK_MAX = 500
LINKTEXT_MAX = 40
LINKTEXT_STANDARD = "Mehr dazu"


class UngueltigeAnkuendigung(ValueError):
    """Angaben einer Ankündigung sind unvollständig oder unzulässig."""


def _link_zulaessig(link: str) -> bool:
    """Nur Pfade dieser Installation (``/…``) oder ``https://``-Adressen, ohne Leer- und Steuerzeichen."""
    if any(zeichen.isspace() or ord(zeichen) < 32 for zeichen in link):
        return False
    if link.startswith("/"):
        return not link.startswith("//")
    teile = urlsplit(link)
    return teile.scheme == "https" and bool(teile.netloc)


@dataclass(frozen=True)
class Ankuendigung:
    """Inhalt einer Ankündigung."""

    schluessel: str
    titel: str
    text: str
    link: str = ""
    linktext: str = ""
    #: Im Band zusätzlich „Rückmeldung geben“ (führt in den Support der Organisation)
    rueckmeldung: bool = False

    def pruefen(self) -> None:
        """Wirft ``UngueltigeAnkuendigung`` mit einer Meldung für den Aufruf, wenn eine Angabe nicht passt."""
        if not SCHLUESSEL_MUSTER.match(self.schluessel):
            raise UngueltigeAnkuendigung(
                "Der Schlüssel besteht aus Kleinbuchstaben, Ziffern und Bindestrichen (höchstens 60 Zeichen), "
                "etwa work-update-2026-11."
            )
        if not self.titel.strip() or len(self.titel) > TITEL_MAX:
            raise UngueltigeAnkuendigung(f"Der Titel ist Pflicht und hat höchstens {TITEL_MAX} Zeichen.")
        if not self.text.strip() or len(self.text) > TEXT_MAX:
            raise UngueltigeAnkuendigung(f"Der Text ist Pflicht und hat höchstens {TEXT_MAX} Zeichen.")
        if self.link and (len(self.link) > LINK_MAX or not _link_zulaessig(self.link)):
            raise UngueltigeAnkuendigung("Der Link beginnt mit https:// oder ist ein Pfad dieser Installation (/…).")
        if len(self.linktext) > LINKTEXT_MAX:
            raise UngueltigeAnkuendigung(f"Der Linktext hat höchstens {LINKTEXT_MAX} Zeichen.")
        if self.linktext and not self.link:
            raise UngueltigeAnkuendigung("Ein Linktext braucht einen Link.")

    def metadaten(self) -> dict[str, Any]:
        daten: dict[str, Any] = {METADATEN_SCHLUESSEL: self.schluessel}
        if self.link:
            daten["linktext"] = self.linktext or LINKTEXT_STANDARD
        if self.rueckmeldung:
            daten["rueckmeldung"] = True
        return daten


@dataclass
class Zeile:
    """Zählung je Organisation."""

    organisation: str
    neu: int = 0
    vorhanden: int = 0
    abgeschaltet: int = 0


@dataclass
class Bericht:
    """Ergebnis eines Laufs (auch im Probelauf), nur Anzahlen."""

    zeilen: list[Zeile] = field(default_factory=list)

    @property
    def neu(self) -> int:
        return sum(zeile.neu for zeile in self.zeilen)

    @property
    def vorhanden(self) -> int:
        return sum(zeile.vorhanden for zeile in self.zeilen)

    @property
    def abgeschaltet(self) -> int:
        return sum(zeile.abgeschaltet for zeile in self.zeilen)


def ereignisschluessel(schluessel: str, membership_id: Any) -> str:
    """Eindeutiger ``event_key`` der Ankündigung für eine Mitgliedschaft."""
    return f"{_praefix(schluessel)}{membership_id}"


def _praefix(schluessel: str) -> str:
    return f"{PRAEFIX}:{schluessel}:"


def _zu_schluessel(schluessel: str) -> QuerySet[Notification]:
    """Alle Benachrichtigungen einer Ankündigung (auch zurückgezogene)."""
    return Notification.objects.filter(
        notification_type=NotificationType.ANNOUNCEMENT, event_key__startswith=_praefix(schluessel)
    )


def empfaenger(
    organisationen: Sequence[Organization] | None = None, *, mit_gaesten: bool = False
) -> QuerySet[Membership]:
    """Aktive Mitgliedschaften aktiver Konten, Gäste nur auf Wunsch."""
    mitgliedschaften = Membership.objects.filter(is_active=True, user__is_active=True)
    if organisationen is not None:
        mitgliedschaften = mitgliedschaften.filter(organization__in=list(organisationen))
    if not mit_gaesten:
        mitgliedschaften = mitgliedschaften.filter(is_guest=False)
    return mitgliedschaften.select_related("organization").order_by("organization__slug", "id")


def ankuendigen(
    ankuendigung: Ankuendigung,
    *,
    organisationen: Sequence[Organization] | None = None,
    mit_gaesten: bool = False,
    probelauf: bool = False,
) -> Bericht:
    """
    Legt die Ankündigung für alle Empfänger an, die sie noch nicht haben.

    Übersprungen werden Mitgliedschaften, die die Ankündigung schon haben (``vorhanden``), und solche, die
    Ankündigungen in Work abgeschaltet haben (``abgeschaltet``). Hat die Person sie in einer anderen Organisation
    schon gelesen, entsteht sie gleich als gelesen. Keine E-Mail, keine Weiterleitung an Vertretungen.
    """
    ankuendigung.pruefen()
    mitgliedschaften = list(empfaenger(organisationen, mit_gaesten=mit_gaesten))
    ids = [mitgliedschaft.id for mitgliedschaft in mitgliedschaften]
    vorhanden = set(_zu_schluessel(ankuendigung.schluessel).values_list("recipient_id", flat=True))
    einstellungen = {
        einstellung.membership_id: einstellung
        for einstellung in NotificationPreference.objects.filter(membership_id__in=ids)
    }
    gelesen_von = set(
        _zu_schluessel(ankuendigung.schluessel).filter(is_read=True).values_list("recipient__user_id", flat=True)
    )

    bericht = Bericht()
    zeilen: dict[str, Zeile] = {}
    # Genannte Organisationen erscheinen auch ohne Empfänger (Zeile mit Nullen)
    for organisation in sorted(organisationen or [], key=lambda org: org.slug):
        zeilen[organisation.slug] = Zeile(organisation=organisation.slug)
        bericht.zeilen.append(zeilen[organisation.slug])
    neue: list[Notification] = []
    jetzt = timezone.now()
    for mitgliedschaft in mitgliedschaften:
        slug = mitgliedschaft.organization.slug
        zeile = zeilen.get(slug)
        if zeile is None:
            zeile = zeilen[slug] = Zeile(organisation=slug)
            bericht.zeilen.append(zeile)
        if mitgliedschaft.id in vorhanden:
            zeile.vorhanden += 1
            continue
        einstellung = einstellungen.get(mitgliedschaft.id)
        if einstellung is not None and not einstellung.is_type_enabled(NotificationType.ANNOUNCEMENT, "in_app"):
            zeile.abgeschaltet += 1
            continue
        zeile.neu += 1
        schon_gelesen = mitgliedschaft.user_id in gelesen_von
        neue.append(
            Notification(
                recipient=mitgliedschaft,
                notification_type=NotificationType.ANNOUNCEMENT,
                title=ankuendigung.titel,
                message=ankuendigung.text,
                link=ankuendigung.link,
                metadata=ankuendigung.metadaten(),
                event_key=ereignisschluessel(ankuendigung.schluessel, mitgliedschaft.id),
                is_read=schon_gelesen,
                read_at=jetzt if schon_gelesen else None,
            )
        )

    if probelauf or not neue:
        return bericht
    with transaction.atomic():
        # Teilindex auf event_key: Ein gleichzeitiger zweiter Lauf legt nichts doppelt an
        Notification.objects.bulk_create(neue, ignore_conflicts=True)
    _zaehler_verwerfen(neu.recipient_id for neu in neue)
    return bericht


def zurueckziehen(schluessel: str, *, probelauf: bool = False) -> int:
    """
    Zieht eine Ankündigung zurück: Glocke, Zähler und Band zeigen sie nicht mehr.

    Die Benachrichtigungen bleiben gespeichert (nur ``metadata["zurueckgezogen_am"]`` kommt hinzu), gelesen und
    ungelesen bleiben unverändert. Gibt die Zahl der betroffenen Benachrichtigungen zurück.
    """
    if not SCHLUESSEL_MUSTER.match(schluessel):
        raise UngueltigeAnkuendigung("Unbekannter Schlüssel.")
    offen = _zu_schluessel(schluessel).exclude(metadata__has_key=METADATEN_ZURUECKGEZOGEN)
    if probelauf:
        return offen.count()
    zeitpunkt = timezone.now().isoformat()
    empfaenger_ids = []
    with transaction.atomic():
        for benachrichtigung in offen.select_for_update():
            benachrichtigung.metadata = {**(benachrichtigung.metadata or {}), METADATEN_ZURUECKGEZOGEN: zeitpunkt}
            benachrichtigung.save(update_fields=["metadata"])
            empfaenger_ids.append(benachrichtigung.recipient_id)
    _zaehler_verwerfen(empfaenger_ids)
    return len(empfaenger_ids)


def gelesen_fuer_person(user_id: Any, schluessel: Iterable[Any]) -> int:
    """Markiert die Ankündigungen mit diesen Schlüsseln in allen Organisationen der Person als gelesen."""
    gesamt = 0
    for eintrag in {wert for wert in schluessel if isinstance(wert, str) and SCHLUESSEL_MUSTER.match(wert)}:
        zeilen = list(
            _zu_schluessel(eintrag).filter(recipient__user_id=user_id, is_read=False).values_list("id", "recipient_id")
        )
        if not zeilen:
            continue
        gesamt += Notification.objects.filter(id__in=[kennung for kennung, _ in zeilen], is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
        _zaehler_verwerfen(empfaenger for _, empfaenger in zeilen)
    return gesamt


def gelesen(benachrichtigung: Notification) -> int:
    """Nach dem Lesen einer Benachrichtigung: Ist sie eine Ankündigung, gilt sie in allen Organisationen als gelesen."""
    if benachrichtigung.notification_type != NotificationType.ANNOUNCEMENT:
        return 0
    schluessel = (benachrichtigung.metadata or {}).get(METADATEN_SCHLUESSEL)
    if not schluessel:
        return 0
    return gelesen_fuer_person(benachrichtigung.recipient.user_id, [schluessel])


def fuer_band(membership: Membership) -> Notification | None:
    """
    Die Ankündigung für das Hinweisband auf Start: die neueste der Mitgliedschaft, solange sie ungelesen ist.

    Ältere ungelesene Ankündigungen erscheinen danach nicht mehr im Band (nur in der Glocke). Hat die Person die
    neueste in einer anderen Organisation schon gelesen, erscheint sie auch hier nicht.
    """
    neueste = (
        Notification.objects.filter(recipient=membership, notification_type=NotificationType.ANNOUNCEMENT)
        .exclude(metadata__has_key=METADATEN_ZURUECKGEZOGEN)
        .order_by("-created_at")
        .first()
    )
    if neueste is None or neueste.is_read:
        return None
    schluessel = (neueste.metadata or {}).get(METADATEN_SCHLUESSEL)
    if (
        isinstance(schluessel, str)
        and SCHLUESSEL_MUSTER.match(schluessel)
        and _zu_schluessel(schluessel).filter(recipient__user_id=membership.user_id, is_read=True).exists()
    ):
        return None
    return neueste


def _zaehler_verwerfen(membership_ids: Iterable[Any]) -> None:
    """Zwischengespeicherte Zähler der Glocke verwerfen."""
    NotificationHub.invalidate_count_caches(membership_ids)
