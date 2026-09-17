# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service-Level-Prüfungen mit E-Mail-Alarm (Issue #231; Command ``check_service_levels``).

Geprüft wird:

- **Speicherplatz** auf dem Medienverzeichnis und dem Wurzeldateisystem (``shutil.disk_usage``)
  gegen ``SERVICE_LEVEL_DISK_MIN_FREE_PERCENT`` und ``SERVICE_LEVEL_DISK_MIN_FREE_GB``.
- **TLS-Zertifikate** der eigenen Domains (``SITE_URL`` und ``MONITOR_TLS_HOSTS``):
  Restlaufzeit unter ``SERVICE_LEVEL_TLS_MIN_DAYS`` oder Handschlag nicht möglich.
- **Fehlerquote 5xx** aus den Metriken der laufenden Instanz (``METRICS_URL``). Die Zähler
  leben im Web-Prozess und beginnen bei jedem Neustart bei null; deshalb merkt sich der Lauf
  den letzten Zählerstand im Cache und bewertet die Differenz seit dem letzten Lauf – bei
  täglichem Cron also die letzten ~24 h. Nach einem Neustart zählt der Stand seit dem Start.
- **Warteschlangenstau**: Django-Tasks laufen hier mit dem ``ImmediateBackend`` (keine
  Warteschlange). Eine echte Warteschlange gibt es nur für die Transkription
  (``minutes.TranscriptionJob``); gemeldet wird, wenn der älteste wartende Auftrag länger als
  ``SERVICE_LEVEL_QUEUE_MAX_AGE_MINUTES`` unbearbeitet ist.

Jeder Alarm geht höchstens einmal je 24 h hinaus (``cache.add`` mit Ablauf); Entwarnungen
werden nicht verschickt, der Bericht (``--report``) zeigt jederzeit den aktuellen Stand.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from prometheus_client.parser import text_string_to_metric_families

logger = logging.getLogger(__name__)

ALARM_SPERRE_SEKUNDEN = 24 * 3600
SNAPSHOT_KEY = "service-level:http-snapshot"
#: Der Zählerstand überlebt ausgelassene Läufe, wird aber nach einigen Tagen bedeutungslos.
SNAPSHOT_TTL = 7 * 24 * 3600


@dataclass(frozen=True)
class Befund:
    key: str  # stabil je Prüfobjekt, Grundlage der 24-h-Sperre
    ok: bool
    titel: str
    detail: str

    @property
    def label(self) -> str:
        return "OK" if self.ok else "ALARM"


# ---------------------------------------------------------------------------
# Speicherplatz
# ---------------------------------------------------------------------------


def _speicherpfade() -> list[tuple[str, Path]]:
    media = Path(settings.MEDIA_ROOT)
    pfade = [("Medienverzeichnis", media)]
    wurzel = Path(media.anchor or "/")
    if wurzel != media:
        pfade.append(("Wurzeldateisystem", wurzel))
    return pfade


def pruefe_speicherplatz(
    pfade: list[tuple[str, Path]] | None = None,
    *,
    min_percent: float | None = None,
    min_gb: float | None = None,
) -> list[Befund]:
    min_percent = settings.SERVICE_LEVEL_DISK_MIN_FREE_PERCENT if min_percent is None else min_percent
    min_gb = settings.SERVICE_LEVEL_DISK_MIN_FREE_GB if min_gb is None else min_gb
    befunde = []
    for name, pfad in pfade or _speicherpfade():
        try:
            nutzung = shutil.disk_usage(pfad)
        except OSError as exc:
            befunde.append(Befund(f"disk:{pfad}", False, f"Speicherplatz {name}", f"nicht messbar: {exc}"))
            continue
        frei_gb = nutzung.free / 1024**3
        frei_prozent = nutzung.free / nutzung.total * 100 if nutzung.total else 0.0
        ok = frei_prozent >= min_percent and frei_gb >= min_gb
        befunde.append(
            Befund(
                f"disk:{pfad}",
                ok,
                f"Speicherplatz {name}",
                f"{frei_gb:.1f} GB frei ({frei_prozent:.1f} %) auf {pfad}; Schwelle {min_percent:g} % / {min_gb:g} GB",
            )
        )
    return befunde


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def tls_hosts() -> list[str]:
    hosts: list[str] = []
    parsed = urlparse(str(settings.SITE_URL))
    if parsed.scheme == "https" and parsed.hostname:
        hosts.append(parsed.hostname)
    for host in getattr(settings, "MONITOR_TLS_HOSTS", []):
        if host not in hosts:
            hosts.append(host)
    return hosts


def zertifikat_restlaufzeit_tage(host: str, port: int = 443, timeout: float = 5.0) -> float:
    """Tage bis zum Ablauf des Zertifikats, das der Host per SNI ausliefert."""
    kontext = ssl.create_default_context()
    # Die Vorgabe ließe noch TLS 1.0/1.1 zu; die eigenen Domains sprechen ohnehin nur TLS 1.2+.
    kontext.minimum_version = ssl.TLSVersion.TLSv1_2
    with (
        socket.create_connection((host, port), timeout=timeout) as sock,
        kontext.wrap_socket(sock, server_hostname=host) as tls,
    ):
        zertifikat = tls.getpeercert()
    if not zertifikat:
        raise ValueError("kein Zertifikat erhalten")
    ablauf = ssl.cert_time_to_seconds(str(zertifikat["notAfter"]))
    return (ablauf - time.time()) / 86400


def pruefe_tls(hosts: list[str] | None = None, *, min_days: int | None = None) -> list[Befund]:
    min_days = settings.SERVICE_LEVEL_TLS_MIN_DAYS if min_days is None else min_days
    befunde = []
    for host in tls_hosts() if hosts is None else hosts:
        try:
            tage = zertifikat_restlaufzeit_tage(host)
        except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
            befunde.append(Befund(f"tls:{host}", False, f"TLS-Zertifikat {host}", f"nicht prüfbar: {exc}"))
            continue
        befunde.append(
            Befund(
                f"tls:{host}",
                tage >= min_days,
                f"TLS-Zertifikat {host}",
                f"läuft in {tage:.0f} Tagen ab; Schwelle {min_days} Tage",
            )
        )
    return befunde


# ---------------------------------------------------------------------------
# Fehlerquote
# ---------------------------------------------------------------------------


def erster_erlaubter_host() -> str:
    """Erster konkreter Eintrag aus ALLOWED_HOSTS (kein Platzhalter, keine Loopback-Adresse)."""
    for host in settings.ALLOWED_HOSTS:
        if host and not host.startswith(("*", ".", "localhost", "127.")):
            return str(host)
    return ""


def lade_metriken(url: str, token: str = "", timeout: float = 5.0, host: str | None = None) -> str:
    anfrage = urllib.request.Request(url)
    # Die Instanz wird über Loopback abgefragt; ohne passenden Host-Header lehnt Django die
    # Anfrage mit 400 ab (ALLOWED_HOSTS). Deshalb den eigenen öffentlichen Host mitschicken.
    host = erster_erlaubter_host() if host is None else host
    if host:
        anfrage.add_header("Host", host)
    if token:
        anfrage.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:  # noqa: S310 – URL aus den Settings
        return str(antwort.read().decode("utf-8"))


def http_zaehler(metriken_text: str) -> tuple[float, float]:
    """(Anfragen gesamt, Antworten 5xx) aus dem Prometheus-Text."""
    anfragen = fehler = 0.0
    for familie in text_string_to_metric_families(metriken_text):
        if familie.name == "mandari_http_requests":
            for sample in familie.samples:
                if sample.name == "mandari_http_requests_total":
                    anfragen += sample.value
                    if sample.labels.get("status_class") == "5xx":
                        fehler += sample.value
    return anfragen, fehler


def pruefe_fehlerquote(
    metriken_text: str | None = None,
    *,
    max_percent: float | None = None,
    min_requests: int | None = None,
) -> list[Befund]:
    max_percent = settings.SERVICE_LEVEL_ERROR_RATE_MAX_PERCENT if max_percent is None else max_percent
    min_requests = settings.SERVICE_LEVEL_ERROR_RATE_MIN_REQUESTS if min_requests is None else min_requests
    if metriken_text is None:
        url = str(getattr(settings, "METRICS_URL", "") or "")
        if not url:
            return []
        try:
            metriken_text = lade_metriken(url, str(getattr(settings, "METRICS_TOKEN", "") or ""))
        except (OSError, ValueError) as exc:
            return [Befund("http:metrics", False, "Fehlerquote 5xx", f"Metriken nicht abrufbar ({url}): {exc}")]

    anfragen, fehler = http_zaehler(metriken_text)
    jetzt = time.time()
    vorher = cache.get(SNAPSHOT_KEY)
    cache.set(SNAPSHOT_KEY, json.dumps({"ts": jetzt, "requests": anfragen, "errors": fehler}), SNAPSHOT_TTL)

    zeitraum = "seit Prozessstart"
    delta_anfragen, delta_fehler = anfragen, fehler
    if vorher:
        alt = json.loads(vorher)
        if anfragen >= float(alt["requests"]):  # sonst Neustart: Zähler beginnen wieder bei null
            delta_anfragen = anfragen - float(alt["requests"])
            delta_fehler = fehler - float(alt["errors"])
            zeitraum = f"seit letztem Lauf vor {(jetzt - float(alt['ts'])) / 3600:.1f} h"

    if delta_anfragen < min_requests:
        return [
            Befund(
                "http:errors",
                True,
                "Fehlerquote 5xx",
                f"{delta_anfragen:.0f} Anfragen {zeitraum} – zu wenige für eine Bewertung (mindestens {min_requests})",
            )
        ]
    quote = delta_fehler / delta_anfragen * 100
    return [
        Befund(
            "http:errors",
            quote <= max_percent,
            "Fehlerquote 5xx",
            f"{quote:.2f} % ({delta_fehler:.0f} von {delta_anfragen:.0f} Anfragen {zeitraum}); Schwelle {max_percent:g} %",
        )
    ]


# ---------------------------------------------------------------------------
# Warteschlange
# ---------------------------------------------------------------------------


def pruefe_warteschlange(*, max_age_minutes: int | None = None) -> list[Befund]:
    max_age_minutes = settings.SERVICE_LEVEL_QUEUE_MAX_AGE_MINUTES if max_age_minutes is None else max_age_minutes
    try:
        from apps.minutes.models import JobStatus, TranscriptionJob

        wartend = TranscriptionJob.objects.filter(status=JobStatus.QUEUED)
        anzahl = wartend.count()
        aeltester = wartend.order_by("queued_at").values_list("queued_at", flat=True).first()
    except Exception as exc:  # noqa: BLE001 – ohne Tabelle (Migration ausstehend) gibt es keine Warteschlange
        logger.debug("Warteschlange nicht abfragbar: %s", exc)
        return []
    if aeltester is None:
        return [Befund("queue:transcription", True, "Warteschlange Transkription", "keine wartenden Aufträge")]
    alter = timezone.now() - aeltester
    return [
        Befund(
            "queue:transcription",
            alter <= timedelta(minutes=max_age_minutes),
            "Warteschlange Transkription",
            f"{anzahl} wartend, ältester Auftrag seit {alter.total_seconds() / 60:.0f} min; Schwelle {max_age_minutes} min",
        )
    ]


# ---------------------------------------------------------------------------
# Gesamtlauf und Alarm
# ---------------------------------------------------------------------------


def alle_pruefungen() -> list[Befund]:
    return [*pruefe_speicherplatz(), *pruefe_tls(), *pruefe_fehlerquote(), *pruefe_warteschlange()]


def faellige_alarme(befunde: list[Befund], *, dry_run: bool = False) -> list[Befund]:
    """Alarme, die noch nicht in den letzten 24 h gemeldet wurden; setzt die Sperre (außer bei dry_run)."""
    faellig = []
    for befund in befunde:
        if befund.ok:
            continue
        schluessel = f"service-level-alert:{befund.key}"
        if dry_run:
            if cache.get(schluessel) is None:
                faellig.append(befund)
        elif cache.add(schluessel, timezone.now().isoformat(), ALARM_SPERRE_SEKUNDEN):
            faellig.append(befund)
    return faellig


def alarm_text(alarme: list[Befund], befunde: list[Befund]) -> str:
    zeilen = ["Folgende Service-Level-Schwellen sind unterschritten:", ""]
    zeilen += [f"- {b.titel}: {b.detail}" for b in alarme]
    zeilen += ["", "Alle Prüfungen dieses Laufs:", ""]
    zeilen += [f"- [{b.label}] {b.titel}: {b.detail}" for b in befunde]
    zeilen += ["", f"Instanz: {settings.SITE_URL}", "Dieser Alarm wird frühestens nach 24 Stunden wiederholt."]
    return "\n".join(zeilen) + "\n"


def sende_alarme(befunde: list[Befund], *, dry_run: bool = False) -> list[Befund]:
    """Verschickt eine Sammelmail für alle fälligen Alarme; Rückgabe: die gemeldeten Befunde."""
    from apps.common.email import send_email
    from insight_core.services.source_health import get_alert_emails

    alarme = faellige_alarme(befunde, dry_run=dry_run)
    if not alarme or dry_run:
        return alarme
    empfaenger = get_alert_emails()
    if not empfaenger:
        logger.warning("Service-Level-Alarm ohne Empfänger (INSIGHT_ALERT_EMAILS leer): %s", [b.key for b in alarme])
        return alarme
    send_email(
        subject=f"[mandari] Service-Level-Alarm: {len(alarme)} Befund(e)",
        body=alarm_text(alarme, befunde),
        to=empfaenger,
        fail_silently=True,
    )
    return alarme
