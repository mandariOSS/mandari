# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfungen der Dienste, die jede Installation braucht (Issue #964): Datenbank, Cache, Mailversand, Aufträge und
Worker. Alle lesen nur: kein Schreibzugriff auf die Datenbank, kein Wert im Cache, keine Mail.

Die Prüfung des Objektspeichers steht bei der Dokumentablage (``insight_core/doctor.py``).
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import replace
from typing import Final

from django.conf import settings
from django.core.cache import cache
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.db import connection
from django.utils.module_loading import import_string

from apps.common.mail.config import SmtpConfig, platform_config
from apps.common.mail_backends import SMTP_BACKEND

from .kern import Befund, Kontext, fehler, ok, pruefung, uebersprungen, warnung

#: Cache-Backends, die nur im Prozess leben: Es gibt keinen Dienst, den man erreichen könnte
_CACHE_IM_PROZESS: Final = ("LocMemCache", "DummyCache")
#: Schlüssel der Leseprobe im Cache; er wird nie geschrieben
CACHE_PROBE: Final = "doctor:leseprobe"


@pruefung("datenbank", "Datenbank", rang=20)
def pruefe_datenbank(kontext: Kontext) -> Befund:
    hinweis = (
        "Läuft der Dienst postgres (docker compose ps)? DATABASE_URL setzt docker-compose.yml aus POSTGRES_USER, "
        "POSTGRES_PASSWORD und POSTGRES_DB."
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - der Text der Ausnahme kann die Verbindungsdaten enthalten
        return fehler(f"nicht erreichbar ({type(exc).__name__})", hinweis)
    return ok(f"{connection.display_name} erreichbar")


@pruefung("cache", "Cache", rang=30)
def pruefe_cache(kontext: Kontext) -> Befund:
    klasse = str(settings.CACHES["default"]["BACKEND"]).rsplit(".", 1)[-1]
    if klasse in _CACHE_IM_PROZESS:
        return uebersprungen(
            f"kein Redis eingestellt ({klasse}, Speicher je Prozess)",
            "Im Betrieb setzt docker-compose.yml REDIS_URL; ohne DEBUG nutzt die Anwendung dann Redis.",
        )
    try:
        # Nur lesen: Ein unerreichbarer Dienst scheitert schon hier
        cache.get(CACHE_PROBE)
    except Exception as exc:  # noqa: BLE001 - der Text der Ausnahme kann die Adresse samt Kennwort enthalten
        return fehler(
            f"nicht erreichbar ({type(exc).__name__})",
            "Läuft der Dienst redis (docker compose ps)? REDIS_URL setzt docker-compose.yml aus REDIS_PASSWORD.",
        )
    return ok(f"{klasse} erreichbar")


# ---------------------------------------------------------------------------------------------- Mailversand


def _plattform_zugang() -> tuple[SmtpConfig, str]:
    """Zugang der Plattform und woher er kommt, ohne die Zeile der Systemeinstellungen anzulegen."""
    from apps.common.models import SiteSettings

    site = SiteSettings.objects.filter(pk=1).first() or SiteSettings()
    quelle = "Systemeinstellungen" if site.email_host else "Umgebung (EMAIL_*)"
    return platform_config(site), quelle


def _smtp_fehler(exc: BaseException, zugang: SmtpConfig) -> Befund:
    """Fester Text je Art des Fehlers; die Antwort des Servers und der Text der Ausnahme bleiben draußen."""
    ziel = f"{zugang.host}:{zugang.port}"
    wo = "Zugang: Systemeinstellungen im Admin, sonst EMAIL_* in der .env."
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return fehler(
            f"{ziel}: Anmeldung abgelehnt (SMTP-Code {int(exc.smtp_code)})",
            f"Benutzername und Passwort prüfen. {wo}",
        )
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return fehler(
            f"{ziel}: Der Server bietet STARTTLS oder die Anmeldung nicht an",
            f"Port und Verschlüsselung prüfen (587 mit STARTTLS, 465 mit SSL). {wo}",
        )
    if isinstance(exc, ssl.SSLError):
        return fehler(
            f"{ziel}: TLS-Aufbau gescheitert ({type(exc).__name__})",
            f"Port und Verschlüsselung passen nicht zusammen (587 mit STARTTLS, 465 mit SSL) oder das Zertifikat "
            f"des Servers ist ungültig. {wo}",
        )
    if isinstance(exc, TimeoutError):
        return fehler(f"{ziel}: keine Antwort innerhalb der Zeitgrenze", f"Host, Port und Firewall prüfen. {wo}")
    if isinstance(exc, smtplib.SMTPException):
        return fehler(f"{ziel}: SMTP-Fehler ({type(exc).__name__})", wo)
    return fehler(f"{ziel}: Verbindung gescheitert ({type(exc).__name__})", f"Host, Port und Firewall prüfen. {wo}")


@pruefung("smtp", "Mailversand (SMTP)", rang=40)
def pruefe_smtp(kontext: Kontext) -> Befund:
    """Verbindung, EHLO, Verschlüsselung und Anmeldung beim Mailserver der Plattform; es geht keine Mail hinaus."""
    if getattr(settings, "DEMO_INSTANCE", False):
        return uebersprungen("Demo-Instanz: Es verlässt keine Mail das System")
    try:
        zugang, quelle = _plattform_zugang()
    except Exception as exc:  # noqa: BLE001 - etwa fehlende Tabelle vor der ersten Migration
        return fehler(
            f"Systemeinstellungen nicht lesbar ({type(exc).__name__})",
            "Datenbank erreichbar und migriert? Siehe Prüfung „Datenbank“.",
        )
    klasse = import_string(zugang.backend)
    if not issubclass(klasse, SMTPEmailBackend):
        return uebersprungen(
            f"Versandweg ohne Mailserver ({klasse.__module__.rsplit('.', 1)[-1]})",
            "Mit DEBUG oder EMAIL_BACKEND gehen Mails nicht an einen Mailserver.",
        )
    if not zugang.host:
        return uebersprungen(
            "kein Mailserver eingestellt: mandari verschickt keine Mails",
            "Einladungen, Passwort-Links und Benachrichtigungen brauchen einen Mailserver: Systemeinstellungen im "
            "Admin oder EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER und EMAIL_HOST_PASSWORD in der .env.",
        )
    if zugang.use_tls and zugang.use_ssl:
        return fehler(
            "STARTTLS und SSL sind beide eingeschaltet",
            "Nur eines wählen: STARTTLS (EMAIL_USE_TLS, Port 587) oder SSL (EMAIL_USE_SSL, Port 465).",
        )
    # Plain SMTP statt des Backends der Systemeinstellungen: Das läse die Einstellungen erneut und legte ihre
    # Zeile an. Die Zeitgrenze des Laufs gilt auch für die Verbindung selbst.
    zeitgrenze = max(1, min(int(zugang.timeout), int(kontext.zeitgrenze)))
    verbindung = replace(zugang, backend=SMTP_BACKEND, timeout=zeitgrenze).connect()
    if not isinstance(verbindung, SMTPEmailBackend):
        return uebersprungen("Versandweg ohne Mailserver")
    try:
        verbindung.open()
    except Exception as exc:  # noqa: BLE001 - jede Art von Fehler ist ein Befund
        return _smtp_fehler(exc, zugang)
    finally:
        verbindung.fail_silently = True  # QUIT darf scheitern, der Befund steht
        verbindung.close()
    verschluesselung = "SSL" if zugang.use_ssl else ("STARTTLS" if zugang.use_tls else "unverschlüsselt")
    ziel = f"{zugang.host}:{zugang.port} ({verschluesselung}), Zugang aus: {quelle}"
    if not (zugang.username and zugang.password):
        if zugang.username or zugang.password:
            return warnung(
                f"{ziel}: erreichbar, aber ohne Anmeldung, weil Benutzername oder Passwort fehlt",
                "Beide Angaben setzen; ohne Anmeldung lehnen die meisten Server den Versand ab.",
            )
        return ok(f"{ziel}: erreichbar, ohne Anmeldung (kein Benutzername eingestellt)")
    if not zugang.use_tls and not zugang.use_ssl:
        return warnung(
            f"{ziel}: Anmeldung angenommen, aber die Zugangsdaten gehen unverschlüsselt über das Netz",
            "STARTTLS (Port 587) oder SSL (Port 465) einschalten.",
        )
    return ok(f"{ziel}: Anmeldung angenommen, keine Mail versendet")


# ---------------------------------------------------------------------------------------------- Aufträge und Worker


@pruefung("aufgaben", "Aufgaben-Backend", rang=60)
def pruefe_aufgaben(kontext: Kontext) -> Befund:
    """Lädt das Backend der Aufträge und liest, falls es das Journal ist, dessen Tabelle."""
    from django.tasks import task_backends

    from apps.events.models import Task
    from apps.events.tasks_backend import JournalBackend

    try:
        backend = task_backends["default"]
    except Exception as exc:  # noqa: BLE001 - falscher Importpfad in TASKS_BACKEND
        return fehler(
            f"Backend nicht ladbar ({type(exc).__name__})",
            "TASKS_BACKEND ist immediate, journal oder der Importpfad eines Backends.",
        )
    if isinstance(backend, JournalBackend):
        try:
            Task.objects.exists()
        except Exception as exc:  # noqa: BLE001
            return fehler(
                f"Journal nicht lesbar ({type(exc).__name__})",
                "Datenbank erreichbar und migriert? Siehe Prüfung „Datenbank“.",
            )
        return ok("journal: Aufträge laufen im Worker")
    if getattr(settings, "MAIL_QUEUE", None):
        return warnung(
            "MAIL_QUEUE ist gesetzt, wirkt aber nur mit TASKS_BACKEND=journal; Mails gehen weiter sofort hinaus",
            "TASKS_BACKEND=journal setzen, sobald der Worker läuft (DEPLOYMENT.md, „Mailversand als Auftrag“), "
            "oder MAIL_QUEUE leeren.",
        )
    return ok(f"{type(backend).__name__}: Aufträge laufen sofort in der Anfrage")


@pruefung("worker", "Worker", rang=61)
def pruefe_worker(kontext: Kontext) -> Befund:
    """Bedienen laufende Worker alle Rollen und Warteschlangen, die die Installation braucht?"""
    from apps.events.presence import required_roles, worker_status

    bedarf = required_roles()
    if not bedarf:
        return uebersprungen(
            "kein Worker verlangt (EVENTS_WORKER_REQUIRED=false)",
            "Ohne Worker laufen keine Zeitpläne (Erinnerungen, Einladungen, Aufräumen).",
        )
    try:
        status = worker_status(required=bedarf)
    except Exception as exc:  # noqa: BLE001
        return fehler(
            f"Lebenszeichen der Worker nicht lesbar ({type(exc).__name__})",
            "Datenbank erreichbar und migriert? Siehe Prüfung „Datenbank“.",
        )
    if status.degraded:
        return fehler(
            f"kein Worker für {status.missing_summary()}",
            "Dienste prüfen: docker compose ps worker worker-heavy worker-live; Protokoll: docker compose logs "
            "worker (DEPLOYMENT.md, „Worker“).",
        )
    return ok(f"{len(status.workers)} Worker, Rollen: {', '.join(sorted(status.required))}")
