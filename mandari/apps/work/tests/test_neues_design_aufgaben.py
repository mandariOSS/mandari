# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben im neuen Erscheinungsbild (Issue #951, Teilpaket 4): Board und Neue Aufgabe.

Mit eingeschaltetem Schalter rendern TaskListView und TaskCreateView je eine neue Vorlage aus work/tasks/neu/. Die
Karten in den Antworten der Board-API (Schnellanlage) und des Panels (OOB) kommen in der neuen Fassung nur, wenn das
neue Board sie über die Kopfzeile X-Work-Gestaltung anfordert. Geprüft werden Gleichstand, diese Fragmente, die
Kopplung an kanbanBoard (IDs, Klassen, Datenattribute), Schalter und Rückweg, Rechte und die Bausteine. Das Verhalten
im Browser (Ziehen, Panel, Schnellanlage) prüft tests_e2e/test_teil4_neu.py.
"""

from __future__ import annotations

import re
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.motions.models import Motion
from apps.work.tasks.models import Task, TaskLabel
from apps.work.tasks.tests.test_board_page import PLACEHOLDER, read_config
from apps.work.tests.gleichstand import funktionen, vergleiche

pytestmark = pytest.mark.django_db

VORLAGEN = Path(settings.BASE_DIR) / "templates" / "work" / "tasks" / "neu"
NEU = {"HTTP_X_WORK_GESTALTUNG": "neu"}


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create", "tasks.manage", "motions.view"], email="vera@example.org")


@pytest.fixture
def bestand(org: Any, person: Any, make_member: Any) -> dict[str, Any]:
    kollegin = make_member(org, ["tasks.view"], email="mira@example.org")
    label = TaskLabel.objects.create(organization=org, name="Haushalt & Finanzen", color="amber")
    offen = Task.objects.create(
        organization=org,
        title="Rede vorbereiten",
        created_by=person,
        assigned_to=person,
        visibility="organization",
        priority="urgent",
        due_date=timezone.localdate() - timedelta(days=1),
        description="Kernpunkte",
    )
    offen.labels.add(label)
    Task.objects.create(
        organization=org,
        title="Presse",
        created_by=person,
        assigned_to=kollegin,
        visibility="organization",
        status="in_progress",
        priority="high",
    )
    Task.objects.create(
        organization=org,
        title="Erledigt",
        created_by=person,
        assigned_to=person,
        visibility="organization",
        status="done",
        is_completed=True,
    )
    return {"offen": offen, "label": label, "kollegin": kollegin}


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def test_gleichstand_board(org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    client = client_for(person.user)
    neu, antwort = vergleiche(client, _url("tasks", org))
    html = antwort.content.decode()
    for kennung in ("column-todo", "column-in_progress", "column-done", "count-todo", "task-panel-container"):
        assert kennung in neu.ids, kennung
    assert f"task-card-{bestand['offen'].id}" in neu.ids
    assert 'class="task-card ' in html and f'data-card-label="{bestand["label"].id}"' in html
    assert "Haushalt &amp; Finanzen" in html and "überfällig" in html and "Dringend" in html
    assert 'hx-headers=\'{"X-Work-Gestaltung": "neu"}\'' in html and "Sortable.min.js" in html
    assert "1 überfällig" in html
    for url in (
        _url("tasks", org) + "?view=all",
        _url("tasks", org) + "?view=all&completed=1&priority=urgent&q=Rede",
        _url("tasks", org) + f"?view=my&label={bestand['label'].id}&overdue=1",
        _url("tasks", org) + f"?open={bestand['offen'].id}",
    ):
        vergleiche(client, url)


def test_board_konfiguration_komponenten_und_kein_inline_skript(
    org: Any, person: Any, bestand: dict[str, Any], client_for: Any
) -> None:
    antwort = client_for(person.user).get(_url("tasks", org) + f"?open={bestand['offen'].id}")
    html = antwort.content.decode()
    for komponente in ("kanbanBoard", "importManager", "fileImportManager"):
        assert f'x-data="{komponente}"' in html, komponente
    konfiguration = read_config(html)
    assert konfiguration["autoOpenTaskId"] == str(bestand["offen"].id)
    assert konfiguration["urls"]["api"] == _url("tasks_api", org)
    assert konfiguration["urls"]["panel"] == f"/work/{org.slug}/tasks/{PLACEHOLDER}/panel/"
    for marker in ("function kanbanBoard", "kanbanBoardInstance", "function importManager", ".kanban-board {"):
        assert marker not in html, marker
    assert not re.search(r"\son(change|click|submit)=\"", html)
    vorlagen = {t.name for t in antwort.templates if t.name}
    for vorlage in (
        "work/tasks/neu/_filter.html",
        "work/tasks/neu/_spalte.html",
        "work/tasks/neu/_weiteres.html",
        "work/tasks/partials/_import_modal.html",
        "work/tasks/partials/_file_import_modal.html",
        "cotton/ui/seitenblatt.html",
    ):
        assert vorlage in vorlagen, vorlage


def test_gleichstand_neue_aufgabe(org: Any, person: Any, bestand: dict[str, Any], client_for: Any) -> None:
    client = client_for(person.user)
    # Die Sichtbarkeit braucht in der neuen Vorlage keinen Alpine-Zustand: die bisherige färbt damit die gewählte Karte
    # und zeigt einen Hinweis, die neue nennt den Hinweis fest bei „Geteilt“; die Vorauswahl setzt der Server
    ausnahmen = {"Modell visibility"}
    neu, antwort = vergleiche(client, _url("task_create", org), ausnahmen)
    assert {"assignmentHandler"} <= neu.komponenten and "task-tags" in neu.json
    motion = Motion.objects.create(organization=org, author=person, title="Antrag Radweg", visibility="organization")
    neu, antwort = vergleiche(client, _url("task_create", org) + f"?related_motion={motion.id}", ausnahmen)
    assert "Antrag Radweg" in antwort.content.decode()
    assert any("related_motion=" + str(motion.id) in f for _, f in neu.formulare)
    # Ungültig abgeschickt: dieselbe neue Vorlage mit Fehler am Titel
    fehler = client.post(_url("task_create", org), {"title": "", "priority": "medium", "status": "todo"})
    assert fehler.status_code == 200 and 'data-gestaltung="neu"' in fehler.content.decode()


class _Sichtbarkeit(HTMLParser):
    """Gewählte Option der Sichtbarkeit und Alpine-Ausdrücke, in denen ein Wert der Sichtbarkeit steht."""

    def __init__(self) -> None:
        super().__init__()
        self.gewaehlt: list[str] = []
        self.ausdruecke: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "input" and a.get("name") == "visibility" and "checked" in a:
            self.gewaehlt.append(a.get("value") or "")
        self.ausdruecke += [w or "" for k, w in attrs if k.startswith(("x-", "@", ":")) and "visibility:" in (w or "")]


def test_sichtbarkeit_vom_server_vorbelegt(org: Any, person: Any, client_for: Any) -> None:
    client = client_for(person.user)
    for ansicht in ("", "?ansicht=bisher"):
        leer = _Sichtbarkeit()
        leer.feed(client.get(_url("task_create", org) + ansicht).content.decode())
        assert leer.gewaehlt == ["private"], ansicht
        for wert, erwartet in (("organization", ["organization"]), ("unbekannt", ["private"])):
            sammler = _Sichtbarkeit()
            antwort = client.post(_url("task_create", org) + ansicht, {"title": "", "visibility": wert, "tags": "[]"})
            sammler.feed(antwort.content.decode())
            assert sammler.gewaehlt == erwartet, (ansicht, wert)
            assert all(wert not in ausdruck for ausdruck in sammler.ausdruecke), (ansicht, sammler.ausdruecke)


def test_anlegen_wie_bisher(org: Any, person: Any, client_for: Any) -> None:
    antwort = client_for(person.user).post(
        _url("task_create", org),
        {"title": "Neue Aufgabe", "priority": "high", "status": "todo", "visibility": "private", "tags": '["A"]'},
    )
    assert antwort.status_code == 302
    assert Task.objects.get(title="Neue Aufgabe").priority == "high"


def test_schnellanlage_liefert_die_karte_der_anfragenden_seite(org: Any, person: Any, client_for: Any) -> None:
    client = client_for(person.user)
    daten = {"action": "quick_add", "title": "Schnell", "status": "todo"}
    neu = client.post(_url("tasks_api", org), daten, HTTP_HX_REQUEST="true", **NEU).content.decode()
    bisher = client.post(_url("tasks_api", org), daten, HTTP_HX_REQUEST="true").content.decode()
    assert "rounded-md border border-gray-200" in neu and "border-l-4" not in neu
    assert "border-l-4" in bisher
    for html in (neu, bisher):
        assert 'class="task-card' in html or "task-card " in html
        assert 'id="count-todo"' in html and "hx-swap-oob" in html


def test_panel_tauscht_die_karte_der_anfragenden_seite(
    org: Any, person: Any, bestand: dict[str, Any], client_for: Any
) -> None:
    client = client_for(person.user)
    aufgabe = bestand["offen"]
    url = _url("task_panel_action", org, task_id=aufgabe.id)
    daten = {"action": "toggle_label", "label_id": str(bestand["label"].id)}
    neu = client.post(url, daten, HTTP_HX_REQUEST="true", **NEU).content.decode()
    # Die Karte selbst trägt hx-swap-oob: htmx 2 setzt bei outerHTML das OOB-Element ein, eine Hülle mit derselben
    # Kennung ergäbe eine doppelte, verschachtelte Karte
    assert f'id="task-card-{aufgabe.id}" hx-swap-oob="outerHTML"' in neu and "border-l-4" not in neu
    assert neu.count(f'id="task-card-{aufgabe.id}"') == 1
    bisher = client.post(url, daten, HTTP_HX_REQUEST="true").content.decode()
    assert f'hx-swap-oob="outerHTML:#task-card-{aufgabe.id}"' in bisher and "border-l-4" in bisher
    # Beide Karten bieten dieselben Aktionen
    alt_karte, neu_karte = funktionen(bisher, url, nur_main=False), funktionen(neu, url, nur_main=False)
    assert alt_karte.aufrufe <= neu_karte.aufrufe and alt_karte.ids <= neu_karte.ids


def test_import_nur_mit_erstellrecht(org: Any, bestand: dict[str, Any], client_for: Any) -> None:
    html = client_for(bestand["kollegin"].user).get(_url("tasks", org)).content.decode()
    assert "Exportieren" in html and "Aus Protokollen" not in html and "Neue Aufgabe" in html


def test_schalter_rueckweg_und_rechte(
    org: Any, person: Any, bestand: dict[str, Any], make_member: Any, client_for: Any
) -> None:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    ohne = make_member(org, ["dashboard.view"], email="ohne@example.org")
    for mitglied in (person, bestand["kollegin"], gast, ohne):
        client = client_for(mitglied.user)
        for name in ("tasks", "task_create"):
            neu, bisher = client.get(_url(name, org)), client.get(_url(name, org), {"ansicht": "bisher"})
            assert neu.status_code == bisher.status_code, (name, mitglied.user.email, neu.status_code)
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    client = client_for(person.user)
    assert "work/tasks/list.html" in {t.name for t in client.get(_url("tasks", org)).templates}
    assert "work/tasks/create.html" in {t.name for t in client.get(_url("task_create", org)).templates}


def test_vorlagen_ohne_farbrahmen_chips_und_overlays() -> None:
    for datei in sorted(VORLAGEN.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert "border-l-4" not in text and "rounded-xl" not in text and ' style="' not in text, datei.name
        assert "fixed inset-0" not in text and "-100 dark:bg-{{" not in text, datei.name
        assert len(text.splitlines()) <= 300, datei.name
    board = (VORLAGEN / "board.html").read_text(encoding="utf-8")
    assert "<c-ui.seitenblatt" in board and "<c-rahmen.kopfband" in board
    assert "{% block gestaltung %}neu{% endblock %}" in board
    assert "<c-ui.alpine-modal" in (VORLAGEN / "neu.html").read_text(encoding="utf-8")
