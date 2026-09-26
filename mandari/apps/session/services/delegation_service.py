# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertretungen im Session RIS (Issue #222).

Eine Vertretung gilt je Session-Nutzer für einen Zeitraum (erster bis einschließlich letzter
Tag) und einen Umfang:

- **Freigaben und Genehmigungen**: Die Vertretung darf Vorlagen freigeben und Niederschriften
  genehmigen, soweit die vertretene Person es selbst darf (``DELEGABLE_PERMISSIONS``). Sichtrechte
  werden nicht übertragen: Nichtöffentliches gibt nur frei, wer es auch selbst sehen darf.
- **Arbeitsvorrat**: Offene Mitzeichnungen der Ämter der vertretenen Person erscheinen im
  eigenen Arbeitsvorrat und lassen sich dort in Vertretung erledigen.
- **Benachrichtigungen**: Freigabe-Aufforderungen und persönliche Mitteilungen an die
  vertretene Person gehen in Kopie an die Vertretung.

Regeln gegen Rechteausweitung:

- Wirksam nur im Zeitraum, nur im selben Mandanten, nur solange beide Konten aktiv sind.
- Nie mehr, als die vertretene Person selbst darf – gerechnet aus ihren eigenen Rollen
  (:func:`apps.session.permissions.role_permissions`), nie aus ihren Vertretungen.
  Damit gibt es keine Kettenvertretung.
- Wer eine Vertretung einträgt, überträgt keine Freigaberechte, die er selbst nicht hat, und trägt
  sich nicht selbst als Vertretung ein – beides nur als Administrator.
- Das Vier-Augen-Prinzip gilt für Handelnde und vertretene Person
  (:mod:`apps.session.services.four_eyes_service`).
- Jede Handlung aus einer Vertretung steht im Audit-Log mit „in Vertretung für …“.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Exists, F, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.session.permissions import grantable_permissions, is_admin_user, role_permissions

if TYPE_CHECKING:
    from apps.session.models import SessionDelegation, SessionTenant, SessionUser

SCOPE_APPROVALS = "approvals"
SCOPE_WORKLIST = "worklist"
SCOPE_NOTIFICATIONS = "notifications"

#: Umfang einer Vertretung mit Erläuterung für die Oberfläche
SCOPES: dict[str, tuple[str, str]] = {
    SCOPE_APPROVALS: (
        "Freigaben und Genehmigungen",
        "Vorlagen freigeben und Niederschriften genehmigen – soweit die vertretene Person es darf",
    ),
    SCOPE_WORKLIST: (
        "Arbeitsvorrat",
        "Offene Mitzeichnungen der Ämter der vertretenen Person erscheinen im eigenen Arbeitsvorrat",
    ),
    SCOPE_NOTIFICATIONS: (
        "Benachrichtigungen",
        "E-Mails an die vertretene Person gehen in Kopie an die Vertretung",
    ),
}

#: Rechte, die eine Vertretung im Umfang „Freigaben und Genehmigungen“ übernehmen kann
DELEGABLE_PERMISSIONS = frozenset({"approve_papers", "approve_protocols"})

#: Längste Vertretung – Dauerregelungen gehören in die Rollen
MAX_DAYS = 366

_CACHE_ATTR = "_session_delegations_cache"

#: Annotation am Session-Nutzer: Wirkt heute eine Vertretung, in der die Person vertritt?
ANNOTATION = "has_active_delegations"


class DelegationError(ValueError):
    """Vertretung kann so nicht eingetragen werden (Meldung für die Oberfläche)."""


def active_q(day: date) -> Q:
    """Filter für Vertretungen, die an diesem Tag wirken."""
    return Q(revoked_at__isnull=True, start_date__lte=day, end_date__gte=day)


def _same_tenant_q() -> Q:
    """Vertretung, vertretene Person und Vertretung gehören zu einem Mandanten."""
    return Q(principal__tenant_id=F("tenant_id"), deputy__tenant_id=F("tenant_id"))


def annotate_active(queryset: QuerySet[Any]) -> QuerySet[Any]:
    """
    Session-Nutzer vorab markieren, ob heute eine Vertretung wirkt (``ANNOTATION``).

    Läuft als Unterabfrage in der ohnehin nötigen Abfrage des Nutzers – ohne Vertretung kostet
    die Rechteprüfung so keine zusätzliche Abfrage (Performance-Budgets).
    """
    from apps.session.models import SessionDelegation

    active = SessionDelegation.objects.filter(
        active_q(timezone.localdate()), deputy_id=OuterRef("pk"), tenant_id=OuterRef("tenant_id")
    )
    return cast("QuerySet[Any]", queryset.annotate(**{ANNOTATION: Exists(active)}))


def incoming(deputy: SessionUser, *, day: date | None = None) -> list[SessionDelegation]:
    """
    Aktive Vertretungen, in denen diese Person vertritt.

    Das Ergebnis wird am Nutzerobjekt zwischengespeichert – es wird je Anfrage neu geladen,
    so kostet die Rechteprüfung auf jeder Seite höchstens eine Abfrage, ohne Vertretung
    (Annotation aus :func:`annotate_active`) gar keine.
    """
    from apps.session.models import SessionDelegation

    if deputy is None or deputy.pk is None:
        return []
    if day is None and getattr(deputy, ANNOTATION, None) is False:
        return []
    day = day or timezone.localdate()
    cached = getattr(deputy, _CACHE_ATTR, None)
    if cached is not None and cached[0] == day:
        return list(cached[1])
    items = list(
        SessionDelegation.objects.filter(
            active_q(day),
            _same_tenant_q(),
            tenant_id=deputy.tenant_id,
            deputy_id=deputy.pk,
            principal__is_active=True,
        )
        .select_related("principal__user")
        .prefetch_related("principal__roles")
        .order_by("start_date", "created_at")
    )
    setattr(deputy, _CACHE_ATTR, (day, items))
    return list(items)


def forget(session_user: Any) -> None:
    """Zwischenspeicher verwerfen (nach dem Eintragen oder Aufheben einer Vertretung)."""
    if session_user is not None and hasattr(session_user, _CACHE_ATTR):
        delattr(session_user, _CACHE_ATTR)


def principals(deputy: SessionUser, scope: str, *, day: date | None = None) -> list[SessionUser]:
    """Vertretene Personen, deren Vertretung heute im Umfang ``scope`` wirkt."""
    from apps.session.models import SessionDelegation

    field = SessionDelegation.SCOPE_FIELDS[scope]
    return [d.principal for d in incoming(deputy, day=day) if getattr(d, field)]


def delegated_permissions(deputy: SessionUser, own: Iterable[str]) -> set[str]:
    """
    Freigaberechte, die diese Person heute ausschließlich aus Vertretungen hat.

    Nur Rechte aus ``DELEGABLE_PERMISSIONS``, nur wenn die vertretene Person sie aus ihren
    eigenen Rollen hat. Wer das Recht selbst hat, handelt im eigenen Namen.
    """
    missing = DELEGABLE_PERMISSIONS - set(own)
    if not missing:
        return set()
    result: set[str] = set()
    for principal in principals(deputy, SCOPE_APPROVALS):
        result |= missing & role_permissions(principal)
    return result


def principals_with_permission(deputy: SessionUser, permission: str) -> list[SessionUser]:
    """Vertretene Personen, aus deren Vertretung ``permission`` heute zur Verfügung steht."""
    if permission not in DELEGABLE_PERMISSIONS:
        return []
    return [p for p in principals(deputy, SCOPE_APPROVALS) if permission in role_permissions(p)]


def notification_deputies(
    people: Iterable[SessionUser], *, day: date | None = None
) -> list[tuple[SessionUser, SessionUser]]:
    """Paare (Vertretung, vertretene Person) mit heute wirksamem Umfang „Benachrichtigungen“."""
    from apps.session.models import SessionDelegation

    ids = [p.pk for p in people if p is not None and p.pk is not None]
    if not ids:
        return []
    day = day or timezone.localdate()
    qs = (
        SessionDelegation.objects.filter(
            active_q(day),
            _same_tenant_q(),
            scope_notifications=True,
            principal_id__in=ids,
            deputy__is_active=True,
            deputy__user__is_active=True,
        )
        .select_related("deputy__user", "principal__user")
        .prefetch_related("deputy__roles")
        .order_by("start_date", "created_at")
    )
    return [(d.deputy, d.principal) for d in qs]


def overlapping_absences(session_user: SessionUser, start: date, end: date) -> list[SessionDelegation]:
    """Eigene Abwesenheiten (die Person wird selbst vertreten), die den Zeitraum überschneiden."""
    from apps.session.models import SessionDelegation

    return list(
        SessionDelegation.objects.filter(
            tenant_id=session_user.tenant_id,
            principal_id=session_user.pk,
            revoked_at__isnull=True,
            start_date__lte=end,
            end_date__gte=start,
        ).order_by("start_date")
    )


def _check_scope_of_creator(
    created_by: SessionUser | None, *, principal: SessionUser, deputy: SessionUser, scopes: set[str]
) -> None:
    """Keine Rechteausweitung über Vertretungen (nur Administratoren tragen beliebig ein)."""
    if created_by is None or is_admin_user(created_by):
        return
    if deputy.pk == created_by.pk:
        raise DelegationError(
            "Sich selbst als Vertretung eintragen kann nur ein Administrator – bitte eine zweite Person "
            "mit der Benutzerverwaltung darum bitten."
        )
    if SCOPE_APPROVALS in scopes:
        gained = (DELEGABLE_PERMISSIONS & role_permissions(principal)) - role_permissions(deputy)
        if gained - grantable_permissions(created_by):
            raise DelegationError(
                "Freigaberechte, die Sie selbst nicht haben, überträgt nur ein Administrator per Vertretung."
            )


def create(
    tenant: SessionTenant,
    *,
    principal: SessionUser,
    deputy: SessionUser,
    start_date: date,
    end_date: date,
    scopes: Iterable[str],
    created_by: SessionUser | None,
) -> SessionDelegation:
    """Vertretung prüfen und eintragen; Fehler als :class:`DelegationError` mit Meldung."""
    from apps.session.models import SessionDelegation

    chosen = {scope for scope in scopes if scope in SCOPES}
    if principal.tenant_id != tenant.pk or deputy.tenant_id != tenant.pk:
        raise DelegationError("Vertretungen sind nur innerhalb des eigenen Mandanten möglich.")
    if principal.pk == deputy.pk:
        raise DelegationError("Eine Person kann sich nicht selbst vertreten.")
    if not principal.is_active or not deputy.is_active:
        raise DelegationError("Vertretungen sind nur zwischen aktiven Nutzern möglich.")
    if end_date < start_date:
        raise DelegationError("Das Ende der Vertretung liegt vor ihrem Beginn.")
    if end_date < timezone.localdate():
        raise DelegationError("Der Zeitraum liegt vollständig in der Vergangenheit.")
    if (end_date - start_date) >= timedelta(days=MAX_DAYS):
        raise DelegationError("Eine Vertretung gilt höchstens ein Jahr. Dauerhafte Aufgaben bitte über Rollen regeln.")
    if not chosen:
        raise DelegationError("Bitte mindestens einen Umfang der Vertretung wählen.")
    _check_scope_of_creator(created_by, principal=principal, deputy=deputy, scopes=chosen)
    overlap = SessionDelegation.objects.filter(
        tenant=tenant,
        principal=principal,
        deputy=deputy,
        revoked_at__isnull=True,
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists()
    if overlap:
        raise DelegationError("Für diesen Zeitraum ist diese Vertretung bereits eingetragen.")

    delegation = SessionDelegation.objects.create(
        tenant=tenant,
        principal=principal,
        deputy=deputy,
        start_date=start_date,
        end_date=end_date,
        scope_approvals=SCOPE_APPROVALS in chosen,
        scope_worklist=SCOPE_WORKLIST in chosen,
        scope_notifications=SCOPE_NOTIFICATIONS in chosen,
        created_by=created_by,
    )
    forget(deputy)
    return delegation


def revoke(delegation: SessionDelegation, *, by: SessionUser | None) -> bool:
    """Vertretung aufheben (bleibt als Nachweis erhalten). False, wenn schon aufgehoben."""
    if delegation.revoked_at is not None:
        return False
    delegation.revoked_at = timezone.now()
    delegation.revoked_by = by
    delegation.save(update_fields=["revoked_at", "revoked_by"])
    forget(delegation.deputy)
    return True


def overview(tenant: SessionTenant, *, history: int = 20) -> dict[str, list[SessionDelegation]]:
    """Aktive, geplante und zuletzt beendete bzw. aufgehobene Vertretungen für die Administration."""
    from apps.session.models import SessionDelegation

    today = timezone.localdate()
    qs = SessionDelegation.objects.filter(tenant=tenant).select_related(
        "principal__user", "deputy__user", "created_by__user", "revoked_by__user"
    )
    return {
        "active": list(qs.filter(active_q(today)).order_by("end_date", "start_date")),
        "planned": list(qs.filter(revoked_at__isnull=True, start_date__gt=today).order_by("start_date")),
        "past": list(qs.filter(Q(revoked_at__isnull=False) | Q(end_date__lt=today)).order_by("-end_date")[:history]),
    }
