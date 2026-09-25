# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Performance-Budgets für Kernseiten (Issue #228).

Misst je Kernseite die Zahl der Datenbankabfragen und die Antwortzeit mit dem
Django-Test-Client gegen die Lasttest-Daten des Profils ``klein``
(``manage.py generate_load_data``). Die Budgets stehen in
``scripts/performance_budgets.json``:

- **Abfragen** sind hart: mehr Abfragen als im Budget lassen den Lauf scheitern.
  Ein Budget darf nur sinken (Ratchet wie beim Service-Layer-Gate).
- **Zeit** ist eine Warnung: CI-Läufer schwanken um den Faktor zwei bis drei,
  deshalb ist die Obergrenze großzügig und blockiert nicht.

Gemessen wird mit leerem Cache („kalt“) — so zählt jede Seite ihre vollen
Abfragen, unabhängig davon, welche Seite vorher lief. Die Zeit ist das Minimum
aus drei Läufen, weil das Minimum am wenigsten vom Rauschen des Läufers abhängt.

    python scripts/check_performance_budgets.py            # prüfen
    python scripts/check_performance_budgets.py --update   # Abfrage-Budgets nachziehen (nur nach unten)
    python scripts/check_performance_budgets.py --json     # Messwerte ausgeben

Das Modul lässt sich ohne Django importieren; ``bewerten`` und ``budgets_nachziehen``
sind reine Funktionen und werden in ``apps/common/tests/test_performance_budgets.py``
geprüft. Django wird erst in ``main`` eingerichtet (Muster der Smoke-Skripte).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT / "mandari"
BUDGETS = ROOT / "scripts" / "performance_budgets.json"
WIEDERHOLUNGEN = 3
#: Neue Zeitbudgets: das Fünffache des gemessenen Werts, mindestens eine halbe Sekunde
ZEIT_FAKTOR = 5
ZEIT_MINDESTENS_MS = 500
SESSION_SLUG = "last-klein-stadt"
ORG_SLUG = "last-klein-stadt-fraktion"
#: Mandantengruppe mit Leitstelle über dem Lasttest-Mandanten (Issue #317), angelegt in _kontext()
LEITSTELLE_SLUG = "last-klein-leitstelle"


@dataclass(frozen=True)
class Messwert:
    abfragen: int
    ms: float
    status: int


@dataclass(frozen=True)
class Ergebnis:
    verletzungen: list[str]
    warnungen: list[str]
    verbesserungen: list[str]


def bewerten(messungen: dict[str, Messwert], budgets: dict[str, dict[str, Any]]) -> Ergebnis:
    """Messwerte gegen Budgets stellen: Abfragen hart, Zeit als Warnung, Verbesserungen als Hinweis."""
    verletzungen: list[str] = []
    warnungen: list[str] = []
    verbesserungen: list[str] = []
    for name, wert in messungen.items():
        if wert.status != 200:
            verletzungen.append(f"{name}: HTTP {wert.status} statt 200")
            continue
        budget = budgets.get(name)
        if budget is None:
            verletzungen.append(f"{name}: kein Budget hinterlegt — mit --update anlegen")
            continue
        if wert.abfragen > budget["abfragen"]:
            verletzungen.append(f"{name}: {wert.abfragen} Abfragen, Budget {budget['abfragen']}")
        elif wert.abfragen < budget["abfragen"]:
            verbesserungen.append(f"{name}: {wert.abfragen} Abfragen, Budget {budget['abfragen']} — nachziehen")
        if wert.ms > budget["ms"]:
            warnungen.append(f"{name}: {wert.ms:.0f} ms, Budget {budget['ms']} ms")
    return Ergebnis(verletzungen, warnungen, verbesserungen)


def budgets_nachziehen(
    messungen: dict[str, Messwert], budgets: dict[str, dict[str, Any]], beschreibungen: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Neue Budgets: Abfragen nur nach unten, Zeit nur für neue Seiten (großzügig)."""
    neu: dict[str, dict[str, Any]] = {}
    for name, wert in messungen.items():
        alt = budgets.get(name)
        if alt is None:
            ms = max(ZEIT_MINDESTENS_MS, -(-int(wert.ms * ZEIT_FAKTOR) // 100) * 100)
            neu[name] = {"beschreibung": beschreibungen[name], "abfragen": wert.abfragen, "ms": ms}
        else:
            neu[name] = {
                "beschreibung": beschreibungen.get(name, alt.get("beschreibung", "")),
                "abfragen": min(alt["abfragen"], wert.abfragen),
                "ms": alt["ms"],
            }
    return neu


def budgets_lesen() -> dict[str, dict[str, Any]]:
    if not BUDGETS.exists():
        return {}
    daten: dict[str, dict[str, Any]] = json.loads(BUDGETS.read_text(encoding="utf-8"))
    return daten


# =============================================================================
# Messung (braucht Django)
# =============================================================================


def _django_einrichten() -> None:
    sys.path.insert(0, str(PROJECT_DIR))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    db_path = Path(tempfile.mkdtemp(prefix="mandari_budget_")) / "budget.sqlite3"
    os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
    os.environ["DEBUG"] = "true"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.setdefault("ENCRYPTION_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
    os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
    os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
    os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
    os.environ["REDIS_URL"] = ""
    # Ohne Elasticsearch fällt die Suche auf die Datenbank zurück. Mit leerer URL geschieht das sofort;
    # mit einer unerreichbaren Adresse erst nach den Wiederholungsversuchen des Clients (über eine Minute).
    os.environ["ELASTICSEARCH_URL"] = ""

    import django

    django.setup()

    from django.conf import settings as dj_settings
    from django.test.utils import setup_test_environment

    dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
    setup_test_environment()

    # Schema aus der Smoke-Vorlage kopieren statt zu migrieren (scripts/_smoke_db.py)
    from _smoke_db import prepare_database

    prepare_database(PROJECT_DIR)


@dataclass(frozen=True)
class Seite:
    name: str
    beschreibung: str
    url: Callable[[dict[str, Any]], str]
    konto: str | None  # E-Mail des angemeldeten Kontos, None = anonym


def _seiten() -> list[Seite]:
    from apps.session.management.commands.generate_load_data import DOMAENE

    sachbearbeitung = f"last-klein-stadt-sachbearbeitung-1@{DOMAENE}"
    fraktion = f"last-klein-stadt-fraktion-1@{DOMAENE}"
    return [
        Seite("insight_startseite", "Insight: Startseite der Kommune", lambda k: "/insight/", None),
        Seite(
            "insight_einstieg_koerperschaft",
            "Insight: Einstieg einer Körperschaft (/insight/k/<slug>/, Issue #317)",
            lambda k: f"/insight/k/{SESSION_SLUG}/",
            None,
        ),
        Seite("insight_suche", "Insight: Suchseite mit Suchbegriff", lambda k: "/insight/suche/?q=Radweg", None),
        Seite(
            "insight_suche_ergebnisse",
            "Insight: Suchergebnisse (HTMX-Partial, ohne Elasticsearch: Datenbank-Rückfall)",
            lambda k: "/insight/suche/partials/results/?q=Radweg",
            None,
        ),
        Seite("insight_termine", "Insight: Sitzungsliste", lambda k: "/insight/termine/", None),
        Seite("insight_vorgaenge", "Insight: Vorgangsliste", lambda k: "/insight/vorgaenge/", None),
        Seite(
            "session_sitzungsliste",
            "Session: Sitzungsliste (Seite 1 von 20)",
            lambda k: f"/session/{SESSION_SLUG}/meetings/",
            sachbearbeitung,
        ),
        Seite(
            "session_sitzungsdetail",
            "Session: Sitzungsdetail mit Tagesordnung",
            lambda k: f"/session/{SESSION_SLUG}/meetings/{k['meeting_id']}/",
            sachbearbeitung,
        ),
        Seite(
            "session_vorlagenliste",
            "Session: Vorlagenliste",
            lambda k: f"/session/{SESSION_SLUG}/papers/",
            sachbearbeitung,
        ),
        Seite(
            "session_abstimmung",
            "Session: Abstimmungserfassung der laufenden Sitzung",
            lambda k: f"/session/{SESSION_SLUG}/agenda/{k['live_item_id']}/voting/",
            sachbearbeitung,
        ),
        Seite(
            "session_leitstelle",
            "Session: Leitstellen-Übersicht einer Mandantengruppe",
            lambda k: f"/session/leitstelle/{LEITSTELLE_SLUG}/",
            sachbearbeitung,
        ),
        Seite("work_dashboard", "Work: Dashboard", lambda k: f"/work/{ORG_SLUG}/", fraktion),
        Seite("work_dokumentliste", "Work: Dokumentliste", lambda k: f"/work/{ORG_SLUG}/documents/", fraktion),
        Seite(
            "work_editor",
            "Work: Editor-Seite eines Antrags",
            lambda k: f"/work/{ORG_SLUG}/documents/{k['motion_id']}/",
            fraktion,
        ),
    ]


def _leitstelle_anlegen() -> None:
    """Mandantengruppe über dem Lasttest-Mandanten; die Sachbearbeitung ist Mitglied der Leitstelle (Issue #317)."""
    from apps.accounts.models import User
    from apps.session.management.commands.generate_load_data import DOMAENE
    from apps.session.models import SessionTenant, SessionTenantGroup, SessionTenantGroupMembership

    gruppe, _ = SessionTenantGroup.objects.get_or_create(slug=LEITSTELLE_SLUG, defaults={"name": "Lasttest-Leitstelle"})
    gruppe.tenant_links.get_or_create(tenant=SessionTenant.objects.get(slug=SESSION_SLUG))
    nutzer = User.objects.get(email=f"last-klein-stadt-sachbearbeitung-1@{DOMAENE}")
    SessionTenantGroupMembership.objects.get_or_create(group=gruppe, user=nutzer)


def _kontext() -> dict[str, Any]:
    from apps.session.models import SessionAgendaItem, SessionMeeting
    from apps.work.models import Motion
    from insight_core.models import OParlBody

    _leitstelle_anlegen()
    live = SessionMeeting.objects.get(tenant__slug=SESSION_SLUG, meeting_state="in_progress")
    return {
        "body_id": OParlBody.objects.get(slug=SESSION_SLUG).id,
        "meeting_id": SessionMeeting.objects.filter(tenant__slug=SESSION_SLUG, meeting_state="completed")
        .order_by("-start")
        .values_list("id", flat=True)
        .first(),
        "live_item_id": SessionAgendaItem.objects.filter(meeting=live, voting_method="roll_call")
        .order_by("order")
        .values_list("id", flat=True)
        .first(),
        "motion_id": Motion.objects.filter(organization__slug=ORG_SLUG)
        .order_by("title")
        .values_list("id", flat=True)
        .first(),
    }


def _client(konto: str | None, body_id: Any) -> Any:
    from django.test import Client

    from apps.accounts.models import User

    client = Client()
    if konto:
        client.force_login(User.objects.get(email=konto))
    # Aktive Kommune wählen, sonst zeigt Insight die Auswahlseite
    client.get(f"/insight/kommune/{body_id}/")
    return client


def messen(seiten: list[Seite], kontext: dict[str, Any]) -> dict[str, Messwert]:
    from django.core.cache import cache
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    ergebnis: dict[str, Messwert] = {}
    for seite in seiten:
        client = _client(seite.konto, kontext["body_id"])
        url = seite.url(kontext)
        client.get(url)  # Aufwärmen: Template-Loader, Import-Nebenkosten
        abfragen = 0
        beste_ms = float("inf")
        status = 0
        for _ in range(WIEDERHOLUNGEN):
            cache.clear()
            with CaptureQueriesContext(connection) as erfasst:
                start = time.perf_counter()
                antwort = client.get(url)
                dauer = (time.perf_counter() - start) * 1000
            status = antwort.status_code
            abfragen = len(erfasst)
            beste_ms = min(beste_ms, dauer)
        ergebnis[seite.name] = Messwert(abfragen=abfragen, ms=beste_ms, status=status)
    return ergebnis


def main(argv: list[str]) -> int:
    _django_einrichten()

    from django.core.management import call_command
    from django.test import override_settings

    medien = Path(tempfile.mkdtemp(prefix="mandari_budget_media_"))
    with override_settings(MEDIA_ROOT=str(medien)):
        call_command("generate_load_data", profile="klein", seed=228, verbosity=0)
        seiten = _seiten()
        messungen = messen(seiten, _kontext())

    if "--json" in argv:
        print(json.dumps({n: m.__dict__ for n, m in messungen.items()}, indent=2, ensure_ascii=False))
        return 0

    beschreibungen = {s.name: s.beschreibung for s in seiten}
    budgets = budgets_lesen()
    print(f"{'Seite':<28}{'Abfragen':>9}{'Budget':>8}{'ms':>8}{'Budget':>8}")
    for name, wert in messungen.items():
        budget = budgets.get(name, {})
        print(
            f"{name:<28}{wert.abfragen:>9}{budget.get('abfragen', '-'):>8}{wert.ms:>8.0f}{budget.get('ms', '-'):>8}"
            f"  {'' if wert.status == 200 else f'HTTP {wert.status}'}"
        )

    if "--update" in argv or not BUDGETS.exists():
        fehler = [n for n, m in messungen.items() if m.status != 200]
        if fehler:
            print(f"\nNicht geschrieben, Seiten antworten nicht mit 200: {', '.join(fehler)}")
            return 1
        neu = budgets_nachziehen(messungen, budgets, beschreibungen)
        BUDGETS.write_text(json.dumps(neu, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nBudgets geschrieben: {BUDGETS.relative_to(ROOT).as_posix()}")
        return 0

    ergebnis = bewerten(messungen, budgets)
    for zeile in ergebnis.warnungen:
        print(f"WARNUNG Zeit: {zeile}")
    for zeile in ergebnis.verbesserungen:
        print(f"Hinweis: {zeile}")
    if ergebnis.verletzungen:
        print("\nPerformance-Budget verletzt:")
        for zeile in ergebnis.verletzungen:
            print(f"  - {zeile}")
        return 1
    print("\nOK: alle Kernseiten innerhalb der Abfrage-Budgets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
