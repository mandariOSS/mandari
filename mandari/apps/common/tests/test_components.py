# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests der Komponentenbibliothek (django-cotton, ``templates/cotton/``).

Prüft das erzeugte Markup der Basiskomponenten, rendert die UI-Kit-Vorschau
und die auf Komponenten umgestellten Konto-Seiten (Pilot, Issue #167).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.template import engines
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django_cotton.compiler_regex import CottonCompiler

from apps.common.views_dev import UiKitDemoForm, build_demo_form


def render(source: str, **context: object) -> str:
    """Rendert einen Template-String inklusive Cotton-Kompilierung (wie der Loader)."""
    compiled = CottonCompiler().process(source)
    return engines["django"].from_string(compiled).render(context)


class TestButton:
    def test_default_is_primary_button(self) -> None:
        html = render("<c-ui.button>Speichern</c-ui.button>")
        assert '<button type="button"' in html
        assert "bg-primary-600" in html
        assert "Speichern" in html

    def test_submit_variant_size_and_full_width(self) -> None:
        html = render('<c-ui.button type="submit" variant="danger" size="lg" full>Löschen</c-ui.button>')
        assert 'type="submit"' in html
        assert "bg-red-600" in html
        assert "min-h-[48px]" in html
        assert "w-full" in html

    def test_href_renders_link_with_icon(self) -> None:
        html = render('<c-ui.button href="/x/" icon="plus">Neu</c-ui.button>')
        assert html.strip().startswith('<a href="/x/"')
        assert html.strip().endswith("</a>")
        assert 'data-lucide="plus"' in html
        assert 'aria-hidden="true"' in html

    def test_extra_attributes_are_passed_through(self) -> None:
        html = render('<c-ui.button hx-post="/save/" disabled>Ok</c-ui.button>')
        assert 'hx-post="/save/"' in html
        assert "disabled" in html


class TestFormField:
    def test_bound_field_with_error_is_marked_accessible(self) -> None:
        form = build_demo_form()
        html = render('<c-form.field :field="form.email" label="E-Mail" type="email" required />', form=form)
        assert '<label for="id_email"' in html
        assert 'name="email"' in html
        assert 'id="id_email"' in html
        assert 'value="keine-adresse"' in html
        assert 'aria-invalid="true"' in html
        assert 'aria-describedby="id_email-error"' in html
        assert 'id="id_email-error"' in html
        assert "border-red-400" in html

    def test_unbound_field_uses_help_text(self) -> None:
        form = UiKitDemoForm()
        html = render('<c-form.field :field="form.email" label="E-Mail" help="Hilfe" />', form=form)
        assert 'aria-describedby="id_email-help"' in html
        assert 'id="id_email-help"' in html
        assert "aria-invalid" not in html
        assert 'value=""' in html

    def test_free_field_without_django_form(self) -> None:
        html = render('<c-form.field name="suche" label="Suche" value="abc" />')
        assert 'name="suche"' in html
        assert 'id="id_suche"' in html
        assert 'for="id_suche"' in html
        assert 'value="abc"' in html

    def test_password_field_has_toggle(self) -> None:
        form = UiKitDemoForm()
        html = render('<c-form.password :field="form.password" label="Passwort" />', form=form)
        assert 'name="password"' in html
        assert 'type="password"' in html
        assert 'aria-label="Passwort anzeigen"' in html
        assert 'autocomplete="current-password"' in html

    def test_checkbox_and_form_errors(self) -> None:
        form = build_demo_form()
        html = render(
            '<c-form.checkbox name="remember_me" label="Bleiben" checked /><c-form.errors :form="form" />',
            form=form,
        )
        assert 'type="checkbox" name="remember_me" id="id_remember_me" checked' in html
        assert 'role="alert"' in html
        assert "formularweiten Fehler" in html

    def test_errors_component_is_silent_without_errors(self) -> None:
        html = render('<c-form.errors :form="form" />', form=UiKitDemoForm())
        assert html.strip() == ""


class TestOtherComponents:
    def test_alert_roles(self) -> None:
        assert 'role="status"' in render("<c-ui.alert>Hi</c-ui.alert>")
        assert 'role="alert"' in render('<c-ui.alert variant="error">Hi</c-ui.alert>')

    def test_card_slots(self) -> None:
        html = render(
            '<c-ui.card title="Titel" subtitle="Unter"><c-slot name="actions">A</c-slot>Inhalt'
            '<c-slot name="footer">F</c-slot></c-ui.card>'
        )
        assert "<header" in html
        assert "Titel" in html
        assert "Unter" in html
        assert "<footer" in html
        assert "Inhalt" in html

    def test_card_without_title_has_no_header(self) -> None:
        html = render("<c-ui.card>Inhalt</c-ui.card>")
        assert "<header" not in html
        assert "<footer" not in html

    def test_badge_color(self) -> None:
        assert "bg-green-100" in render('<c-ui.badge color="green">Ok</c-ui.badge>')

    def test_modal_uses_native_dialog(self) -> None:
        html = render('<c-ui.modal id="m" title="T">Body</c-ui.modal>')
        assert '<dialog id="m" aria-labelledby="m-title"' in html
        assert 'id="m-title"' in html
        assert 'aria-label="Schließen"' in html

    def test_icon_accessibility(self) -> None:
        assert 'aria-hidden="true"' in render('<c-ui.icon name="check" />')
        html = render('<c-ui.icon name="check" label="Erledigt" />')
        assert 'role="img" aria-label="Erledigt"' in html
        assert "aria-hidden" not in html

    def test_empty_state_and_th(self) -> None:
        assert "<h3" in render('<c-ui.empty-state title="Leer">x</c-ui.empty-state>')
        assert 'scope="col"' in render("<c-ui.th>Spalte</c-ui.th>")


class TestUiKitPreview:
    def test_preview_template_renders_all_components(self) -> None:
        html = render_to_string("dev/ui_kit.html", {"form": build_demo_form(), "empty_form": UiKitDemoForm()})
        assert "UI-Kit" in html
        for marker in ("<dialog", "<header", "<footer", 'scope="col"', 'role="alert"', 'aria-invalid="true"'):
            assert marker in html, marker
        # Keine unaufgelösten Cotton-Tags im Ergebnis
        assert not re.search(r"<c-[a-z]", html)

    def test_preview_url_is_hidden_without_debug(self, client: Client, settings: Any) -> None:
        settings.DEBUG = False
        if reverse_or_none("dev_ui_kit") is None:
            pytest.skip("Vorschau-URL ist ohne DEBUG nicht registriert")
        assert client.get(reverse("dev_ui_kit")).status_code == 404


def reverse_or_none(name: str) -> str | None:
    from django.urls import NoReverseMatch

    try:
        return reverse(name)
    except NoReverseMatch:
        return None


@pytest.mark.django_db
class TestAccountsPilot:
    """Die Konto-Seiten nutzen ausschließlich Komponenten (Issue #167)."""

    def test_login_page(self, client: Client) -> None:
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'name="email"' in html
        assert 'name="password"' in html
        assert 'name="remember_me"' in html
        assert 'type="submit"' in html
        assert not re.search(r"<c-[a-z]", html)

    def test_login_error_is_rendered(self, client: Client) -> None:
        response = client.post(reverse("accounts:login"), {"email": "nobody@example.org", "password": "falsch"})
        assert response.status_code == 200
        html = response.content.decode()
        assert 'role="alert"' in html or 'aria-invalid="true"' in html

    def test_password_reset_pages(self, client: Client) -> None:
        assert client.get(reverse("accounts:password_reset")).status_code == 200
        response = client.get(
            reverse("accounts:password_reset_confirm", kwargs={"uidb64": "abc", "token": "ungueltig-token"})
        )
        assert response.status_code == 200
        assert "Link ungültig" in response.content.decode()
