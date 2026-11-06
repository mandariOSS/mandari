# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1231, Epic #1226): Jede Organisation hat genau eine Standardart „Fraktionssitzung“.

Bestehende Organisationen bekommen sie aus der Datenmigration, neue beim Anlegen (:func:`register`). Wo eine Art
fehlt, legt :meth:`FactionMeetingType.default_for <apps.work.faction.models.FactionMeetingType.default_for>` sie an,
ohne je doppelt anzulegen; neue Reihen, Sitzungen und Standard-TOPs ohne Art bekommen beim Speichern die Standardart.

Teilnehmende und Sichtbarkeit (Issue #1239) setzt Work auf dem Server durch, und zwar nur über diese Funktionen:

* :func:`teilnehmende`: wer zu einer Art gehört (aktive Mitglieder ohne Gastzugang: alle, eine Rolle, ein Gremium aus
  ``Membership.oparl_committees`` oder die ausgewählten Personen). Neue Sitzungen laden genau sie ein.
* :func:`sichtbar_fuer` und :func:`darf_sehen`: wer eine Sitzung sieht. Arten mit ``public`` und ``members`` sehen alle
  Mitglieder wie bisher. Bei ``participants`` sehen sie nur die Teilnehmenden der Art, wer zur Sitzung eingeladen ist
  (Anwesenheit), Sitzungsleitung und Schriftführung der Sitzung und alle mit ``faction.manage``. Alle anderen bekommen
  404 (:func:`sitzung_oder_404`), damit niemand erfährt, dass es die Sitzung gibt.
* :func:`oeffentlich`: Öffentliche API, öffentliche Protokolle und Kalender zeigen nur Arten mit ``public``.

Die Regeln des nichtöffentlichen Teils (``visibility.py``) gelten zusätzlich, sie werden hier nicht ersetzt.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.db.models import Exists, OuterRef, Q, QuerySet

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization
    from apps.work.faction.models import FactionMeeting, FactionMeetingType

#: Sichtbarkeit, bei der nur Teilnehmende, Eingeladene, Leitung, Schriftführung und ``faction.manage`` die Sitzung sehen
NUR_TEILNEHMENDE = "participants"
#: Sichtbarkeit für öffentliche Ausgaben (API, öffentliche Protokolle, Kalender)
OEFFENTLICH = "public"
#: Wer alle Sitzungen jeder Art sieht
RECHT_ALLE = "faction.manage"


def organization_created(sender: Any, instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
    """post_save(Organization): neue Organisation bekommt ihre Standardart (nicht beim Laden von Fixtures)."""
    if not created or raw:
        return
    from apps.work.faction.models import FactionMeetingType

    FactionMeetingType.default_for(instance)


#: Schalter, deren Einschalten die Standardart öffentlich macht (:func:`standardart_oeffentlich`): Modell → Feld
_OEFFENTLICH_SCHALTER = {"Organization": "publish_protocols", "FactionPublicApiAccess": "is_enabled"}


def _schalter_vorher(sender: Any, instance: Any, raw: bool = False, update_fields: Any = None, **kwargs: Any) -> None:
    """pre_save: Wird der Schalter jetzt eingeschaltet (vorher aus oder neu)? Merkt es sich an der Instanz."""
    feld = _OEFFENTLICH_SCHALTER[sender.__name__]
    instance._standardart_oeffentlich = False
    if raw or not getattr(instance, feld) or (update_fields is not None and feld not in update_fields):
        return
    vorher = sender.objects.filter(pk=instance.pk).values_list(feld, flat=True).first() if instance.pk else None
    instance._standardart_oeffentlich = not vorher


def _schalter_nachher(sender: Any, instance: Any, raw: bool = False, **kwargs: Any) -> None:
    """post_save: eingeschaltet → Standardart öffentlich (Organisation direkt oder über den API-Zugang)."""
    if raw or not getattr(instance, "_standardart_oeffentlich", False):
        return
    instance._standardart_oeffentlich = False
    standardart_oeffentlich(instance if sender.__name__ == "Organization" else instance.organization_id)


def register() -> None:
    """Receiver registrieren (WorkConfig.ready)."""
    from django.db.models.signals import post_save, pre_save

    from apps.tenants.models import Organization
    from apps.work.faction.models import FactionPublicApiAccess

    post_save.connect(organization_created, sender=Organization, dispatch_uid="faction_meeting_type_default")
    # Öffentliche Protokolle und öffentliche API (Issue #1239): auf jedem Weg (Einstellungen, Admin, Shell)
    for model in (Organization, FactionPublicApiAccess):
        uid = f"faction_meeting_type_public_{model.__name__}"
        pre_save.connect(_schalter_vorher, sender=model, dispatch_uid=f"{uid}_pre")
        post_save.connect(_schalter_nachher, sender=model, dispatch_uid=f"{uid}_post")


# =============================================================================
# Teilnehmende einer Art
# =============================================================================


def teilnehmende(meeting_type: FactionMeetingType) -> QuerySet[Membership]:
    """
    Teilnehmende der Art: aktive Mitglieder ohne Gastzugang der Organisation.

    ``role`` und ``committee`` ohne Rolle bzw. Gremium (gelöscht, ``SET_NULL``) haben niemanden; ein unbekannter Modus
    ebenso. So lädt eine falsch eingestellte Art niemanden ein, statt alle.
    """
    from apps.tenants.models import Membership

    qs = Membership.objects.filter(organization_id=meeting_type.organization_id, is_active=True, is_guest=False)
    modus = meeting_type.participants_mode
    if modus == "all":
        return qs
    if modus == "role":
        rolle = meeting_type.participants_role_id
        return qs.filter(roles=rolle) if rolle else qs.none()
    if modus == "committee":
        gremium = meeting_type.participants_committee_id
        return qs.filter(oparl_committees=gremium) if gremium else qs.none()
    if modus == "persons":
        return qs.filter(faction_meeting_types=meeting_type)
    return qs.none()


def _teilnehmend_q(membership: Membership) -> Q:
    """Bedingung auf ``FactionMeetingType``: Arten, zu deren Teilnehmenden das Mitglied gehört."""
    from apps.work.faction.models import FactionMeetingType

    if not membership.is_active or membership.is_guest:
        return Q(pk__in=[])
    auswahl = FactionMeetingType.participants.through.objects.filter(membership_id=membership.pk)
    return (
        Q(participants_mode="all")
        | Q(participants_mode="role", participants_role__in=membership.roles.values("pk"))
        | Q(participants_mode="committee", participants_committee__in=membership.oparl_committees.values("pk"))
        | Q(participants_mode="persons", pk__in=auswahl.values("factionmeetingtype_id"))
    )


def arten_mit_teilnahme(membership: Membership) -> QuerySet[FactionMeetingType]:
    """Arten der Organisation, zu deren Teilnehmenden das Mitglied gehört (unabhängig von der Sichtbarkeit)."""
    from apps.work.faction.models import FactionMeetingType

    return FactionMeetingType.objects.filter(_teilnehmend_q(membership), organization_id=membership.organization_id)


def ist_teilnehmend(membership: Membership, meeting_type: FactionMeetingType) -> bool:
    """Gehört das Mitglied zu den Teilnehmenden der Art?"""
    return teilnehmende(meeting_type).filter(pk=membership.pk).exists()


def sichtbare_arten(membership: Membership | None) -> QuerySet[FactionMeetingType]:
    """
    Arten, deren Sitzungen das Mitglied unabhängig von der einzelnen Sitzung sieht: alle außer „nur Teilnehmende“,
    dazu die Arten, zu deren Teilnehmenden es gehört. Mit ``faction.manage`` alle Arten der Organisation.
    """
    from apps.work.faction.models import FactionMeetingType

    if membership is None:
        return FactionMeetingType.objects.none()
    qs = FactionMeetingType.objects.filter(organization_id=membership.organization_id)
    if membership.has_permission(RECHT_ALLE):
        return qs
    return qs.filter(~Q(visibility=NUR_TEILNEHMENDE) | _teilnehmend_q(membership))


# =============================================================================
# Wer eine Sitzung sieht
# =============================================================================


def sichtbar_fuer(membership: Membership | None, qs: QuerySet[FactionMeeting]) -> QuerySet[FactionMeeting]:
    """
    Nur die Sitzungen aus ``qs``, die das Mitglied sehen darf; immer nur aus seiner Organisation.

    Eine Abfrage ohne Schleife: Arten aus :func:`sichtbare_arten` als Unterabfrage, dazu Sitzungen, zu denen es
    eingeladen ist (Anwesenheit) oder die es leitet bzw. protokolliert.
    """
    from apps.work.faction.models import FactionAttendance

    if membership is None:
        return qs.none()
    qs = qs.filter(organization_id=membership.organization_id)
    if membership.has_permission(RECHT_ALLE):
        return qs
    eingeladen = FactionAttendance.objects.filter(meeting=OuterRef("pk"), membership_id=membership.pk)
    return qs.filter(
        Q(meeting_type__in=sichtbare_arten(membership).values("pk"))
        | Q(chaired_by_id=membership.pk)
        | Q(minute_taker_id=membership.pk)
        | Exists(eingeladen)
    )


def darf_sehen(membership: Membership | None, meeting: FactionMeeting | None) -> bool:
    """Darf das Mitglied diese Sitzung sehen? Dieselbe Regel wie :func:`sichtbar_fuer`, für eine geladene Sitzung."""
    if membership is None or meeting is None or meeting.organization_id != membership.organization_id:
        return False
    meeting_type = meeting.meeting_type
    if meeting_type.visibility != NUR_TEILNEHMENDE:
        return True
    if membership.has_permission(RECHT_ALLE):
        return True
    if membership.pk in (meeting.chaired_by_id, meeting.minute_taker_id):
        return True
    if ist_teilnehmend(membership, meeting_type):
        return True
    return bool(meeting.attendances.filter(membership_id=membership.pk).exists())


def sitzung_oder_404(
    membership: Membership | None,
    organization: Organization,
    meeting_id: Any,
    qs: QuerySet[FactionMeeting] | None = None,
) -> FactionMeeting:
    """
    Sitzung der Organisation, die das Mitglied sehen darf, sonst 404 (nicht 403: niemand soll erfahren, dass es eine
    unsichtbare Sitzung gibt). ``qs`` erlaubt eigenes ``select_related``; die Art wird immer mitgeladen.
    """
    from django.core.exceptions import ValidationError
    from django.http import Http404

    from apps.work.faction.models import FactionMeeting

    basis = qs if qs is not None else FactionMeeting.objects.all()
    try:
        meeting = basis.select_related("meeting_type").filter(pk=meeting_id, organization=organization).first()
    except (ValueError, TypeError, ValidationError):
        meeting = None
    if meeting is None or not darf_sehen(membership, meeting):
        raise Http404("Keine Sitzung gefunden.")
    return meeting


def nur_sichtbar(meeting: FactionMeeting, memberships: Iterable[Membership]) -> list[Membership]:
    """Empfänger auf die beschränken, die die Sitzung sehen dürfen (Benachrichtigungen, E-Mails)."""
    return [m for m in memberships if darf_sehen(m, meeting)]


def empfaenger(meeting: FactionMeeting) -> QuerySet[Membership]:
    """
    Wen Mitteilungen zur ganzen Sitzung erreichen (Protokollversand, Genehmigung des Protokolls): die Teilnehmenden der
    Art und alle zur Sitzung Eingeladenen, jeweils aktive Mitglieder ohne Gastzugang. Bei der Standardart sind das wie
    bisher alle Mitglieder.
    """
    from apps.tenants.models import Membership

    eingeladen = meeting.attendances.filter(membership__isnull=False).values("membership_id")
    return Membership.objects.filter(
        Q(pk__in=teilnehmende(meeting.meeting_type).values("pk")) | Q(pk__in=eingeladen),
        organization_id=meeting.organization_id,
        is_active=True,
        is_guest=False,
    )


# =============================================================================
# Öffentliche Ausgaben
# =============================================================================


def oeffentlich(qs: QuerySet[FactionMeeting]) -> QuerySet[FactionMeeting]:
    """Nur Sitzungen öffentlicher Arten (öffentliche API, öffentliche Protokolle, Kalender)."""
    return qs.filter(meeting_type__visibility=OEFFENTLICH)


def standardart_oeffentlich(organization: Organization | Any) -> bool:
    """
    Schaltet eine Organisation die öffentliche API oder die öffentlichen Protokolle ein (Wechsel von aus nach an, siehe
    :func:`register`), wird ihre Standardart öffentlich, sofern sie „alle Mitglieder“ ist. Sonst blieben die
    Fraktionssitzungen dort leer, solange es keine Einstellung der Sitzungsarten gibt; für die Mitglieder selbst sehen
    „öffentlich“ und „alle Mitglieder“ gleich aus. „Nur Teilnehmende“ bleibt unverändert. Ausschalten ändert nichts.
    Organisation oder ihre ID; liefert, ob sich etwas geändert hat.
    """
    from apps.work.faction.models import FactionMeetingType

    standard = FactionMeetingType.default_for(organization)
    if standard.visibility != "members":
        return False
    return bool(FactionMeetingType.objects.filter(pk=standard.pk, visibility="members").update(visibility=OEFFENTLICH))
