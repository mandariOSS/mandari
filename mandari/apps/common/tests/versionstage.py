# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tag einer Version unter „Was ist neu?“ (Issues #1261, #1463): eine Prüfung für beide Dateien.

Einträge mit Version tragen den Tag aus der Überschrift der Version in der CHANGELOG.md, nicht den Tag der Übernahme
nach dev: In einer Installation steht die Neuerung ab dem Release. Genutzt von den Tests zu
``insight_core/neuigkeiten.toml`` und ``apps/work/neuigkeiten.toml``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Protocol

import pytest
from django.conf import settings


class EintragMitVersion(Protocol):
    @property
    def datum(self) -> date: ...

    @property
    def version(self) -> str: ...


def pruefe_tag_je_version(eintraege: Iterable[EintragMitVersion]) -> None:
    """Je Version ein Tag, und zwar der aus der CHANGELOG.md; liegt sie nicht neben dem Projekt (Image), übersprungen."""
    tage: dict[str, set[date]] = {}
    for eintrag in eintraege:
        if eintrag.version:
            tage.setdefault(eintrag.version, set()).add(eintrag.datum)
    assert tage, "mindestens ein Eintrag mit Version"
    for version, menge in tage.items():
        assert len(menge) == 1, f"Version {version} an mehreren Tagen: {sorted(map(str, menge))}"

    changelog = Path(settings.BASE_DIR).parent / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md liegt nicht neben dem Projekt (Image)")
    kopf = dict(
        re.findall(r"^## \[(\d+\.\d+\.\d+)\] – (\d{4}-\d{2}-\d{2})", changelog.read_text(encoding="utf-8"), re.M)
    )
    for version, menge in tage.items():
        assert version in kopf, f"Version {version} steht nicht in der CHANGELOG.md"
        assert menge == {date.fromisoformat(kopf[version])}, (
            f"Version {version}: {sorted(menge)[0]} statt {kopf[version]} (Überschrift in der CHANGELOG.md)"
        )


def _zahlen(version: str) -> tuple[int, ...]:
    return tuple(int(teil) for teil in version.split("."))


def pruefe_jede_version_hat_einen_eintrag(eintraege: Iterable[EintragMitVersion], seite: str) -> None:
    """
    Jede Version der CHANGELOG.md hat auf der Seite mindestens einen Eintrag, ab der ältesten Version, die die Datei
    kennt (Sven, 22.11.2026: „Es sollte IMMER einen kleinen Eintrag geben“). Ändert eine Version an ``seite`` nichts,
    sagt ein kurzer Eintrag genau das. Erscheinen mehrere Versionen am selben Tag, steht die höhere zuerst: Die Seite
    zeigt die Einträge eines Tags in der Reihenfolge der Datei.
    """
    liste = list(eintraege)
    mit_version = [eintrag.version for eintrag in liste if eintrag.version]
    assert mit_version, "mindestens ein Eintrag mit Version"
    # Reihenfolge auf der Seite: neueste zuerst, am selben Tag die höhere Version zuerst
    folge = list(dict.fromkeys(mit_version))
    tag = {eintrag.version: eintrag.datum for eintrag in liste if eintrag.version}
    for vorher, nachher in zip(folge, folge[1:], strict=False):
        if tag[vorher] == tag[nachher]:
            assert _zahlen(vorher) > _zahlen(nachher), f"{nachher} steht am selben Tag über {vorher}: höhere zuerst"

    changelog = Path(settings.BASE_DIR).parent / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md liegt nicht neben dem Projekt (Image)")
    versionen = re.findall(r"^## \[(\d+\.\d+\.\d+)\] – \d{4}-\d{2}-\d{2}", changelog.read_text(encoding="utf-8"), re.M)
    aelteste = min(mit_version, key=_zahlen)
    fehlend = [v for v in versionen if _zahlen(v) >= _zahlen(aelteste) and v not in mit_version]
    assert not fehlend, (
        f"Ohne Eintrag unter „Was ist neu?“ ({seite}): {', '.join(fehlend)}. Jede Version bekommt mindestens einen "
        f"kurzen Eintrag, auch wenn sie an {seite} nichts ändert."
    )
