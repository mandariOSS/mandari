# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Das Werkzeug für den Gleichstandstest (apps/work/tests/gleichstand.py, Issue #951) erkennt, was einer neuen Vorlage
fehlt: Rückfragen (data-confirm-*), Sperre gegen doppeltes Absenden (data-submit-once), gesperrte Felder (disabled),
versteckte Felder mit ihrem Wert, Ziele und Methoden von Formularen, htmx-Aufrufe, Alpine-Aufrufe und Kennungen.
"""

from __future__ import annotations

from apps.work.tests.gleichstand import fehlt, funktionen

BISHER = """
<main>
  <form method="post" action="/work/o/tasks/1/" data-submit-once data-confirm-title="Löschen?" data-confirm-text="Weg ist weg">
    <input type="hidden" name="action" value="delete">
    <input type="text" name="title" required>
    <select name="priority"><option value="low">Niedrig</option><option value="high">Hoch</option></select>
    <input type="checkbox" name="roles" value="2" disabled>
  </form>
  <button hx-post="/work/o/notifications/1/mark-read/" data-after-request="notification-read" data-parent="[data-n]">Gelesen</button>
  <button @click="deleteTask('/x/')" data-confirm-message="Wirklich?">Löschen</button>
  <div id="column-todo" x-data="kanbanBoard"></div>
  <a href="/work/o/tasks/export/?format=csv">CSV</a>
</main>
"""


def _ohne(html: str, teil: str) -> list[str]:
    assert teil in html, teil
    return fehlt(funktionen(BISHER, "/work/o/tasks/"), funktionen(html.replace(teil, "", 1), "/work/o/tasks/"))


def test_gleiche_seite_hat_keine_luecken() -> None:
    assert fehlt(funktionen(BISHER, "/p/"), funktionen(BISHER, "/p/")) == []


def test_sperre_gegen_doppeltes_absenden_zaehlt_zum_formular() -> None:
    luecken = _ohne(BISHER, " data-submit-once")
    assert any(x.startswith("Formular POST /work/o/tasks/1/") for x in luecken), luecken


def test_rueckfragen_mit_text() -> None:
    assert _ohne(BISHER, ' data-confirm-title="Löschen?"') == ["Aktion data-confirm-title=Löschen?"]
    assert _ohne(BISHER, ' data-confirm-text="Weg ist weg"') == ["Aktion data-confirm-text=Weg ist weg"]


def test_geaenderter_rueckfragetext_faellt_auf() -> None:
    luecken = fehlt(funktionen(BISHER, "/p/"), funktionen(BISHER.replace('"Wirklich?"', '"Sicher?"'), "/p/"))
    assert luecken == ["Aktion data-confirm-message=Wirklich?"]


def test_versteckte_felder_mit_wert_und_gesperrte_felder() -> None:
    assert any("action=delete" in x for x in _ohne(BISHER, '<input type="hidden" name="action" value="delete">'))
    anders = BISHER.replace('value="delete"', 'value="archive"')
    assert any("action=delete" in x for x in fehlt(funktionen(BISHER, "/p/"), funktionen(anders, "/p/")))
    entsperrt = BISHER.replace(' value="2" disabled>', ' value="2">')
    assert fehlt(funktionen(BISHER, "/p/"), funktionen(entsperrt, "/p/")) == ["Gesperrt POST /work/o/tasks/1/ roles"]


def test_pflichtfeld_optionen_htmx_alpine_kennung_und_link() -> None:
    erwartet = {
        " required": "Pflichtfeld POST /work/o/tasks/1/ title",
        '<option value="high">Hoch</option>': "Auswahl priority: high",
        ' hx-post="/work/o/notifications/1/mark-read/"': "Aktion HX-POST /work/o/notifications/1/mark-read/",
        ' data-parent="[data-n]"': "Aktion data-parent=[data-n]",
        " @click=\"deleteTask('/x/')\"": "Aufruf deleteTask()",
        ' id="column-todo"': "Kennung column-todo",
        ' x-data="kanbanBoard"': "Komponente kanbanBoard",
        '<a href="/work/o/tasks/export/?format=csv">CSV</a>': "Link /work/o/tasks/export/?format",
    }
    for teil, luecke in erwartet.items():
        assert luecke in _ohne(BISHER, teil), (teil, _ohne(BISHER, teil))


def test_anderes_formularziel_oder_methode_fehlt() -> None:
    for alt, neu in (('action="/work/o/tasks/1/"', 'action="/work/o/tasks/2/"'), ('method="post"', 'method="get"')):
        luecken = fehlt(funktionen(BISHER, "/p/"), funktionen(BISHER.replace(alt, neu), "/p/"))
        assert any(x.startswith("Formular POST /work/o/tasks/1/") for x in luecken), (alt, luecken)


def test_ausnahmen_mit_begruendung_im_test() -> None:
    ohne_link = BISHER.replace('<a href="/work/o/tasks/export/?format=csv">CSV</a>', "")
    alt, neu = funktionen(BISHER, "/p/"), funktionen(ohne_link, "/p/")
    assert fehlt(alt, neu) == ["Link /work/o/tasks/export/?format"]
    assert fehlt(alt, neu, {"Link /work/o/tasks/export/?format"}) == []
