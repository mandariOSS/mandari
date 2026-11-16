# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorrat an fertig migrierten Datenbankvorlagen für die CI (Issue #1315): ``.github/workflows/testvorlage.yml`` legt
sie ab, die Jobs "Test" und "Smoke-Tests" holen sie mit ``scripts/ci_vorlage_holen.py``.
"""

from __future__ import annotations

import importlib.util
import io
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

WURZEL = Path(__file__).resolve().parents[4]
CI = WURZEL / ".github" / "workflows" / "pr-check.yml"
VORRAT = WURZEL / ".github" / "workflows" / "testvorlage.yml"
SKRIPT = WURZEL / "scripts" / "ci_vorlage_holen.py"

pytestmark = pytest.mark.skipif(not VORRAT.exists(), reason="Workflows nur im Repository, nicht im Image")


def _modul() -> Any:
    spec = importlib.util.spec_from_file_location("ci_vorlage_holen", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _artefakt(**lauf: Any) -> dict[str, Any]:
    werte = {"head_branch": "dev", "repository_id": 7, "head_repository_id": 7, **lauf}
    return {"id": 1, "expired": False, "created_at": "2026-11-10T10:00:00Z", "workflow_run": werte}


def _archiv(dateien: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as zip_datei:
        for name, inhalt in dateien.items():
            zip_datei.writestr(name, inhalt)
    return puffer.getvalue()


def _namen(text: str) -> set[str]:
    """Die Dateimuster aller hashFiles-Aufrufe für den Namen des Artefakts."""
    aufrufe = re.findall(r"testvorlage-\$\{\{ hashFiles\(([^)]*)\) \}\}", text)
    assert aufrufe, "Kein Artefaktname mit hashFiles gefunden"
    return set(aufrufe)


def test_alle_stellen_bilden_den_namen_aus_denselben_dateien() -> None:
    """Weicht eine Liste ab, findet die CI das Artefakt nie und migriert still weiter selbst."""
    ablegen = _namen(VORRAT.read_text(encoding="utf-8"))
    holen = _namen(CI.read_text(encoding="utf-8"))
    assert len(ablegen) == 1 and holen == ablegen


def test_vorrat_erneuert_sich_bei_jeder_datei_im_namen() -> None:
    """Jedes Muster aus dem Namen löst den Workflow auf dev aus; sonst gäbe es zum neuen Stand kein Artefakt."""
    workflow = yaml.safe_load(VORRAT.read_text(encoding="utf-8"))
    push = (workflow.get("on") or workflow[True])["push"]
    assert push["branches"] == ["dev"]
    muster = re.findall(r"'([^']+)'", next(iter(_namen(VORRAT.read_text(encoding="utf-8")))))
    for einzeln in muster:
        erwartet = einzeln.replace("migrations/*.py", "migrations/**")
        assert erwartet in push["paths"], f"{einzeln} fehlt unter on.push.paths"
    for einzeln in muster:
        if "*" not in einzeln:
            assert (WURZEL / einzeln).is_file(), f"{einzeln} gibt es nicht"


def test_test_und_vorrat_nutzen_denselben_testschluessel() -> None:
    """Migrationen verschlüsseln Werte; die Tests lesen sie aus der eingespielten Vorlage."""
    vorrat = yaml.safe_load(VORRAT.read_text(encoding="utf-8"))["jobs"]["vorlagen"]
    test = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["test"]
    schluessel = vorrat["env"]["ENCRYPTION_MASTER_KEY"]
    assert schluessel and test["env"]["ENCRYPTION_MASTER_KEY"] == schluessel
    assert test["permissions"] == {"contents": "read", "actions": "read"}


def test_nur_artefakte_aus_dem_eigenen_repository_auf_dev_oder_main() -> None:
    modul = _modul()
    assert modul.vertrauenswuerdig(_artefakt())
    assert modul.vertrauenswuerdig(_artefakt(head_branch="main"))
    # Pull Request aus einem Fork, dessen Zweig auch "dev" heißt
    assert not modul.vertrauenswuerdig(_artefakt(head_repository_id=99))
    assert not modul.vertrauenswuerdig(_artefakt(head_branch="feat/irgendwas"))
    assert not modul.vertrauenswuerdig({**_artefakt(), "expired": True})
    assert not modul.vertrauenswuerdig({"id": 2, "expired": False})


def test_das_juengste_passende_artefakt_gewinnt() -> None:
    modul = _modul()
    alt = {**_artefakt(), "id": 1, "created_at": "2026-11-01T00:00:00Z"}
    neu = {**_artefakt(), "id": 2, "created_at": "2026-11-09T00:00:00Z"}
    fremd = {**_artefakt(head_repository_id=99), "id": 3, "created_at": "2026-11-10T00:00:00Z"}
    assert modul.auswaehlen([alt, fremd, neu])["id"] == 2
    assert modul.auswaehlen([fremd]) is None


def test_entpacken_schreibt_nur_einfache_dateinamen(tmp_path: Path) -> None:
    modul = _modul()
    archiv = _archiv({"testvorlage.dump": b"abbild", "../ausbruch.txt": b"x", "ordner/datei.txt": b"y"})
    assert modul.entpacken(archiv, tmp_path / "ziel") == ["testvorlage.dump"]
    assert (tmp_path / "ziel" / "testvorlage.dump").read_bytes() == b"abbild"
    assert not (tmp_path / "ausbruch.txt").exists()


def test_ohne_artefakt_oder_bei_netzfehler_endet_das_skript_mit_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modul = _modul()
    monkeypatch.setenv("GITHUB_REPOSITORY", "beispiel/projekt")
    monkeypatch.setenv("GH_TOKEN", "t")

    with mock.patch.object(modul, "_abruf", return_value=b'{"artifacts": []}'):
        assert modul.main(["testvorlage-abc", str(tmp_path)]) == 0
    assert "Kein Artefakt" in capsys.readouterr().out

    with mock.patch.object(modul, "_abruf", side_effect=OSError("weg")):
        assert modul.main(["testvorlage-abc", str(tmp_path)]) == 0
    assert "migriert selbst" in capsys.readouterr().out

    monkeypatch.delenv("GH_TOKEN")
    with mock.patch.object(modul, "_abruf") as abruf:
        assert modul.main(["testvorlage-abc", str(tmp_path)]) == 0
    abruf.assert_not_called()


def test_holen_laedt_und_entpackt_das_artefakt(tmp_path: Path) -> None:
    modul = _modul()
    liste = b'{"artifacts": [{"id": 5, "expired": false, "created_at": "2026-11-10T10:00:00Z", "workflow_run": {"head_branch": "dev", "repository_id": 7, "head_repository_id": 7}}]}'
    archiv = _archiv({"testvorlage.dump": b"abbild", "testvorlage.dump.migrations": b"fingerabdruck"})
    with mock.patch.object(modul, "_abruf", side_effect=[liste, archiv]) as abruf:
        meldung = modul.holen("testvorlage-abc", tmp_path, "beispiel/projekt", "t")
    assert abruf.call_args_list[1].args[0].endswith("/actions/artifacts/5/zip")
    assert "testvorlage.dump" in meldung and (tmp_path / "testvorlage.dump.migrations").read_bytes() == b"fingerabdruck"


# --- Abbild der PostgreSQL-Vorlage (scripts/testdb_vorlage.py) ----------------------------------------------


def _vorlage_modul() -> Any:
    spec = importlib.util.spec_from_file_location("testdb_vorlage", WURZEL / "scripts" / "testdb_vorlage.py")
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_abbild_passt_nur_mit_dem_fingerabdruck_des_arbeitsstands(tmp_path: Path) -> None:
    modul = _vorlage_modul()
    abbild = tmp_path / "testvorlage.dump"
    assert not modul._abbild_passt(abbild), "ohne Datei"
    abbild.write_bytes(b"abbild")
    assert not modul._abbild_passt(abbild), "ohne Fingerabdruck"
    fingerabdruck = tmp_path / "testvorlage.dump.migrations"
    fingerabdruck.write_text("veraltet", encoding="utf-8")
    assert not modul._abbild_passt(abbild), "anderer Stand der Migrationen"
    fingerabdruck.write_text(modul.migration_fingerprint(modul.PROJEKT) + "\n", encoding="utf-8")
    assert modul._abbild_passt(abbild)


def test_abbild_schreiben_und_einspielen(tmp_path: Path) -> None:
    """Rundlauf mit pg_dump und pg_restore gegen den Server der CI: Tabelle, Daten und Erweiterung kommen an."""
    import os
    import shutil
    import uuid

    url = os.environ.get("DATABASE_URL", "")
    im_container = bool(os.environ.get("MANDARI_PG_CONTAINER", "").strip())
    if not url.startswith(("postgres://", "postgresql://")) or not (im_container or shutil.which("pg_dump")):
        pytest.skip("braucht PostgreSQL und pg_dump (in der CI beides da)")
    import psycopg
    from psycopg import sql

    modul = _vorlage_modul()
    wartung = modul._mit_datenbank(url, "postgres")
    quelle, ziel = (f"abbildtest_{uuid.uuid4().hex[:12]}_{art}" for art in ("quelle", "ziel"))
    abbild = tmp_path / "abbild.dump"
    try:
        modul._neu_anlegen(wartung, sql.Identifier(quelle), quelle)
        with psycopg.connect(modul._mit_datenbank(url, quelle), autocommit=True) as verbindung:
            verbindung.execute("CREATE TABLE probe (id integer PRIMARY KEY, wert text NOT NULL)")
            verbindung.execute("INSERT INTO probe VALUES (1, 'Köln'), (2, 'Münster')")
        modul._abbild_schreiben(abbild, modul._mit_datenbank(url, quelle))
        assert modul._abbild_passt(abbild)

        modul._neu_anlegen(wartung, sql.Identifier(ziel), ziel)
        assert modul._einspielen(abbild, modul._mit_datenbank(url, ziel)) is True
        with psycopg.connect(modul._mit_datenbank(url, ziel), autocommit=True) as verbindung:
            assert verbindung.execute("SELECT wert FROM probe ORDER BY id").fetchall() == [("Köln",), ("Münster",)]

        # Kaputtes Abbild: kein Abbruch, der Aufrufer migriert dann selbst
        abbild.write_bytes(b"kein Abbild")
        assert modul._einspielen(abbild, modul._mit_datenbank(url, ziel)) is False
    finally:
        with psycopg.connect(wartung, autocommit=True) as verbindung:
            for name in (quelle, ziel):
                verbindung.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_token_geht_bei_weiterleitung_nicht_an_einen_anderen_host() -> None:
    """Der Abruf eines Artefakts leitet auf den Dateispeicher weiter; dorthin gehört das Token nicht."""
    import http.server
    import threading

    gesehen: dict[str, list[str | None]] = {"ziel": [], "selber_host": []}

    class Ziel(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            gesehen["ziel" if self.path == "/datei" else "selber_host"].append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"inhalt")

        def log_message(self, *args: Any) -> None:
            pass

    ziel = http.server.HTTPServer(("127.0.0.1", 0), Ziel)

    class Quelle(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            # /fremd: anderer Host (anderer Port), /gleich: derselbe Host
            ort = f"http://127.0.0.1:{ziel.server_port}/datei" if self.path == "/fremd" else "/hier"
            self.send_header("Location", ort)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    class QuelleMitZiel(Quelle):
        def do_GET(self) -> None:
            if self.path == "/hier":
                Ziel.do_GET(self)  # type: ignore[arg-type]
            else:
                super().do_GET()

    quelle = http.server.HTTPServer(("127.0.0.1", 0), QuelleMitZiel)
    for server in (ziel, quelle):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        modul = _modul()
        oeffner = urllib.request.build_opener(modul._OhneTokenWeiter)
        for pfad in ("/fremd", "/gleich"):
            anfrage = urllib.request.Request(  # noqa: S310 - lokaler Testserver
                f"http://127.0.0.1:{quelle.server_port}{pfad}", headers={"Authorization": "Bearer geheim"}
            )
            assert oeffner.open(anfrage, timeout=10).read() == b"inhalt"
    finally:
        for server in (ziel, quelle):
            server.shutdown()
            server.server_close()

    assert gesehen["ziel"] == [None], "Das Token ging an den anderen Host"
    assert gesehen["selber_host"] == ["Bearer geheim"], "Auf demselben Host bleibt die Anmeldung"

    with pytest.raises(ValueError, match="https"):
        modul._abruf("http://example.invalid/", "t")
