# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für ORM-Schreibzugriffe in Views (``scripts/check_view_writes_ratchet.py``, Issue #1588, Kennzahl K9):
Zählregeln an kleinen Beispielen, Umfang der View-Module und die Baseline, die nur sinken darf.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_view_writes_ratchet.py"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_view_writes_ratchet", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


@pytest.mark.parametrize(
    ("rumpf", "anzahl"),
    [
        # Manager und Querysets
        ("Baustein.objects.create(title='x')", 1),
        ("Baustein.objects.get_or_create(title='x')", 1),
        ("Baustein.objects.update_or_create(title='x', defaults={})", 1),
        ("Baustein.objects.bulk_create(liste)\n    Baustein.objects.bulk_update(liste, ['title'])", 2),
        ("Baustein.objects.filter(pk=1).update(title='x')", 1),
        ("Baustein._default_manager.all().delete()", 1),
        ("await Baustein.objects.acreate(title='x')", 1),
        ("sitzung.agenda_items.filter(is_public=True).update(is_public=False)", 1),
        ("sitzung.agenda_items.create(name='x')", 1),
        ("qs = Baustein.objects.filter(pk=1)\n    qs.update(title='x')", 1),
        # Objekte und Formulare
        ("baustein.save()\n    baustein.delete()", 2),
        ("baustein.save(update_fields=['title'])", 1),
        ("form.save()", 1),
        ("self.object.delete()", 1),
        ("super().save()", 1),
    ],
)
def test_schreibzugriffe_zaehlen(ratchet: ModuleType, rumpf: str, anzahl: int) -> None:
    assert ratchet.count_writes(f"async def post(self, request):\n    {rumpf}\n") == anzahl


@pytest.mark.parametrize(
    "rumpf",
    [
        # Wörterbücher, Mengen und andere update()
        "context.update(seite=1)\n    gesehen.update(neu)",
        # Positionsargumente: kein ORM
        "cache.delete('schluessel')\n    default_storage.delete(name)\n    mappe.save(antwort)",
        # Dienste und Funktionen aus importierten Modulen
        "delegation_service.create(vertretung=x)\n    audit.delete(eintrag=x)",
        # Sitzung der Anfrage, Formular ohne Schreiben, Mehrfachbeziehungen, Lesen
        "request.session.save()\n    form.save(commit=False)\n    rolle.users.add(person)",
        "Baustein.objects.filter(pk=1).count()\n    Baustein.objects.values('pk')",
    ],
)
def test_kein_schreibzugriff(ratchet: ModuleType, rumpf: str) -> None:
    quelltext = f"from apps.session.services import delegation_service\nfrom apps.session import audit\n\n\ndef post(self, request):\n    {rumpf}\n"
    assert ratchet.count_writes(quelltext) == 0


def test_queryset_variablen_gelten_je_funktion(ratchet: ModuleType) -> None:
    quelltext = "def a():\n    qs = Baustein.objects.all()\n    qs.update(x=1)\n\n\ndef b(qs):\n    qs.update(x=1)\n"
    assert ratchet.count_writes(quelltext) == 1


@pytest.mark.parametrize(
    ("pfad", "zaehlt"),
    [
        ("apps/session/views/textblocks.py", True),
        ("apps/accounts/views.py", True),
        ("apps/accounts/views_webauthn.py", True),
        ("apps/accounts/oidc/views.py", True),
        ("apps/work/faction/views/panel.py", True),
        ("apps/work/faction/views/unter/teil.py", True),
        ("insight_core/views/home.py", True),
        ("insight_core/views.py", True),
        # keine View-Module
        ("apps/session/befehle/textbausteine.py", False),
        ("apps/session/services/textblock_service.py", False),
        ("apps/session/views/tests/test_x.py", False),
        ("apps/session/tests/test_views.py", False),
        ("apps/session/migrations/0001_initial.py", False),
        ("apps/work/a/b/views.py", False),
        ("hub/api/views.py", False),
        ("apps/views.py", False),
        ("apps/session/views/README.md", False),
    ],
)
def test_umfang_der_view_module(ratchet: ModuleType, pfad: str, zaehlt: bool) -> None:
    assert ratchet.is_view_module(PurePosixPath(pfad)) is zaehlt


def test_neue_und_zusaetzliche_schreibzugriffe_sind_verstoesse(ratchet: ModuleType) -> None:
    verstoesse, gesunken = ratchet.compare({"a.py": 3, "b.py": 1, "neu.py": 1}, {"a.py": 2, "b.py": 1})
    assert verstoesse == [
        "a.py: 3 Schreibzugriffe, erlaubt 2",
        "neu.py: 1 neue Schreibzugriffe (Datei steht nicht in der Baseline)",
    ]
    assert gesunken == []


def test_update_senkt_nur(ratchet: ModuleType) -> None:
    aktuell = {"a.py": 1, "b.py": 5, "neu.py": 2}
    basis = {"a.py": 2, "b.py": 3, "weg.py": 4}
    assert ratchet.lowered_baseline(aktuell, basis) == {"a.py": 1, "b.py": 3}


def test_main_meldet_verstoesse_und_abbau(
    ratchet: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    projekt = tmp_path / "mandari"
    (projekt / "apps" / "work").mkdir(parents=True)
    datei = projekt / "apps" / "work" / "views.py"
    datei.write_text("def post(baustein):\n    baustein.save()\n    baustein.delete()\n", encoding="utf-8")
    basis = tmp_path / "baseline.json"
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "PROJECT", projekt)
    monkeypatch.setattr(ratchet, "BASELINE", basis)

    ratchet.write_baseline({"apps/work/views.py": 1})
    assert ratchet.main([]) == 1
    assert "apps/work/views.py: 2 Schreibzugriffe, erlaubt 1" in capsys.readouterr().out
    # --update erhöht nie
    assert ratchet.main(["--update"]) == 1
    assert ratchet.load_baseline() == {"apps/work/views.py": 1}

    ratchet.write_baseline({"apps/work/views.py": 3})
    assert ratchet.main([]) == 1
    assert "--update" in capsys.readouterr().out
    assert ratchet.main(["--update"]) == 0
    assert json.loads(basis.read_text(encoding="utf-8"))["files"] == {"apps/work/views.py": 2}
    assert ratchet.main([]) == 0


def test_ausgelieferter_stand_haelt_die_baseline_ein(ratchet: ModuleType) -> None:
    verstoesse, gesunken = ratchet.compare(ratchet.measure(), ratchet.load_baseline())
    assert verstoesse == [], "Neue Schreibzugriffe in Views: Änderung als Befehl beim Eigentümer anlegen"
    assert gesunken == [], "Baseline mit scripts/check_view_writes_ratchet.py --update nachziehen"
