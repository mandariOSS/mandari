# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Was ist neu?“ in Work und der Hinweis auf die Version in der Seitenleiste (Issue #1463).

Geprüft wird die Datei ``apps/work/neuigkeiten.toml`` (Pflichtfelder, Bereich, Längen, Anrede, ein Tag je Version und
derselbe Tag wie in der CHANGELOG.md), die Gliederung je Version und Bereich, die Seite in beiden Erscheinungsbildern, die Zeile mit der Version und der Punkt daneben:
Er steht, bis die Person das Neueste gesehen hat, und kommt mit einem neuen Eintrag wieder.
"""

from __future__ import annotations

import re
from dataclasses import replace
from html import unescape
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests import factories
from apps.common.tests.versionstage import pruefe_jede_version_hat_einen_eintrag, pruefe_tag_je_version
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
        assert eintrag.bereich in neuigkeiten.bereiche(), f"Bereich „{eintrag.bereich}“: {eintrag.titel}"
    assert [e.datum for e in eintraege] == sorted((e.datum for e in eintraege), reverse=True)
    assert len({e.titel for e in eintraege}) == len(eintraege), "kein Titel doppelt"


def test_je_version_ein_tag_und_derselbe_wie_im_changelog() -> None:
    """Die Seite zeigt eine Überschrift je Version; ihr Tag ist der aus der Überschrift der CHANGELOG.md."""
    pruefe_tag_je_version(portal.lies(neuigkeiten.DATEI))


def test_jede_version_hat_einen_eintrag() -> None:
    """Auch eine Version ohne Änderung an Work steht auf der Seite, mit einem kurzen Eintrag, der das sagt."""
    pruefe_jede_version_hat_einen_eintrag(portal.lies(neuigkeiten.DATEI), "Work")


def test_gruppen_eine_je_version() -> None:
    gruppen = neuigkeiten.gruppen()
    assert gruppen and all(g.eintraege for g in gruppen)
    versionen = [g.version for g in gruppen if g.version]
    assert len(versionen) == len(set(versionen))


def test_versionen_gliedern_je_bereich_in_der_reihenfolge_der_datei() -> None:
    """Je Version die Bereiche in der Reihenfolge ihres ersten Eintrags, darin die Einträge wie in der Datei."""
    versionen, gruppen = neuigkeiten.versionen(), neuigkeiten.gruppen()
    assert [(v.datum, v.version) for v in versionen] == [(g.datum, g.version) for g in gruppen]
    for version, gruppe in zip(versionen, gruppen, strict=True):
        assert [b.schluessel for b in version.bereiche] == list(dict.fromkeys(e.bereich for e in gruppe.eintraege))
        for bereich in version.bereiche:
            assert list(bereich.eintraege) == [e for e in gruppe.eintraege if e.bereich == bereich.schluessel]
            assert (bereich.label, bereich.icon) == neuigkeiten.bereiche()[bereich.schluessel]
            assert bereich.mehrere == (len(bereich.eintraege) > 1)


def test_bereiche_wie_in_der_navigation() -> None:
    bereiche = neuigkeiten.bereiche()
    assert bereiche["aufgaben"] == ("Aufgaben", "square-check")
    assert bereiche["hilfe"] == ("Hilfe und Support", "circle-help")
    assert bereiche[neuigkeiten.UEBERALL] == ("Überall in Work", "app-window")
    assert all(label and icon for label, icon in bereiche.values())


def test_unbekannter_bereich_zaehlt_als_ueberall(monkeypatch: pytest.MonkeyPatch) -> None:
    eintrag = replace(neuigkeiten.neuigkeiten()[0], bereich="gibt-es-nicht")
    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: (eintrag,))
    [version] = neuigkeiten.versionen()
    assert [(b.schluessel, b.label, b.eintraege) for b in version.bereiche] == [
        (neuigkeiten.UEBERALL, "Überall in Work", (eintrag,))
    ]


def test_stand_ist_juengster_tag_und_zahl_seiner_eintraege(monkeypatch: pytest.MonkeyPatch) -> None:
    eintraege = neuigkeiten.neuigkeiten()
    juengster = eintraege[0].datum
    erwartet = f"{juengster.isoformat()}|{sum(1 for e in eintraege if e.datum == juengster)}"
    assert neuigkeiten.stand() == erwartet

    # Ein berichtigter Titel ändert den Stand nicht
    berichtigt = (replace(eintraege[0], titel="Berichtigt."), *eintraege[1:])
    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: berichtigt)
    assert neuigkeiten.stand() == erwartet

    # Ein Eintrag mehr am selben Tag schon, gleich an welcher Stelle er steht
    weiterer = replace(eintraege[0], titel="Noch etwas Neues.")
    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: (*berichtigt, weiterer))
    assert neuigkeiten.stand() != erwartet

    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: ())
    assert neuigkeiten.stand() == ""


def test_falscher_aufbau_ist_ein_fehler_der_datei(tmp_path: Path) -> None:
    """Gültiges TOML, aber kein Eintrag mit Feldern: Der Leser meldet es wie fehlende Pflichtfelder."""
    datei = tmp_path / "neu.toml"
    falscher_bereich = '[[eintrag]]\ndatum = 2026-01-05\ntitel = "Neu."\ntext = "Text."\nbereich = 1\n'
    for inhalt in (
        'eintrag = ["nur Text"]\n',
        'eintrag = "kein Eintrag"\n',
        "[eintrag]\ntitel = 1\n",
        falscher_bereich,
    ):
        datei.write_text(inhalt, encoding="utf-8")
        with pytest.raises(ValueError):
            portal.lies(datei)


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
    # Je Version ein Abschnitt, darin die Bereiche in der Reihenfolge der Datei und darin genau ihre Einträge
    versionen = neuigkeiten.versionen()
    abschnitte = [teil.split("</section>")[0] for teil in re.split(r'<section aria-labelledby="neu-\d+"', html)[1:]]
    assert len(abschnitte) == len(versionen)
    for abschnitt, version in zip(abschnitte, versionen, strict=True):
        # Neuerungen, die noch in keiner Version stehen, tragen statt der Nummer eine eigene Überschrift
        kopf = (
            f'<span class="sr-only">Version </span>{version.version}' if version.version else "Seit der letzten Version"
        )
        assert f"{kopf}</h2>" in abschnitt
        teile = re.split(r'<div data-bereich="([^"]+)"', abschnitt)
        assert teile[1::2] == [bereich.schluessel for bereich in version.bereiche]
        for bereich, inhalt in zip(version.bereiche, teile[2::2], strict=True):
            symbol = re.escape(f'data-lucide="{bereich.icon}"')
            assert re.search(symbol + r"[^>]*></i>\s*" + re.escape(bereich.label) + "</h3>", inhalt), bereich.label
            titel = [unescape(t) for t in re.findall(r'dark:text-white">([^<]+)</h4>', inhalt)]
            assert titel == [eintrag.titel for eintrag in bereich.eintraege]


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
    monkeypatch.setattr(neuigkeiten, "DATEI", datei)
    kunde = client_for(person.user)
    # Kein TOML, und gültiges TOML mit falschem Aufbau: Beides lässt die Liste leer
    for inhalt in ("[[eintrag]\n", 'eintrag = ["nur Text"]\n'):
        datei.write_text(inhalt, encoding="utf-8")
        neuigkeiten.neuigkeiten.cache_clear()
        try:
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
    # Am Ende der Datei, nicht oben: Der Punkt hängt nicht an der Stelle des Eintrags
    monkeypatch.setattr(neuigkeiten, "neuigkeiten", lambda: (*bisher, neu))

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
