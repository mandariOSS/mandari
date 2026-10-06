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

from apps.common.views_dev import UiKitDemoForm, build_demo_form, ui_kit_context


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

    def test_select_from_bound_field_marks_current_choice(self) -> None:
        form = build_demo_form()
        html = render('<c-form.select :field="form.role" label="Rolle" placeholder="Bitte wählen" />', form=form)
        assert '<select name="role" id="id_role"' in html
        assert '<option value="">Bitte wählen</option>' in html
        assert '<option value="chair" selected>Fraktionsvorsitz</option>' in html
        assert '<option value="member">Fraktionsmitglied</option>' in html

    def test_select_with_slot_options(self) -> None:
        html = render('<c-form.select name="sort" label="Sortierung"><option value="d">Datum</option></c-form.select>')
        assert 'name="sort" id="id_sort"' in html
        assert '<option value="d">Datum</option>' in html

    def test_textarea_bound_and_free(self) -> None:
        form = build_demo_form()
        html = render('<c-form.textarea :field="form.message" label="Nachricht" rows="3" />', form=form)
        assert '<textarea name="message" id="id_message" rows="3"' in html
        assert ">Beispieltext</textarea>" in html
        free = render('<c-form.textarea name="notiz" value="abc" />')
        assert 'name="notiz" id="id_notiz" rows="4"' in free
        assert ">abc</textarea>" in free

    def test_errors_component_is_silent_without_errors(self) -> None:
        html = render('<c-form.errors :form="form" />', form=UiKitDemoForm())
        assert html.strip() == ""

    def test_explicit_id_for_repeated_names(self) -> None:
        # Derselbe Name mehrfach auf einer Seite (z. B. je Sitzungsreihe): eigene ID, Label bleibt zugeordnet
        html = render(
            '<c-form.checkbox name="aktiv" id="aktiv_1" label="Aktiv" />'
            '<c-form.select name="tag" id="tag_1" label="Tag"><option value="0">Montag</option></c-form.select>'
            '<c-form.field name="zeit" id="zeit_1" type="time" label="Zeit" />'
        )
        for name, fid in (("aktiv", "aktiv_1"), ("tag", "tag_1"), ("zeit", "zeit_1")):
            assert f'name="{name}" id="{fid}"' in html
            assert f'for="{fid}"' in html
            assert f'id="id_{name}"' not in html
        # Ohne Angabe bleibt die ID aus dem Namen
        assert 'name="aktiv" id="id_aktiv"' in render('<c-form.checkbox name="aktiv" label="Aktiv" />')


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

    def test_tabs_are_wai_aria_conform(self) -> None:
        html = render(
            '<c-ui.tabs default="a" label="Test"><c-slot name="list"><c-ui.tab name="a">A</c-ui.tab>'
            '<c-ui.tab name="b">B</c-ui.tab></c-slot><c-ui.tab-panel name="a">PA</c-ui.tab-panel>'
            '<c-ui.tab-panel name="b">PB</c-ui.tab-panel></c-ui.tabs>'
        )
        assert 'role="tablist" aria-label="Test"' in html
        assert 'role="tab" id="tab-a" aria-controls="panel-a"' in html
        assert 'role="tabpanel" id="panel-b" aria-labelledby="tab-b"' in html
        assert "x-data=\"{ tab: 'a' }\"" in html
        assert "@keydown.right.prevent" in html

    def test_empty_state_and_th(self) -> None:
        assert "<h3" in render('<c-ui.empty-state title="Leer">x</c-ui.empty-state>')
        assert 'scope="col"' in render("<c-ui.th>Spalte</c-ui.th>")


class TestRahmen:
    """Gemeinsame Bausteine der Navigation von Insight und Work (Issue #852)."""

    def test_nav_link_aktiv_ueber_bereich_oder_schalter(self) -> None:
        aktiv = render(
            '<c-rahmen.nav-link href="/a/" icon="map" area="karte" aktuell="karte">Karte</c-rahmen.nav-link>'
        )
        assert 'aria-current="page"' in aktiv and "bg-band-hell" in aktiv
        inaktiv = render('<c-rahmen.nav-link href="/a/" icon="map" area="karte" aktuell="">Karte</c-rahmen.nav-link>')
        assert "aria-current" not in inaktiv
        dicht = render('<c-rahmen.nav-link href="/a/" icon="map" :aktiv="an" dicht>Karte</c-rahmen.nav-link>', an=True)
        assert 'aria-current="page"' in dicht and "h-9" in dicht and "rahmen-label" in dicht

    def test_insight_nutzt_die_gemeinsamen_bausteine(self) -> None:
        html = render(
            '<c-insight.nav-link href="/k/" icon="map" area="karte">Karte</c-insight.nav-link>', insight_area="karte"
        )
        assert 'aria-current="page"' in html and "rahmen-eintrag" in html
        # Beschriftung wie vor #852: die Navigationstests von Insight lesen sie über genau diese Klassen
        assert '<span class="flex-1 min-w-0 truncate">Karte</span>' in html and "rahmen-label" not in html
        tab = render('<c-insight.tab href="/k/" icon="map" area="karte">Karte</c-insight.tab>', insight_area="karte")
        assert 'aria-current="page"' in tab and "bg-primary-100" in tab
        blatt = render(
            '<c-insight.blatt-link href="/k/" icon="map" area="karte">Karte</c-insight.blatt-link>', insight_area="x"
        )
        assert "aria-current" not in blatt


class TestUiKitPreview:
    def test_preview_template_renders_all_components(self) -> None:
        html = render_to_string("dev/ui_kit.html", ui_kit_context())
        assert "UI-Kit" in html
        markers = ("<dialog", "<header", "<footer", 'scope="col"', 'role="alert"', 'aria-invalid="true"')
        for marker in (*markers, "Herunterladen", 'aria-current="step"'):
            assert marker in html, marker
        # Keine unaufgelösten Cotton-Tags im Ergebnis
        assert not re.search(r"<c-[a-z]", html)
        # Vorschaudaten vollständig: keine Links ohne Ziel (z. B. Filterspalte der Suche ab 2xl, #867)
        assert 'href=""' not in html and 'hx-get=""' not in html

    def test_preview_url_is_hidden_without_debug(self, client: Client, settings: Any) -> None:
        settings.DEBUG = False
        settings.UI_KIT_PREVIEW = False
        assert client.get(reverse("dev_ui_kit")).status_code == 404

    def test_preview_url_renders_with_preview_flag(self, client: Client, settings: Any) -> None:
        settings.UI_KIT_PREVIEW = True
        response = client.get(reverse("dev_ui_kit"))
        assert response.status_code == 200
        assert "UI-Kit" in response.content.decode()


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


class TestKpiTile:
    """Kennzahl-Kachel (c-ui.kpi-tile, Hotspot-Zerlegung #174)."""

    def test_default_tone_and_label(self) -> None:
        html = render('<c-ui.kpi-tile label="Gesamt" value="12" />')
        assert "Gesamt" in html
        assert ">12<" in html
        assert "text-gray-900 dark:text-white" in html
        assert "rounded-xl border" in html

    def test_tone_colors_value_and_passes_attributes(self) -> None:
        html = render('<c-ui.kpi-tile label="Überfällig" value="3" tone="red" class="mb-2" data-test="x" />')
        assert "text-red-600 dark:text-red-400" in html
        assert "mb-2" in html
        assert 'data-test="x"' in html


class TestLinkTile:
    """Verlinkte Kachel (c-ui.link-tile, Einstellungsübersicht Session, Issue #138)."""

    def test_link_icon_tone_title_and_slot(self) -> None:
        html = render(
            '<c-ui.link-tile href="/einstellungen/" icon="video" title="Sitzungsformate & mehr" '
            'tone="bg-sky-100 text-sky-600" data-test="x">Landesprofil</c-ui.link-tile>'
        )
        assert html.strip().startswith('<a href="/einstellungen/"')
        assert 'data-lucide="video"' in html
        assert 'aria-hidden="true"' in html
        assert "bg-sky-100 text-sky-600" in html
        assert "Sitzungsformate &amp; mehr" in html
        assert "Landesprofil" in html
        assert 'data-test="x"' in html


class TestPanelComponents:
    """Alpine-Modal, Panel-Abschnitt und Textbutton (Hotspot-Zerlegung #174, Satz B)."""

    def test_alpine_modal_binds_show_variable_and_sizes(self) -> None:
        html = render(
            '<c-ui.alpine-modal show="showItemModal" size="lg" panel_class="max-h-[90vh]" class="py-8">Body</c-ui.alpine-modal>'
        )
        assert 'x-show="showItemModal"' in html
        assert '@click="showItemModal = false"' in html
        assert "max-w-2xl" in html
        assert "max-h-[90vh]" in html
        assert "py-8" in html
        assert "Body" in html

    def test_alpine_modal_default_size_is_small(self) -> None:
        html = render('<c-ui.alpine-modal show="open">x</c-ui.alpine-modal>')
        assert "max-w-md" in html
        assert "x-cloak" in html

    def test_panel_section_renders_count_and_action_slot(self) -> None:
        html = render(
            '<c-ui.panel-section title="Aufgaben" :count="n"><c-slot name="action">'
            '<c-ui.text-button icon="plus">Aufgabe</c-ui.text-button></c-slot>Inhalt</c-ui.panel-section>',
            n=3,
        )
        assert "Aufgaben" in html
        assert "(3)" in html
        assert "Inhalt" in html
        assert 'data-lucide="plus"' in html
        assert "uppercase tracking-wider" in html

    def test_panel_section_hides_zero_count(self) -> None:
        html = render('<c-ui.panel-section title="Anhänge" :count="n">x</c-ui.panel-section>', n=0)
        assert "(0)" not in html

    def test_text_button_variants_and_attributes(self) -> None:
        assert "text-primary-600" in render("<c-ui.text-button>Ok</c-ui.text-button>")
        html = render(
            '<c-ui.text-button variant="danger" type="submit" icon="trash-2" title="Löschen">Entfernen</c-ui.text-button>'
        )
        assert 'type="submit"' in html
        assert "text-red-500" in html
        assert 'title="Löschen"' in html
        assert "text-gray-500" in render('<c-ui.text-button variant="muted">x</c-ui.text-button>')
