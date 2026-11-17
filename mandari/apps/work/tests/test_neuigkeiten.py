# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Was ist neu?“ in Work und der Hinweis auf die Version in der Seitenleiste (Issue #1463).

Geprüft wird die Datei ``apps/work/neuigkeiten.toml`` (Pflichtfelder, Längen, Anrede, ein Tag je Version und derselbe
Tag wie in der CHANGELOG.md), die Seite in beiden Erscheinungsbildern, die Zeile mit der Version und der Punkt daneben:
Er steht, bis die Person das Neueste gesehen hat, und kommt mit einem neuen Eintrag wieder.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings
from django.urls import reverse

from apps.common.tests import factories
from apps.work import neuigkeiten
from insight_core.services import neuigkeiten as portal

pytestmark = pytest.mark.django_db

VERSION = portal.mandari_version()


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    """Mitglied ohne besondere Rechte: Die Seite braucht keines."""
    return make_member(org, ["dashboard.view"], email="nora@example.org")


@pytest.fixture(params=[False, True], ids=["bisher", "neu"])
def erscheinungsbild(request: pytest.FixtureRequest, org: Any) -> bool:
    org.work_new_design = request.param
    org.save(update_fields=["work_new_design"])
    return bool(request.param)


def _seite(org: Any) -> str:
    return reverse("work:neuigkeiten", kwargs={"org_slug": org.slug})


def _start(org: Any) -> str:
    return reverse("work:dashboard", kwargs={"org_slug": org.slug})


def _hat_punkt(html: str) -> bool:
    return 'data-testid="work-version-neu"' in html


# ---------------------------------------------------------------------------
# Die Datei
# ---------------------------------------------------------------------------


def test_datei_vollstaendig_und_in_sie_form() -> None:
    eintraege = portal.lies(neuigkeiten.DATEI)
    assert eintraege, "mindestens ein Eintrag"
    for eintrag in eintraege:
        assert eintrag.titel.endswith("."), eintrag.titel
        assert len(eintrag.titel) <= portal.TITEL_MAX, eintrag.titel
        assert len(eintrag.text) <= portal.TEXT_MAX, eintrag.titel
        assert "\n" not in eintrag.text, f"ein Absatz: {eintrag.titel}"
        assert not re.search(r"\b(du|dir|dich|dein\w*|euch|euer\w*)\b", f"{eintrag.titel} {eintrag.text}", re.I)
        assert not eintrag.version or re.fullmatch(r"\d+\.\d+\.\d+", eintrag.version), eintrag.version
    assert [e.datum for e in eintraege] == sorted((e.datum for e in eintraege), reverse=True)
    assert len({e.titel for e in eintraege}) == len(eintraege), "kein Titel doppelt"


def test_je_version_ein_tag_und_derselbe_wie_im_changelog() -> None:
    """Die Seite zeigt eine Überschrift je Version; ihr Tag ist der aus der Überschrift der CHANGELOG.md."""
    tage: dict[str, set[date]] = {}
    for eintrag in portal.lies(neuigkeiten.DATEI):
        if eintrag.version:
            tage.setdefault(eintrag.version, set()).add(eintrag.datum)
    assert tage and all(len(menge) == 1 for menge in tage.values()), tage

    changelog = Path(settings.BASE_DIR).parent / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md liegt nicht neben dem Projekt (Image)")
    kopf = dict(
        re.findall(r"^## \[(\d+\.\d+\.\d+)\] – (\d{4}-\d{2}-\d{2})", changelog.read_text(encoding="utf-8"), re.M)
    )
    for version, menge in tage.items():
        assert version in kopf, f"Version {version} steht nicht in der CHANGELOG.md"
        assert {date.fromisoformat(kopf[version])} == menge, f"Tag der Version {version}"


def test_gruppen_eine_je_version() -> None:
    gruppen = neuigkeiten.gruppen()
    assert gruppen and all(g.eintraege for g in gruppen)
    versionen = [g.version for g in gruppen if g.version]
    assert len(versionen) == len(set(versionen))


def test_stand_folgt_dem_obersten_eintrag(monkeypatch: pytest.MonkeyPatch) -> None:
    erster = neuigkeiten.neuigkeiten()[0]
    assert neuigkeiten.stand() == f"{erster.datum.isoformat()}|{erster.titel}"

    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: ())
    assert neuigkeiten.stand() == ""


# ---------------------------------------------------------------------------
# Die Seite
# ---------------------------------------------------------------------------


def test_seite_nennt_version_und_neuerungen(org: Any, person: Any, client_for: Any, erscheinungsbild: bool) -> None:
    antwort = client_for(person.user).get(_seite(org))

    assert antwort.status_code == 200
    html = antwort.content.decode()
    vorlage = "work/neuigkeiten/neu/seite.html" if erscheinungsbild else "work/neuigkeiten/seite.html"
    assert vorlage in [t.name for t in antwort.templates]
    assert "Was ist neu?" in html and f"Sie arbeiten mit Version {VERSION}." in html
    gruppen = neuigkeiten.gruppen()
    assert html.count('aria-labelledby="neu-') == len(gruppen)
    assert f"Version {gruppen[0].version}" in html
    for eintrag in neuigkeiten.neuigkeiten():
        assert f'<h3 class="text-sm font-semibold text-gray-900 dark:text-white">{eintrag.titel}</h3>' in html


def test_seite_ohne_anmeldung_und_fuer_gaeste_nicht(org: Any, client: Any, client_for: Any) -> None:
    assert client.get(_seite(org)).status_code == 302

    gast = cast(Any, factories.MembershipFactory)(organization=org, is_guest=True, roles=[])
    antwort = client_for(gast.user).get(_seite(org))
    assert antwort.status_code == 302
    assert antwort["Location"] == reverse("work:guest_documents", kwargs={"org_slug": org.slug})


def test_kaputte_datei_bricht_keine_seite(
    org: Any, person: Any, client_for: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datei = tmp_path / "kaputt.toml"
    datei.write_text("[[eintrag]\n", encoding="utf-8")
    monkeypatch.setattr(neuigkeiten, "DATEI", datei)
    neuigkeiten.neuigkeiten.cache_clear()
    try:
        kunde = client_for(person.user)
        html = kunde.get(_seite(org)).content.decode()
        start = kunde.get(_start(org))
    finally:
        neuigkeiten.neuigkeiten.cache_clear()

    assert "Gerade gibt es hier nichts zu lesen." in html
    assert start.status_code == 200 and not _hat_punkt(start.content.decode())


# ---------------------------------------------------------------------------
# Version in der Seitenleiste und der Punkt daneben
# ---------------------------------------------------------------------------


def test_seitenleiste_nennt_die_version_mit_link(
    org: Any, person: Any, client_for: Any, erscheinungsbild: bool
) -> None:
    html = client_for(person.user).get(_start(org)).content.decode()

    treffer = re.search(
        r'<a href="([^"]+)" data-testid="work-version"[^>]*>\s*<span class="truncate">([^<]+)</span>', html
    )
    assert treffer, "Zeile mit der Version"
    assert treffer.group(1) == _seite(org)
    assert treffer.group(2) == f"Version {VERSION}: Was ist neu?"
    # Am Handy steht derselbe Weg im Blatt „Mehr“ (nur im neuen Erscheinungsbild; im bisherigen ist es die Seitenleiste)
    assert ('data-testid="work-version-mobil"' in html) == erscheinungsbild


def test_punkt_steht_bis_die_seite_geoeffnet_wurde(
    org: Any, person: Any, client_for: Any, erscheinungsbild: bool
) -> None:
    person.user.settings = {"anderes": {"bleibt": True}}
    person.user.save(update_fields=["settings"])
    kunde = client_for(person.user)

    assert _hat_punkt(kunde.get(_start(org)).content.decode()), "Neues, noch nicht gesehen"
    assert not _hat_punkt(kunde.get(_seite(org)).content.decode()), "schon auf der Seite selbst ohne Punkt"
    assert not _hat_punkt(kunde.get(_start(org)).content.decode())

    person.user.refresh_from_db()
    assert person.user.settings == {"anderes": {"bleibt": True}, neuigkeiten.GESEHEN_SCHLUESSEL: neuigkeiten.stand()}


def test_punkt_kommt_mit_einem_neuen_eintrag_wieder(
    org: Any, person: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    kunde = client_for(person.user)
    kunde.get(_seite(org))
    assert not _hat_punkt(kunde.get(_start(org)).content.decode())

    bisher = neuigkeiten.neuigkeiten()
    neu = portal.Neuigkeit(datum=bisher[0].datum, version=bisher[0].version, titel="Noch etwas Neues.", text="Text.")
    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: (neu, *bisher))

    assert _hat_punkt(kunde.get(_start(org)).content.decode()), "auch ein weiterer Eintrag am selben Tag zählt"


def test_seite_schreibt_nur_wenn_sich_der_stand_aendert(
    org: Any, person: Any, client_for: Any, django_assert_num_queries: Any
) -> None:
    neuigkeiten.als_gesehen_merken(person.user)
    person.user.refresh_from_db()

    with django_assert_num_queries(0):
        neuigkeiten.als_gesehen_merken(person.user)
        assert not neuigkeiten.ungesehen(person.user)


def test_gaeste_sehen_keine_zeile_mit_der_version(org: Any, client_for: Any, erscheinungsbild: bool) -> None:
    gast = cast(Any, factories.MembershipFactory)(organization=org, is_guest=True, roles=[])

    html = client_for(gast.user).get(reverse("work:guest_documents", kwargs={"org_slug": org.slug})).content.decode()

    assert 'data-testid="work-version' not in html
