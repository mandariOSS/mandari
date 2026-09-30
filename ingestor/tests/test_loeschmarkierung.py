"""
Der Ingestor markiert gelöschte OParl-Objekte, statt sie zu löschen (Issue #17).

Liefert ein RIS ein Objekt mit ``deleted: true``, setzt der Ingestor nur die Lösch-Markierung
(``mark_entity_deleted``). Physisch löscht ausschließlich Djangos ``purge_deleted`` nach
ausdrücklichem Auftrag der Kommune.

Die Prüfungen standen früher in ``scripts/smoke_tombstones.py``. Die CI startet die Smoke-Tests
aber nur bei Änderungen an der Django-Anwendung; ein Pull Request, der nur den Ingestor ändert,
hätte einen zurückgekehrten Lösch-Pfad also erst nach dem Merge bemerkt. Hier laufen sie mit
jeder Ingestor-Änderung.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ORCHESTRATOR = (SRC / "sync" / "orchestrator.py").read_text(encoding="utf-8")
DATABASE = (SRC / "storage" / "database.py").read_text(encoding="utf-8")


def test_orchestrator_markiert_geloeschte_objekte() -> None:
    assert "_mark_deleted" in ORCHESTRATOR


def test_orchestrator_loescht_nicht_physisch() -> None:
    assert "delete_entity" not in ORCHESTRATOR


def test_speicher_markiert_statt_zu_loeschen() -> None:
    assert "mark_entity_deleted" in DATABASE
    assert "delete(model)" not in DATABASE


def test_beide_abgleiche_pruefen_markierung_vor_dem_upsert() -> None:
    # Einmal im vollständigen, einmal im inkrementellen Abgleich; sonst spielte der volle Abgleich
    # ein gelöschtes Objekt wieder als aktiv ein
    assert ORCHESTRATOR.count('item.get("deleted") is True') >= 2
