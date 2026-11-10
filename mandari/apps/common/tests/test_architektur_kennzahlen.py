# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Architektur-Kennzahlen der Datendrehscheibe (``scripts/architektur_kennzahlen.py``, Issue #569): Zählregeln an
kleinen Beispielen, Übernahme der vorhandenen Zählungen (Schichtverträge, RIS-Direktzugriffe, Ausnahmeliste der
Löschsemantik) und die Ausgabe als Tabelle, JSON und Verlaufszeile.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

WURZEL = Path(__file__).resolve().parents[4]
SKRIPT = WURZEL / "scripts" / "architektur_kennzahlen.py"
DOKU = WURZEL / "docs" / "ARCHITEKTUR_KENNZAHLEN.md"

MANAGER = frozenset({"objects", "_default_manager", "_base_manager"})
#: Wie in ``mandari/pyproject.toml``: Fachmodule je für sich, Drehscheibe und Plattform je eine Einheit.
EINHEITEN = {
    "apps.session": "apps.session",
    "apps.work": "apps.work",
    "hub": "schicht-2",
    "insight_core": "schicht-2",
    "apps.common": "schicht-3",
    "apps.tenants": "schicht-3",
}


@pytest.fixture(scope="module")
def kennzahlen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("architektur_kennzahlen", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _schreiber(kennzahlen: ModuleType, quelltext: str, pfad: str = "apps/work/dienst.py") -> list[tuple[str, Any]]:
    gefunden = kennzahlen.fremde_schreiber([kennzahlen.lies(quelltext, pfad)], EINHEITEN, MANAGER)
    return [(eintrag.ort.split("::")[1], eintrag.eigentuemer) for eintrag in gefunden]


# --- K1: Funktionen, die Daten eines fremden Moduls schreiben ---------------------------------------------

IMPORT = "from apps.session.models import SessionPaper\n"


@pytest.mark.parametrize(
    "rumpf",
    [
        "SessionPaper.objects.create(name='x')",
        "SessionPaper.objects.filter(pk=1).update(name='x')",
        "SessionPaper.objects.filter(pk=1).exclude(name='y').delete()",
        "SessionPaper._default_manager.bulk_create([])",
        "SessionPaper.objects.get_or_create(name='x')[0]",
        "await SessionPaper.objects.acreate(name='x')",
        "SessionPaper(name='x').save()",
        "vorlage = SessionPaper.objects.get(pk=1)\n    vorlage.name = 'x'\n    vorlage.save(update_fields=['name'])",
        "vorlage, neu = SessionPaper.objects.get_or_create(name='x')\n    vorlage.delete()",
        "vorlage = SessionPaper(name='x')\n    vorlage.save()",
        "vorlage = get_object_or_404(SessionPaper, pk=1)\n    vorlage.save()",
        "for vorlage in SessionPaper.objects.filter(name='x'):\n        vorlage.delete()",
        "if (vorlage := SessionPaper.objects.first()) is not None:\n        vorlage.save()",
    ],
)
def test_schreibzugriffe_auf_fremde_modelle_zaehlen(kennzahlen: ModuleType, rumpf: str) -> None:
    quelltext = f"{IMPORT}async def schreiben():\n    {rumpf}\n"
    assert _schreiber(kennzahlen, quelltext) == [("schreiben", ("apps.session",))]


@pytest.mark.parametrize(
    "rumpf",
    [
        "SessionPaper.objects.filter(pk=1).count()",
        "vorlage = SessionPaper.objects.get(pk=1)\n    return vorlage.name",
        "SessionPaper.DoesNotExist",
        # Variablen ohne Bezug zu einem Modell
        "daten = {}\n    daten.update(name='x')\n    formular.save()",
        # Aufruf eines fremden Dienstes: kein Schreibzugriff über das ORM (steht bei den Importausnahmen, K7)
        "dienst.anlegen(SessionPaper)",
    ],
)
def test_lesen_und_fremde_dienste_zaehlen_nicht(kennzahlen: ModuleType, rumpf: str) -> None:
    assert _schreiber(kennzahlen, f"{IMPORT}def lesen():\n    {rumpf}\n") == []


@pytest.mark.parametrize(
    ("pfad", "quelltext", "erwartet"),
    [
        # Eigene Modelle, auch aus einem anderen Paket desselben Moduls und über relative Importe
        ("apps/work/dienst.py", "from apps.work.motions.models import Motion\nMotion.objects.create()", []),
        ("apps/work/motions/dienst.py", "from .models import Motion\nMotion.objects.create()", []),
        ("apps/work/motions/dienst.py", "from ..models import Notiz\nNotiz.objects.create()", []),
        ("apps/work/motions/__init__.py", "from .models import Motion\nMotion.objects.create()", []),
        # Drehscheibe und Plattform sind je eine Einheit
        ("hub/ris/abruf.py", "from insight_core.models import OParlFile\nOParlFile.objects.update()", []),
        ("apps/common/dienst.py", "from apps.tenants.models import Role\nRole.objects.create()", []),
        # Über die Grenze einer Einheit: Fachmodul → Plattform, Drehscheibe → Plattform, Plattform → Fachmodul
        (
            "apps/work/dienst.py",
            "from apps.tenants.models import Role\nRole.objects.create()",
            [("<modul>", ("apps.tenants",))],
        ),
        (
            "hub/dienst.py",
            "from apps.common.models import Eintrag\nEintrag.objects.all().delete()",
            [("<modul>", ("apps.common",))],
        ),
        (
            "apps/common/management/commands/demo.py",
            "from apps.work.models import Notiz\nNotiz.objects.create()",
            [("<modul>", ("apps.work",))],
        ),
        # Modul statt Klasse importiert, mit und ohne Umbenennung
        (
            "apps/work/dienst.py",
            "from apps.session import models as sitzung\nsitzung.SessionPaper.objects.create()",
            [("<modul>", ("apps.session",))],
        ),
        (
            "apps/work/dienst.py",
            "from apps.session.models import SessionPaper as Vorlage\nVorlage.objects.create()",
            [("<modul>", ("apps.session",))],
        ),
        # Kein Modell: Name aus einem anderen Modul bzw. Konstante aus models
        ("apps/work/dienst.py", "from apps.session.services import SessionPaper\nSessionPaper.objects.create()", []),
        ("apps/work/dienst.py", "from apps.session.models import STATUS\nSTATUS.objects.create()", []),
        # Außerhalb der Schichtverträge (Projektpaket): keine Zuordnung, keine Zählung
        ("mandari/urls.py", "from apps.session.models import SessionPaper\nSessionPaper.objects.create()", []),
    ],
)
def test_eigentum_folgt_den_schichtvertraegen(
    kennzahlen: ModuleType, pfad: str, quelltext: str, erwartet: list[tuple[str, tuple[str, ...]]]
) -> None:
    assert _schreiber(kennzahlen, quelltext + "\n", pfad) == erwartet


def test_gezaehlt_wird_je_funktion(kennzahlen: ModuleType) -> None:
    quelltext = """
from apps.tenants.models import Role


class Dienst:
    def anlegen(self):
        from apps.session.models import SessionPaper

        SessionPaper.objects.create()
        SessionPaper.objects.filter(pk=1).delete()
        Role.objects.create()

    def lesen(self):
        return Role.objects.count()


def aussen():
    def innen():
        Role.objects.create()

    return innen
"""
    assert _schreiber(kennzahlen, quelltext) == [
        ("Dienst.anlegen", ("apps.session", "apps.tenants")),
        ("aussen.innen", ("apps.tenants",)),
    ]


def test_eigentuemer_einheiten_aus_den_vertraegen(kennzahlen: ModuleType) -> None:
    schichten = [
        (False, {"apps.session": False, "apps.work": False, "apps.portal": True}),
        (False, {"hub": False, "insight_core": False}),
        (False, {"apps.common": False}),
    ]
    einheiten = kennzahlen.eigentuemer_einheiten(schichten, ["apps.session", "apps.work"])
    assert einheiten == {
        "apps.session": "apps.session",
        "apps.work": "apps.work",
        "apps.portal": "schicht-1",
        "hub": "schicht-2",
        "insight_core": "schicht-2",
        "apps.common": "schicht-3",
    }
    assert kennzahlen.bereich("apps.work.motions.models", einheiten) == "apps.work"
    assert kennzahlen.bereich("apps.workflow.models", einheiten) is None
    assert kennzahlen.bereich("hub", einheiten) == "hub"


# --- K2: Schreibwege des Suchindex --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quelltext", "anzahl"),
    [
        ("client.index(index='papers', id='1', document={})", 1),
        ("client.delete(index='papers', id='1', ignore=[404])", 1),
        ("client.update(index='papers', id='1', doc={})", 1),
        ("client.delete_by_query(index='papers', body={})", 1),
        ("es.bulk(operations=[])", 1),
        ("from elasticsearch.helpers import streaming_bulk\nfor ok, _ in streaming_bulk(client, []):\n    pass", 1),
        ("from elasticsearch import helpers\nhelpers.bulk(client, [])", 1),
        # Der Ingestor spricht Elasticsearch über HTTP an
        ("async def f(self):\n    await self._client.post('/_bulk', content=b'')", 1),
        ("def f(c, name, kennung):\n    c.put(f'/{name}/_doc/{kennung}', json={})", 1),
        ("client.index(index='a', id='1', document={})\nclient.delete(index='a', id='1')", 2),
        # Lesen und Index-Verwaltung
        ("es.search(index='papers', query={})\nes.count(index='papers')\nes.mget(index='papers', ids=[])", 0),
        ("es.indices.delete(index='papers')\nes.indices.create(index='papers')", 0),
        ("client.post('/_search', json={})\nclient.get('/papers/_doc/1')", 0),
        # Gleichnamige Methoden von Listen, Wörterbüchern und des ORM
        ("stelle = liste.index('x')\nwerte.update(index=1)\nPunkt.objects.create(index=1)", 0),
        ("Punkt.objects.filter(pk=1).update(index=2)\npunkt.delete()", 0),
        ("def bulk(eintraege):\n    pass\nbulk([])", 0),
    ],
)
def test_schreibstellen_des_suchindex(kennzahlen: ModuleType, quelltext: str, anzahl: int) -> None:
    assert kennzahlen.suchindex_schreibstellen(kennzahlen.lies(quelltext + "\n", "insight_search/x.py")) == anzahl


def test_schreibwege_sind_dateien_mit_schreibstellen(kennzahlen: ModuleType) -> None:
    anwendung = [
        kennzahlen.lies("es.bulk(operations=[])\n", "insight_search/abonnement.py"),
        kennzahlen.lies("es.search(index='papers')\n", "insight_core/services/search_service.py"),
    ]
    ingestor = [kennzahlen.lies("c.post('/_bulk')\nc.post('/_bulk')\n", "indexing/elasticsearch.py", "src")]
    assert kennzahlen.suchindex_schreiber([("mandari", anwendung), ("ingestor/src", ingestor)]) == {
        "mandari/insight_search/abonnement.py": 1,
        "ingestor/src/indexing/elasticsearch.py": 2,
    }


# --- K3: Mailversand ---------------------------------------------------------------------------------


def test_funktionen_mit_mailversand(kennzahlen: ModuleType) -> None:
    dienst = kennzahlen.lies(
        """
from apps.common import mail
from apps.common.mail import send_template as senden


def einladung(org):
    mail.send(kind="work.einladung", subject="x", body="y", to=["a@example.org"], organization=org)
    mail.send(kind="work.einladung", subject="x", body="y", to=["b@example.org"], organization=org)


def testmail():
    return mail.send_template(kind="work.test", subject="x", template_name="t", context={}, to=[], sofort=True)


def erinnerung():
    senden(kind="work.erinnerung", subject="x", template_name="t", context={}, to=[])


def spaeter():
    from apps.common import mail as post

    post.send(kind="work.spaeter", subject="x", body="y", to=[])


def ohne_mail(kanal, signal):
    kanal.send(text_data="x")
    signal.send(sender=None)
    mail.queue_enabled("work.einladung")
""",
        "apps/work/dienst.py",
    )
    # Management-Befehle sind eigene Prozesse; eine gleichnamige Funktion aus anderem Modul ist kein Versand
    befehl = kennzahlen.lies(
        "from apps.common import mail\n\n\ndef handle():\n    mail.send(kind='x', subject='x', body='y', to=[])\n",
        "apps/work/management/commands/digest.py",
    )
    fremd = kennzahlen.lies(
        "from apps.work.push import send\n\n\ndef melden():\n    send(kind='x')\n", "apps/work/melden.py"
    )

    assert kennzahlen.mail_funktionen([dienst, befehl, fremd]) == [
        "apps/work/dienst.py::einladung",
        "apps/work/dienst.py::testmail",
        "apps/work/dienst.py::erinnerung",
        "apps/work/dienst.py::spaeter",
    ]


# --- Listen der Fitnessfunktionen (K3b, K4, K6) -----------------------------------------------------------

MAIL_TEST = "mandari/apps/common/tests/test_mail_dienst.py"
THREAD_TEST = "mandari/apps/events/tests/test_zeitplaene_statt_faden.py"
CASCADE_TEST = "mandari/hub/ris/tests/test_loeschsemantik.py"


def _baum(wurzel: Path, dateien: dict[str, str]) -> Path:
    for pfad, inhalt in dateien.items():
        datei = wurzel / pfad
        datei.parent.mkdir(parents=True, exist_ok=True)
        datei.write_text(inhalt, encoding="utf-8")
    return wurzel


def test_sofort_stellen_aus_der_fitnessfunktion(kennzahlen: ModuleType, tmp_path: Path) -> None:
    inhalt = (
        "from pathlib import Path\n\nSOFORT = {\n"
        '    Path("apps/work/organization/services.py"): 2,  # Testmail\n'
        '    Path("apps/common/admin.py"): 1,\n}\n'
    )
    assert kennzahlen.sofort_stellen(_baum(tmp_path, {MAIL_TEST: inhalt})) == {
        "apps/common/admin.py": 1,
        "apps/work/organization/services.py": 2,
    }
    assert kennzahlen.sofort_stellen(_baum(tmp_path, {MAIL_TEST: "SOFORT = {}\n"})) == {}

    with pytest.raises(ValueError, match="SOFORT nicht gefunden"):
        kennzahlen.sofort_stellen(_baum(tmp_path, {MAIL_TEST: "ANDERE = {}\n"}))


def test_cascade_ausnahmen_aus_der_fitnessfunktion(kennzahlen: ModuleType, tmp_path: Path) -> None:
    inhalt = (
        'ANDERE = frozenset({"x"})\nAUSNAHMEN_CASCADE = frozenset(\n    {\n        "insight_core.Street.body",\n'
        '        "insight_core.Address.body",\n    }\n)\n'
    )
    assert kennzahlen.cascade_ausnahmen(_baum(tmp_path, {CASCADE_TEST: inhalt})) == [
        "insight_core.Address.body",
        "insight_core.Street.body",
    ]
    assert kennzahlen.cascade_ausnahmen(_baum(tmp_path, {CASCADE_TEST: "AUSNAHMEN_CASCADE = frozenset()\n"})) == []

    with pytest.raises(ValueError, match="AUSNAHMEN_CASCADE nicht gefunden"):
        kennzahlen.cascade_ausnahmen(_baum(tmp_path, {CASCADE_TEST: "ANDERE = frozenset()\n"}))


def test_thread_starts_ausserhalb_des_workers(kennzahlen: ModuleType, tmp_path: Path) -> None:
    wurzel = _baum(
        tmp_path, {THREAD_TEST: 'ERLAUBT = (\n    "apps/events/",\n    "apps/common/metrics_server.py",\n)\n'}
    )
    worker = kennzahlen.worker_pfade(wurzel)
    assert worker == ("apps/events/", "apps/common/metrics_server.py")

    quellen = [
        kennzahlen.lies(
            "import threading\n\n\ndef start():\n    threading.Thread(target=print, daemon=True).start()\n",
            "insight_sync/daemon.py",
        ),
        kennzahlen.lies(
            "from threading import Thread, Timer as Wecker\n\n\nclass Waechter(Thread):\n    pass\n\n\n"
            "Wecker(5, print).start()\nThread(target=print)\n",
            "apps/work/hintergrund.py",
        ),
        kennzahlen.lies("import threading\nthreading.Timer(1, print)\n", "apps/work/management/commands/lauf.py"),
        # Der Worker der Ereignistechnik und sein Metrik-Server
        kennzahlen.lies("import threading\nthreading.Thread(target=print)\n", "apps/events/worker.py"),
        kennzahlen.lies("import threading\nthreading.Thread(target=print)\n", "apps/common/metrics_server.py"),
        # Keine Hintergrund-Threads: Pool innerhalb einer Anfrage, Sperren, fremde Klasse gleichen Namens
        kennzahlen.lies(
            "import threading\nfrom concurrent.futures import ThreadPoolExecutor\n"
            "from apps.common.pool import Thread\n\nsperre = threading.Lock()\nThreadPoolExecutor(max_workers=2)\n"
            "Thread()\n",
            "apps/common/health.py",
        ),
    ]

    assert kennzahlen.thread_starts(quellen, worker) == {
        "ausserhalb_des_workers": {
            "insight_sync/daemon.py": 1,
            "apps/work/hintergrund.py": 3,
            "apps/work/management/commands/lauf.py": 1,
        },
        "worker": {"apps/events/worker.py": 1, "apps/common/metrics_server.py": 1},
    }


# --- Erhebung an einem kleinen Baum ----------------------------------------------------------------------

PYPROJECT = """
[tool.importlinter]
root_packages = ["apps", "hub", "insight_core"]

[[tool.importlinter.contracts]]
id = "schichten"
type = "layers"
layers = ["apps.session : apps.work", "hub : insight_core", "apps.events : apps.common : apps.tenants"]
ignore_imports = [
    "**.tests.** -> **",
    "apps.work.dienst -> apps.session.models",
    "apps.common.demo -> apps.work.models",
]

[[tool.importlinter.contracts]]
id = "module-unabhaengig"
type = "independence"
modules = ["apps.session", "apps.work"]
ignore_imports = ["**.tests.** -> **", "apps.work.dienst -> apps.session.models"]

[[tool.importlinter.contracts]]
id = "plattform-fachfrei"
type = "forbidden"
source_modules = ["apps.events", "apps.common", "apps.tenants"]
forbidden_modules = ["apps.session", "apps.work", "hub", "insight_core"]
ignore_imports = ["**.tests.** -> **", "apps.common.demo -> apps.work.models"]
"""

BAUM = {
    "mandari/pyproject.toml": PYPROJECT,
    "mandari/apps/work/dienst.py": (
        "import threading\n\nfrom apps.common import mail\nfrom apps.session.models import SessionPaper\n"
        "from insight_core.models import OParlPaper\n\n\ndef einreichen(org):\n"
        "    SessionPaper.objects.create(name='x')\n"
        "    mail.send(kind='work.eingang', subject='x', body='y', to=[], organization=org, sofort=True)\n\n\n"
        "def lesen():\n    threading.Thread(target=print).start()\n"
        "    return OParlPaper.objects.count() + OParlPaper.objects.filter(deleted=False).count()\n"
    ),
    "mandari/apps/common/demo.py": (
        "from apps.work.models import Notiz\n\n\ndef anlegen():\n    Notiz.objects.create()\n"
    ),
    "mandari/apps/events/worker.py": "import threading\n\n\ndef start():\n    threading.Thread(target=print).start()\n",
    "mandari/hub/ris/abruf.py": (
        "from insight_core.models import OParlFile\n\n\n"
        "def merken():\n    OParlFile.objects.filter(pk=1).update(size=1)\n"
    ),
    "mandari/insight_core/signals.py": (
        "def indexieren(client):\n    client.index(index='papers', id='1', document={})\n"
    ),
    # Listen der Fitnessfunktionen
    MAIL_TEST: 'SOFORT = {\n    Path("apps/work/dienst.py"): 1,\n    Path("apps/common/admin.py"): 2,\n}\n',
    THREAD_TEST: 'ERLAUBT = ("apps/events/", "apps/common/metrics_server.py")\n',
    CASCADE_TEST: (
        'AUSNAHMEN_CASCADE = frozenset({"insight_core.Street.body", "work.Notiz.paper", "insight_core.Address.body"})\n'
    ),
    # Zählen nie mit: Tests, Migrationen, E2E-Tests
    "mandari/apps/work/tests/test_dienst.py": (
        "import threading\n\nfrom apps.common import mail\nfrom apps.session.models import SessionPaper\n\n\n"
        "def test_x(client):\n    SessionPaper.objects.create()\n    threading.Thread(target=print)\n"
        "    client.index(index='a', id='1', document={})\n    mail.send(kind='x', subject='x', body='y', to=[])\n"
    ),
    "mandari/apps/work/migrations/0001_initial.py": (
        "from apps.session.models import SessionPaper\n\nSessionPaper.objects.create()\n"
    ),
    "mandari/tests_e2e/ablauf.py": "from apps.session.models import SessionPaper\n\nSessionPaper.objects.create()\n",
    "ingestor/src/indexing/elasticsearch.py": (
        "async def senden(client):\n    await client.post('/_bulk', content=b'')\n"
        "    await client.post('/_bulk', content=b'')\n"
    ),
    "ingestor/src/tests/test_indexing.py": "async def test_x(client):\n    await client.post('/_bulk')\n",
}

ERWARTET = {
    "K1": 2,
    "K2a": 2,
    "K2b": 3,
    "K3a": 1,
    "K3b": 3,
    "K4": 1,
    "K5": 2,
    "K6a": 1,
    "K6b": 2,
    "K7": 2,
    "K8": None,
}


def test_erhebung_an_einem_kleinen_baum(kennzahlen: ModuleType, tmp_path: Path) -> None:
    ergebnis = {k.nummer: k for k in kennzahlen.erheben(_baum(tmp_path, BAUM))}

    assert {nummer: k.wert for nummer, k in ergebnis.items()} == ERWARTET
    assert ergebnis["K1"].einzelheiten == {
        "je_richtung": {"apps.common -> apps.work": 1, "apps.work -> apps.session": 1},
        "funktionen": {
            "apps/common/demo.py::anlegen": ["apps.work"],
            "apps/work/dienst.py::einreichen": ["apps.session"],
        },
    }
    assert ergebnis["K2a"].einzelheiten == {
        "dateien": {"mandari/insight_core/signals.py": 1, "ingestor/src/indexing/elasticsearch.py": 2}
    }
    assert ergebnis["K3a"].einzelheiten == {"funktionen": ["apps/work/dienst.py::einreichen"]}
    assert ergebnis["K3b"].einzelheiten == {"dateien": {"apps/common/admin.py": 2, "apps/work/dienst.py": 1}}
    assert ergebnis["K4"].einzelheiten == {
        "dateien": {"apps/work/dienst.py": 1},
        "worker": {"apps/events/worker.py": 1},
    }
    assert ergebnis["K6a"].einzelheiten == {"relationen": ["work.Notiz.paper"]}
    # Dieselbe Ausnahme in mehreren Verträgen ist ein Import; die Freistellung der Tests zählt nicht
    assert ergebnis["K7"].einzelheiten == {
        "je_vertrag": {"module-unabhaengig": 1, "plattform-fachfrei": 1, "schichten": 2}
    }


def test_ausgabe_haengt_nur_vom_quellstand_ab(kennzahlen: ModuleType, tmp_path: Path) -> None:
    vorwaerts = kennzahlen.erheben(_baum(tmp_path / "a", BAUM))
    rueckwaerts = kennzahlen.erheben(_baum(tmp_path / "b", dict(reversed(BAUM.items()))))

    assert kennzahlen.als_json(vorwaerts) == kennzahlen.als_json(rueckwaerts)
    assert kennzahlen.als_markdown(vorwaerts) == kennzahlen.als_markdown(rueckwaerts)


def test_tabelle_json_und_verlaufszeile(kennzahlen: ModuleType, tmp_path: Path) -> None:
    ergebnis = kennzahlen.erheben(_baum(tmp_path, BAUM))

    tabelle = kennzahlen.als_markdown(ergebnis).splitlines()
    assert tabelle[:2] == ["| Nr. | Kennzahl | Wert | Ziel | Quelle |", "|---|---|---:|---:|---|"]
    assert tabelle[2] == (
        "| K1 | Funktionen, die Daten eines fremden Moduls schreiben | 2 | 0 | `scripts/architektur_kennzahlen.py` |"
    )
    assert len(tabelle) == 2 + len(ERWARTET)
    # Aus Produktionsmetriken, nicht aus dem Quellstand: genannt mit Herkunft, ohne Wert
    assert tabelle[-1].startswith("| K8 | Ereignis-Rückstand p95 | – | ≤ 5 s | Produktionsmetrik ")
    assert "mandari_events_lag_seconds" in tabelle[-1]

    daten = json.loads(kennzahlen.als_json(ergebnis))["kennzahlen"]
    assert {eintrag["nr"]: eintrag["wert"] for eintrag in daten} == ERWARTET
    assert set(daten[0]) == {"nr", "kennzahl", "wert", "ziel", "quelle", "einzelheiten"}

    assert kennzahlen.verlauf_kopf(ergebnis).splitlines() == [
        "| Datum | Stand | Anlass | K1 | K2a | K2b | K3a | K3b | K4 | K5 | K6a | K6b | K7 | K8 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    assert kennzahlen.verlauf_zeile(ergebnis, "2026-11-10", "`0123abcd` + #1234", "Etappe 2") == (
        "| 2026-11-10 | `0123abcd` + #1234 | Etappe 2 | 2 | 2 | 3 | 1 | 3 | 1 | 2 | 1 | 2 | 2 | – |\n"
    )


class _Lauf:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_verlauf_nennt_den_stand_auf_dev_und_den_pull_request(
    kennzahlen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Commit des Zweigs überlebt den Squash der Merge-Queue nicht; belegt wird, worauf er aufsetzt."""
    antworten = {"merge-base": _Lauf(0, "0123abcd4567ef89\n"), "rev-parse": _Lauf(0, "fedcba9876543210\n")}
    befehle: list[list[str]] = []

    def git(befehl: list[str], **_: Any) -> _Lauf:
        befehle.append(befehl)
        return antworten[befehl[1]]

    monkeypatch.setattr(kennzahlen.subprocess, "run", git)

    assert kennzahlen.basis_commit(WURZEL) == "0123abcd"
    assert befehle == [["git", "merge-base", "HEAD", "origin/dev"]]
    assert kennzahlen.stand_text("0123abcd", 1234) == "`0123abcd` + #1234"
    # Auf dev selbst erhoben (nach dem Merge): nur der Commit
    assert kennzahlen.stand_text("0123abcd", None) == "`0123abcd`"

    # Ohne origin/dev (etwa ein Archiv mit flacher Historie): der Arbeitsstand selbst
    antworten["merge-base"] = _Lauf(128, "")
    assert kennzahlen.basis_commit(WURZEL) == "fedcba98"
    antworten["rev-parse"] = _Lauf(128, "")
    assert kennzahlen.basis_commit(WURZEL) == "unbekannt"

    def ohne_git(befehl: list[str], **_: Any) -> _Lauf:
        raise FileNotFoundError("git")

    monkeypatch.setattr(kennzahlen.subprocess, "run", ohne_git)
    assert kennzahlen.basis_commit(WURZEL) == "unbekannt"


# --- Stand im Repository --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stand(kennzahlen: ModuleType) -> dict[str, Any]:
    return {k.nummer: k for k in kennzahlen.erheben()}


def test_vorhandene_zaehlungen_werden_uebernommen(kennzahlen: ModuleType, stand: dict[str, Any]) -> None:
    ris = sys.modules["check_ris_access_ratchet"]
    schichten = sys.modules["check_import_linter_ratchet"]
    ausnahmen, _ = schichten.exceptions(schichten.load_config(WURZEL / "mandari" / "pyproject.toml"))

    assert stand["K5"].wert == sum(ris.measure(WURZEL / "mandari").values())
    assert stand["K7"].einzelheiten["je_vertrag"] == {vertrag: len(liste) for vertrag, liste in ausnahmen.items()}
    assert stand["K7"].wert == len({eintrag for liste in ausnahmen.values() for eintrag in liste})


def test_listen_der_fitnessfunktionen_werden_uebernommen(kennzahlen: ModuleType, stand: dict[str, Any]) -> None:
    """Die aus dem Syntaxbaum gelesenen Listen sind die, mit denen die Fitnessfunktionen selbst prüfen."""
    from apps.common.tests.test_mail_dienst import SOFORT
    from apps.events.tests.test_zeitplaene_statt_faden import ERLAUBT
    from hub.ris.tests.test_loeschsemantik import AUSNAHMEN_CASCADE

    assert stand["K3b"].einzelheiten["dateien"] == {pfad.as_posix(): anzahl for pfad, anzahl in SOFORT.items()}
    assert stand["K3b"].wert == sum(SOFORT.values())
    assert kennzahlen.worker_pfade(WURZEL) == ERLAUBT
    # Der Worker startet Threads; die Zählung fände sie also auch außerhalb
    assert stand["K4"].einzelheiten["worker"]
    relationen = stand["K6a"].einzelheiten["relationen"] + stand["K6b"].einzelheiten["relationen"]
    assert sorted(relationen) == sorted(AUSNAHMEN_CASCADE)
    assert stand["K6a"].wert + stand["K6b"].wert == len(AUSNAHMEN_CASCADE)


def test_alle_statischen_kennzahlen_haben_einen_wert(stand: dict[str, Any]) -> None:
    assert list(stand) == list(ERWARTET)
    assert stand["K8"].wert is None
    assert all(isinstance(k.wert, int) and k.wert >= 0 for nummer, k in stand.items() if nummer != "K8")


def test_dokumentation_fuehrt_jede_kennzahl(kennzahlen: ModuleType, stand: dict[str, Any]) -> None:
    """Tabelle und Verlauf in ``docs/ARCHITEKTUR_KENNZAHLEN.md`` haben die Zeilen bzw. Spalten des Skripts."""
    text = DOKU.read_text(encoding="utf-8")
    kopf, trenner = kennzahlen.verlauf_kopf(list(stand.values())).splitlines()

    assert f"{kopf}\n{trenner}\n" in text, "Verlaufstabelle: Kopf wie von --verlauf ausgegeben"
    for k in stand.values():
        assert f"| {k.nummer} | {k.name} |" in text, f"{k.nummer} fehlt in der Tabelle der letzten Erhebung"


def test_aufruf_gibt_die_tabelle_aus(
    kennzahlen: ModuleType, stand: dict[str, Any], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert kennzahlen.main([]) == 0
    ausgabe = capsysbinary.readouterr().out.decode("utf-8")
    assert ausgabe.startswith("| Nr. | Kennzahl | Wert | Ziel | Quelle |\n")
    assert ausgabe == kennzahlen.als_markdown(list(stand.values()))
