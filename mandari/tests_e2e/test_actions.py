# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Deklarative Aktionen statt Inline-Handlern (#172, ``frontend/js/actions.ts``).

Eine Content-Security-Policy ohne ``unsafe-inline`` verbietet ``onclick=``/``onsubmit=``.
Die Templates beschreiben Aktionen in ``data-*``-Attributen; das Bundle hängt delegierte
Listener an ``document``. Geprüft wird das echte Bundle auf einer echten Seite (Login),
in die die Testfälle kleine Fragmente einfügen – so bleibt der Test unabhängig von den
Fachseiten, die die Attribute verwenden.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db


def _seite_mit_fragment(page: Any, live_server: Any, html: str) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.evaluate(
        "(html) => { const box = document.createElement('div'); box.id = 'probe'; box.innerHTML = html; document.body.append(box) }",
        html,
    )


def test_data_confirm_haelt_formular_bei_abbruch_auf(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<form id="f" method="get" action="/accounts/login/" data-confirm="Wirklich?">'
        '<input type="hidden" name="probe" value="1"><button id="b" type="submit">Los</button></form>',
    )
    fragen: list[str] = []

    def abbrechen(dialog: Any) -> None:
        fragen.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", abbrechen)
    page.click("#b")
    page.wait_for_timeout(300)
    assert fragen == ["Wirklich?"]
    assert "probe=1" not in page.url, "Abbruch der Rückfrage darf nicht absenden"

    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#b")
    page.wait_for_url("**/accounts/login/?probe=1")


def test_data_autosubmit_und_filter_param(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<form method="get" action="/accounts/login/"><select id="s" name="sort" data-autosubmit>'
        '<option value="">–</option><option value="neu">neu</option></select></form>',
    )
    page.select_option("#s", "neu")
    page.wait_for_url("**/accounts/login/?sort=neu")

    _seite_mit_fragment(
        page,
        live_server,
        '<select id="st" data-filter-param="status"><option value="">Alle</option><option value="open">offen</option></select>',
    )
    page.select_option("#st", "open")
    page.wait_for_url("**/accounts/login/?status=open")


def test_data_href_navigiert_ausser_auf_inneren_links(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<table><tr id="zeile" data-href="/accounts/login/?zeile=1"><td id="zelle">Text</td>'
        '<td><a id="innen" href="/accounts/login/?innen=1">innen</a></td></tr></table>',
    )
    page.click("#innen")
    page.wait_for_url("**/accounts/login/?innen=1")

    _seite_mit_fragment(
        page, live_server, '<div id="zeile" data-href="/accounts/login/?zeile=1"><span id="zelle">Text</span></div>'
    )
    page.click("#zelle")
    page.wait_for_url("**/accounts/login/?zeile=1")


def test_data_prompt_und_submit_to(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<form id="f" method="get" action="/accounts/login/" data-prompt="Grund?" data-prompt-field="reason">'
        '<input type="hidden" name="reason" value=""><button id="b" type="submit">Absetzen</button></form>',
    )
    page.once("dialog", lambda dialog: dialog.accept("krank"))
    page.click("#b")
    page.wait_for_url("**/accounts/login/?reason=krank")

    _seite_mit_fragment(
        page,
        live_server,
        '<form id="g" method="get" data-submit-to="/accounts/login/{person}/"><select id="p" name="person">'
        '<option value="">–</option><option value="7">7</option></select><button id="c" type="submit">Export</button></form>',
    )
    page.click("#c")
    page.wait_for_timeout(300)
    assert page.url.endswith("/accounts/login/"), "leeres Feld darf nicht absenden"
    page.select_option("#p", "7")
    page.click("#c")
    page.wait_for_url("**/accounts/login/7/?person=7")


def test_data_action_clear_target(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<div id="box"><p id="inhalt">Formular</p><button id="x" type="button" data-action="clear-target" data-target="#box">Abbrechen</button></div>',
    )
    page.click("#x")
    expect(page.locator("#inhalt")).to_have_count(0)
