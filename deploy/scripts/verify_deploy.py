# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gesundheitsprüfung nach dem Deploy, läuft IM App-Container über ``manage.py shell``
(Issue #285). Prüft nicht nur Statuscodes, sondern dass die Anwendung Seiten mit
Inhalt ausliefert: Ein Container kann „healthy“ sein und trotzdem jede Seite mit
500 beantworten oder ein leeres Layout rendern.

Aufruf (durch deploy/scripts/deploy.sh):
    docker cp verify_deploy.py <app>:/tmp/verify_deploy.py
    docker exec <app> python manage.py shell -c "exec(open('/tmp/verify_deploy.py').read())"

Steuerung über Umgebungsvariablen (im Container gesetzt oder per ``docker exec -e``):
    VERIFY_HOST        Host-Header für den Test-Client (Standard: erster ALLOWED_HOSTS-Eintrag)
    VERIFY_PATHS       anonyme Pfade, kommagetrennt (Standard: Login, Bürgerportal, OParl-System, Readiness)
    VERIFY_DEMO_PAGES  angemeldete Prüfungen: ``mail=/pfad,/pfad;mail2=/pfad`` (Konten müssen existieren)
    VERIFY_MIN_BYTES   Mindestlänge einer HTML-Antwort (Standard 800)

Exit-Code 1 bei jedem Fehlschlag; die Ausgabe nennt jede Prüfung mit Ergebnis.
"""

from __future__ import annotations

import os
import sys

from django.conf import settings
from django.test import Client

STANDARD_PFADE = "/accounts/login/,/insight/,/oparl/v1/system,/health/ready/"
MIN_BYTES = int(os.environ.get("VERIFY_MIN_BYTES", "800"))
FEHLERMARKER = ("Server Error (500)", "Internal Server Error", "TemplateDoesNotExist")


def host() -> str:
    gesetzt = os.environ.get("VERIFY_HOST", "").strip()
    if gesetzt:
        return gesetzt
    for eintrag in settings.ALLOWED_HOSTS:
        if eintrag and not eintrag.startswith(("*", ".", "localhost", "127.")):
            return str(eintrag)
    return "localhost"


def pruefe(client: Client, pfad: str, *, erwartet: int = 200) -> tuple[bool, str]:
    antwort = client.get(pfad, secure=True, follow=False)
    status = antwort.status_code
    inhalt = antwort.content if hasattr(antwort, "content") else b""
    if status != erwartet:
        return False, f"Status {status}, erwartet {erwartet}"
    if pfad.endswith("/health/ready/"):
        return True, "bereit"
    text = inhalt.decode("utf-8", "replace")
    for marker in FEHLERMARKER:
        if marker in text:
            return False, f"Fehlermarker „{marker}“ im Inhalt"
    if "html" in antwort.get("Content-Type", "") and len(inhalt) < MIN_BYTES:
        return False, f"nur {len(inhalt)} Bytes Inhalt"
    return True, f"{len(inhalt)} Bytes"


def main() -> int:
    fehler = 0
    h = host()
    client = Client(HTTP_HOST=h)
    print(f"Prüfe als {h}")

    for pfad in [p.strip() for p in os.environ.get("VERIFY_PATHS", STANDARD_PFADE).split(",") if p.strip()]:
        ok, detail = pruefe(client, pfad)
        fehler += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {pfad}: {detail}")

    demo = os.environ.get("VERIFY_DEMO_PAGES", "").strip()
    if demo:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        for block in demo.split(";"):
            if "=" not in block:
                continue
            mail, pfade = block.split("=", 1)
            user = User.objects.filter(email=mail.strip()).first()
            if user is None:
                fehler += 1
                print(f"  FAIL Konto fehlt: {mail.strip()}")
                continue
            angemeldet = Client(HTTP_HOST=h)
            angemeldet.force_login(user)
            for pfad in [p.strip() for p in pfade.split(",") if p.strip()]:
                ok, detail = pruefe(angemeldet, pfad)
                fehler += 0 if ok else 1
                print(f"  {'OK  ' if ok else 'FAIL'} {pfad} (als {mail.strip().split('@')[0]}): {detail}")

    print("ERGEBNIS: " + ("alle Prüfungen bestanden" if fehler == 0 else f"{fehler} Prüfung(en) fehlgeschlagen"))
    return 1 if fehler else 0


if __name__ == "__main__" or True:  # unter manage.py shell -c exec() ist __name__ nicht "__main__"
    sys.exit(main())
