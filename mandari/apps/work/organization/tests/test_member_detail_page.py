# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Mitglied-Detailseite (Hotspot-Zerlegung, Issue #174):
Die Seite besteht aus `member_detail.html` und den Partials unter
`work/organization/partials/_member_*.html`; Bestätigungen laufen über `confirmAction`
statt `onsubmit="confirm(…)"`, das Vereidigungs-Formular über Alpine statt `onchange`.
"""

import re
from typing import Any

import pytest
from django.urls import reverse

EDIT_PERMISSIONS = ["members.view", "members.edit"]
INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*type=\"application/json\")", re.I)
ON_HANDLER_RE = re.compile(r"\son[a-z]+=\"", re.I)


def assert_templates_clean(response: Any, prefix: str) -> None:
    """Eigene Templates (nicht das Layout) enthalten weder Inline-Skripte noch on*-Handler."""
    names = {t.name for t in response.templates if t.name and t.name.startswith(prefix)}
    assert names, f"keine Templates mit Präfix {prefix} gerendert"
    for template in response.templates:
        if template.name in names:
            source = template.source
            assert not INLINE_SCRIPT_RE.search(source), template.name
            assert not ON_HANDLER_RE.search(source), template.name


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, EDIT_PERMISSIONS, email="admin@example.org")


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.view"], email="mitglied@example.org")


def detail_url(org: Any, member: Any) -> str:
    return reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": member.id})


@pytest.mark.django_db
def test_member_detail_renders_partials_without_inline_handlers(
    org: Any, admin: Any, member: Any, client_for: Any
) -> None:
    response = client_for(admin.user).get(detail_url(org, member))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Rollen verwalten" in html
    assert "Effektive Berechtigungen" in html
    assert 'name="action" value="update_sworn_in"' in html
    assert '@change="$el.form.submit()"' in html
    assert "Mitglied deaktivieren" in html
    assert "confirmAction({title: 'Mitglied entfernen'" in html
    assert "Keine Kommune verknüpft" in html

    used = {t.name for t in response.templates if t.name}
    assert "work/organization/partials/_member_permissions.html" in used
    assert "work/organization/partials/_member_committees.html" in used
    assert "work/organization/partials/_member_actions.html" in used
    assert "work/organization/partials/_member_guest_access.html" not in used
    assert_templates_clean(response, "work/organization/")
    assert not re.search(r"<c-[a-z]", html)


@pytest.mark.django_db
def test_member_detail_for_guest_shows_share_overview(org: Any, admin: Any, make_member: Any, client_for: Any) -> None:
    guest = make_member(org, [], email="gast@example.org")
    guest.is_guest = True
    guest.save(update_fields=["is_guest"])

    response = client_for(admin.user).get(detail_url(org, guest))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Was sieht dieser Gast?" in html
    assert "Dieser Gast hat aktuell keine Freigaben" in html
    assert "Rollen verwalten" not in html
    assert "Effektive Berechtigungen" not in html
    used = {t.name for t in response.templates if t.name}
    assert "work/organization/partials/_member_guest_access.html" in used
    assert_templates_clean(response, "work/organization/")


@pytest.mark.django_db
def test_member_detail_read_only_for_viewer(org: Any, admin: Any, member: Any, client_for: Any) -> None:
    response = client_for(member.user).get(detail_url(org, admin))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Rollen verwalten</h2>" not in html
    assert 'value="update_roles"' not in html
    assert "Aktionen</h2>" not in html
    assert "Rollen</h2>" in html
    used = {t.name for t in response.templates if t.name}
    assert "work/organization/partials/_member_actions.html" not in used
