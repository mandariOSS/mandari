# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-Ausgaben gegen einen externen Validator prüfen.

    python scripts/oparl_validator.py --validator /pfad/zu/oparl-validator-rs

Baut eine frische Instanz mit den Demo-Daten (``setup_demo_praesentation``) in einer temporären
SQLite-Datenbank, startet sie im eigenen Prozess auf ``127.0.0.1`` und prüft beide OParl-Ausgaben:

- den Aggregator unter ``/oparl/v1/system`` (der gespiegelte Demo-Mandant wird dafür gelistet) und
- die Session-Schnittstelle des Demo-Mandanten unter ``/session/<slug>/api/oparl/``.

Je Ausgabe laufen zwei Prüfungen:

1. **Externer Validator** ``oparl-validator-rs`` (https://github.com/konstin/oparl-validator-rs, MIT oder
   Apache-2.0): Pflichtfelder, Feldtypen, leere Texte, externe Listen mit Blättern und die Abrufbarkeit
   verlinkter Objekte. Er wird nur ausgeführt, nicht mitgeliefert. Ohne ``--validator`` entfällt er mit
   einem Hinweis (Exit-Code 0); die CI übergibt ihn immer.
2. **Eigene Typprüfung** (``mandari/oparl_api/tests/konformitaet.py``): Datums- und Zeitformate,
   ``organizationType``, unbekannte Eigenschaften, gelöschte Objekte – über alle externen Listen.

Die Instanz läuft über HTTP auf dem eigenen Rechner. Den Hinweis des Validators auf „unsicheres HTTP“
werten wir deshalb nicht als Fehler – er dient als Zählung der besuchten Objekte: Sieht der Validator
weniger als ``MINDEST_OBJEKTE``, hat er die Ausgabe nicht wirklich durchlaufen, und der Lauf scheitert.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT / "mandari"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: So viele Objekte muss der Validator je Ausgabe mindestens besucht haben
MINDEST_OBJEKTE = 20
#: Befund, der nur an der Testinstanz liegt (HTTP auf dem eigenen Rechner)
NUR_TESTINSTANZ = "Das unsichere HTTP wird verwendet"
LIZENZ = "https://www.govdata.de/dl-de/zero-2-0"

ZUSAMMENFASSUNG = "=== Validierungsreport: Zusammenfassung ==="
EINZELFAELLE = "=== Detailreport: Alle Einzelfälle ==="
BEFUND = re.compile(r"^(?P<text>.*) \((?P<zahl>\d+) Fälle\)\. Beispiel: ")

#: Externe Listen je Ausgabe: Feld am Body -> Objekttyp
LISTEN = {
    "organization": "Organization",
    "person": "Person",
    "meeting": "Meeting",
    "paper": "Paper",
    "membership": "Membership",
    "agendaItem": "AgendaItem",
    "consultation": "Consultation",
    "file": "File",
    "locationList": "Location",
    "legislativeTermList": "LegislativeTerm",
}


def freier_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def umgebung(tmp: Path, port: int) -> dict[str, str]:
    """Umgebung der Testinstanz (vor ``django.setup()`` gesetzt)."""
    return {
        "DJANGO_SETTINGS_MODULE": "mandari.settings",
        "DEBUG": "true",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": f"sqlite:///{(tmp / 'oparl.sqlite3').as_posix()}",
        "ENCRYPTION_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "SECRET_KEY": "nur-oparl-validator-" + secrets.token_urlsafe(40),
        "ELASTICSEARCH_AUTO_INDEX": "False",
        "MANDARI_SYNC_WATCHDOG": "0",
        "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
        "SITE_URL": f"http://127.0.0.1:{port}",
        "OPARL_API_RATE_LIMIT": "0",
        "OPARL_LICENSE_URL": LIZENZ,
    }


def daten_anlegen(tmp: Path) -> str:
    """Demo-Daten anlegen; liefert den Slug des Demo-Mandanten."""
    import django

    django.setup()

    from django.conf import settings

    settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30
    settings.MEDIA_ROOT = str(tmp / "media")

    from _smoke_db import prepare_database

    prepare_database(PROJECT_DIR)

    from apps.common.management.commands.setup_demo_environment import DEMO_SESSION_SLUG
    from apps.session.models import SessionTenant
    from apps.session.services import oparl_access
    from apps.session.services.insight_service import oparl_system_url
    from django.core.management import call_command
    from django.db import connections
    from insight_core.models import OParlBody

    # Die Ausgabe des Befehls nennt ein erzeugtes Passwort – nicht ins Protokoll schreiben
    call_command("setup_demo_praesentation", stdout=io.StringIO(), stderr=io.StringIO())
    tenant = SessionTenant.objects.get(slug=DEMO_SESSION_SLUG)
    oparl_access.set_license(tenant, LIZENZ)
    # Der Spiegel des Demo-Mandanten ist ungelistet; der Aggregator listet ihn hier, damit der
    # Validator über /oparl/v1/bodies zu den Daten findet.
    gelistet = OParlBody.objects.filter(source__url=oparl_system_url(tenant)).update(is_listed=True)
    if gelistet != 1:
        raise SystemExit(f"Erwartet: genau eine gespiegelte Kommune des Demo-Mandanten, gefunden: {gelistet}")
    connections.close_all()
    return DEMO_SESSION_SLUG


def abruf(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=60) as antwort:  # noqa: S310 – eigene Testinstanz
        return json.loads(antwort.read().decode("utf-8"))


def server_starten(port: int) -> Any:
    """Die Anwendung im eigenen Prozess ausliefern (gleiche Datenbank und Einstellungen wie die Demo-Daten)."""
    from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler
    from django.core.wsgi import get_wsgi_application

    class StillerHandler(WSGIRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 – Signatur der Basisklasse
            return

    server = ThreadedWSGIServer(("127.0.0.1", port), StillerHandler)
    server.set_app(get_wsgi_application())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def eigene_pruefung(system_url: str) -> tuple[int, list[str]]:
    """Alle externen Listen ablaufen und jedes Objekt gegen die Feldtypen prüfen."""
    from oparl_api.tests.konformitaet import pruefe, pruefe_liste

    system = abruf(system_url)
    probleme = pruefe(system, "System")
    bodies = abruf(system["body"])
    probleme += pruefe_liste(bodies, "Body")
    objekte = 1 + len(bodies["data"])
    for body in bodies["data"]:
        for feld, typ in LISTEN.items():
            url = body.get(feld)
            while url:
                seite = abruf(url)
                probleme += pruefe_liste(seite, typ)
                objekte += len(seite.get("data", []))
                url = seite.get("links", {}).get("next")
    return objekte, probleme


def externer_validator(validator: str, system_url: str, bericht: Path) -> tuple[int, list[str]]:
    """Validator laufen lassen; liefert die Zahl besuchter Objekte und die Befunde."""
    lauf = subprocess.run(  # noqa: S603 – vom Aufrufer übergebenes Prüfwerkzeug
        [validator, "--quiet", "--report", str(bericht), system_url],
        cwd=bericht.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )
    if lauf.returncode != 0 or not bericht.is_file():
        return 0, [f"Der Validator ist abgebrochen (Exit-Code {lauf.returncode}): {lauf.stderr.strip()[-2000:]}"]
    text = bericht.read_text(encoding="utf-8", errors="replace")
    if ZUSAMMENFASSUNG not in text:
        return 0, ["Der Bericht des Validators hat keine Zusammenfassung."]
    zusammenfassung = text.split(ZUSAMMENFASSUNG, 1)[1].split(EINZELFAELLE, 1)[0]
    besucht = 0
    befunde: list[str] = []
    for zeile in filter(None, (z.strip() for z in zusammenfassung.splitlines())):
        treffer = BEFUND.match(zeile)
        if treffer and treffer["text"] == NUR_TESTINSTANZ:
            besucht = int(treffer["zahl"])
        else:
            befunde.append(zeile)
    if besucht < MINDEST_OBJEKTE:
        befunde.append(f"Der Validator hat nur {besucht} Objekte besucht (erwartet mindestens {MINDEST_OBJEKTE}).")
    return besucht, befunde


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--validator", help="Pfad zum Programm oparl-validator-rs")
    args = parser.parse_args()
    if args.validator and not Path(args.validator).is_file():
        print(f"Validator nicht gefunden: {args.validator}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mandari_oparl_validator_"))
    port = freier_port()
    env = umgebung(tmp, port)
    os.environ.update(env)
    slug = daten_anlegen(tmp)
    basis = f"http://127.0.0.1:{port}"
    ausgaben = {
        "Aggregator": f"{basis}/oparl/v1/system",
        "Session-Schnittstelle": f"{basis}/session/{slug}/api/oparl/",
    }

    fehler = 0
    server = server_starten(port)
    try:
        for nummer, (name, system_url) in enumerate(ausgaben.items()):
            print(f"== {name}: {system_url}")
            objekte, probleme = eigene_pruefung(system_url)
            print(f"   eigene Typprüfung: {objekte} Objekte, {len(probleme)} Abweichungen")
            if args.validator:
                besucht, befunde = externer_validator(args.validator, system_url, tmp / f"bericht-{nummer}.txt")
                print(f"   oparl-validator-rs: {besucht} Objekte, {len(befunde)} Befunde")
                probleme += befunde
            if objekte < MINDEST_OBJEKTE:
                probleme.append(f"Nur {objekte} Objekte in den Listen (erwartet mindestens {MINDEST_OBJEKTE}).")
            for problem in probleme[:50]:
                print(f"   - {problem}")
            if len(probleme) > 50:
                print(f"   … und {len(probleme) - 50} weitere")
            fehler += len(probleme)
    finally:
        server.shutdown()
        server.server_close()

    if not args.validator:
        print("Hinweis: ohne --validator lief nur die eigene Typprüfung.")
    if fehler:
        print(f"\nOParl-Prüfung fehlgeschlagen: {fehler} Abweichungen.")
        return 1
    print("\nOParl-Prüfung bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
