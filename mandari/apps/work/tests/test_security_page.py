# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Konto-Sicherheitsseite im Work-Portal (Issue #174, Satz A): Die Alpine-Komponente
`securitySettings` lebt in `frontend/alpine/security-settings.ts`; das Template liefert die
Passwortregeln als JSON (`json_script`), ist in Partials mit Cotton-Komponenten zerlegt und
enthält kein Inline-JavaScript mehr.
"""

import json
import re
from typing import Any

import pytest

REQUIREMENTS_RE = re.compile(r'<script[^>]*id="password-requirements"[^>]*>(.*?)</script>', re.S)


@pytest.mark.django_db
def test_security_page_provides_json_requirements_and_no_inline_script(
    org: Any, make_member: Any, client_for: Any
) -> None:
    member = make_member(org, ["dashboard.view"], email="sicher@example.org")
    response = client_for(member.user).get(f"/work/{org.slug}/profile/security/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="securitySettings"' in html

    match = REQUIREMENTS_RE.search(html)
    assert match, "json_script #password-requirements fehlt"
    requirements = json.loads(match.group(1))
    assert isinstance(requirements["min_length"], int)
    assert set(requirements) == {
        "min_length",
        "require_uppercase",
        "require_lowercase",
        "require_digit",
        "require_special",
    }

    # Formulare und Komponenten sind da, das frühere Inline-Skript nicht mehr
    assert 'name="old_password"' in html
    assert 'name="new_password"' in html
    assert 'name="confirm_password"' in html
    assert 'value="setup_2fa"' not in html  # 2FA-Einrichtung läuft per fetch in der Komponente
    assert "function securitySettings" not in html
    assert "checkPasswordStrength()" in html
    assert not re.search(r"<c-[a-z]", html)

    used_templates = {t.name for t in response.templates if t.name}
    for partial in (
        "_security_overview",
        "_security_password_card",
        "_security_two_factor_card",
        "_security_sessions_card",
        "_security_setup_modal",
        "_security_code_modals",
    ):
        assert f"work/profile/partials/{partial}.html" in used_templates, partial
    assert "cotton/ui/card.html" in used_templates
    assert "cotton/form/password.html" in used_templates
