# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlagenwahl der Work-Seiten im neuen Erscheinungsbild (Issue #951, ``apps.work.rahmen.NeueVorlageMixin``).

Mit eingeschaltetem Schalter der Organisation rendert eine View ihre ``neue_vorlage``, sonst die bisherige Vorlage.
``?ansicht=bisher`` öffnet die bisherige Vorlage auch bei eingeschaltetem Schalter; ohne ``neue_vorlage`` bleibt alles
wie bisher.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.views.generic import TemplateView

from apps.work.rahmen import NeueVorlageMixin


class _Seite(NeueVorlageMixin, TemplateView):
    template_name = "work/bisher.html"
    neue_vorlage = "work/neu/seite.html"


class _OhneNeue(NeueVorlageMixin, TemplateView):
    template_name = "work/bisher.html"


def _view(klasse: type[TemplateView], schalter: bool, adresse: str = "/") -> TemplateView:
    view = klasse()
    view.request = RequestFactory().get(adresse)
    view.organization = SimpleNamespace(work_new_design=schalter)  # type: ignore[attr-defined]
    return view


@pytest.mark.parametrize(
    ("klasse", "schalter", "adresse", "erwartet"),
    [
        (_Seite, True, "/", "work/neu/seite.html"),
        (_Seite, False, "/", "work/bisher.html"),
        (_Seite, True, "/?ansicht=bisher", "work/bisher.html"),
        (_Seite, True, "/?ansicht=neu", "work/neu/seite.html"),
        (_OhneNeue, True, "/", "work/bisher.html"),
    ],
)
def test_vorlage_nach_schalter_und_rueckweg(
    klasse: type[TemplateView], schalter: bool, adresse: str, erwartet: str
) -> None:
    view = _view(klasse, schalter, adresse)
    assert view.get_template_names() == [erwartet]
    assert view.neue_ansicht() is (erwartet != "work/bisher.html")  # type: ignore[attr-defined]


def test_ohne_organisation_bisherige_vorlage() -> None:
    view = _Seite()
    view.request = RequestFactory().get("/")
    assert view.get_template_names() == ["work/bisher.html"]
