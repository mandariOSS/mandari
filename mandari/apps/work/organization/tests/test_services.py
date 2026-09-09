# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service- und Selector-Tests für Organisation, Mitglieder und Profil (Issue #160, Service-Layer).

Geprüft werden die Fachregeln unabhängig von den Views: Einladungs- und Gastfluss,
Rechte-Eskalationsschutz im Mitglieder-Detail und bei Änderungsanträgen, Rollen,
Ratsfraktionen, Einstellungen (E-Mail, API, Registrierung, Stammdaten) und Abwesenheiten.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib.messages import constants as message_levels
from django.utils import timezone

from apps.common.tests.factories import PermissionFactory
from apps.tenants.models import CouncilParty, Membership, Role, UserInvitation
from apps.work.notifications.models import Notification
from apps.work.organization import selectors, services
from apps.work.organization.models import MemberAbsence, MemberChangeRequest
from apps.work.organization.services import ServiceError


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.edit"], email="admin@example.org", is_admin=True)


@pytest.fixture
def editor(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.edit"], email="editor@example.org")


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="mitglied@example.org")


# ---------------------------------------------------------------------------
# Einladungen und Gäste
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_invite_member_creates_invitation_and_blocks_duplicates(org: Any, admin: Any, member: Any) -> None:
    role = Role.objects.create(organization=org, name="Beisitz")

    message = services.invite_member(org, admin.user, "neu@example.org", [str(role.id)], "Willkommen")
    assert message == "Einladung an neu@example.org wurde versendet."
    invitation = UserInvitation.objects.get(organization=org, email="neu@example.org")
    assert list(invitation.roles.all()) == [role]
    assert selectors.find_pending_invitation(org, "neu@example.org") == invitation
    assert list(selectors.pending_invitations(org)) == [invitation]

    with pytest.raises(ServiceError) as excinfo:
        services.invite_member(org, admin.user, "neu@example.org", [], "")
    assert excinfo.value.level == message_levels.WARNING

    with pytest.raises(ServiceError) as excinfo:
        services.invite_member(org, admin.user, member.user.email, [], "")
    assert "bereits Mitglied" in str(excinfo.value)

    # Inaktive Mitgliedschaft wird reaktiviert statt neu eingeladen
    member.is_active = False
    member.save()
    assert (
        services.invite_member(org, admin.user, member.user.email, [], "") == f"{member.user.email} wurde reaktiviert."
    )
    member.refresh_from_db()
    assert member.is_active is True


@pytest.mark.django_db
def test_accept_invitation_creates_membership_with_roles_and_owner(org: Any, admin: Any, make_member: Any) -> None:
    from apps.common.tests.factories import UserFactory

    role = Role.objects.create(organization=org, name="Neu")
    invitation = UserInvitation.create_for_organization(
        organization=org, email="gast@example.org", invited_by=admin.user, roles=[role]
    )
    user_factory: Any = UserFactory
    user = user_factory(email="gast@example.org")
    org.owner = None
    org.save()

    message = services.accept_invitation(invitation, user)
    assert message == f"Willkommen bei {org.name}!"
    membership = Membership.objects.get(user=user, organization=org)
    assert list(membership.roles.all()) == [role]
    assert membership.invited_by == admin.user
    org.refresh_from_db()
    assert org.owner == user
    invitation.refresh_from_db()
    assert invitation.accepted_by == user
    assert invitation.accepted_at is not None

    assert services.accept_invitation(invitation, user) == "Sie sind bereits Mitglied dieser Organisation."


@pytest.mark.django_db
def test_invite_guest_creates_account_and_respects_limit(org: Any, admin: Any, member: Any) -> None:
    result = services.invite_guest(
        org, admin, email="gast@example.org", note="Hallo", share_level="edit", document_ids=[], folder_ids=[]
    )
    assert result.message == "Gastzugang für gast@example.org wurde eingerichtet."
    guest = Membership.objects.get(organization=org, user__email="gast@example.org")
    assert guest.is_guest is True
    assert guest.invited_by == admin.user
    assert not guest.user.has_usable_password()
    assert [g.id for g in selectors.guests_with_share_counts(org)] == [guest.id]
    # shared_document_count kommt als Annotation aus dem Selector, mypy kennt sie nicht
    guest_row = cast(Any, selectors.guests_with_share_counts(org)[0])
    assert guest_row.shared_document_count == 0

    with pytest.raises(ServiceError) as excinfo:
        services.invite_guest(
            org, admin, email=member.user.email, note="", share_level="view", document_ids=[], folder_ids=[]
        )
    assert excinfo.value.level == message_levels.WARNING

    org.guest_limit = 1
    org.save()
    with pytest.raises(ServiceError) as excinfo:
        services.invite_guest(
            org, admin, email="zweiter@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
        )
    assert "Gast-Limit" in str(excinfo.value)
    assert not Membership.objects.filter(organization=org, user__email="zweiter@example.org").exists()

    # Reaktivierung eines Gastes unterliegt demselben Limit
    guest.is_active = False
    guest.save()
    services.invite_guest(
        org, admin, email="dritter@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )
    with pytest.raises(ServiceError):
        services.reactivate_member(org, guest)


# ---------------------------------------------------------------------------
# Mitglieder-Detail: Rechte-Eskalationsschutz
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_member_roles_guards_against_escalation(org: Any, admin: Any, editor: Any, member: Any) -> None:
    admin_role = Role.objects.create(organization=org, name="Admin-Rolle", is_admin=True)
    plain_role = Role.objects.create(organization=org, name="Einfach")

    with pytest.raises(ServiceError, match="Eigene Rollen"):
        services.update_member_roles(org, editor, editor, [str(plain_role.id)])
    with pytest.raises(ServiceError, match="Administrator-Rolle"):
        services.update_member_roles(org, member, editor, [str(admin_role.id)])

    services.update_member_roles(org, member, editor, [str(plain_role.id)])
    assert list(member.roles.all()) == [plain_role]

    services.update_member_roles(org, member, admin, [str(admin_role.id)])
    assert list(member.roles.all()) == [admin_role]
    matrix = selectors.permission_matrix(member)
    assert "organization.admin" in matrix["effective_permission_codes"]
    assert matrix["role_permission_sources"]["organization.admin"] == ["Admin-Rolle (Administrator)"]

    with pytest.raises(ServiceError, match="Gast"):
        services.update_member_roles(org, Membership(is_guest=True, user=member.user, organization=org), admin, [])


@pytest.mark.django_db
def test_update_member_permissions_only_for_admins(org: Any, admin: Any, editor: Any, member: Any) -> None:
    PermissionFactory(codename="motions.share")  # type: ignore[no-untyped-call]
    with pytest.raises(ServiceError, match="Administratoren"):
        services.update_member_permissions(member, editor, ["motions.share"], [])

    services.update_member_permissions(member, admin, ["motions.share"], ["dashboard.view"])
    matrix = selectors.permission_matrix(member)
    assert matrix["individual_permission_codes"] == {"motions.share"}
    assert matrix["denied_permission_codes"] == {"dashboard.view"}
    assert "dashboard.view" not in matrix["effective_permission_codes"]
    assert "motions.share" in matrix["effective_permission_codes"]


@pytest.mark.django_db
def test_member_lifecycle_protects_owner_and_self(org: Any, admin: Any, editor: Any, member: Any) -> None:
    org.owner = member.user
    org.save()
    with pytest.raises(ServiceError, match="Eigentümer"):
        services.deactivate_member(org, member, editor.user)
    with pytest.raises(ServiceError, match="Eigentümer"):
        services.remove_member(org, member, editor.user)
    with pytest.raises(ServiceError, match="selbst"):
        services.deactivate_member(org, editor, editor.user)
    with pytest.raises(ServiceError, match="aktuelle Eigentümer"):
        services.transfer_ownership(org, editor, admin.user)

    services.transfer_ownership(org, admin, member.user)
    org.refresh_from_db()
    assert org.owner == admin.user

    services.deactivate_member(org, member, editor.user)
    member.refresh_from_db()
    assert member.is_active is False
    assert list(selectors.inactive_members(org)) == [member]

    with pytest.raises(ServiceError, match="eigenen Vereidigungsstatus"):
        services.update_sworn_in(editor, editor, True)
    services.update_sworn_in(editor, admin, True)
    editor.refresh_from_db()
    assert editor.is_sworn_in is True

    name = services.remove_member(org, member, editor.user)
    assert name == member.user.email
    assert not Membership.objects.filter(id=member.id).exists()


@pytest.mark.django_db
def test_committee_context_without_linked_body(org: Any, member: Any) -> None:
    context = selectors.committee_context(org, member)
    assert context["has_body"] is False
    assert context["active_committees"] == []
    with pytest.raises(ServiceError, match="Keine Kommune"):
        services.update_member_committees(org, member, [])
    with pytest.raises(ServiceError):
        services.apply_committee_suggestions(org, member)


# ---------------------------------------------------------------------------
# Änderungsanträge
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_request_flow_with_escalation_guard(org: Any, admin: Any, editor: Any, member: Any) -> None:
    admin_role = Role.objects.create(organization=org, name="Admin-Rolle", is_admin=True)
    plain_role = Role.objects.create(organization=org, name="Einfach")

    with pytest.raises(ServiceError, match="erforderlich"):
        services.submit_change_request(org, member, "role_change", "", [])
    with pytest.raises(ServiceError, match="Antragstyp"):
        services.submit_change_request(org, member, "unbekannt", "Bitte", [])

    request = services.submit_change_request(org, member, "role_change", "Bitte", [str(admin_role.id)])
    assert request.request_data == {"requested_roles": [str(admin_role.id)]}
    assert list(selectors.my_change_requests(org, member)) == [request]
    # Aus Sicht der Prüfenden sichtbar, aus eigener Sicht nicht
    assert list(selectors.pending_change_requests(org, exclude=admin)) == [request]
    assert list(selectors.pending_change_requests(org, exclude=member)) == []
    # Administratoren (außer dem Antragsteller) werden benachrichtigt
    assert Notification.objects.filter(recipient=admin, notification_type="change_request_new").exists()

    assert selectors.can_review_change_requests(member) is False
    assert selectors.can_review_change_requests(editor) is True
    with pytest.raises(ServiceError, match="Keine Berechtigung"):
        services.approve_change_request(org, member, request.id)
    with pytest.raises(ServiceError, match="Administratoren vorbehalten"):
        services.approve_change_request(org, editor, request.id)

    approved = services.approve_change_request(org, admin, request.id)
    assert approved.status == "approved"
    assert approved.decided_by == admin
    assert list(member.roles.all()) == [admin_role]
    assert Notification.objects.filter(recipient=member, title="Antrag genehmigt").exists()

    second = services.submit_change_request(org, member, "role_change", "Zurück", [str(plain_role.id)])
    rejected = services.reject_change_request(org, editor, second.id, "Nein")
    assert rejected.status == "rejected"
    assert rejected.decision_comment == "Nein"
    assert Notification.objects.filter(recipient=member, message__contains="Kommentar: Nein").exists()

    third = services.submit_change_request(org, member, "permission_request", "Bitte", ["motions.share"])
    services.withdraw_change_request(org, member, third.id)
    assert MemberChangeRequest.objects.get(id=third.id).status == "withdrawn"


# ---------------------------------------------------------------------------
# Ratsfraktionen, Rollen, Einstellungen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_council_parties_and_contacts(org: Any) -> None:
    with pytest.raises(ServiceError, match="erforderlich"):
        services.add_party(org, services.PartyInput(name="", short_name="X"))
    party = services.add_party(org, services.PartyInput(name="Grüne Liste", short_name="GL", is_coalition_member=True))
    with pytest.raises(ServiceError, match="existiert bereits"):
        services.add_party(org, services.PartyInput(name="Andere", short_name="GL"))

    services.update_party(org, party, services.PartyInput(name="Grüne Liste", short_name="GRL", is_active=False))
    party.refresh_from_db()
    assert party.short_name == "GRL"
    assert party.is_active is False
    assert list(selectors.council_parties(org)) == [party]
    assert selectors.find_party(org, party.id) == party
    assert services.delete_party(party) == "Grüne Liste"
    assert not CouncilParty.objects.filter(id=party.id).exists()

    with pytest.raises(ServiceError):
        services.add_admin_contact(org, "", "amt@example.org")
    contact = services.add_admin_contact(org, "Amt 12", "amt@example.org")
    assert list(selectors.admin_contacts(org)) == [contact]
    assert services.delete_admin_contact(org, contact.id) is True
    assert services.delete_admin_contact(org, contact.id) is False

    services.update_coalition_name(org, "Bunte Koalition")
    org.refresh_from_db()
    assert org.coalition_name == "Bunte Koalition"


@pytest.mark.django_db
def test_role_services(org: Any, member: Any) -> None:
    PermissionFactory(codename="motions.view")  # type: ignore[no-untyped-call]
    with pytest.raises(ServiceError, match="erforderlich"):
        services.create_role(org, services.RoleInput(name=""))
    role = services.create_role(
        org,
        services.RoleInput(name="Beisitz", color="rot", priority_raw="150", permission_codes=["motions.view"]),
    )
    assert role.color == "#6b7280"
    assert role.priority == 100
    assert selectors.role_permission_codes(role) == {"motions.view"}
    with pytest.raises(ServiceError, match="existiert bereits"):
        services.create_role(org, services.RoleInput(name="Beisitz"))

    services.update_role(org, role, services.RoleInput(name="Beisitz neu", is_admin=True, color="#112233"))
    role.refresh_from_db()
    assert role.is_admin is True
    assert role.color == "#112233"
    assert role.permissions.count() == 0  # Admin-Rollen tragen keine Einzelberechtigungen

    member.roles.add(role)
    with pytest.raises(ServiceError, match="zugewiesen"):
        services.delete_role(role)
    member.roles.remove(role)
    assert services.delete_role(role) == "Beisitz neu"

    system_role = Role.objects.create(organization=org, name="System", is_system_role=True, priority=10)
    services.update_role(org, system_role, services.RoleInput(name="System", is_admin=True, priority_raw="99"))
    system_role.refresh_from_db()
    assert system_role.is_admin is False
    assert system_role.priority == 10
    with pytest.raises(ServiceError, match="Systemrollen"):
        services.delete_role(system_role)


@pytest.mark.django_db
def test_email_api_and_registration_settings(org: Any, admin: Any) -> None:
    with pytest.raises(ServiceError, match="Absender-Adresse"):
        services.save_email_settings(
            org,
            services.EmailSettingsInput(
                mail_sender_mode="smtp",
                smtp_fallback_to_mandari=True,
                smtp_host="",
                smtp_port_raw="x",
                smtp_username="",
                smtp_use_tls=True,
                smtp_from_email="kaputt",
                smtp_from_name="",
                smtp_password="",
                smtp_password_clear=False,
            ),
        )
    needs_host = services.save_email_settings(
        org,
        services.EmailSettingsInput(
            mail_sender_mode="smtp",
            smtp_fallback_to_mandari=False,
            smtp_host="",
            smtp_port_raw="99999",
            smtp_username="user",
            smtp_use_tls=False,
            smtp_from_email="info@example.org",
            smtp_from_name="Fraktion",
            smtp_password="geheim",
            smtp_password_clear=False,
        ),
    )
    org.refresh_from_db()
    assert needs_host is True
    assert org.smtp_port == 65535
    assert org.mail_sender_mode == "smtp"
    assert org.get_smtp_password() == "geheim"

    enabled = services.save_api_settings(
        org,
        admin,
        services.ApiSettingsInput(
            is_enabled=True,
            show_location=True,
            show_agenda=False,
            past_days="-5",
            future_days=None,
            cache_seconds="abc",
            allowed_origins_raw="https://fraktion.example/, ftp://nein, http://lokal.test\nhttps://fraktion.example",
        ),
    )
    assert enabled is True
    from apps.work.faction.models import FactionPublicApiAccess

    access = FactionPublicApiAccess.for_organization(org)
    assert access.past_days == 0
    assert access.allowed_origins == "https://fraktion.example, http://lokal.test"
    token_before = access.token
    services.regenerate_api_token(org, admin)
    assert FactionPublicApiAccess.for_organization(org).token != token_before

    role = Role.objects.create(organization=org, name="Standard")
    services.save_registration_settings(
        org,
        enabled=True,
        auto_approve=False,
        domains_text="@Example.org\n\n beispiel.de ",
        default_role_id=str(role.id),
    )
    org.refresh_from_db()
    assert org.registration_enabled is True
    assert org.registration_email_domains == ["example.org", "beispiel.de"]
    assert org.registration_default_role == role


@pytest.mark.django_db
def test_organization_settings_and_parties(org: Any) -> None:
    with pytest.raises(ServiceError, match="Name"):
        services.update_general_settings(org, name="", description="", primary_color="", logo=None, remove_logo=False)
    services.update_general_settings(
        org, name="Neue Fraktion", description="Text", primary_color="#abcdef", logo=None, remove_logo=False
    )
    org.refresh_from_db()
    assert org.name == "Neue Fraktion"
    assert org.primary_color == "#abcdef"

    with pytest.raises(ServiceError, match="E-Mail"):
        services.update_contact_settings(org, contact_email="nope", contact_phone="", website="", address="")
    with pytest.raises(ServiceError, match="Website"):
        services.update_contact_settings(org, contact_email="", contact_phone="", website="nope", address="")
    services.update_contact_settings(
        org, contact_email="info@example.org", contact_phone="1", website="https://example.org", address="Weg 1"
    )
    org.refresh_from_db()
    assert org.website == "https://example.org"

    services.update_parties(org, [], "Neue Partei")
    names = {p.name for p in selectors.org_parties(org)}
    assert "Neue Partei" in names
    if org.party_group:
        assert org.party_group.name in names


@pytest.mark.django_db
def test_absence_flow_notifies_deputy(org: Any, member: Any, editor: Any) -> None:
    with pytest.raises(ServiceError, match="erforderlich"):
        services.create_absence(org, member, services.AbsenceInput(start_date="", end_date=""))
    with pytest.raises(ServiceError, match="Datumsformat"):
        services.create_absence(org, member, services.AbsenceInput(start_date="2026-13-01", end_date="2026-01-02"))
    with pytest.raises(ServiceError, match="Enddatum"):
        services.create_absence(org, member, services.AbsenceInput(start_date="2026-02-02", end_date="2026-02-01"))

    today = timezone.now().date()
    absence = services.create_absence(
        org,
        member,
        services.AbsenceInput(
            start_date=today.isoformat(),
            end_date=today.isoformat(),
            reason="Urlaub",
            deputy_id=str(editor.id),
            notify_deputy=True,
        ),
    )
    assert absence.deputy == editor
    assert Notification.objects.filter(recipient=editor, notification_type="absence_deputy").exists()

    context = selectors.absence_context(org, member, today)
    assert context["current_absence"] == absence
    assert list(context["active_absences"]) == [absence]
    assert list(selectors.absence_context(org, editor, today)["deputy_for"]) == [absence]
    assert selectors.current_absences_by_member(org, today) == {member.id: absence}
    assert [m.id for m in selectors.available_deputies(org, member)] == [editor.id]

    services.cancel_absence(org, member, absence.id)
    assert MemberAbsence.objects.get(id=absence.id).is_active is False


@pytest.mark.django_db
def test_profile_and_notification_preferences(org: Any, member: Any) -> None:
    services.save_profile_visibility(
        member.user, bio="x" * 600, show_email=False, show_phone=True, preferred_contact="signal", contact_signal="s"
    )
    profile = selectors.profile_settings(member.user)
    assert len(profile["bio"]) == 500
    assert profile["show_email"] is False
    assert profile["preferred_contact"] == "signal"

    prefs = services.save_notification_preferences(
        member, {"email_enabled": "on", "email_digest": "daily", "type_task_assigned_in_app": "on"}
    )
    assert prefs.email_enabled is True
    assert prefs.email_digest == "daily"
    assert prefs.type_settings["task_assigned"] == {"in_app": True, "email": False}
    assert prefs.type_settings["motion_shared"] == {"in_app": False, "email": False}


@pytest.mark.django_db
def test_activity_and_team_selectors(org: Any, member: Any, editor: Any) -> None:
    stats = selectors.activity_statistics(org, member)
    assert stats["tasks_total"] == 0
    assert set(stats) >= {"motions_authored", "meetings_total", "preparations", "agenda_notes"}
    assert selectors.activity_timeline(org, member) == []

    assert {m.id for m in selectors.team_members(org, "")} == {member.id, editor.id}
    assert [m.id for m in selectors.team_members(org, "editor@")] == [editor.id]
    assert selectors.get_team_member_or_404(org, member.id) == member
