# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wie ``test_meine_gremien``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4).

Zwei Tests prüfen Texte der bisherigen Vorlage (Untertitel „Ihre zugewiesenen Gremien“, Hinweiskasten „Keine Gremien
ausgewählt“). Die neue Seite (Teilpaket 2) sagt dasselbe im Satz des Kopfbands; das prüfen die Ersatztests unten.
"""

from typing import Any

from django.urls import reverse

from apps.work.meetings.tests import test_meine_gremien as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(
    quelle,
    globals(),
    ohne=("test_ohne_gefolgte_gelten_die_zugewiesenen", "test_ohne_gremien_hinweis_und_alle_sitzungen"),
)


def test_neu_ohne_gefolgte_gelten_die_zugewiesenen(
    org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any
) -> None:
    mitglied.oparl_committees.set([ris["Bauausschuss"]])

    antwort = quelle._liste(client_for(mitglied.user), org)

    assert quelle._sitzungen(antwort) == ["Sitzung Bauausschuss"]
    assert "der Gremien, die Ihnen zugewiesen sind: Bauausschuss" in antwort.content.decode()


def test_neu_ohne_gremien_hinweis_und_alle_sitzungen(
    org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any
) -> None:
    antwort = quelle._liste(client_for(mitglied.user), org)

    assert len(antwort.context["meetings"]) == 3
    inhalt = antwort.content.decode()
    assert "Sie folgen noch keinen Gremien, und Ihnen sind keine zugewiesen" in inhalt
    assert reverse("work:profile_committees", kwargs={"org_slug": org.slug}) in inhalt
