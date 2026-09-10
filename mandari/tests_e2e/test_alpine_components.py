# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenbezogene Alpine-Komponenten werden im Browser wirklich initialisiert.

Hintergrund: `work.ts` und das Editor-Bundle registrieren ihre Komponenten mit
`Alpine.data()` erst, wenn ihr Modul läuft. Startet `main.ts` Alpine vorher, bleibt
jedes `x-data="…"` dieser Seiten leer – Dokumentliste, Aufgaben, Fraktionssitzung,
Editor und Sitzungsvorbereitung sind dann nicht bedienbar. Dass `window.Alpine`
existiert, beweist das nicht; geprüft wird deshalb der initialisierte Datenstapel
am Element (`_x_dataStack`) und dass die Konsole keine Alpine-Fehler meldet.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.work.motions.models import Motion

ALPINE_ERROR_MARKERS = ("Alpine Expression Error", "is not defined")


@pytest.fixture
def alpine_errors(page: Any) -> list[str]:
    """Sammelt Alpine-Fehlermeldungen aus der Browserkonsole."""
    errors: list[str] = []

    def collect(message: Any) -> None:
        if any(marker in message.text for marker in ALPINE_ERROR_MARKERS):
            errors.append(message.text)

    page.on("console", collect)
    return errors


def wait_for_component(page: Any, name: str) -> None:
    """Wartet, bis Alpine das erste Element mit `x-data="<name>…"` initialisiert hat."""
    page.wait_for_function(
        '(name) => { const el = document.querySelector(`[x-data^="${name}"]`);'
        " return !!(el && el._x_dataStack && el._x_dataStack.length); }",
        arg=name,
        timeout=15000,
    )


class TestAlpineKomponenten:
    def test_work_komponenten_werden_initialisiert(
        self, page: Any, goto: Any, login: Any, member_user: Any, alpine_errors: list[str]
    ) -> None:
        membership, password = member_user
        login(membership.user.email, password)
        slug = membership.organization.slug

        goto(f"/work/{slug}/documents/")
        wait_for_component(page, "documentManager")

        goto(f"/work/{slug}/tasks/")
        wait_for_component(page, "kanbanBoard")

        assert alpine_errors == [], "\n".join(alpine_errors)

    def test_editor_komponente_wird_initialisiert(
        self, page: Any, goto: Any, login: Any, member_user: Any, alpine_errors: list[str]
    ) -> None:
        membership, password = member_user
        motion = Motion.objects.create(
            organization=membership.organization,
            author=membership,
            title="E2E-Dokument",
            visibility="organization",
        )
        login(membership.user.email, password)

        goto(f"/work/{membership.organization.slug}/documents/{motion.id}/")
        wait_for_component(page, "documentEditor")

        assert alpine_errors == [], "\n".join(alpine_errors)
