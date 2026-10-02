# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung der Beitragsvereinbarung im Pull Request (scripts/cla_pruefung.py, .github/workflows/cla.yml).

Die Prüfung entscheidet, ob ein Beitrag von außen angenommen werden darf, und läuft mit einem
schreibenden Token auch für Pull Requests aus Forks. Beides fällt im Alltag nicht auf, wenn es
bricht: Pull Requests von Projektkonten bestehen immer, und der erste Beitrag von außen ist der
falsche Moment, um einen Fehler zu finden. Diese Tests halten deshalb die Regeln fest (wer ist
freigestellt, wer braucht einen Eintrag) und die Eigenschaften des Workflows, die ihn sicher machen.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "scripts" / "cla_pruefung.py"
WORKFLOW = REPO / ".github" / "workflows" / "cla.yml"
KONFIGURATION = REPO / ".github" / "cla.json"

pytestmark = pytest.mark.skipif(not SKRIPT.exists(), reason="Skript nur im Repository, nicht im Image")

REPO_NAME = "beispiel/projekt"
PR = 7

INHABERIN = {"login": "Projektkonto", "id": 1}
DEPENDABOT = {"login": "dependabot[bot]", "id": 49699333}
ANNA = {"login": "anna-extern", "id": 1001}
BEN = {"login": "ben-extern", "id": 1002}


@pytest.fixture(scope="module")
def cla() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cla_pruefung", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _konfiguration(cla: ModuleType, versionen: list[str] | None = None) -> Any:
    return cla.konfiguration_aus(
        {
            "gueltige_versionen": ["1.0"] if versionen is None else versionen,
            "projektkonten": ["Projektkonto"],
            "bots": ["dependabot[bot]"],
        }
    )


def _konto(cla: ModuleType, daten: Mapping[str, Any] | None) -> Any:
    return None if daten is None else cla.Konto(login=daten["login"], id=daten["id"])


def _commit(cla: ModuleType, sha: str, *autoren: Mapping[str, Any] | None) -> Any:
    return cla.Commit(sha=sha * 8, autoren=tuple(_konto(cla, autor) for autor in autoren))


def _signatur(cla: ModuleType, konto: Mapping[str, Any], version: str = "1.0") -> Any:
    return cla.Signatur(konto=konto["login"], id=konto["id"], datum="2026-10-01", version=version)


def _liste(*konten: Mapping[str, Any], version: str = "1.0") -> str:
    return json.dumps(
        {
            "signaturen": [
                {"konto": konto["login"], "id": konto["id"], "datum": "2026-10-01", "version": version}
                for konto in konten
            ]
        }
    )


# =============================================================================
# Regeln
# =============================================================================


def test_projektkonto_besteht_ohne_eintrag(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, INHABERIN), [_commit(cla, "a", INHABERIN)], _konfiguration(cla), [])

    assert ergebnis.bestanden
    assert ergebnis.konten == {"Projektkonto": cla.PROJEKT}


def test_projektkonto_wird_ohne_ruecksicht_auf_die_schreibweise_erkannt(cla: ModuleType) -> None:
    konto = {"login": "projektKONTO", "id": 1}

    assert cla.pruefe(_konto(cla, konto), [_commit(cla, "a", konto)], _konfiguration(cla), []).bestanden


def test_bot_besteht_ohne_eintrag(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, DEPENDABOT), [_commit(cla, "a", DEPENDABOT)], _konfiguration(cla), [])

    assert ergebnis.bestanden
    assert ergebnis.konten == {"dependabot[bot]": cla.BOT}


def test_nicht_eingetragener_bot_ist_nicht_freigestellt(cla: ModuleType) -> None:
    """Sonst ließe sich die Prüfung umgehen, indem eine eigene GitHub-App den Pull Request öffnet."""
    fremder_bot = {"login": "irgendein-werkzeug[bot]", "id": 555}

    ergebnis = cla.pruefe(_konto(cla, fremder_bot), [_commit(cla, "a", fremder_bot)], _konfiguration(cla), [])

    assert ergebnis.fehlende_konten == ["irgendein-werkzeug[bot]"]


def test_externes_konto_ohne_eintrag_scheitert(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], _konfiguration(cla), [])

    assert not ergebnis.bestanden
    assert ergebnis.fehlende_konten == ["anna-extern"]


def test_externes_konto_mit_eintrag_besteht(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], _konfiguration(cla), [_signatur(cla, ANNA)])

    assert ergebnis.bestanden
    assert ergebnis.konten == {"anna-extern": cla.ZUSTIMMUNG}


def test_eintrag_zu_einer_nicht_mehr_gueltigen_fassung_zaehlt_nicht(cla: ModuleType) -> None:
    konfiguration = _konfiguration(cla, versionen=["2.0"])

    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], konfiguration, [_signatur(cla, ANNA, "1.0")])

    assert ergebnis.fehlende_konten == ["anna-extern"]


def test_mehrere_gueltige_fassungen_nebeneinander(cla: ModuleType) -> None:
    konfiguration = _konfiguration(cla, versionen=["1.0", "2.0"])

    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], konfiguration, [_signatur(cla, ANNA, "1.0")])

    assert ergebnis.bestanden


def test_ohne_freigegebene_fassung_besteht_kein_externes_konto(cla: ModuleType) -> None:
    """Auslieferungszustand: Solange keine Fassung eingetragen ist, gilt kein Eintrag."""
    ergebnis = cla.pruefe(
        _konto(cla, ANNA), [_commit(cla, "a", ANNA)], _konfiguration(cla, versionen=[]), [_signatur(cla, ANNA)]
    )

    assert ergebnis.fehlende_konten == ["anna-extern"]
    assert not ergebnis.vereinbarung_verfuegbar


def test_eintrag_folgt_der_kontokennung_nicht_dem_namen(cla: ModuleType) -> None:
    """
    Ein umbenanntes Konto behält seine Zustimmung. Wer den frei gewordenen Namen neu registriert,
    erbt sie nicht.
    """
    umbenannt = {"login": "anna-neu", "id": ANNA["id"]}
    namensnachfolger = {"login": "anna-extern", "id": 999_999}
    signaturen = [_signatur(cla, ANNA)]

    assert cla.pruefe(_konto(cla, umbenannt), [_commit(cla, "a", umbenannt)], _konfiguration(cla), signaturen).bestanden
    nachfolger = cla.pruefe(
        _konto(cla, namensnachfolger), [_commit(cla, "a", namensnachfolger)], _konfiguration(cla), signaturen
    )
    assert nachfolger.fehlende_konten == ["anna-extern"]


def test_mehrere_autoren_jeder_braucht_einen_eintrag(cla: ModuleType) -> None:
    commits = [_commit(cla, "a", ANNA), _commit(cla, "b", BEN), _commit(cla, "c", INHABERIN), _commit(cla, "d", ANNA)]

    nur_anna = cla.pruefe(_konto(cla, ANNA), commits, _konfiguration(cla), [_signatur(cla, ANNA)])
    beide = cla.pruefe(_konto(cla, ANNA), commits, _konfiguration(cla), [_signatur(cla, ANNA), _signatur(cla, BEN)])

    assert nur_anna.fehlende_konten == ["ben-extern"]
    assert nur_anna.konten == {
        "anna-extern": cla.ZUSTIMMUNG,
        "ben-extern": cla.FEHLT,
        "Projektkonto": cla.PROJEKT,
    }
    assert beide.bestanden


def test_mitautor_aus_co_authored_by_braucht_einen_eintrag(cla: ModuleType) -> None:
    commits = [_commit(cla, "a", ANNA, BEN)]

    ergebnis = cla.pruefe(_konto(cla, ANNA), commits, _konfiguration(cla), [_signatur(cla, ANNA)])

    assert ergebnis.fehlende_konten == ["ben-extern"]


def test_externer_commit_im_pull_request_eines_projektkontos_scheitert(cla: ModuleType) -> None:
    commits = [_commit(cla, "a", INHABERIN), _commit(cla, "b", BEN)]

    ergebnis = cla.pruefe(_konto(cla, INHABERIN), commits, _konfiguration(cla), [])

    assert ergebnis.fehlende_konten == ["ben-extern"]


def test_autorenangabe_eines_projektkontos_ersetzt_die_zustimmung_nicht(cla: ModuleType) -> None:
    """
    Die Autorenangabe eines Commits ist frei wählbar. Maßgeblich bleibt das Konto, das den Pull
    Request geöffnet hat: Es ist von GitHub angemeldet.
    """
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", INHABERIN)], _konfiguration(cla), [])

    assert ergebnis.fehlende_konten == ["anna-extern"]


def test_commit_ohne_konto_zaehlt_beim_projektkonto_als_dessen_beitrag(cla: ModuleType) -> None:
    """Im Projekt üblich: Die Commit-Adresse ist nicht die Adresse des GitHub-Kontos."""
    ergebnis = cla.pruefe(_konto(cla, INHABERIN), [_commit(cla, "a", None)], _konfiguration(cla), [])

    assert ergebnis.bestanden
    assert ergebnis.commits_ohne_konto == ["aaaaaaa"]
    assert ergebnis.offene_commits == []


def test_commit_ohne_konto_im_pull_request_eines_bots_besteht(cla: ModuleType) -> None:
    """Nachgezogene Lock-Datei im Dependabot-Zweig: Dorthin kann nur schreiben, wer Schreibrecht hat."""
    commits = [_commit(cla, "a", DEPENDABOT), _commit(cla, "b", None)]

    assert cla.pruefe(_konto(cla, DEPENDABOT), commits, _konfiguration(cla), []).bestanden


def test_commit_ohne_konto_scheitert_bei_externem_pull_request_trotz_eintrag(cla: ModuleType) -> None:
    commits = [_commit(cla, "a", ANNA), _commit(cla, "b", None), _commit(cla, "c", ANNA, None)]

    ergebnis = cla.pruefe(_konto(cla, ANNA), commits, _konfiguration(cla), [_signatur(cla, ANNA)])

    assert not ergebnis.bestanden
    assert ergebnis.fehlende_konten == []
    assert ergebnis.offene_commits == ["bbbbbbb", "ccccccc"]


# =============================================================================
# Konfiguration und Signaturliste
# =============================================================================


def test_konfiguration_im_repository_ist_gueltig(cla: ModuleType) -> None:
    konfiguration = cla.lade_konfiguration(KONFIGURATION)

    assert konfiguration.projektkonten, "ohne Projektkonto scheitert jeder eigene Pull Request"
    assert "dependabot[bot]" in konfiguration.bots


@pytest.mark.parametrize(
    "daten",
    [
        [],
        {"gueltige_versionen": [], "projektkonten": []},
        {"gueltige_versionen": [], "projektkonten": [], "bots": [], "gueltige_version": ["1.0"]},
        {"gueltige_versionen": "1.0", "projektkonten": [], "bots": []},
        {"gueltige_versionen": [""], "projektkonten": [], "bots": []},
        {"gueltige_versionen": [], "projektkonten": [], "bots": ["anna-extern"]},
    ],
    ids=[
        "kein-objekt",
        "schluessel-fehlt",
        "tippfehler-im-schluessel",
        "text-statt-liste",
        "leere-fassung",
        "kein-bot",
    ],
)
def test_fehlerhafte_konfiguration_wird_abgelehnt(cla: ModuleType, daten: Any) -> None:
    with pytest.raises(cla.AuswertungError):
        cla.konfiguration_aus(daten)


@pytest.mark.parametrize("text", [None, "", "  \n"])
def test_fehlende_signaturliste_ist_leer(cla: ModuleType, text: str | None) -> None:
    assert cla.signaturen_aus(text) == []


def test_signaturliste_wird_gelesen(cla: ModuleType) -> None:
    assert cla.signaturen_aus(_liste(ANNA, BEN)) == [_signatur(cla, ANNA), _signatur(cla, BEN)]


@pytest.mark.parametrize(
    "eintrag",
    [
        {"konto": "geheim-konto", "datum": "2026-10-01", "version": "1.0"},
        {"konto": "geheim-konto", "id": "1001", "datum": "2026-10-01", "version": "1.0"},
        {"konto": "geheim-konto", "id": True, "datum": "2026-10-01", "version": "1.0"},
        {"konto": "geheim-konto", "id": 1001, "datum": "01.10.2026", "version": "1.0"},
        {"konto": "geheim-konto", "id": 1001, "datum": "2026-10-01", "version": ""},
        {"konto": "geheim-konto", "id": 1001, "datum": "2026-10-01", "version": "1.0", "email": "geheim@example.org"},
    ],
    ids=[
        "ohne-kennung",
        "kennung-als-text",
        "kennung-als-wahrheitswert",
        "datum-deutsch",
        "ohne-fassung",
        "mit-e-mail",
    ],
)
def test_fehlerhafter_eintrag_wird_abgelehnt_ohne_inhalt_zu_nennen(cla: ModuleType, eintrag: dict[str, Any]) -> None:
    """Die Meldung steht im öffentlichen Protokoll und darf deshalb nichts aus der Liste verraten."""
    with pytest.raises(cla.AuswertungError) as fehler:
        cla.signaturen_aus(json.dumps({"signaturen": [eintrag]}))

    assert "Eintrag 1" in str(fehler.value)
    assert "geheim" not in str(fehler.value)


@pytest.mark.parametrize("text", ["{kein json", "[]", '{"signaturen": {}}'])
def test_unlesbare_signaturliste_wird_abgelehnt(cla: ModuleType, text: str) -> None:
    with pytest.raises(cla.AuswertungError):
        cla.signaturen_aus(text)


# =============================================================================
# Texte
# =============================================================================


def test_hinweis_vor_der_freigabe_nennt_den_stand_und_den_weg_ueber_ein_issue(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], _konfiguration(cla, versionen=[]), [])

    text = cla.kommentar_text(ergebnis, REPO_NAME, "dev")

    assert text.startswith(cla.MARKE)
    assert "@anna-extern" in text
    assert "wird derzeit finalisiert" in text
    assert "Issue" in text
    assert f"https://github.com/{REPO_NAME}/blob/dev/CONTRIBUTING.md#lizenz-und-urheberrecht" in text


def test_hinweis_nach_der_freigabe_verweist_auf_den_ablauf(cla: ModuleType) -> None:
    commits = [_commit(cla, "a", ANNA), _commit(cla, "b", BEN)]
    ergebnis = cla.pruefe(_konto(cla, ANNA), commits, _konfiguration(cla), [_signatur(cla, ANNA)])

    text = cla.kommentar_text(ergebnis, REPO_NAME, "dev")

    assert "@ben-extern" in text
    assert "@anna-extern" not in text, "wer zugestimmt hat, wird nicht erneut angesprochen"
    assert "finalisiert" not in text
    assert f"https://github.com/{REPO_NAME}/blob/dev/docs/cla/README.md" in text


def test_hinweis_zu_commits_ohne_konto_nennt_nur_die_kennungen(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(
        _konto(cla, ANNA),
        [_commit(cla, "a", ANNA), _commit(cla, "b", None)],
        _konfiguration(cla),
        [_signatur(cla, ANNA)],
    )

    text = cla.kommentar_text(ergebnis, REPO_NAME, "dev")

    assert "`bbbbbbb`" in text
    assert "keinem GitHub-Konto zugeordnet" in text
    assert "liegt noch keine Zustimmung" not in text


def test_hinweis_spricht_hoechstens_zehn_konten_an(cla: ModuleType) -> None:
    """Die Autorenangabe ist frei wählbar; ohne Grenze ließen sich über den Hinweis beliebige Konten anschreiben."""
    fremde = [{"login": f"konto-{nummer:02d}", "id": 2000 + nummer} for nummer in range(1, 31)]
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA, *fremde)], _konfiguration(cla), [])

    text = cla.kommentar_text(ergebnis, REPO_NAME, "dev")

    assert text.count("@") == 10
    assert "und 21 weitere" in text


def test_kein_hinweis_wenn_alles_vollstaendig_ist(cla: ModuleType) -> None:
    ergebnis = cla.pruefe(_konto(cla, INHABERIN), [_commit(cla, "a", None)], _konfiguration(cla), [])

    assert cla.kommentar_text(ergebnis, REPO_NAME, "dev") is None


def test_zusammenfassung_nennt_weder_datum_noch_fassung_einer_zustimmung(cla: ModuleType) -> None:
    signatur = cla.Signatur(konto="anna-extern", id=ANNA["id"], datum="2026-10-01", version="fassung-x")
    konfiguration = _konfiguration(cla, versionen=["fassung-x"])
    ergebnis = cla.pruefe(_konto(cla, ANNA), [_commit(cla, "a", ANNA)], konfiguration, [signatur])

    text = cla.zusammenfassung(ergebnis)

    assert "| anna-extern | Zustimmung liegt vor |" in text
    assert "2026-10-01" not in text
    assert "fassung-x" not in text


# =============================================================================
# Ablauf gegen die GitHub-API (nachgebildet)
# =============================================================================


class GitHubAttrappe:
    """Antwortet wie die GitHub-API auf die Aufrufe des Skripts und merkt sich Schreibzugriffe."""

    def __init__(
        self,
        autor: Mapping[str, Any],
        commits: list[list[Mapping[str, Any] | None]],
        kommentare: list[dict[str, Any]] | None = None,
        seitengroesse: int = 100,
        kommentieren_verboten: bool = False,
    ) -> None:
        self.autor = autor
        self.commits = commits
        self.kommentare = kommentare or []
        self.seitengroesse = seitengroesse
        self.kommentieren_verboten = kommentieren_verboten
        self.schreibzugriffe: list[tuple[str, str]] = []
        self.fehler: type[Exception] = Exception

    def rest(self, methode: str, pfad: str, daten: Mapping[str, Any] | None = None) -> Any:
        if methode == "GET" and pfad == f"/repos/{REPO_NAME}/pulls/{PR}":
            return {"user": dict(self.autor), "base": {"ref": "dev"}}
        if methode == "GET" and pfad.startswith(f"/repos/{REPO_NAME}/issues/{PR}/comments?"):
            treffer = re.search(r"page=(\d+)$", pfad)
            assert treffer
            anfang = (int(treffer.group(1)) - 1) * 100
            return self.kommentare[anfang : anfang + 100]
        assert daten is not None
        if self.kommentieren_verboten:
            raise self.fehler("HTTP 403")
        self.schreibzugriffe.append((methode, pfad))
        if methode == "POST" and pfad == f"/repos/{REPO_NAME}/issues/{PR}/comments":
            self.kommentare.append(
                {"id": 100 + len(self.kommentare), "user": {"login": "github-actions[bot]"}, **daten}
            )
            return None
        treffer = re.fullmatch(rf"/repos/{REPO_NAME}/issues/comments/(\d+)", pfad)
        assert methode == "PATCH" and treffer, f"unerwarteter Aufruf {methode} {pfad}"
        kommentar = next(k for k in self.kommentare if k["id"] == int(treffer.group(1)))
        kommentar.update(daten)
        return None

    def graphql(self, abfrage: str, variablen: Mapping[str, Any]) -> Any:
        assert variablen["owner"] == "beispiel" and variablen["name"] == "projekt" and variablen["nummer"] == PR
        anfang = int(variablen["cursor"] or 0)
        ende = anfang + self.seitengroesse
        knoten = [
            {
                "commit": {
                    "oid": f"{nummer:040x}",
                    "authors": {
                        "totalCount": len(autoren),
                        "nodes": [
                            {"user": None if a is None else {"login": a["login"], "databaseId": a["id"]}}
                            for a in autoren
                        ],
                    },
                }
            }
            for nummer, autoren in enumerate(self.commits[anfang:ende], start=anfang + 1)
        ]
        seite = {"hasNextPage": ende < len(self.commits), "endCursor": str(ende)}
        return {"repository": {"pullRequest": {"commits": {"pageInfo": seite, "nodes": knoten}}}}

    def eigener_kommentar(self) -> str:
        eigene = [k["body"] for k in self.kommentare if k["user"]["login"] == "github-actions[bot]"]
        assert len(eigene) == 1, "genau ein Kommentar des Workflows erwartet"
        return str(eigene[0])


@pytest.fixture
def konfigurationsdatei(tmp_path: Path) -> Path:
    pfad = tmp_path / "cla.json"
    pfad.write_text(
        json.dumps({"gueltige_versionen": ["1.0"], "projektkonten": ["Projektkonto"], "bots": ["dependabot[bot]"]}),
        encoding="utf-8",
    )
    return pfad


def _lauf(
    cla: ModuleType, github: GitHubAttrappe, konfigurationsdatei: Path, signaturen: str | None = None, *mehr: str
) -> int:
    github.fehler = cla.AuswertungError
    umgebung = {} if signaturen is None else {"CLA_SIGNATUREN": signaturen}
    argumente = ["--repo", REPO_NAME, "--pr", str(PR), "--konfiguration", str(konfigurationsdatei), *mehr]
    ergebnis: int = cla.main(argumente, umgebung, github)
    return ergebnis


def test_commits_werden_ueber_alle_seiten_gelesen(cla: ModuleType) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA], [ANNA, BEN], [None]], seitengroesse=2)

    pull_request = cla.lade_pull_request(github, REPO_NAME, PR)

    assert pull_request.autor == cla.Konto(login="anna-extern", id=1001)
    assert pull_request.basiszweig == "dev"
    assert [len(commit.autoren) for commit in pull_request.commits] == [1, 2, 1]
    assert pull_request.commits[1].autoren[1] == cla.Konto(login="ben-extern", id=1002)
    assert pull_request.commits[2].autoren == (None,)


def test_lauf_projektkonto_besteht_und_kommentiert_nicht(cla: ModuleType, konfigurationsdatei: Path) -> None:
    github = GitHubAttrappe(INHABERIN, [[None], [INHABERIN]])

    assert _lauf(cla, github, konfigurationsdatei) == 0
    assert github.schreibzugriffe == []


def test_lauf_externes_konto_ohne_eintrag_scheitert_mit_hinweis(cla: ModuleType, konfigurationsdatei: Path) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA]])

    assert _lauf(cla, github, konfigurationsdatei, _liste(BEN)) == 1
    assert "@anna-extern" in github.eigener_kommentar()


def test_lauf_externes_konto_mit_eintrag_besteht(cla: ModuleType, konfigurationsdatei: Path) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA]])

    assert _lauf(cla, github, konfigurationsdatei, _liste(ANNA)) == 0
    assert github.schreibzugriffe == []


def test_hinweis_wird_aktualisiert_statt_wiederholt(cla: ModuleType, konfigurationsdatei: Path) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA], [BEN]])

    assert _lauf(cla, github, konfigurationsdatei) == 1
    assert _lauf(cla, github, konfigurationsdatei) == 1
    assert [methode for methode, _ in github.schreibzugriffe] == ["POST"], (
        "unveränderter Hinweis wird nicht neu geschrieben"
    )

    assert _lauf(cla, github, konfigurationsdatei, _liste(ANNA)) == 1
    assert "@ben-extern" in github.eigener_kommentar()
    assert "@anna-extern" not in github.eigener_kommentar()

    assert _lauf(cla, github, konfigurationsdatei, _liste(ANNA, BEN)) == 0
    assert "liegt die Zustimmung zur Beitragsvereinbarung vor" in github.eigener_kommentar()
    assert [methode for methode, _ in github.schreibzugriffe] == ["POST", "PATCH", "PATCH"]


def test_fremder_kommentar_mit_der_marke_bleibt_unberuehrt(cla: ModuleType, konfigurationsdatei: Path) -> None:
    fremd = {"id": 1, "user": {"login": "anna-extern"}, "body": f"{cla.MARKE} zitiert"}
    github = GitHubAttrappe(ANNA, [[ANNA]], kommentare=[fremd])

    assert _lauf(cla, github, konfigurationsdatei) == 1
    assert github.kommentare[0]["body"] == f"{cla.MARKE} zitiert"
    assert github.schreibzugriffe == [("POST", f"/repos/{REPO_NAME}/issues/{PR}/comments")]


def test_eigener_kommentar_wird_auch_hinter_der_ersten_seite_gefunden(
    cla: ModuleType, konfigurationsdatei: Path
) -> None:
    andere = [{"id": n, "user": {"login": "anna-extern"}, "body": "Rückfrage"} for n in range(1, 101)]
    eigener = {"id": 500, "user": {"login": "github-actions[bot]"}, "body": f"{cla.MARKE} alt"}
    github = GitHubAttrappe(ANNA, [[ANNA]], kommentare=[*andere, eigener])

    assert _lauf(cla, github, konfigurationsdatei) == 1
    assert github.schreibzugriffe == [("PATCH", f"/repos/{REPO_NAME}/issues/comments/500")]


def test_kein_kommentar_auf_wunsch(cla: ModuleType, konfigurationsdatei: Path) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA]])

    assert _lauf(cla, github, konfigurationsdatei, None, "--kein-kommentar") == 1
    assert github.schreibzugriffe == []


def test_ergebnis_gilt_auch_wenn_der_kommentar_nicht_geschrieben_werden_kann(
    cla: ModuleType, konfigurationsdatei: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA]], kommentieren_verboten=True)

    assert _lauf(cla, github, konfigurationsdatei) == 1
    assert "::warning::Kommentar nicht geschrieben" in capsys.readouterr().out


def test_unlesbare_liste_ist_ein_technischer_fehler_und_kein_vorwurf(
    cla: ModuleType, konfigurationsdatei: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = GitHubAttrappe(ANNA, [[ANNA]])

    assert _lauf(cla, github, konfigurationsdatei, "{kaputt") == 2
    assert github.schreibzugriffe == [], "kein Hinweis auf fehlende Zustimmung, wenn die Liste nicht lesbar ist"
    assert "konnte nicht ausgewertet werden" in capsys.readouterr().out


def test_unlesbare_liste_haelt_pull_requests_von_projektkonten_nicht_auf(
    cla: ModuleType, konfigurationsdatei: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = GitHubAttrappe(INHABERIN, [[INHABERIN]])

    assert _lauf(cla, github, konfigurationsdatei, "{kaputt") == 0
    assert "::warning::Signaturliste" in capsys.readouterr().out


def test_ausgabe_verraet_nichts_aus_der_signaturliste(
    cla: ModuleType, konfigurationsdatei: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Das Protokoll des Workflows ist öffentlich, die Liste nicht."""
    github = GitHubAttrappe(ANNA, [[ANNA]])

    assert _lauf(cla, github, konfigurationsdatei, _liste(ANNA, BEN)) == 0

    ausgabe = capsys.readouterr().out
    assert "ben-extern" not in ausgabe
    assert "2026-10-01" not in ausgabe


def test_liste_pruefen_ohne_netzwerk(cla: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gut = tmp_path / "gut.json"
    gut.write_text(_liste(ANNA, BEN), encoding="utf-8")
    schlecht = tmp_path / "schlecht.json"
    schlecht.write_text('{"signaturen": [{"konto": "anna-extern"}]}', encoding="utf-8")

    assert cla.main(["--liste-pruefen", str(gut)], {}) == 0
    assert "2 Einträge" in capsys.readouterr().out
    assert cla.main(["--liste-pruefen", str(schlecht)], {}) == 2


def test_ohne_token_technischer_fehler(cla: ModuleType, konfigurationsdatei: Path) -> None:
    argumente = ["--repo", REPO_NAME, "--pr", str(PR), "--konfiguration", str(konfigurationsdatei)]

    assert cla.main(argumente, {}) == 2


# =============================================================================
# Workflow: Eigenschaften, die ihn für Pull Requests aus Forks sicher machen
# =============================================================================


def _workflow() -> dict[Any, Any]:
    daten: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return daten


def _schritte() -> list[dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["pruefung"], "weitere Jobs brauchen eigene Tests für Rechte und Checkout"
    schritte: list[dict[str, Any]] = jobs["pruefung"]["steps"]
    return schritte


def test_workflow_laeuft_nur_bei_pull_request_target() -> None:
    # PyYAML liest den Schlüssel "on" als True
    ausloeser = _workflow()[True]

    assert list(ausloeser) == ["pull_request_target"]
    assert set(ausloeser["pull_request_target"]["types"]) == {"opened", "reopened", "synchronize"}


def test_workflow_hat_nur_die_noetigen_rechte() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {}
    assert workflow["jobs"]["pruefung"]["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_workflow_checkt_nur_den_zielzweig_aus() -> None:
    checkouts = [schritt for schritt in _schritte() if str(schritt.get("uses", "")).startswith("actions/checkout@")]

    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "${{ github.event.pull_request.base.ref }}"
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_workflow_verwendet_den_stand_des_pull_requests_nirgends() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for verboten in ("pull_request.head", "github.head_ref", "refs/pull/", "merge_commit_sha"):
        assert verboten not in text, f"{verboten}: Der Workflow darf den Stand des Pull Requests nicht anfassen"


def test_workflow_setzt_keine_ausdruecke_in_shell_befehle_ein() -> None:
    """Werte aus dem Ereignis gehen über env an das Skript, nie als Text in den Befehl."""
    befehle = [schritt["run"] for schritt in _schritte() if "run" in schritt]

    assert befehle, "der Prüfschritt fehlt"
    for befehl in befehle:
        assert "${{" not in befehl


def test_workflow_nutzt_keine_fremden_actions() -> None:
    for schritt in _schritte():
        if "uses" in schritt:
            assert schritt["uses"].startswith("actions/"), schritt["uses"]


def test_workflow_ruft_das_skript_mit_der_signaturliste_aus_dem_secret() -> None:
    (schritt,) = [schritt for schritt in _schritte() if "cla_pruefung.py" in schritt.get("run", "")]

    assert schritt["env"]["CLA_SIGNATUREN"] == "${{ secrets.CLA_SIGNATUREN }}"
    assert schritt["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert "scripts/cla_pruefung.py" in _schritte()[0]["with"]["sparse-checkout"]
    assert ".github/cla.json" in _schritte()[0]["with"]["sparse-checkout"]
