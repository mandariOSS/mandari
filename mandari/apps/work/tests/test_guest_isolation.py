# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gast-Isolation im Work-Portal: Isolations-Matrix, WebSocket-Consumer, Ordner-Freigaben, Multi-Org.

Migriert aus scripts/smoke_guest_isolation.py — migriert 77 Prüfungen (75 ``check``-Aufrufe im Skript, eine
davon in einer Schleife über drei URLs; die Isolations-Matrix ist hier je URL-Name und Methode ein eigener Fall).

Geprüft wird:
- Isolations-Matrix: ALLE work-URL-Namen werden enumeriert und als eingeloggter Gast per GET und POST
  aufgerufen. Alles außerhalb der expliziten Gast-Whitelist muss dicht sein (Redirect auf die Gast-Übersicht
  /freigaben/ bzw. 403/404/405) — insbesondere Fraktionssitzungen, Sitzungsvorbereitung, Aufgaben,
  Mitgliederliste, Dashboard, RIS, Einstellungen, Benachrichtigungen, Suche, HTMX/JSON.
- WebSocket-Consumer: DocumentCollaborationConsumer (share-basiert) und PreparationConsumer (Gäste
  ausgeschlossen).
- Ordner-Freigaben (FolderGuestShare): gelten rekursiv für Unterordner und enthaltene Dokumente, auch künftig
  hinzukommende; Level-Vererbung (höchstes Level gewinnt); Gast-Übersicht mit navigierbarem Baum;
  Verwaltungs-Endpunkte (freigeben/entziehen) inkl. Org-Grenzen.
- Multi-Org: derselbe User ist Gast in Org A und Voll-Mitglied in Org B — in A nur Freigaben, in B alles
  Normale; Org-Switcher zeigt beide; bestehende User (Gast anderswo) können in weitere Orgs eingeladen werden
  (Gast-Einladung + reguläre Einladung).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from django.test import Client
from django.urls import reverse

import apps.work.urls as work_urls
from apps.accounts.models import User
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import Membership, Organization, Role, UserInvitation
from apps.work.meetings.consumers import PreparationConsumer
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionShare
from insight_core.models import OParlBody, OParlPaper, OParlSource

ORG_SLUG_A = "fraktion-a"
BASE_A = f"/work/{ORG_SLUG_A}"

# Whitelist: Views mit guest_allowed=True (Zugriff dort share-basiert geprüft)
GUEST_ALLOWED = {
    "guest_documents",
    "document_editor",
    "document_export",
    "document_comment",
    "document_comment_resolve",
    "document_revisions",
    "document_revision_detail",
    "profile",
    "security",
}
# Kein Org-Kontext (Redirect-Helfer bzw. öffentliche Einladungsannahme)
SKIP = {"root", "accept_invitation"}

FIXED_KWARGS: dict[str, Any] = {
    "org_slug": ORG_SLUG_A,
    "token": "dummy-token",
    "anchor_type": "file",
    "category_slug": "kategorie",
    "article_slug": "artikel",
}


def _matrix_url_names() -> list[str]:
    """Alle benannten work-URLs außerhalb von Whitelist und Skip-Liste (zur Sammelzeit enumeriert)."""
    return [
        pattern.name
        for pattern in work_urls.urlpatterns
        if pattern.name and pattern.name not in SKIP and pattern.name not in GUEST_ALLOWED
    ]


def _matrix_url(name: str) -> str:
    """URL eines work-URL-Namens mit festen Slugs und zufälligen IDs (Objekte existieren nicht)."""
    pattern = next(p for p in work_urls.urlpatterns if p.name == name)
    kwargs = {param: FIXED_KWARGS.get(param, uuid.uuid4()) for param in pattern.pattern.converters}
    return reverse(f"work:{name}", kwargs=kwargs)


MATRIX_URL_NAMES = _matrix_url_names()


@dataclass
class Guests:
    """Vier Fraktionen einer Kommune; ein User ist Gast in A und Admin in B."""

    body: OParlBody
    org_a: Organization
    org_b: Organization
    org_c: Organization
    user_admin: User
    m_admin: Membership
    user_multi: User
    m_guest_a: Membership
    m_member_b: Membership
    user_admin_c: User
    m_admin_c: Membership
    user_plain: User
    folder_root: DocumentFolder
    folder_sub: DocumentFolder
    folder_subsub: DocumentFolder
    folder_secret: DocumentFolder
    doc_root: Motion
    doc_sub: Motion
    doc_secret: Motion
    doc_no_folder: Motion
    doc_direct_share: Motion
    c_admin: Client
    c_guest: Client
    c_admin_c: Client

    @property
    def base_b(self) -> str:
        return f"/work/{self.org_b.slug}"


def _admin_role(organization: Organization) -> Role:
    role = Role.objects.filter(organization=organization, is_admin=True).first()
    assert role is not None, f"Standardrollen für {organization.slug} fehlen"
    return role


def _create_org(name: str, slug: str, body: OParlBody | None = None) -> Organization:
    return cast(Organization, OrganizationFactory(name=name, slug=slug, body=body))  # type: ignore[no-untyped-call]


def _create_user(email: str) -> User:
    return cast(User, UserFactory(email=email))  # type: ignore[no-untyped-call]


def _create_membership(
    user: User, organization: Organization, roles: list[Role] | None = None, is_guest: bool = False
) -> Membership:
    membership = MembershipFactory(user=user, organization=organization, roles=roles, is_guest=is_guest)  # type: ignore[no-untyped-call]
    return cast(Membership, membership)


def _login(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def guests(db: None) -> Guests:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1", source=source, name="Stadt Testhausen"
    )
    org_a = _create_org("Fraktion A", ORG_SLUG_A, body=body)
    org_b = _create_org("Fraktion B", "fraktion-b", body=body)
    org_c = _create_org("Fraktion C", "fraktion-c", body=body)

    # Admin in Org A
    user_admin = _create_user("admin-a@example.org")
    m_admin = _create_membership(user_admin, org_a, roles=[_admin_role(org_a)])

    # Multi-Org-User: Gast in Org A + Voll-Mitglied (Admin) in Org B
    user_multi = _create_user("multi@example.org")
    m_guest_a = _create_membership(user_multi, org_a, is_guest=True)
    m_member_b = _create_membership(user_multi, org_b, roles=[_admin_role(org_b)])

    # Admin in Org C (lädt den Multi-User später als Gast ein)
    user_admin_c = _create_user("admin-c@example.org")
    m_admin_c = _create_membership(user_admin_c, org_c, roles=[_admin_role(org_c)])

    # Mitglied ohne Ordner-Verwaltungsrechte in Org A
    user_plain = _create_user("plain-a@example.org")
    _create_membership(user_plain, org_a)

    # Ordnerbaum in Org A: Projekte > 2026 > Q1  |  Intern (nicht freigegeben)
    folder_root = DocumentFolder.objects.create(organization=org_a, name="Projekte", created_by=m_admin)
    folder_sub = DocumentFolder.objects.create(organization=org_a, name="2026", parent=folder_root, created_by=m_admin)
    folder_subsub = DocumentFolder.objects.create(organization=org_a, name="Q1", parent=folder_sub, created_by=m_admin)
    folder_secret = DocumentFolder.objects.create(organization=org_a, name="Intern", created_by=m_admin)

    doc_root = Motion.objects.create(
        organization=org_a, author=m_admin, title="Projektplan", visibility="private", folder=folder_root
    )
    doc_sub = Motion.objects.create(
        organization=org_a, author=m_admin, title="Jahresplanung", visibility="organization", folder=folder_sub
    )
    doc_secret = Motion.objects.create(
        organization=org_a, author=m_admin, title="Internes Papier", visibility="organization", folder=folder_secret
    )
    doc_no_folder = Motion.objects.create(organization=org_a, author=m_admin, title="Ohne Ordner", visibility="private")
    doc_direct_share = Motion.objects.create(
        organization=org_a, author=m_admin, title="Direkt geteilt", visibility="shared"
    )
    MotionShare.objects.create(
        motion=doc_direct_share, scope="user", user=user_multi, level="view", created_by=user_admin
    )

    return Guests(
        body=body,
        org_a=org_a,
        org_b=org_b,
        org_c=org_c,
        user_admin=user_admin,
        m_admin=m_admin,
        user_multi=user_multi,
        m_guest_a=m_guest_a,
        m_member_b=m_member_b,
        user_admin_c=user_admin_c,
        m_admin_c=m_admin_c,
        user_plain=user_plain,
        folder_root=folder_root,
        folder_sub=folder_sub,
        folder_subsub=folder_subsub,
        folder_secret=folder_secret,
        doc_root=doc_root,
        doc_sub=doc_sub,
        doc_secret=doc_secret,
        doc_no_folder=doc_no_folder,
        doc_direct_share=doc_direct_share,
        c_admin=_login(user_admin),
        c_guest=_login(user_multi),
        c_admin_c=_login(user_admin_c),
    )


@pytest.fixture
def share_root(guests: Guests) -> FolderGuestShare:
    """Ordner-Freigabe (view) auf den Wurzel-Ordner "Projekte" für den Gast."""
    return FolderGuestShare.objects.create(
        folder=guests.folder_root, user=guests.user_multi, level="view", created_by=guests.user_admin
    )


@pytest.fixture
def share_sub_edit(guests: Guests, share_root: FolderGuestShare) -> FolderGuestShare:
    """Zusätzliche Freigabe (edit) auf den Unterordner "2026" — höheres Level im Teilbaum."""
    return FolderGuestShare.objects.create(
        folder=guests.folder_sub, user=guests.user_multi, level="edit", created_by=guests.user_admin
    )


@pytest.fixture
def guest_client(db: None) -> Client:
    """Minimal-Setup für die Isolations-Matrix: Org A mit einem eingeloggten Gast."""
    org_a = _create_org("Fraktion A", ORG_SLUG_A)
    user_multi = _create_user("multi@example.org")
    _create_membership(user_multi, org_a, is_guest=True)
    return _login(user_multi)


def _content(response: Any) -> str:
    return str(response.content.decode("utf-8", errors="ignore"))


def _redirects_to_guest_overview(response: Any) -> bool:
    return bool(response.status_code == 302 and response.headers.get("Location", "").endswith("/freigaben/"))


# =============================================================================
# A. Ordner-Freigabe: rekursive Semantik
# =============================================================================


@pytest.mark.django_db
def test_folder_share_grants_access_recursively(guests: Guests, share_root: FolderGuestShare) -> None:
    assert guests.doc_root.can_access(guests.m_guest_a), "Dokument im freigegebenen Ordner nicht zugänglich"
    assert guests.doc_sub.can_access(guests.m_guest_a), "Dokument im Unterordner (rekursiv) nicht zugänglich"


@pytest.mark.django_db
def test_folder_share_does_not_leak_outside_subtree(guests: Guests, share_root: FolderGuestShare) -> None:
    assert not guests.doc_secret.can_access(guests.m_guest_a), "Dokument in fremdem Ordner zugänglich"
    assert not guests.doc_no_folder.can_access(guests.m_guest_a), "Dokument ohne Ordner/Freigabe zugänglich"


@pytest.mark.django_db
def test_view_level_allows_neither_edit_nor_comment(guests: Guests, share_root: FolderGuestShare) -> None:
    assert not guests.doc_root.can_edit(guests.m_guest_a), "Level view erlaubt Edit"
    assert not guests.doc_root.can_comment(guests.m_guest_a), "Level view erlaubt Kommentar"


@pytest.mark.django_db
def test_future_document_in_subtree_is_accessible(guests: Guests, share_root: FolderGuestShare) -> None:
    doc_future = Motion.objects.create(
        organization=guests.org_a,
        author=guests.m_admin,
        title="Später hinzugefügt",
        visibility="private",
        folder=guests.folder_subsub,
    )
    assert doc_future.can_access(guests.m_guest_a), "Künftiges Dokument im Teilbaum nicht zugänglich"


@pytest.mark.django_db
def test_editor_access_follows_folder_share(guests: Guests, share_root: FolderGuestShare) -> None:
    allowed = guests.c_guest.get(f"{BASE_A}/documents/{guests.doc_sub.id}/")
    assert allowed.status_code == 200, f"Editor für Ordner-Dokument: erhalten {allowed.status_code}"
    denied = guests.c_guest.get(f"{BASE_A}/documents/{guests.doc_secret.id}/")
    assert denied.status_code == 403, f"Editor für nicht freigegebenes Dokument: erhalten {denied.status_code}"


@pytest.mark.django_db
def test_visible_to_contains_subtree_and_direct_share_only(guests: Guests, share_root: FolderGuestShare) -> None:
    Motion.objects.create(
        organization=guests.org_a,
        author=guests.m_admin,
        title="Später hinzugefügt",
        visibility="private",
        folder=guests.folder_subsub,
    )
    visible = set(_visible_to(guests.m_guest_a).values_list("title", flat=True))
    assert visible == {"Projektplan", "Jahresplanung", "Später hinzugefügt", "Direkt geteilt"}, str(visible)


def _visible_to(membership: Membership) -> Any:
    return Motion.visible_to(membership)  # type: ignore[no-untyped-call]


# =============================================================================
# A. Level-Vererbung (höchstes Level gewinnt)
# =============================================================================


@pytest.mark.django_db
def test_folder_levels_inherit_highest_level(guests: Guests, share_sub_edit: FolderGuestShare) -> None:
    levels = FolderGuestShare.shared_folder_levels(guests.user_multi, guests.org_a)
    assert levels.get(guests.folder_root.id) == "view", f"Wurzel-Ordner: {levels}"
    assert levels.get(guests.folder_sub.id) == "edit", "Unterordner: direkte Freigabe schlägt geerbte nicht"
    assert levels.get(guests.folder_subsub.id) == "edit", "Tiefster Ordner erbt edit nicht"
    assert guests.folder_secret.id not in levels, "Nicht freigegebener Ordner in den Freigaben"


@pytest.mark.django_db
def test_edit_subtree_grants_edit_and_comment(guests: Guests, share_sub_edit: FolderGuestShare) -> None:
    assert guests.doc_sub.can_edit(guests.m_guest_a), "Dokument im edit-Teilbaum: kein Edit"
    assert guests.doc_sub.can_comment(guests.m_guest_a), "Dokument im edit-Teilbaum: kein Kommentar"
    assert not guests.doc_root.can_edit(guests.m_guest_a), "Dokument im view-Teil: Edit möglich"


# =============================================================================
# A. Gast-Übersicht: navigierbarer Baum
# =============================================================================


@pytest.mark.django_db
def test_guest_overview_lists_shared_root_and_direct_share(guests: Guests, share_root: FolderGuestShare) -> None:
    response = guests.c_guest.get(f"{BASE_A}/freigaben/")
    assert response.status_code == 200, f"Übersicht: erhalten {response.status_code}"
    content = _content(response)
    assert "Projekte" in content, "Übersicht listet freigegebenen Wurzel-Ordner nicht"
    assert "Direkt geteilt" in content, "Übersicht listet Direkt-Freigabe nicht"
    assert "Intern" not in content, "Übersicht listet fremden Ordner"


@pytest.mark.django_db
def test_guest_overview_folder_view_shows_documents_and_subfolders(
    guests: Guests, share_root: FolderGuestShare
) -> None:
    response = guests.c_guest.get(f"{BASE_A}/freigaben/?ordner={guests.folder_root.id}")
    assert response.status_code == 200, f"Ordner-Ansicht: erhalten {response.status_code}"
    content = _content(response)
    assert "Projektplan" in content, "Ordner-Ansicht zeigt Dokument nicht"
    assert "2026" in content, "Ordner-Ansicht zeigt Unterordner nicht"


@pytest.mark.django_db
def test_guest_overview_deep_subfolder_is_navigable(guests: Guests, share_root: FolderGuestShare) -> None:
    Motion.objects.create(
        organization=guests.org_a,
        author=guests.m_admin,
        title="Später hinzugefügt",
        visibility="private",
        folder=guests.folder_subsub,
    )
    response = guests.c_guest.get(f"{BASE_A}/freigaben/?ordner={guests.folder_subsub.id}")
    assert response.status_code == 200, f"Unter-Unterordner: erhalten {response.status_code}"
    assert "Später hinzugefügt" in _content(response), "Künftiges Dokument nicht sichtbar"


@pytest.mark.django_db
def test_guest_overview_unshared_or_unknown_folder_is_404(guests: Guests, share_root: FolderGuestShare) -> None:
    unshared = guests.c_guest.get(f"{BASE_A}/freigaben/?ordner={guests.folder_secret.id}")
    assert unshared.status_code == 404, f"Nicht freigegebener Ordner: erhalten {unshared.status_code}"
    unknown = guests.c_guest.get(f"{BASE_A}/freigaben/?ordner={uuid.uuid4()}")
    assert unknown.status_code == 404, f"Unbekannter Ordner: erhalten {unknown.status_code}"


# =============================================================================
# A. Verwaltungs-Endpunkte (freigeben/entziehen)
# =============================================================================


def _share_url(org_slug: str, folder: DocumentFolder) -> str:
    return f"/work/{org_slug}/documents/folders/{folder.id}/share/"


@pytest.mark.django_db
def test_admin_can_share_folder_with_guest(guests: Guests) -> None:
    response = guests.c_admin.post(
        _share_url(ORG_SLUG_A, guests.folder_secret),
        {"email": guests.user_multi.email, "level": "view"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200, f"Ordner freigeben: erhalten {response.status_code}"
    share = FolderGuestShare.objects.filter(folder=guests.folder_secret, user=guests.user_multi).first()
    assert share is not None and share.level == "view", "Freigabe nicht mit level=view angelegt"
    assert guests.doc_secret.can_access(guests.m_guest_a), "Gast sieht Dokument im neu freigegebenen Ordner nicht"


@pytest.mark.django_db
def test_share_folder_rejects_unknown_email(guests: Guests) -> None:
    response = guests.c_admin.post(
        _share_url(ORG_SLUG_A, guests.folder_secret),
        {"email": "unbekannt@example.org", "level": "view"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400, f"Unbekannte E-Mail: erhalten {response.status_code}"


@pytest.mark.django_db
def test_share_folder_rejects_user_without_org_access(guests: Guests) -> None:
    response = guests.c_admin.post(
        _share_url(ORG_SLUG_A, guests.folder_secret),
        {"email": guests.user_admin_c.email, "level": "view"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400, f"Nutzer ohne Org-Zugang: erhalten {response.status_code}"


@pytest.mark.django_db
def test_member_without_permission_cannot_share_folder(guests: Guests) -> None:
    # Mitglied ohne guests.manage/motions.share kommt nicht an den Endpunkt
    response = _login(guests.user_plain).post(
        _share_url(ORG_SLUG_A, guests.folder_secret),
        {"email": guests.user_multi.email, "level": "view"},
    )
    assert response.status_code == 403, f"Mitglied ohne Recht: erhalten {response.status_code}"


@pytest.mark.django_db
def test_foreign_org_cannot_touch_folder_shares(guests: Guests, share_root: FolderGuestShare) -> None:
    share = guests.c_admin_c.post(
        _share_url(guests.org_c.slug, guests.folder_secret),
        {"email": guests.user_admin_c.email, "level": "view"},
    )
    assert share.status_code == 404, f"Fremde Org: Ordner-Freigabe erhalten {share.status_code}"
    remove = guests.c_admin_c.post(f"/work/{guests.org_c.slug}/documents/folders/shares/{share_root.id}/remove/")
    assert remove.status_code == 404, f"Fremde Org: Freigabe entziehen erhalten {remove.status_code}"


@pytest.mark.django_db
def test_admin_can_revoke_folder_share(guests: Guests) -> None:
    share = FolderGuestShare.objects.create(
        folder=guests.folder_secret, user=guests.user_multi, level="view", created_by=guests.user_admin
    )
    assert guests.doc_secret.can_access(guests.m_guest_a)
    response = guests.c_admin.post(
        f"{BASE_A}/documents/folders/shares/{share.id}/remove/", HTTP_X_REQUESTED_WITH="XMLHttpRequest"
    )
    assert response.status_code == 200, f"Freigabe entziehen: erhalten {response.status_code}"
    assert not guests.doc_secret.can_access(guests.m_guest_a), "Zugriff nach Entzug nicht dicht"


# =============================================================================
# A. Gast-Einladung mit Ordner-Freigabe
# =============================================================================


@pytest.mark.django_db
def test_guest_invitation_page_offers_folders(guests: Guests) -> None:
    response = guests.c_admin.get(f"{BASE_A}/organization/members/invite-guest/")
    assert response.status_code == 200, f"Einladungsseite: erhalten {response.status_code}"
    content = _content(response)
    assert "Ordner freigeben" in content, "Einladungsseite ohne Ordner-Auswahl"
    assert "Projekte" in content and "Intern" in content, "Einladungsseite listet Ordner nicht"


@pytest.mark.django_db
def test_guest_invitation_creates_folder_share(guests: Guests) -> None:
    response = guests.c_admin.post(
        f"{BASE_A}/organization/members/invite-guest/",
        {"email": "ordnergast@example.org", "share_level": "comment", "folders": [str(guests.folder_root.id)]},
    )
    assert response.status_code == 302, f"Einladung: erhalten {response.status_code}"
    folder_guest = User.objects.filter(email="ordnergast@example.org").first()
    assert folder_guest is not None, "Gast-User nicht angelegt"
    share = FolderGuestShare.objects.filter(folder=guests.folder_root, user=folder_guest).first()
    assert share is not None and share.level == "comment", "Ordner-Freigabe aus Einladung fehlt oder falsches Level"
    m_folder_guest = Membership.objects.get(user=folder_guest, organization=guests.org_a)
    assert guests.doc_sub.can_comment(m_folder_guest), "Eingeladener Gast kann Dokument im Teilbaum nicht kommentieren"
    assert guests.org_a.get_active_guest_count() == 2, "Gast-Limit zählt nicht pro Gast"


# =============================================================================
# B. Isolations-Matrix: alle work-URLs als Gast (GET + POST)
# =============================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("name", MATRIX_URL_NAMES)
def test_guest_matrix_url_is_blocked(guest_client: Client, name: str, method: str) -> None:
    url = _matrix_url(name)
    request = getattr(guest_client, method)
    response = request(url)
    location = response.headers.get("Location", "")
    blocked = (response.status_code in (301, 302) and location.endswith("/freigaben/")) or response.status_code in (
        403,
        404,
        405,
    )
    if not blocked and response.status_code in (301, 302):
        # Legacy-Redirects (/motions/ -> /documents/): Kette folgen — das Ziel muss selbst dicht sein
        # (Gast-Übersicht, 403 oder 404).
        final = request(url, follow=True)
        final_path = final.request.get("PATH_INFO", "")
        blocked = final.status_code in (403, 404, 405) or (
            final.status_code == 200 and final_path.endswith("/freigaben/")
        )
    assert blocked, f"{method.upper()} work:{name} ({url}) für Gast offen: {response.status_code} {location}"


@pytest.mark.django_db
def test_whitelist_views_are_reachable_for_guest(guests: Guests) -> None:
    assert guests.c_guest.get(f"{BASE_A}/freigaben/").status_code == 200, "Gast-Übersicht nicht erreichbar"
    assert guests.c_guest.get(f"{BASE_A}/profile/").status_code == 200, "Profil nicht erreichbar"


@pytest.mark.django_db
def test_whitelist_views_check_shares(guests: Guests, share_root: FolderGuestShare) -> None:
    doc = guests.doc_no_folder.id
    revisions = guests.c_guest.get(f"{BASE_A}/documents/{doc}/revisions/")
    assert revisions.status_code == 403, f"Revisions ohne Freigabe: erhalten {revisions.status_code}"
    export = guests.c_guest.get(f"{BASE_A}/documents/{doc}/export/?format=pdf")
    assert export.status_code == 403, f"Export ohne Freigabe: erhalten {export.status_code}"
    comment = guests.c_guest.post(f"{BASE_A}/documents/{doc}/comment/", {"content": "Hack"})
    assert comment.status_code == 403, f"Kommentar ohne Freigabe: erhalten {comment.status_code}"
    shared = guests.c_guest.get(f"{BASE_A}/documents/{guests.doc_sub.id}/revisions/")
    assert shared.status_code == 200, f"Revisions mit Ordner-Freigabe: erhalten {shared.status_code}"


@pytest.mark.django_db
def test_guest_without_membership_is_forbidden_in_foreign_org(guests: Guests) -> None:
    response = guests.c_guest.get(f"/work/{guests.org_c.slug}/freigaben/")
    assert response.status_code == 403, f"Fremde Org ohne Membership: erhalten {response.status_code}"


# =============================================================================
# B. WebSocket-Consumer
# =============================================================================


def _doc_ws_access(user: User, motion: Motion) -> tuple[str | None, Any]:
    # Consumer-Klassen sind untypisiert (Channels): als Any instanziieren, um private Attribute setzen zu dürfen
    consumer = cast(Any, DocumentCollaborationConsumer)()
    consumer.document_id = str(motion.id)
    consumer.user = user
    access, membership_id = async_to_sync(consumer._check_access)()
    return access, membership_id


def _prep_ws_access(user: User, org_slug: str, paper: OParlPaper) -> Any:
    consumer = cast(Any, PreparationConsumer)()
    consumer.org_slug = org_slug
    consumer.scope_type = "paper"
    consumer.object_id = str(paper.id)
    return async_to_sync(consumer._check_access)(user)


# transaction=True: der Consumer schließt über database_sync_to_async alte Verbindungen; innerhalb einer
# Test-Transaktion (Postgres) wäre die Verbindung danach tot.
@pytest.mark.django_db(transaction=True)
def test_document_consumer_follows_folder_shares(guests: Guests, share_sub_edit: FolderGuestShare) -> None:
    access, _ = _doc_ws_access(guests.user_multi, guests.doc_no_folder)
    assert access is None, f"Doc-WS: Gast ohne Freigabe zugelassen ({access})"
    access, membership_id = _doc_ws_access(guests.user_multi, guests.doc_sub)
    assert access == "edit" and membership_id == guests.m_guest_a.id, f"Doc-WS: Ordner-Freigabe edit -> {access}"
    access, _ = _doc_ws_access(guests.user_multi, guests.doc_root)
    assert access == "view", f"Doc-WS: Ordner-Freigabe view -> {access}"
    access, _ = _doc_ws_access(guests.user_admin_c, guests.doc_sub)
    assert access is None, f"Doc-WS: Fremder ohne Membership zugelassen ({access})"


@pytest.mark.django_db(transaction=True)
def test_preparation_consumer_excludes_guests(guests: Guests) -> None:
    paper = OParlPaper.objects.create(external_id="https://ris.example.org/paper/1", body=guests.body, name="Vorlage")
    assert _prep_ws_access(guests.user_multi, guests.org_a.slug, paper) is None, "Prep-WS: Gast zugelassen"
    assert _prep_ws_access(guests.user_admin, guests.org_a.slug, paper) == guests.org_a.id, "Prep-WS: Admin abgewiesen"


# =============================================================================
# C. Multi-Org: Gast in A + Voll-Mitglied in B
# =============================================================================


@pytest.mark.django_db
def test_guest_dashboard_redirects_to_guest_overview(guests: Guests) -> None:
    response = guests.c_guest.get(f"{BASE_A}/")
    assert _redirects_to_guest_overview(response), (
        f"In A: Dashboard erhalten {response.status_code} -> {response.headers.get('Location')}"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("label", "path"),
    [("Fraktionssitzungen", "/faction/"), ("Aufgaben", "/tasks/"), ("Mitglieder", "/organization/members/")],
)
def test_guest_is_blocked_in_org_a(guests: Guests, label: str, path: str) -> None:
    response = guests.c_guest.get(f"{BASE_A}{path}")
    assert _redirects_to_guest_overview(response), f"In A: {label} nicht dicht (erhalten {response.status_code})"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("label", "path"),
    [
        ("Dashboard", "/"),
        ("Dokumente", "/documents/"),
        ("Fraktionssitzungen", "/faction/"),
        ("Mitgliederliste", "/organization/members/"),
    ],
)
def test_full_member_has_access_in_org_b(guests: Guests, label: str, path: str) -> None:
    response = guests.c_guest.get(f"{guests.base_b}{path}")
    assert response.status_code == 200, f"In B: {label} erhalten {response.status_code}"


@pytest.mark.django_db
def test_org_switcher_shows_both_organizations(guests: Guests) -> None:
    in_b = _content(guests.c_guest.get(f"{guests.base_b}/"))
    assert f"/work/{guests.org_a.slug}/" in in_b, "Switcher in B zeigt Org A nicht"
    assert "(Gast)" in in_b, "Switcher in B markiert Gast-Zugang nicht"
    in_a = _content(guests.c_guest.get(f"{BASE_A}/freigaben/"))
    assert f"/work/{guests.org_b.slug}/" in in_a, "Switcher in A (Gast-Ansicht) zeigt Org B nicht"


# =============================================================================
# C. Bestehender User in weitere Org einladbar
# =============================================================================


@pytest.mark.django_db
def test_existing_user_can_be_invited_as_guest_elsewhere(guests: Guests) -> None:
    guests.c_admin_c.post(
        f"/work/{guests.org_c.slug}/organization/members/invite-guest/",
        {"email": guests.user_multi.email, "share_level": "view"},
    )
    m_guest_c = Membership.objects.filter(user=guests.user_multi, organization=guests.org_c).first()
    assert m_guest_c is not None and m_guest_c.is_guest, "Gast-Einladung für bestehenden User ohne Gast-Membership"
    assert guests.user_multi.memberships.filter(is_active=True).count() == 3, "Erwartet 3 Mitgliedschaften (A, B, C)"
    response = guests.c_guest.get(f"/work/{guests.org_c.slug}/")
    assert _redirects_to_guest_overview(response), f"In C: als Gast nicht nur Freigaben ({response.status_code})"


@pytest.mark.django_db
def test_existing_guest_can_accept_regular_invitation(guests: Guests) -> None:
    org_d = _create_org("Fraktion D", "fraktion-d", body=guests.body)
    member_role_d = Role.objects.filter(organization=org_d, name="Fraktionsmitglied").first()
    assert member_role_d is not None
    invitation = UserInvitation.create_for_organization(
        organization=org_d,
        email=guests.user_multi.email,
        invited_by=guests.user_admin_c,
        roles=Role.objects.filter(id=member_role_d.id),
        valid_days=7,
    )
    guests.c_guest.post(f"/work/invitation/{invitation.token}/")
    m_member_d = Membership.objects.filter(user=guests.user_multi, organization=org_d).first()
    assert m_member_d is not None and not m_member_d.is_guest, "Reguläre Einladung ergab keine Voll-Mitgliedschaft"
    invitation.refresh_from_db()
    assert invitation.accepted_at is not None, "Einladung nicht als angenommen markiert"
    response = guests.c_guest.get(f"/work/{org_d.slug}/")
    assert response.status_code == 200, f"In D: Dashboard als Voll-Mitglied erhalten {response.status_code}"
    # Gast-Status ist pro Membership, nicht pro User
    assert guests.m_guest_a.is_guest and not guests.m_member_b.is_guest and not m_member_d.is_guest


@pytest.mark.django_db
def test_guest_status_is_per_membership(guests: Guests) -> None:
    guests.c_admin_c.post(
        f"/work/{guests.org_c.slug}/organization/members/invite-guest/",
        {"email": guests.user_multi.email, "share_level": "view"},
    )
    memberships = {
        m.organization.slug: m.is_guest for m in guests.user_multi.memberships.select_related("organization")
    }
    assert memberships == {ORG_SLUG_A: True, guests.org_b.slug: False, guests.org_c.slug: True}, str(memberships)
