# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsmatrix des Session RIS: Tenant-Isolation, Ö/NÖ-Sichtbarkeit, Permission-Matrix (Issue #28).

Migriert aus scripts/smoke_session_matrix.py — migriert 62 Prüfungen (42 ``check``-Aufrufe im Skript, davon
die Permission-Matrix 21-mal über alle Rollen ausgeführt; hier zu einzelnen parametrisierten Fällen aufgelöst).

Geprüft wird:
- Permission-Matrix: alle Session-GET-Views gegen Nutzer mit exakt einer Berechtigung (erlaubt/verboten), plus
  Admin und Nutzer ohne Rechte
- Mutations-Endpunkte verweigern ohne Berechtigung (403) und mutieren nicht
- Tenant-Isolation: Listen-/Detail-/API-Views liefern ausschließlich Daten des eigenen Tenants; fremde
  Objekt-IDs unter eigenem Slug → 404; fremder Tenant-Slug → 403
- Ö/NÖ-Sichtbarkeit: NÖ-Sitzungen, NÖ-Vorlagen, NÖ-TOPs und NÖ-Anlagen sind für unberechtigte Rollen
  unsichtbar — UI und Session-API
- OParl-API liefert ausschließlich is_public-Daten (anonym)

Die Testdaten sind modulweit angelegt (beide Tenants, alle Rollen-Clients); sämtliche Tests sind lesend bzw.
erwarten abgewiesene Mutationen, sodass sie den gemeinsamen Datenstand nicht verändern.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionFile,
    SessionInvitationDispatch,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

# Alle Permission-Flags des Role-Models (can_* Booleans)
ALL_PERM_FLAGS = [
    f.name[4:] for f in SessionRole._meta.get_fields() if f.name.startswith("can_") and getattr(f, "concrete", False)
]

# Berechtigungen, die in der GET-Matrix vorkommen — je Berechtigung gibt es genau einen Nutzer
MATRIX_PERMS = [
    "view_dashboard",
    "view_meetings",
    "create_meetings",
    "edit_meetings",
    "view_non_public_meetings",
    "view_papers",
    "create_papers",
    "edit_papers",
    "approve_papers",
    "view_non_public_papers",
    "view_applications",
    "process_applications",
    "view_protocols",
    "edit_protocols",
    "manage_organizations",
    "manage_users",
    "manage_settings",
    "view_audit_log",
]

ADMIN = "admin"
NO_PERM = "ohne_rechte"
COMBO = "process_applications+create_papers"

# Rolle → effektive Berechtigungen (Admin: alles)
ROLE_PERMS: dict[str, frozenset[str]] = {
    ADMIN: frozenset(MATRIX_PERMS),
    NO_PERM: frozenset(),
    **{perm: frozenset({perm}) for perm in MATRIX_PERMS},
    COMBO: frozenset({"process_applications", "create_papers"}),
}
ROLES = list(ROLE_PERMS)

# (Pfad-Vorlage relativ zum Tenant, benötigte Berechtigungen); Platzhalter werden mit Objekt-IDs gefüllt
GET_MATRIX: list[tuple[str, frozenset[str]]] = [
    ("/dashboard/", frozenset({"view_dashboard"})),
    ("/meetings/", frozenset({"view_meetings"})),
    ("/meetings/{meeting_pub}/", frozenset({"view_meetings"})),
    ("/meetings/create/", frozenset({"create_meetings"})),
    ("/meetings/{meeting_pub}/edit/", frozenset({"edit_meetings"})),
    ("/meetings/{meeting_pub}/invitation/", frozenset({"edit_meetings"})),
    ("/meetings/{meeting_pub}/agenda.pdf", frozenset({"view_meetings"})),
    ("/meetings/{meeting_pub}/sitzung.ics", frozenset({"view_meetings"})),
    ("/meetings/{meeting_pub}/protocol/", frozenset({"view_protocols"})),
    ("/meetings/{meeting_pub}/protocol/edit/", frozenset({"edit_protocols"})),
    ("/meetings/{meeting_pub}/niederschrift.pdf", frozenset({"view_protocols"})),
    ("/agenda/{top_pub}/edit/", frozenset({"edit_meetings"})),
    ("/resolutions/", frozenset({"view_meetings"})),
    ("/agenda/{top_decided}/beschlussauszug.pdf", frozenset({"view_meetings"})),
    ("/meetings/{meeting_pub}/beschlussauszuege.pdf", frozenset({"view_meetings"})),
    ("/papers/", frozenset({"view_papers"})),
    ("/papers/{paper_pub}/", frozenset({"view_papers"})),
    ("/papers/create/", frozenset({"create_papers"})),
    ("/papers/{paper_pub}/edit/", frozenset({"edit_papers"})),
    ("/papers/review/", frozenset({"approve_papers"})),
    ("/applications/", frozenset({"view_applications"})),
    ("/applications/{app_a}/", frozenset({"view_applications"})),
    ("/applications/{app_a}/process/", frozenset({"process_applications"})),
    ("/applications/{app_a}/convert/", frozenset({"process_applications", "create_papers"})),
    ("/organizations/", frozenset({"view_meetings"})),
    ("/organizations/{org_a}/", frozenset({"view_meetings"})),
    ("/organizations/create/", frozenset({"manage_organizations"})),
    ("/organizations/{org_a}/edit/", frozenset({"manage_organizations"})),
    ("/persons/", frozenset({"view_meetings"})),
    ("/persons/{person_a}/", frozenset({"view_meetings"})),
    ("/persons/create/", frozenset({"manage_organizations"})),
    ("/persons/{person_a}/edit/", frozenset({"manage_organizations"})),
    ("/settings/", frozenset({"manage_settings"})),
    ("/settings/users/", frozenset({"manage_users"})),
    ("/settings/users/invite/", frozenset({"manage_users"})),
    ("/audit/", frozenset({"view_audit_log"})),
    ("/files/{file_pub}/download/", frozenset({"view_papers"})),
]

# (Pfad-Vorlage, POST-Daten) — Mutationen, die ohne Berechtigung 403 liefern und nichts verändern dürfen
MUTATIONS: list[tuple[str, dict[str, str]]] = [
    ("/meetings/create/", {"name": "M", "organization": "{org_a}", "start": "2026-08-01T10:00"}),
    ("/meetings/{meeting_pub}/invitation/", {"dispatch_type": "invitation"}),
    ("/papers/create/", {"reference": "V/X", "name": "P", "paper_type": "proposal"}),
    ("/papers/{paper_pub}/workflow/submit/", {}),
    ("/papers/{paper_pub}/workflow/approve/", {}),
    ("/meetings/{meeting_pub}/agenda/add/", {"name": "T"}),
    ("/meetings/{meeting_pub}/attendance/generate/", {}),
    ("/meetings/{meeting_pub}/attendance/add/", {"person": "{person_a}"}),
    ("/meetings/{meeting_pub}/protocol/create/", {}),
    ("/meetings/{meeting_pub}/protocol/submit/", {}),
    ("/meetings/{meeting_pub}/protocol/approve/", {}),
    ("/meetings/{meeting_pub}/resolutions/generate/", {}),
    ("/agenda/{top_decided}/forwarding/add/", {"recipient": "Bauamt"}),
    ("/meetings/{meeting_pub}/agenda/reorder/", {"order": ""}),
    ("/agenda/{top_pub}/withdraw/", {"reason": "x"}),
    ("/agenda/{top_pub}/delete/", {}),
    ("/files/upload/", {"target_type": "paper", "target_id": "{paper_pub}"}),
    ("/files/{file_pub}/delete/", {}),
    ("/files/{file_pub}/update/", {"is_public": "on"}),
    ("/organizations/create/", {"name": "O", "organization_type": "committee"}),
    ("/organizations/{org_a}/deactivate/", {}),
    ("/organizations/{org_a}/memberships/add/", {"person": "{person_a}"}),
    ("/persons/create/", {"given_name": "X", "family_name": "Y"}),
    ("/persons/{person_a}/deactivate/", {}),
    ("/settings/users/invite/", {"email": "x@example.org"}),
    ("/applications/{app_a}/process/", {"status": "received"}),
    # Beratungsfolge (Issue #34)
    ("/papers/{paper_pub}/consultations/add/", {"organization": "{org_a}"}),
    ("/consultations/{consultation_a}/update/", {"role": "hearing"}),
    ("/consultations/{consultation_a}/delete/", {}),
    ("/consultations/{consultation_a}/move/", {"direction": "up"}),
    ("/consultations/{consultation_a}/schedule/", {"meeting": "{meeting_pub}"}),
    ("/consultations/{consultation_a}/forward/", {}),
]

# Listen-/API-Views des eigenen Tenants, die keine Fremddaten enthalten dürfen
LIST_PATHS = [
    "/dashboard/",
    "/meetings/",
    "/papers/",
    "/applications/",
    "/organizations/",
    "/persons/",
    "/settings/users/",
    "/audit/",
    "/api/session/meetings/",
    "/api/session/papers/",
    "/api/session/applications/",
    "/api/oparl/meetings/",
    "/api/oparl/papers/",
    "/api/oparl/organizations/",
    "/api/oparl/people/",
    "/api/oparl/agendaitems/",
    "/api/oparl/consultations/",
    "/api/oparl/files/",
    "/api/oparl/memberships/",
]

# Fremde Objekt-IDs unter eigenem Tenant-Slug (GET) → 404
FOREIGN_DETAIL_PATHS = [
    "/meetings/{meeting_b}/",
    "/meetings/{meeting_b}/edit/",
    "/meetings/{meeting_b}/invitation/",
    "/meetings/{meeting_b}/agenda.pdf",
    "/meetings/{meeting_b}/sitzung.ics",
    "/meetings/{meeting_b}/protocol/",
    "/meetings/{meeting_b}/niederschrift.pdf",
    "/papers/{paper_b}/",
    "/papers/{paper_b}/edit/",
    "/applications/{app_b}/",
    "/organizations/{org_b}/",
    "/organizations/{org_b}/edit/",
    "/persons/{person_b}/",
    "/persons/{person_b}/edit/",
    "/agenda/{top_b}/edit/",
    "/files/{file_b}/download/",
]

# Fremde Objekt-Mutationen unter eigenem Tenant-Slug (POST) → 404
FOREIGN_MUTATIONS: list[tuple[str, dict[str, str]]] = [
    ("/agenda/{top_b}/delete/", {}),
    ("/files/{file_b}/delete/", {}),
    ("/memberships/{membership_b}/end/", {}),
    ("/organizations/{org_b}/deactivate/", {}),
    ("/meetings/{meeting_b}/attendance/generate/", {}),
    ("/meetings/{meeting_b}/resolutions/generate/", {}),
    ("/agenda/{top_b}/forwarding/add/", {"recipient": "Bauamt"}),
    ("/papers/{paper_b}/workflow/submit/", {}),
    ("/papers/{paper_b}/consultations/add/", {"organization": "{org_b}"}),
    ("/consultations/{consultation_b}/update/", {"role": "hearing"}),
    ("/consultations/{consultation_b}/delete/", {}),
]

OPARL_SEGMENTS = [
    "meetings",
    "papers",
    "organizations",
    "people",
    "agendaitems",
    "consultations",
    "files",
    "memberships",
    "legislativeterms",
]
FOREIGN_MARKERS = (b"FREMD", b"fremd-anlage")
NON_PUBLIC_MARKERS = (b"GEHEIM", b"geheime-anlage")


@dataclass
class World:
    """Zwei Tenants mit Ö/NÖ-Daten, dazu eingeloggte Clients je Rolle."""

    tenant_a: SessionTenant
    tenant_b: SessionTenant
    ids: dict[str, Any]
    clients: dict[str, Client]
    users: list[User] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"/session/{self.tenant_a.slug}"

    def url(self, template: str) -> str:
        return self.base + template.format(**self.ids)

    def data(self, template: dict[str, str]) -> dict[str, str]:
        return {key: value.format(**self.ids) for key, value in template.items()}


def _create_user(email: str) -> User:
    return cast(User, UserFactory(email=email))  # type: ignore[no-untyped-call]


def _make_user(world: World, tenant: SessionTenant, name: str, perms: set[str], is_admin: bool = False) -> Client:
    """Eingeloggter Client für einen Nutzer im Tenant mit exakt den angegebenen Berechtigungen."""
    flags = {f"can_{p}": p in perms for p in ALL_PERM_FLAGS}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle_{name}", is_admin=is_admin, **flags)
    user = _create_user(f"{name}@example.org")
    world.users.append(user)
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def _build_world() -> World:
    tenant_a = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    tenant_b = SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")
    now = timezone.now()

    # Tenant A: Daten mit Ö/NÖ-Trennung
    org_a = SessionOrganization.objects.create(tenant=tenant_a, name="Hauptausschuss A")
    person_a = SessionPerson.objects.create(tenant=tenant_a, given_name="Anna", family_name="Eigen")
    meeting_pub = SessionMeeting.objects.create(
        tenant=tenant_a, name="OEFFENTLICHE-SITZUNG-A", organization=org_a, start=now, is_public=True
    )
    meeting_np = SessionMeeting.objects.create(
        tenant=tenant_a, name="GEHEIME-SITZUNG-A", organization=org_a, start=now, is_public=False
    )
    paper_pub = SessionPaper.objects.create(
        tenant=tenant_a, reference="V/2026/1001", name="OEFFENTLICHE-VORLAGE-A", is_public=True
    )
    paper_np = SessionPaper.objects.create(
        tenant=tenant_a, reference="V/2026/1002", name="GEHEIME-VORLAGE-A", is_public=False
    )
    top_pub = SessionAgendaItem.objects.create(
        meeting=meeting_pub, number="1", name="OEFFENTLICHER-TOP-A", is_public=True
    )
    top_np = SessionAgendaItem.objects.create(meeting=meeting_pub, number="N1", name="GEHEIMER-TOP-A", is_public=False)
    top_decided = SessionAgendaItem.objects.create(
        meeting=meeting_pub, number="2", name="BESCHLOSSENER-TOP-A", is_public=True, vote_result="approved"
    )
    app_a = SessionApplication.objects.create(
        tenant=tenant_a,
        title="ANTRAG-A",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    consultation_a = SessionConsultation.objects.create(paper=paper_pub, organization=org_a, order=1)
    SessionProtocol.objects.create(meeting=meeting_pub, content="Protokoll A")
    file_pub = SessionFile.objects.create(
        tenant=tenant_a,
        name="oeffentliche-anlage-a.txt",
        file=SimpleUploadedFile("oeffentliche-anlage-a.txt", b"public content A"),
        is_public=True,
        paper=paper_pub,
    )
    file_np = SessionFile.objects.create(
        tenant=tenant_a,
        name="geheime-anlage-a.txt",
        file=SimpleUploadedFile("geheime-anlage-a.txt", b"secret content A"),
        is_public=False,
        paper=paper_pub,
    )

    # Tenant B: Fremddaten mit Markern
    org_b = SessionOrganization.objects.create(tenant=tenant_b, name="FREMDGREMIUM-XYZ")
    person_b = SessionPerson.objects.create(tenant=tenant_b, given_name="Fritz", family_name="FREMDPERSON-XYZ")
    meeting_b = SessionMeeting.objects.create(
        tenant=tenant_b, name="FREMD-SITZUNG-XYZ", organization=org_b, start=now, is_public=True
    )
    paper_b = SessionPaper.objects.create(
        tenant=tenant_b, reference="V/2026/9001", name="FREMD-VORLAGE-XYZ", is_public=True
    )
    top_b = SessionAgendaItem.objects.create(meeting=meeting_b, number="1", name="FREMD-TOP-XYZ", is_public=True)
    app_b = SessionApplication.objects.create(
        tenant=tenant_b,
        title="FREMD-ANTRAG-XYZ",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    file_b = SessionFile.objects.create(
        tenant=tenant_b,
        name="fremd-anlage-xyz.txt",
        file=SimpleUploadedFile("fremd-anlage-xyz.txt", b"foreign content B"),
        is_public=True,
        paper=paper_b,
    )
    membership_b = SessionOrganizationMembership.objects.create(organization=org_b, person=person_b)
    consultation_b = SessionConsultation.objects.create(paper=paper_b, organization=org_b, order=1)

    ids: dict[str, Any] = {
        "org_a": org_a.id,
        "person_a": person_a.id,
        "meeting_pub": meeting_pub.id,
        "meeting_np": meeting_np.id,
        "paper_pub": paper_pub.id,
        "paper_np": paper_np.id,
        "top_pub": top_pub.id,
        "top_np": top_np.id,
        "top_decided": top_decided.id,
        "app_a": app_a.id,
        "consultation_a": consultation_a.id,
        "file_pub": file_pub.id,
        "file_np": file_np.id,
        "org_b": org_b.id,
        "person_b": person_b.id,
        "meeting_b": meeting_b.id,
        "paper_b": paper_b.id,
        "top_b": top_b.id,
        "app_b": app_b.id,
        "file_b": file_b.id,
        "membership_b": membership_b.id,
        "consultation_b": consultation_b.id,
    }
    world = World(tenant_a=tenant_a, tenant_b=tenant_b, ids=ids, clients={})

    # Nutzer: Admin, ohne Rechte, je Berechtigung genau ein Nutzer, eine Kombination, Admin in Tenant B
    world.clients[ADMIN] = _make_user(world, tenant_a, "admin-a", set(), is_admin=True)
    world.clients[NO_PERM] = _make_user(world, tenant_a, "nichts", set())
    for perm in MATRIX_PERMS:
        world.clients[perm] = _make_user(world, tenant_a, f"nur-{perm.replace('_', '-')}", {perm})
    world.clients[COMBO] = _make_user(world, tenant_a, "konverter", set(ROLE_PERMS[COMBO]))
    world.clients["admin_b"] = _make_user(world, tenant_b, "admin-b", set(), is_admin=True)
    world.clients["anon"] = Client()
    return world


@pytest.fixture(scope="module")
def world(django_db_setup: None, django_db_blocker: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    """Modulweite Testdaten (lesend genutzt); Uploads landen in einem temporären MEDIA_ROOT."""
    media_root = tmp_path_factory.mktemp("session-media")
    with override_settings(MEDIA_ROOT=str(media_root)), django_db_blocker.unblock():
        built = _build_world()
        yield built
        SessionTenant.objects.filter(pk__in=[built.tenant_a.pk, built.tenant_b.pk]).delete()
        User.objects.filter(pk__in=[user.pk for user in built.users]).delete()


def _counts() -> tuple[int, ...]:
    return tuple(
        model.objects.count()
        for model in (
            SessionMeeting,
            SessionPaper,
            SessionAgendaItem,
            SessionFile,
            SessionOrganization,
            SessionPerson,
            SessionOrganizationMembership,
            SessionInvitationDispatch,
            SessionAttendance,
            SessionConsultation,
        )
    )


# =============================================================================
# Phase A: Permission-Matrix (GET-Views x Rollen)
# =============================================================================


@pytest.mark.parametrize(("path", "required"), GET_MATRIX, ids=[path for path, _ in GET_MATRIX])
@pytest.mark.parametrize("role", ROLES)
def test_get_permission_matrix(world: World, role: str, path: str, required: frozenset[str]) -> None:
    url = world.url(path)
    expected = 200 if required <= ROLE_PERMS[role] else 403
    status = world.clients[role].get(url).status_code
    assert status == expected, f"Rolle {role}: GET {url} erwartet {expected}, erhalten {status}"


# =============================================================================
# Phase B: Mutations-Endpunkte ohne Berechtigung
# =============================================================================


@pytest.mark.parametrize(("path", "data"), MUTATIONS, ids=[path for path, _ in MUTATIONS])
def test_mutation_without_permission_is_forbidden(world: World, path: str, data: dict[str, str]) -> None:
    url = world.url(path)
    status = world.clients[NO_PERM].post(url, world.data(data)).status_code
    assert status == 403, f"POST {url} ohne Rechte: erwartet 403, erhalten {status}"


def test_mutations_without_permission_change_nothing(world: World) -> None:
    before = _counts()
    for path, data in MUTATIONS:
        world.clients[NO_PERM].post(world.url(path), world.data(data))
    assert _counts() == before, f"Mutation ohne Berechtigung ausgeführt: {before} -> {_counts()}"
    assert not SessionAgendaItem.objects.get(pk=world.ids["top_pub"]).is_withdrawn, "TOP wurde abgesetzt"
    assert SessionApplication.objects.get(pk=world.ids["app_a"]).status == "submitted", "Antrag-Status verändert"


# =============================================================================
# Phase C: Tenant-Isolation
# =============================================================================


@pytest.mark.parametrize("path", LIST_PATHS)
def test_list_views_contain_no_foreign_data(world: World, path: str) -> None:
    url = world.url(path)
    response = world.clients[ADMIN].get(url)
    assert response.status_code == 200, f"GET {url}: Status {response.status_code}"
    assert not any(marker in response.content for marker in FOREIGN_MARKERS), f"GET {url}: Fremddaten sichtbar"


@pytest.mark.parametrize("path", FOREIGN_DETAIL_PATHS)
def test_foreign_object_under_own_slug_is_404(world: World, path: str) -> None:
    url = world.url(path)
    status = world.clients[ADMIN].get(url).status_code
    assert status == 404, f"GET {url} (fremdes Objekt): erwartet 404, erhalten {status}"


@pytest.mark.parametrize(("path", "data"), FOREIGN_MUTATIONS, ids=[path for path, _ in FOREIGN_MUTATIONS])
def test_foreign_object_mutation_is_404(world: World, path: str, data: dict[str, str]) -> None:
    url = world.url(path)
    status = world.clients[ADMIN].post(url, world.data(data)).status_code
    assert status == 404, f"POST {url} (fremdes Objekt): erwartet 404, erhalten {status}"
    assert SessionAgendaItem.objects.filter(pk=world.ids["top_b"]).exists(), "Fremder TOP wurde gelöscht"


def test_foreign_tenant_slug_is_forbidden(world: World) -> None:
    response = world.clients[ADMIN].get(f"/session/{world.tenant_b.slug}/meetings/")
    assert response.status_code == 403, f"Fremder Tenant-Slug: erwartet 403, erhalten {response.status_code}"


def test_admin_of_other_tenant_sees_no_own_data(world: World) -> None:
    response = world.clients["admin_b"].get(f"/session/{world.tenant_b.slug}/meetings/")
    assert response.status_code == 200
    assert b"SITZUNG-A" not in response.content, "B-Admin sieht Daten aus Tenant A"


# =============================================================================
# Phase D: Ö/NÖ-Sichtbarkeit (UI + Session-API)
# =============================================================================


def test_meeting_list_hides_non_public_meeting(world: World) -> None:
    # view_meetings, aber NICHT view_non_public_meetings
    response = world.clients["view_meetings"].get(world.url("/meetings/"))
    assert response.status_code == 200
    assert b"GEHEIME-SITZUNG-A" not in response.content, "Sitzungsliste: NÖ-Sitzung sichtbar"
    assert b"OEFFENTLICHE-SITZUNG-A" in response.content, "Sitzungsliste: Ö-Sitzung fehlt"


def test_non_public_meeting_detail_without_right_is_404(world: World) -> None:
    status = world.clients["view_meetings"].get(world.url("/meetings/{meeting_np}/")).status_code
    assert status == 404, f"NÖ-Sitzungsdetail ohne NÖ-Recht: erwartet 404, erhalten {status}"


def test_public_meeting_detail_hides_non_public_agenda_item(world: World) -> None:
    response = world.clients["view_meetings"].get(world.url("/meetings/{meeting_pub}/"))
    assert b"GEHEIMER-TOP-A" not in response.content, "Ö-Sitzungsdetail: NÖ-TOP sichtbar"
    assert b"OEFFENTLICHER-TOP-A" in response.content, "Ö-Sitzungsdetail: Ö-TOP fehlt"


def test_admin_sees_non_public_agenda_item(world: World) -> None:
    response = world.clients[ADMIN].get(world.url("/meetings/{meeting_pub}/"))
    assert b"GEHEIMER-TOP-A" in response.content, "Admin sieht NÖ-TOP nicht"


def test_non_public_meeting_not_editable_without_right(world: World) -> None:
    status = world.clients["edit_meetings"].get(world.url("/meetings/{meeting_np}/edit/")).status_code
    assert status == 404, f"NÖ-Sitzung ohne NÖ-Recht editierbar: erwartet 404, erhalten {status}"


def test_paper_list_hides_non_public_paper(world: World) -> None:
    response = world.clients["view_papers"].get(world.url("/papers/"))
    assert b"GEHEIME-VORLAGE-A" not in response.content, "Vorlagenliste: NÖ-Vorlage sichtbar"
    assert b"OEFFENTLICHE-VORLAGE-A" in response.content, "Vorlagenliste: Ö-Vorlage fehlt"


def test_non_public_paper_detail_without_right_is_404(world: World) -> None:
    status = world.clients["view_papers"].get(world.url("/papers/{paper_np}/")).status_code
    assert status == 404, f"NÖ-Vorlagendetail ohne NÖ-Recht: erwartet 404, erhalten {status}"


def test_paper_detail_hides_non_public_file(world: World) -> None:
    response = world.clients["view_papers"].get(world.url("/papers/{paper_pub}/"))
    assert b"geheime-anlage-a.txt" not in response.content, "Vorlagendetail: NÖ-Anlage sichtbar"
    assert b"oeffentliche-anlage-a.txt" in response.content, "Vorlagendetail: Ö-Anlage fehlt"


def test_non_public_file_download_without_right_is_forbidden(world: World) -> None:
    status = world.clients["view_papers"].get(world.url("/files/{file_np}/download/")).status_code
    assert status == 403, f"NÖ-Anlagen-Download ohne NÖ-Recht: erwartet 403, erhalten {status}"


def test_session_api_meetings_respect_non_public_right(world: World) -> None:
    viewer = world.clients["view_meetings"].get(world.url("/api/session/meetings/"))
    assert b"GEHEIME-SITZUNG-A" not in viewer.content, "Session-API: NÖ-Sitzung für Viewer sichtbar"
    admin = world.clients[ADMIN].get(world.url("/api/session/meetings/"))
    assert b"GEHEIME-SITZUNG-A" in admin.content, "Session-API: NÖ-Sitzung für Admin unsichtbar"


def test_session_api_papers_hide_non_public_paper(world: World) -> None:
    response = world.clients["view_papers"].get(world.url("/api/session/papers/"))
    assert b"GEHEIME-VORLAGE-A" not in response.content, "Session-API: NÖ-Vorlage für Viewer sichtbar"


def test_session_api_applications_without_right_is_forbidden(world: World) -> None:
    status = world.clients[NO_PERM].get(world.url("/api/session/applications/")).status_code
    assert status == 403, f"Session-API Anträge ohne Recht: erwartet 403, erhalten {status}"


# =============================================================================
# Phase E: OParl-API liefert nur is_public-Daten (anonym)
# =============================================================================


def test_oparl_meetings_only_public(world: World) -> None:
    response = world.clients["anon"].get(world.url("/api/oparl/meetings/"))
    assert response.status_code == 200, f"OParl-Meetings anonym: erhalten {response.status_code}"
    assert b"GEHEIME-SITZUNG-A" not in response.content, "OParl-Meetings: NÖ-Sitzung enthalten"
    assert b"OEFFENTLICHE-SITZUNG-A" in response.content, "OParl-Meetings: Ö-Sitzung fehlt"
    assert b"GEHEIMER-TOP-A" not in response.content, "OParl-Meetings: NÖ-TOP eingebettet"


def test_oparl_papers_only_public(world: World) -> None:
    response = world.clients["anon"].get(world.url("/api/oparl/papers/"))
    assert response.status_code == 200, f"OParl-Papers anonym: erhalten {response.status_code}"
    assert b"GEHEIME-VORLAGE-A" not in response.content, "OParl-Papers: NÖ-Vorlage enthalten"
    assert b"OEFFENTLICHE-VORLAGE-A" in response.content, "OParl-Papers: Ö-Vorlage fehlt"
    assert b"geheime-anlage-a" not in response.content, "OParl-Papers: NÖ-Anlage enthalten"


def test_oparl_organizations_contain_no_foreign_data(world: World) -> None:
    response = world.clients["anon"].get(world.url("/api/oparl/organizations/"))
    assert b"FREMDGREMIUM" not in response.content, "OParl-Organizations: Fremddaten sichtbar"


@pytest.mark.parametrize("segment", OPARL_SEGMENTS)
def test_oparl_list_is_public_and_clean(world: World, segment: str) -> None:
    response = world.clients["anon"].get(world.url(f"/api/oparl/{segment}/"))
    assert response.status_code == 200, f"OParl-Liste {segment}: Status {response.status_code}"
    leaked = [m.decode() for m in (*NON_PUBLIC_MARKERS, *FOREIGN_MARKERS) if m in response.content]
    assert not leaked, f"OParl-Liste {segment}: NÖ-/Fremddaten sichtbar ({leaked})"


@pytest.mark.parametrize(
    ("kind", "id_key"),
    [("meeting", "meeting_np"), ("paper", "paper_np"), ("agendaitem", "top_np"), ("file", "file_np")],
)
def test_oparl_non_public_object_is_404(world: World, kind: str, id_key: str) -> None:
    # Nie veröffentlicht, also kein Tombstone
    status = world.clients["anon"].get(world.url(f"/api/oparl/{kind}/{{{id_key}}}/")).status_code
    assert status == 404, f"OParl-Objekt {kind} (NÖ): erwartet 404, erhalten {status}"


def test_oparl_non_public_file_download_is_404(world: World) -> None:
    status = world.clients["anon"].get(world.url("/api/oparl/file/{file_np}/download/")).status_code
    assert status == 404, f"OParl-Datei-Download (NÖ): erwartet 404, erhalten {status}"


def test_anonymous_meeting_list_redirects_to_login(world: World) -> None:
    status = world.clients["anon"].get(world.url("/meetings/")).status_code
    assert status == 302, f"Anonym Sitzungsliste: erwartet Login-Redirect, erhalten {status}"


def test_anonymous_cannot_download_non_public_file(world: World) -> None:
    status = world.clients["anon"].get(world.url("/files/{file_np}/download/")).status_code
    assert status in (302, 403), f"Anonym NÖ-Anlage: erwartet 302/403, erhalten {status}"
