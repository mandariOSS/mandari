# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesende Zugriffe für Organisation, Mitglieder, Team und Profil (Issue #160, Service-Layer).

Alle Querysets sind an die Organisation gebunden (bzw. an die Mitgliedschaft oder den
angemeldeten Benutzer, wo es fachlich um Konto-Daten geht). Views reichen nur noch
IDs, Filter und den Request-Kontext durch.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Count, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.models import TrustedDevice, User, UserSession
from apps.tenants.models import (
    AdministrationContact,
    CouncilParty,
    Membership,
    Organization,
    PartyGroup,
    Permission,
    Role,
    Topic,
    UserInvitation,
)
from insight_core.models import OParlBody, OParlMembership, OParlOrganization, OParlPerson

from .models import DataExport, MemberAbsence, MemberChangeRequest

if TYPE_CHECKING:
    from apps.session.models import SessionApplication
    from apps.work.faction.models import FactionMeetingSchedule
    from apps.work.motions.models import (
        DocumentFolder,
        FolderGuestShare,
        Motion,
        MotionShare,
        MotionTemplate,
        MotionType,
        OrganizationLetterhead,
    )

REVIEW_PERMISSIONS = ("members.edit", "organization.manage_roles")


# ---------------------------------------------------------------------------
# Typisierte Hüllen um untypisierte Helfer
# ---------------------------------------------------------------------------


def organization_bodies(organization: Organization) -> QuerySet[OParlBody]:
    """Alle verknüpften Körperschaften der Organisation (kann leer sein)."""
    return cast("QuerySet[OParlBody]", cast(Any, organization).get_all_bodies())


def permission_checker(membership: Membership | None) -> Any:
    """PermissionChecker (untypisiert) für Berechtigungsfragen in Views und Services."""
    from apps.common.permissions import PermissionChecker

    return cast(Any, PermissionChecker)(membership)


def can_review_change_requests(membership: Membership | None) -> bool:
    """Darf das Mitglied Änderungsanträge prüfen (members.edit, organization.manage_roles oder Admin)?"""
    checker = permission_checker(membership)
    return bool(any(checker.has_permission(code) for code in REVIEW_PERMISSIONS) or checker.is_admin())


def attach(obj: object, **attrs: Any) -> None:
    """Berechnete Attribute für Templates an ein Modellobjekt hängen."""
    for name, value in attrs.items():
        setattr(obj, name, value)


# ---------------------------------------------------------------------------
# Rollen und Berechtigungen
# ---------------------------------------------------------------------------


def roles_for_organization(organization: Organization) -> QuerySet[Role]:
    """Rollen der Organisation alphabetisch."""
    return Role.objects.filter(organization=organization).order_by("name")


def roles_with_member_count(organization: Organization) -> QuerySet[Role]:
    """Rollen mit Berechtigungen und Mitgliederzahl für die Rollenverwaltung."""
    return (
        Role.objects.filter(organization=organization)
        .prefetch_related("permissions")
        .annotate(member_count=Count("memberships"))
        .order_by("-priority", "name")
    )


def get_role_or_404(organization: Organization, role_id: Any) -> Role:
    """Rolle der Organisation oder 404."""
    return get_object_or_404(Role, id=role_id, organization=organization)


def role_name_exists(organization: Organization, name: str, *, exclude_id: Any = None) -> bool:
    """Gibt es bereits eine Rolle mit diesem Namen (optional ohne eine bestimmte Rolle)?"""
    queryset = Role.objects.filter(organization=organization, name=name)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)
    return queryset.exists()


def roles_by_ids(organization: Organization, role_ids: list[str]) -> QuerySet[Role]:
    """Rollen der Organisation zu einer ID-Liste (fremde IDs fallen weg)."""
    return Role.objects.filter(id__in=role_ids, organization=organization)


def contains_admin_role(roles: QuerySet[Role]) -> bool:
    """Enthält die Auswahl eine Administrator-Rolle?"""
    return roles.filter(is_admin=True).exists()


def role_permission_codes(role: Role) -> set[str]:
    """Codenames der Berechtigungen einer Rolle."""
    return set(role.permissions.values_list("codename", flat=True))


def permissions_by_codenames(codenames: list[str]) -> QuerySet[Permission]:
    """Berechtigungen aus dem Katalog zu Codenames."""
    return Permission.objects.filter(codename__in=codenames)


def all_permissions() -> QuerySet[Permission]:
    """Gesamter Berechtigungskatalog (für Antragsformulare)."""
    return Permission.objects.all().order_by("category", "codename")


def permission_matrix(member: Membership) -> dict[str, Any]:
    """
    Effektive Berechtigungen eines Mitglieds mit Herkunft.

    Drei Zustände je Berechtigung: aus Rollen (mit Rollennamen), individuell
    hinzugefügt, explizit verweigert (schlägt Rollen).
    """
    from apps.common.permissions import PERMISSIONS

    role_permission_sources: dict[str, list[str]] = {}
    for role in member.roles.all():
        if role.is_admin:
            for code in PERMISSIONS:
                role_permission_sources.setdefault(code, []).append(f"{role.name} (Administrator)")
        else:
            for perm in role.permissions.all():
                role_permission_sources.setdefault(perm.codename, []).append(role.name)

    individual_codes = set(member.individual_permissions.values_list("codename", flat=True))
    denied_codes = set(member.denied_permissions.values_list("codename", flat=True))
    return {
        "role_permission_sources": role_permission_sources,
        "individual_permission_codes": individual_codes,
        "denied_permission_codes": denied_codes,
        "effective_permission_codes": (set(role_permission_sources) | individual_codes) - denied_codes,
    }


# ---------------------------------------------------------------------------
# Mitglieder
# ---------------------------------------------------------------------------


def active_members(organization: Organization) -> QuerySet[Membership]:
    """Aktive Mitglieder ohne Gäste (die haben eine eigene Sektion)."""
    return (
        Membership.objects.filter(organization=organization, is_active=True, is_guest=False)
        .select_related("user")
        .prefetch_related("roles")
        .order_by("user__first_name", "user__last_name")
    )


def guests_with_share_counts(organization: Organization) -> list[Membership]:
    """Aktive Gäste mit Anzahl freigegebener Dokumente und Ordner (``shared_document_count`` u. a.)."""
    from apps.work.motions.models import FolderGuestShare, MotionShare

    guests = list(
        Membership.objects.filter(organization=organization, is_active=True, is_guest=True)
        .select_related("user")
        .order_by("user__first_name", "user__last_name")
    )
    user_ids = [g.user_id for g in guests]
    doc_counts = dict(
        MotionShare.objects.filter(motion__organization=organization, scope="user", user__in=user_ids)
        .exclude(motion__status="deleted")
        .values_list("user_id")
        .annotate(n=Count("id"))
        .values_list("user_id", "n")
    )
    folder_counts = dict(
        FolderGuestShare.objects.filter(folder__organization=organization, user__in=user_ids)
        .values_list("user_id")
        .annotate(n=Count("id"))
        .values_list("user_id", "n")
    )
    for guest in guests:
        attach(
            guest,
            shared_document_count=doc_counts.get(guest.user_id, 0),
            shared_folder_count=folder_counts.get(guest.user_id, 0),
        )
    return guests


def inactive_members(organization: Organization) -> QuerySet[Membership]:
    """Deaktivierte Mitgliedschaften."""
    return (
        Membership.objects.filter(organization=organization, is_active=False)
        .select_related("user")
        .prefetch_related("roles")
    )


def pending_registrations(organization: Organization) -> QuerySet[Membership]:
    """Ausstehende Selbstregistrierungen (inaktiv, ohne angenommene Einladung)."""
    return (
        Membership.objects.filter(organization=organization, is_active=False, invitation_accepted_at__isnull=True)
        .select_related("user")
        .order_by("-joined_at")
    )


def get_member_or_404(organization: Organization, member_id: Any, **filters: Any) -> Membership:
    """Mitgliedschaft der Organisation oder 404."""
    return get_object_or_404(Membership, id=member_id, organization=organization, **filters)


def get_team_member_or_404(organization: Organization, member_id: Any) -> Membership:
    """Aktives Mitglied mit Rollen und Gremien für das Teamprofil, sonst 404."""
    return get_object_or_404(
        Membership.objects.select_related("user").prefetch_related("roles", "oparl_committees"),
        id=member_id,
        organization=organization,
        is_active=True,
    )


def find_active_member(organization: Organization, member_id: Any) -> Membership | None:
    """Aktives Mitglied per ID oder ``None`` (z. B. Stellvertretung)."""
    return Membership.objects.filter(id=member_id, organization=organization, is_active=True).first()


def find_membership(organization: Organization, user: User) -> Membership | None:
    """Mitgliedschaft eines Benutzers in der Organisation (aktiv oder inaktiv)."""
    return Membership.objects.filter(user=user, organization=organization).first()


def find_user_by_email(email: str) -> User | None:
    """Benutzerkonto zu einer E-Mail-Adresse (Einladungsfluss)."""
    return User.objects.filter(email=email).first()


def admin_members(organization: Organization, *, exclude: Membership) -> list[Membership]:
    """Aktive Administratoren der Organisation (für Benachrichtigungen), ohne ein bestimmtes Mitglied."""
    return list(organization.memberships.filter(is_active=True, roles__is_admin=True).distinct().exclude(id=exclude.id))


def available_deputies(organization: Organization, membership: Membership) -> QuerySet[Membership]:
    """Andere aktive Mitglieder als mögliche Stellvertretung."""
    return (
        Membership.objects.filter(organization=organization, is_active=True)
        .exclude(id=membership.id)
        .select_related("user")
        .order_by("user__first_name", "user__last_name")
    )


def team_members(organization: Organization, search_query: str) -> QuerySet[Membership]:
    """Aktive Mitglieder für das Teamverzeichnis, optional nach Name/E-Mail gefiltert."""
    members = (
        Membership.objects.filter(organization=organization, is_active=True)
        .select_related("user")
        .prefetch_related("roles", "oparl_committees")
        .order_by("user__last_name", "user__first_name")
    )
    if search_query:
        members = members.filter(
            Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
            | Q(user__email__icontains=search_query)
        )
    return members


# ---------------------------------------------------------------------------
# Gremien, RIS-Personen, Fachgebiete
# ---------------------------------------------------------------------------


def active_committees(bodies: QuerySet[OParlBody], today: date) -> QuerySet[OParlOrganization]:
    """Gremien ohne Enddatum oder mit Enddatum in der Zukunft."""
    return (
        OParlOrganization.objects.filter(body__in=bodies)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .order_by("name")
    )


def inactive_committees(bodies: QuerySet[OParlBody], today: date) -> QuerySet[OParlOrganization]:
    """Gremien, deren Enddatum überschritten ist."""
    return OParlOrganization.objects.filter(body__in=bodies, end_date__lt=today).order_by("name")


def committees_by_ids(bodies: QuerySet[OParlBody], committee_ids: list[str]) -> QuerySet[OParlOrganization]:
    """Gremien der Körperschaften zu einer ID-Liste (fremde IDs fallen weg)."""
    return OParlOrganization.objects.filter(id__in=committee_ids, body__in=bodies)


def all_ris_organizations(organization: Organization) -> QuerySet[OParlOrganization]:
    """Alle OParl-Gremien der verknüpften Kommune(n)."""
    return OParlOrganization.objects.filter(body__in=organization_bodies(organization)).order_by("name")


def find_matching_persons(user: User, bodies: QuerySet[OParlBody]) -> list[OParlPerson]:
    """Findet OParl-Personen anhand des Benutzernamens (max. 5 Vorschläge)."""
    first = user.first_name.strip() if user.first_name else ""
    last = user.last_name.strip() if user.last_name else ""
    if not first and not last:
        return []

    query = Q(body__in=bodies)
    if first and last:
        query &= (
            Q(given_name__iexact=first, family_name__iexact=last)
            | Q(name__icontains=f"{first} {last}")
            | Q(name__icontains=f"{last}, {first}")
        )
    elif last:
        query &= Q(family_name__iexact=last) | Q(name__icontains=last)
    return list(OParlPerson.objects.filter(query)[:5])


def suggested_committees(
    oparl_person: OParlPerson, bodies: QuerySet[OParlBody], today: date
) -> list[OParlOrganization]:
    """Aktive Gremienmitgliedschaften einer OParl-Person innerhalb der Körperschaften."""
    memberships = (
        OParlMembership.objects.filter(person=oparl_person, organization__body__in=bodies)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .select_related("organization")
    )
    return [m.organization for m in memberships if m.organization.is_active]


def get_oparl_person_or_404(bodies: QuerySet[OParlBody], person_id: Any) -> OParlPerson:
    """RIS-Person innerhalb der Körperschaften oder 404."""
    return get_object_or_404(OParlPerson, id=person_id, body__in=bodies)


def committee_context(organization: Organization, member: Membership) -> dict[str, Any]:
    """Gremienzuordnung und RIS-Personen-Vorschläge für das Mitglieder-Detail."""
    bodies = organization_bodies(organization)
    if not bodies.exists():
        return {
            "has_body": False,
            "active_committees": [],
            "inactive_committees": [],
            "member_committees": [],
            "oparl_person": None,
            "suggested_committee_ids": [],
            "potential_oparl_persons": [],
        }
    today = timezone.now().date()
    context: dict[str, Any] = {
        "has_body": True,
        "active_committees": active_committees(bodies, today),
        "inactive_committees": inactive_committees(bodies, today),
        "member_committees": list(member.oparl_committees.values_list("id", flat=True)),
        "oparl_person": member.oparl_person,
        "suggested_committee_ids": [],
    }
    if member.oparl_person:
        context["suggested_committee_ids"] = [c.id for c in suggested_committees(member.oparl_person, bodies, today)]
    else:
        context["potential_oparl_persons"] = find_matching_persons(member.user, bodies)
    return context


def org_topics(organization: Organization) -> QuerySet[Topic]:
    """Themenkatalog (Fachgebiete) der Organisation."""
    return Topic.objects.filter(organization=organization)


def topics_by_ids(organization: Organization, topic_ids: list[str]) -> QuerySet[Topic]:
    """Fachgebiete der Organisation zu einer ID-Liste."""
    return Topic.objects.filter(id__in=topic_ids, organization=organization)


def expertise_ids(member: Membership) -> set[Any]:
    """IDs der Fachgebiete eines Mitglieds."""
    return set(member.expertise_topics.values_list("id", flat=True))


def profile_committee_context(organization: Organization, membership: Membership) -> dict[str, Any]:
    """Verfügbare, gefolgte und zugewiesene Gremien für „Meine Gremien“."""
    bodies = organization_bodies(organization)
    if bodies.exists():
        available = active_committees(bodies, timezone.now().date()).select_related("body")
    else:
        available = OParlOrganization.objects.none()
    return {
        "available_committees": available,
        "followed_ids": set(membership.followed_organizations.values_list("id", flat=True)),
        "assigned_committees": membership.oparl_committees.all().order_by("name"),
        "show_body_names": organization.has_multiple_bodies,
    }


# ---------------------------------------------------------------------------
# Gast-Freigaben und Einladungen
# ---------------------------------------------------------------------------


def guest_document_shares(organization: Organization, user: User) -> list[MotionShare]:
    """Persönliche Dokument-Freigaben eines Gastes (ohne gelöschte Dokumente)."""
    from apps.work.motions.models import MotionShare

    return list(
        MotionShare.objects.filter(motion__organization=organization, scope="user", user=user)
        .exclude(motion__status="deleted")
        .select_related("motion", "created_by")
        .order_by("-created_at")
    )


def guest_folder_shares(organization: Organization, user: User) -> list[FolderGuestShare]:
    """Ordner-Freigaben eines Gastes mit ``subfolder_count`` und ``document_count`` (rekursiv)."""
    from apps.work.motions.models import FolderGuestShare, Motion

    shares = list(
        FolderGuestShare.objects.filter(folder__organization=organization, user=user)
        .select_related("folder", "created_by")
        .order_by("-created_at")
    )
    for share in shares:
        descendants = share.folder.get_descendants()
        document_count = (
            Motion.objects.filter(organization=organization, folder__in=[share.folder, *descendants])
            .exclude(status="deleted")
            .count()
        )
        attach(share, subfolder_count=len(descendants), document_count=document_count)
    return shares


def guest_shared_documents(organization: Organization, user: User) -> list[Motion]:
    """Alle einem Gast persönlich freigegebenen Dokumente."""
    from apps.work.motions.models import MotionShare

    return [
        share.motion
        for share in MotionShare.objects.filter(
            motion__organization=organization, scope="user", user=user
        ).select_related("motion")
    ]


def guest_shared_folders(organization: Organization, user: User) -> list[DocumentFolder]:
    """Alle einem Gast freigegebenen Ordner."""
    from apps.work.motions.models import FolderGuestShare

    return [
        share.folder
        for share in FolderGuestShare.objects.filter(folder__organization=organization, user=user).select_related(
            "folder"
        )
    ]


def shareable_documents(organization: Organization, membership: Membership) -> QuerySet[Motion]:
    """Dokumente, die der Einladende freigeben darf (eigene + org-sichtbare, nicht gelöscht)."""
    from apps.work.motions.models import Motion

    return (
        Motion.objects.filter(organization=organization)
        .filter(Q(visibility="organization") | Q(author=membership))
        .exclude(status="deleted")
        .order_by("-updated_at")
    )


def shareable_folders(organization: Organization, membership: Membership) -> list[tuple[DocumentFolder, int]]:
    """
    Ordner, die der Einladende freigeben darf, als [(folder, depth)].

    Eine Ordner-Freigabe öffnet ALLE enthaltenen Dokumente (rekursiv, auch künftige) —
    daher nur Ordner, die der Einladende auch verwalten darf.
    """
    from apps.work.motions.views import _can_manage_folder, _flatten_folder_tree

    tree = cast("list[tuple[DocumentFolder, int]]", cast(Any, _flatten_folder_tree)(organization))
    return [(folder, depth) for folder, depth in tree if _can_manage_folder(membership, folder)]


def pending_invitations(organization: Organization) -> QuerySet[UserInvitation]:
    """Offene, nicht abgelaufene Einladungen."""
    return UserInvitation.objects.filter(
        organization=organization, accepted_at__isnull=True, expires_at__gt=timezone.now()
    ).order_by("-created_at")


def find_pending_invitation(organization: Organization, email: str) -> UserInvitation | None:
    """Offene, nicht abgelaufene Einladung für eine E-Mail-Adresse."""
    return UserInvitation.objects.filter(
        organization=organization, email=email, accepted_at__isnull=True, expires_at__gt=timezone.now()
    ).first()


def get_open_invitation_or_404(organization: Organization, invitation_id: Any) -> UserInvitation:
    """Noch nicht angenommene Einladung der Organisation oder 404."""
    return get_object_or_404(UserInvitation, id=invitation_id, organization=organization, accepted_at__isnull=True)


def find_invitation_by_token(token: str | None) -> UserInvitation | None:
    """Einladung per Token (öffentlicher Annahme-Fluss) inkl. Organisation, Einladendem und Rollen."""
    return (
        UserInvitation.objects.select_related("organization", "invited_by")
        .prefetch_related("roles")
        .filter(token=token)
        .first()
    )


# ---------------------------------------------------------------------------
# Änderungsanträge
# ---------------------------------------------------------------------------


def my_change_requests(organization: Organization, membership: Membership) -> QuerySet[MemberChangeRequest]:
    """Eigene Änderungsanträge (neueste zuerst, max. 20)."""
    return (
        MemberChangeRequest.objects.filter(requester=membership, organization=organization)
        .select_related("decided_by__user")
        .order_by("-created_at")[:20]
    )


def pending_change_requests(organization: Organization, *, exclude: Membership) -> QuerySet[MemberChangeRequest]:
    """Offene Anträge anderer Mitglieder (für Prüfende)."""
    return (
        MemberChangeRequest.objects.filter(organization=organization, status="pending")
        .exclude(requester=exclude)
        .select_related("requester__user")
        .order_by("created_at")
    )


def get_pending_change_request_or_404(
    organization: Organization, request_id: Any, **filters: Any
) -> MemberChangeRequest:
    """Offener Antrag der Organisation oder 404."""
    return get_object_or_404(MemberChangeRequest, id=request_id, organization=organization, status="pending", **filters)


def change_request_form_context(organization: Organization, membership: Membership) -> dict[str, Any]:
    """Auswahllisten (Rollen, Gremien, Berechtigungen) für neue Änderungsanträge."""
    context: dict[str, Any] = {
        "available_roles": roles_for_organization(organization),
        "current_role_ids": list(membership.roles.values_list("id", flat=True)),
        "available_permissions": all_permissions(),
    }
    bodies = organization_bodies(organization)
    if bodies.exists():
        context["available_committees"] = active_committees(bodies, timezone.now().date())
        context["current_committee_ids"] = list(membership.oparl_committees.values_list("id", flat=True))
    else:
        context["available_committees"] = []
        context["current_committee_ids"] = []
    return context


# ---------------------------------------------------------------------------
# Ratsfraktionen und Verwaltungskontakte
# ---------------------------------------------------------------------------


def council_parties(organization: Organization) -> QuerySet[CouncilParty]:
    """Ratsfraktionen der Organisation in Koalitionsreihenfolge."""
    return CouncilParty.objects.filter(organization=organization).order_by("coalition_order", "name")


def admin_contacts(organization: Organization) -> QuerySet[AdministrationContact]:
    """Verwaltungskontakte der Organisation."""
    return AdministrationContact.objects.filter(organization=organization)


def find_party(organization: Organization, party_id: Any) -> CouncilParty | None:
    """Ratsfraktion der Organisation oder ``None``."""
    return CouncilParty.objects.filter(id=party_id, organization=organization).first()


def party_short_name_exists(organization: Organization, short_name: str, *, exclude_id: Any = None) -> bool:
    """Ist der Kurzname in der Organisation bereits vergeben?"""
    queryset = CouncilParty.objects.filter(organization=organization, short_name=short_name)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)
    return queryset.exists()


# ---------------------------------------------------------------------------
# Profil, Konto, Datenschutz
# ---------------------------------------------------------------------------


def recent_sessions(user: User) -> QuerySet[UserSession]:
    """Letzte Sitzungen des Benutzers (Sicherheitsereignisse)."""
    return UserSession.objects.filter(user=user).order_by("-created_at")[:10]


def trusted_devices(user: User) -> QuerySet[TrustedDevice]:
    """Vertrauenswürdige Geräte mit gültiger Laufzeit."""
    return TrustedDevice.objects.filter(user=user, expires_at__gt=timezone.now()).order_by("-last_used_at")


def find_trusted_device(user: User, device_id: Any) -> TrustedDevice | None:
    """Vertrauenswürdiges Gerät des Benutzers oder ``None``."""
    return TrustedDevice.objects.filter(id=device_id, user=user).first()


def recent_exports(organization: Organization, membership: Membership) -> QuerySet[DataExport]:
    """Export-Historie des Mitglieds (max. 10)."""
    return DataExport.objects.filter(membership=membership, organization=organization).order_by("-created_at")[:10]


def has_active_export(organization: Organization, membership: Membership) -> bool:
    """Läuft bereits ein Export (ausstehend oder in Arbeit)?"""
    return DataExport.objects.filter(
        membership=membership, organization=organization, status__in=["pending", "processing"]
    ).exists()


def get_export_or_404(organization: Organization, membership: Membership, export_id: Any, **filters: Any) -> DataExport:
    """Datenexport des Mitglieds oder 404."""
    return get_object_or_404(DataExport, id=export_id, membership=membership, organization=organization, **filters)


def profile_settings(user: User) -> dict[str, Any]:
    """Sichtbarkeits- und Kontaktangaben aus ``User.settings["profile"]`` mit Standardwerten."""
    user_settings = user.settings or {}
    profile = user_settings.get("profile", {})
    return {
        "bio": profile.get("bio", ""),
        "show_email": profile.get("show_email", True),
        "show_phone": profile.get("show_phone", False),
        "preferred_contact": profile.get("preferred_contact", "email"),
        "contact_signal": profile.get("contact_signal", ""),
    }


# ---------------------------------------------------------------------------
# Abwesenheiten
# ---------------------------------------------------------------------------


def absence_context(organization: Organization, membership: Membership, today: date) -> dict[str, Any]:
    """Eigene Abwesenheiten (aktuell, aktiv, vergangen) und Stellvertretungen für andere."""
    my_absences = MemberAbsence.objects.filter(membership=membership, organization=organization)
    return {
        "current_absence": my_absences.filter(is_active=True, start_date__lte=today, end_date__gte=today).first(),
        "active_absences": my_absences.filter(is_active=True, end_date__gte=today)
        .select_related("deputy__user")
        .order_by("start_date"),
        "past_absences": my_absences.filter(end_date__lt=today)
        .select_related("deputy__user")
        .order_by("-start_date")[:10],
        "deputy_for": MemberAbsence.objects.filter(
            deputy=membership, organization=organization, is_active=True, end_date__gte=today
        )
        .select_related("membership__user")
        .order_by("start_date"),
    }


def current_absences_by_member(organization: Organization, today: date) -> dict[Any, MemberAbsence]:
    """Aktuelle Abwesenheiten aller Mitglieder, nach Mitgliedschafts-ID."""
    absences = MemberAbsence.objects.filter(
        organization=organization, is_active=True, start_date__lte=today, end_date__gte=today
    ).select_related("deputy__user")
    return {a.membership_id: a for a in absences}


def current_absence_for(organization: Organization, member: Membership, today: date) -> MemberAbsence | None:
    """Aktuelle Abwesenheit eines Mitglieds oder ``None``."""
    return (
        MemberAbsence.objects.filter(
            membership=member, organization=organization, is_active=True, start_date__lte=today, end_date__gte=today
        )
        .select_related("deputy__user")
        .first()
    )


def get_absence_or_404(organization: Organization, membership: Membership, absence_id: Any) -> MemberAbsence:
    """Eigene Abwesenheit oder 404."""
    return get_object_or_404(MemberAbsence, id=absence_id, membership=membership, organization=organization)


# ---------------------------------------------------------------------------
# Aktivitätsübersicht
# ---------------------------------------------------------------------------


def activity_statistics(organization: Organization, membership: Membership) -> dict[str, int]:
    """Kennzahlen des Mitglieds: Aufgaben, Anträge, Kommentare, Fraktionssitzungen, Vorbereitungen."""
    from apps.work.faction.models import FactionAttendance, FactionMeeting
    from apps.work.meetings.models import AgendaItemNote, MeetingPreparation
    from apps.work.motions.models import Motion, MotionComment
    from apps.work.tasks.models import Task

    my_tasks = Task.objects.filter(organization=organization).filter(
        Q(created_by=membership) | Q(assigned_to=membership)
    )
    attendance = FactionAttendance.objects.filter(membership=membership, meeting__organization=organization)
    return {
        "tasks_total": my_tasks.count(),
        "tasks_completed": my_tasks.filter(is_completed=True).count(),
        "tasks_open": my_tasks.filter(is_completed=False).count(),
        "motions_authored": Motion.objects.filter(organization=organization, author=membership).count(),
        "motion_comments": MotionComment.objects.filter(motion__organization=organization, author=membership).count(),
        "meetings_present": attendance.filter(status="present").count(),
        "meetings_excused": attendance.filter(status="excused").count(),
        "meetings_absent": attendance.filter(status="absent").count(),
        "meetings_total": FactionMeeting.objects.filter(organization=organization, status="completed").count(),
        "preparations": MeetingPreparation.objects.filter(organization=organization, membership=membership).count(),
        "agenda_notes": AgendaItemNote.objects.filter(organization=organization, author=membership).count(),
    }


def activity_timeline(organization: Organization, membership: Membership, *, limit: int = 20) -> list[dict[str, Any]]:
    """Letzte Aktivitäten (Aufgaben, Anträge, Anwesenheiten, Vorbereitungen) nach Datum absteigend."""
    from apps.work.faction.models import FactionAttendance
    from apps.work.meetings.models import MeetingPreparation
    from apps.work.motions.models import Motion
    from apps.work.tasks.models import Task

    timeline: list[dict[str, Any]] = []
    my_tasks = Task.objects.filter(organization=organization).filter(
        Q(created_by=membership) | Q(assigned_to=membership)
    )
    for t in my_tasks.order_by("-updated_at")[:5]:
        timeline.append(
            {
                "date": t.updated_at,
                "icon": "check-square",
                "color": "green" if t.is_completed else "blue",
                "title": f"Aufgabe: {t.title}",
                "detail": "Erledigt" if t.is_completed else f"Status: {t.get_status_display()}",
            }
        )
    for m in Motion.objects.filter(organization=organization, author=membership).order_by("-updated_at")[:5]:
        timeline.append(
            {
                "date": m.updated_at,
                "icon": "file-text",
                "color": "indigo",
                "title": f"Antrag: {m.title}",
                "detail": m.get_status_display(),
            }
        )
    recent_attendance = (
        FactionAttendance.objects.filter(membership=membership, meeting__organization=organization)
        .select_related("meeting")
        .order_by("-meeting__start")[:5]
    )
    for a in recent_attendance:
        timeline.append(
            {
                "date": a.meeting.start if a.meeting.start else a.meeting.created_at,
                "icon": "users",
                "color": "purple",
                "title": f"Sitzung: {a.meeting.title}",
                "detail": a.get_status_display(),
            }
        )
    recent_preps = MeetingPreparation.objects.filter(organization=organization, membership=membership).order_by(
        "-updated_at"
    )[:5]
    for p in recent_preps:
        timeline.append(
            {
                "date": p.updated_at,
                "icon": "clipboard-check",
                "color": "amber",
                "title": "Sitzungsvorbereitung",
                "detail": f"Aktualisiert am {p.updated_at.strftime('%d.%m.%Y')}",
            }
        )

    now = timezone.now()
    timeline.sort(key=lambda x: x["date"] if x["date"] else now, reverse=True)
    return timeline[:limit]


# ---------------------------------------------------------------------------
# Organisationseinstellungen
# ---------------------------------------------------------------------------


def linked_bodies(organization: Organization) -> QuerySet[OParlBody]:
    """Weitere Kommunen neben der Heimat-Kommune (read-only)."""
    bodies = organization_bodies(organization)
    if organization.body_id:
        bodies = bodies.exclude(pk=organization.body_id)
    return bodies.order_by("name")


def org_parties(organization: Organization) -> QuerySet[PartyGroup]:
    """Alle Parteien der Organisation (M2M + primäre Parteigruppe)."""
    return cast("QuerySet[PartyGroup]", cast(Any, organization).get_all_parties()).order_by("name")


def available_parties() -> QuerySet[PartyGroup]:
    """Aktive Parteigruppen zur Auswahl."""
    return PartyGroup.objects.filter(is_active=True).order_by("name")


def parties_by_ids(party_ids: list[str]) -> list[PartyGroup]:
    """Aktive Parteigruppen zu einer ID-Liste."""
    return list(PartyGroup.objects.filter(id__in=party_ids, is_active=True))


def find_party_group_by_name(name: str) -> PartyGroup | None:
    """Parteigruppe per Name (ohne Groß-/Kleinschreibung)."""
    return PartyGroup.objects.filter(name__iexact=name).first()


def faction_schedules(organization: Organization) -> QuerySet[FactionMeetingSchedule]:
    """Sitzungsreihen mit Ausnahmen und Ausfallregeln."""
    from apps.work.faction.models import FactionMeetingSchedule

    return (
        FactionMeetingSchedule.objects.filter(organization=organization)
        .prefetch_related("exceptions", "suspension_rules__ris_organization")
        .order_by("weekday", "time")
    )


def find_schedule(organization: Organization, schedule_id: Any) -> FactionMeetingSchedule | None:
    """Sitzungsreihe der Organisation oder ``None``."""
    from apps.work.faction.models import FactionMeetingSchedule

    return FactionMeetingSchedule.objects.filter(id=schedule_id, organization=organization).first()


def find_ris_organization(organization: Organization, ris_organization_id: Any) -> OParlOrganization | None:
    """OParl-Gremium der verknüpften Kommune(n) oder ``None``."""
    return OParlOrganization.objects.filter(id=ris_organization_id, body__in=organization_bodies(organization)).first()


def document_settings_context(organization: Organization) -> dict[str, Any]:
    """Dokumenttypen, Vorlagen und Briefköpfe inkl. Zähler."""
    from apps.work.motions.models import MotionTemplate, MotionType, OrganizationLetterhead

    motion_types: QuerySet[MotionType] = MotionType.objects.filter(organization=organization).order_by(
        "sort_order", "name"
    )
    templates: QuerySet[MotionTemplate] = (
        MotionTemplate.objects.filter(organization=organization).select_related("motion_type").order_by("name")
    )
    letterheads: QuerySet[OrganizationLetterhead] = OrganizationLetterhead.objects.filter(
        organization=organization
    ).order_by("name")
    return {
        "motion_types": motion_types,
        "templates": templates,
        "letterheads": letterheads,
        "type_count": motion_types.count(),
        "template_count": templates.count(),
        "letterhead_count": letterheads.count(),
    }


def submitted_applications(organization: Organization) -> QuerySet[SessionApplication]:
    """An die Verwaltung eingereichte Anträge (neueste zuerst, max. 20)."""
    from apps.session.models import SessionApplication

    return (
        SessionApplication.objects.filter(submitting_organization=organization)
        .select_related("tenant", "work_motion")
        .order_by("-submitted_at")[:20]
    )


def registration_roles(organization: Organization) -> QuerySet[Role]:
    """Rollen für das Standardrollen-Dropdown der Selbstregistrierung."""
    return organization.roles.order_by("priority", "name")


def find_role(organization: Organization, role_id: Any) -> Role | None:
    """Rolle der Organisation oder ``None``."""
    return Role.objects.filter(id=role_id, organization=organization).first()
