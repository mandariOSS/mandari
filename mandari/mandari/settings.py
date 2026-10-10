# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Django settings for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

import os
import uuid
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file from project root
from dotenv import load_dotenv

env_path = BASE_DIR.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
_INSECURE_SECRET_KEY = "django-insecure-change-me-in-production-with-a-real-secret-key"
SECRET_KEY = os.environ.get("SECRET_KEY", "") or _INSECURE_SECRET_KEY

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

# Der eingebaute Rückfallschlüssel ist öffentlich und taugt nur für die Entwicklung (DEBUG).
if not DEBUG and SECRET_KEY == _INSECURE_SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SECRET_KEY muss ohne DEBUG gesetzt sein (Umgebungsvariable SECRET_KEY).")

# Site URL for emails and external links
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000")

# Parse domain from SITE_URL
from urllib.parse import urlparse

_site_domain = urlparse(SITE_URL).netloc

# Allowed hosts from environment (filter empty strings)
_allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]

# Ensure we always have localhost for health checks + domain from SITE_URL
if "localhost" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("localhost")
if _site_domain and _site_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_site_domain)
# Add wildcard subdomain support (e.g., *.mandari.de)
# Django uses leading dot for subdomain matching. Nötig für die Weiterleitung von
# Organisations-Subdomains (volt.mandari.de -> /work/volt/). Vertrauen für Formulare und
# WebSockets folgt daraus NICHT (siehe CSRF_TRUSTED_ORIGINS und apps/common/websocket_origin.py).
_wildcard_domain = f".{_site_domain.replace('www.', '')}"
if _wildcard_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_wildcard_domain)

# Vertrauenswürdige Ursprünge für Formulare (CSRF): nur SITE_URL und ausdrücklich genannte
# Ursprünge aus der Umgebung, kommagetrennt mit Schema (z. B. https://ratsinfo.stadt.example).
# Subdomains gelten nicht pauschal als vertrauenswürdig, denn dort können andere Anwendungen oder
# Inhalte liegen (z. B. eine Demo-Instanz). Anfragen an den eigenen Host (Organisations-Subdomains,
# PORTAL_HOSTS) prüft Django ohnehin gegen den Host der Anfrage; dafür ist kein Eintrag nötig.
_csrf_env = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(",") if o.strip()]

# Ensure SITE_URL is always in CSRF trusted origins
if SITE_URL and SITE_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)

# Subdomain redirect settings (e.g., volt.mandari.de -> /work/volt/)
# Extract main domain from SITE_URL (e.g., 'mandari.de')
MAIN_DOMAIN = os.environ.get("MAIN_DOMAIN", _site_domain.replace("www.", ""))
SUBDOMAIN_REDIRECT_ENABLED = os.environ.get("SUBDOMAIN_REDIRECT_ENABLED", "true").lower() == "true"

# Bürgerportal je Körperschaft unter eigenem Host (Issue #317): "host=slug,host2=slug2". Der Slug ist
# der Einstiegs-Slug (/insight/k/<slug>/). Wirkt nur für Hosts, die auch in ALLOWED_HOSTS stehen
# (insight_core/portal.py); DNS, Zertifikat und Reverse Proxy richtet der Betrieb ein.
PORTAL_HOSTS: dict[str, str] = {
    host.strip().lower(): slug.strip()
    for host, _, slug in (eintrag.partition("=") for eintrag in os.environ.get("PORTAL_HOSTS", "").split(","))
    if host.strip() and slug.strip()
}


# Application definition

INSTALLED_APPS = [
    # ASGI Server (must be first for Channels)
    "daphne",
    # Unfold Admin Theme (muss vor django.contrib.admin stehen!)
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    # Django Core
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third-party apps
    "django_htmx",
    "django_cotton",  # Komponentenbibliothek (templates/cotton/)
    "django_vite",  # Frontend-Assets über Vite-Manifest (static/dist/)
    "ninja",  # Session-API v1: OpenAPI + Swagger UI (lokale Assets, kein CDN)
    "django_safemigrate",
    # Mandari Insight apps (OSS)
    "insight_core",
    "insight_sync",
    "insight_search",
    "insight_ai",
    # Mandari Work apps (OSS - AGPL-3.0-or-later)
    "apps.common",
    "apps.accounts",
    "apps.tenants",
    # Ereignistechnik der Datendrehscheibe: Journal, Folgenummer, Abonnements, Aufträge (docs/adr/20260929-*)
    "apps.events",
    # Datendrehscheibe: Vertragsregister für Ereignisse und Befehle, ohne Modelle (docs/adr/20260929-ereignisvertraege.md)
    "hub.contracts",
    # Datendrehscheibe: Sichten aus Ereignissen, vorerst die Schatten-Quelle des RIS-Projektors (Issue #536)
    "hub.projections",
    # Datendrehscheibe: Live-Übertragungen von Gremiensitzungen (Issue #915, docs/LIVE_UEBERTRAGUNG.md)
    "hub.live",
    # Datendrehscheibe: RIS-Bestand, ohne Modelle – Abruf der Dateien (Issue #919), Befehl dokumentkette
    "hub.ris",
    "apps.provisioning",
    "apps.work",
    # Mandari Session RIS (OSS - AGPL-3.0-or-later)
    "apps.session",
    # Protokollierung (Aufzeichnung, Transkription, KI-Entwurf) — Session + Work
    "apps.minutes",
]

# Custom User Model
AUTH_USER_MODEL = "accounts.User"

# ASGI Application (for Django Channels WebSocket support)
ASGI_APPLICATION = "mandari.asgi.application"

# Öffentliche Demo-Instanz (Issue #99, apps/common/demo.py): eigene Installation mit eigener
# Datenbank und nur synthetischen Daten. Schaltet Mailversand und KI ab, sperrt Änderungen an
# Passwort, zweitem Faktor und Konto und blendet einen Hinweis ein. In Produktion nie setzen.
DEMO_INSTANCE = os.environ.get("DEMO_INSTANCE", "false").lower() in ("1", "true", "yes")

MIDDLEWARE = [
    # Vor allem anderen: gibt die Datenbankverbindung am Ende jeder Anfrage im Thread der
    # View zurück, auch wenn der Client aufgelegt und asgiref die Aufgabe abgebrochen hat.
    # Ohne sie läuft der Pool nach abgebrochenen Anfragen leer (Issue #344).
    "apps.common.db_connections.ReleaseDatabaseConnectionsMiddleware",
    # Direkt danach, damit die gemessene Antwortzeit den gesamten Middleware-Stapel umfasst (apps/common/metrics.py)
    "apps.common.metrics.RequestMetricsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Content-Security-Policy (Django 6): zunächst Report-Only, siehe Block SECURE_CSP_REPORT_ONLY
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # Request-Kennung (X-Request-ID) für Logs und Fehlerkorrelation, siehe apps/common/observability.py
    "apps.common.observability.RequestIdMiddleware",
    # Database error handler - shows maintenance page on DB connection issues
    "apps.common.middleware.DatabaseErrorMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Öffentliche Medien (Logos, Personenfotos) vor Sitzung und Anmeldung ausliefern: ohne Datenbank
    # und ohne Cookie. Sonst schrieb jedes Bild die Sitzung zurück (SESSION_SAVE_EVERY_REQUEST) und
    # belegte eine Pool-Verbindung; Dutzende Fotos einer Seite scheiterten mit 400 (Issue #667).
    "mandari.media.PublicMediaMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Eigener Host je Körperschaft (PORTAL_HOSTS, Issue #317); ohne Einträge wirkungslos
    "insight_core.portal.PortalHostMiddleware",
    # Subdomain redirect for organization shortcuts (e.g., volt.mandari.de -> /work/volt/)
    "apps.tenants.middleware.SubdomainRedirectMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Wartungsmodus aus den Systemeinstellungen (Issue #588): 503 für Besucher, Admin bleibt erreichbar
    "apps.common.maintenance.MaintenanceModeMiddleware",
    # Nur in der Demo-Instanz aktiv: sperrt Konto-Sicherheitsänderungen (Issue #99)
    "apps.common.demo.DemoInstanceMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Admin-Ansichten im Dialog (Bezugsobjekte) für dieselbe Herkunft einbettbar; muss nach der
    # XFrameOptionsMiddleware stehen, damit deren Voreinstellung den Kopf nicht vorher setzt (#686)
    "apps.common.admin_dialogs.AdminDialogFrameMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    # Django-Admin nur aus freigegebenen Netzen (ADMIN_ALLOWED_NETWORKS)
    "apps.accounts.middleware.AdminNetworkMiddleware",
    # 2FA-Pflicht auch für bereits angemeldete Sitzungen durchsetzen
    "apps.accounts.middleware.TwoFactorEnforcementMiddleware",
    # Work module organization context
    "apps.tenants.middleware.OrganizationMiddleware",
    # Session RIS tenant context + Audit-Log-Attribution (nur /session/-Pfade)
    "apps.session.middleware.SessionTenantMiddleware",
    # Veröffentlichung im Bürgerportal beendet (Issue #618): Hinweis, 503 oder 410 je Kommune
    "insight_core.publication.PublicationStateMiddleware",
]

# Wartungsmodus (apps/common/maintenance.py, Issue #588): Der Schalter in den Systemeinstellungen
# wirkt nur, wenn die Durchsetzung an ist. Die Tests schalten sie ab (jede Anfrage läse sonst die
# Einstellungen aus der Datenbank); die Durchsetzung testet apps/common/tests/test_wartungsmodus.py.
MAINTENANCE_MODE_ENFORCEMENT = True

# Zugangsschutz: Pflicht zum zweiten Faktor für Admins sowie je Organisation/Mandant
# (apps/accounts/two_factor_policy.py). Standard: in Produktion aktiv, bei DEBUG aus.
TWO_FACTOR_ENFORCEMENT = os.environ.get("TWO_FACTOR_ENFORCEMENT", "false" if DEBUG else "true").lower() in (
    "1",
    "true",
    "yes",
)
# Gemeinsam genutzte Demo-Zugänge ohne echte Daten sind von der Pflicht ausgenommen
TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = [
    d.strip().lower().lstrip("@")
    for d in os.environ.get("TWO_FACTOR_EXEMPT_EMAIL_DOMAINS", "demo.mandari.de").split(",")
    if d.strip()
]
# Django-Admin nur aus diesen Netzen (CIDR, kommagetrennt; leer = keine Beschränkung).
# Voraussetzung: Der vorgelagerte Proxy ersetzt X-Forwarded-For durch die echte Client-Adresse.
import ipaddress as _ipaddress  # noqa: E402

ADMIN_ALLOWED_NETWORKS = [
    str(_ipaddress.ip_network(network.strip(), strict=False))
    for network in os.environ.get("ADMIN_ALLOWED_NETWORKS", "").split(",")
    if network.strip()
]
# Metriken-Endpunkt /metrics/ (Issue #231): nur aus diesen Netzen oder mit Bearer-Token erreichbar, sonst 404.
# Vorgabe: Loopback und private Netze (Docker-Netz, VPN); ein Prometheus im selben Compose-Netz braucht kein Token.
METRICS_ALLOWED_NETWORKS = [
    str(_ipaddress.ip_network(network.strip(), strict=False))
    for network in os.environ.get(
        "METRICS_ALLOWED_NETWORKS", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    ).split(",")
    if network.strip()
]
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")
# Woher check_service_levels die Metriken der laufenden Instanz holt (leer = Fehlerquote nicht prüfen).
# Der Abruf setzt den Host-Header auf den ersten Eintrag aus ALLOWED_HOSTS, damit Django die
# Loopback-Adresse nicht mit 400 abweist.
METRICS_URL = os.environ.get("METRICS_URL", "http://127.0.0.1:8000/metrics/")
# Statusseite (Gatus) für den monatlichen Verfügbarkeitsbericht (availability_report)
GATUS_URL = os.environ.get("GATUS_URL", "")

# Service-Level-Alarme (check_service_levels, Issue #231); Empfänger: INSIGHT_ALERT_EMAILS
SERVICE_LEVEL_DISK_MIN_FREE_PERCENT = float(os.environ.get("SERVICE_LEVEL_DISK_MIN_FREE_PERCENT", "10"))
SERVICE_LEVEL_DISK_MIN_FREE_GB = float(os.environ.get("SERVICE_LEVEL_DISK_MIN_FREE_GB", "2"))
SERVICE_LEVEL_TLS_MIN_DAYS = int(os.environ.get("SERVICE_LEVEL_TLS_MIN_DAYS", "14"))
SERVICE_LEVEL_ERROR_RATE_MAX_PERCENT = float(os.environ.get("SERVICE_LEVEL_ERROR_RATE_MAX_PERCENT", "1"))
SERVICE_LEVEL_ERROR_RATE_MIN_REQUESTS = int(os.environ.get("SERVICE_LEVEL_ERROR_RATE_MIN_REQUESTS", "100"))
SERVICE_LEVEL_QUEUE_MAX_AGE_MINUTES = int(os.environ.get("SERVICE_LEVEL_QUEUE_MAX_AGE_MINUTES", "120"))
# Zusätzliche Hosts, deren TLS-Zertifikat geprüft wird (kommagetrennt); SITE_URL wird immer geprüft
MONITOR_TLS_HOSTS = [h.strip() for h in os.environ.get("MONITOR_TLS_HOSTS", "").split(",") if h.strip()]
# Statusseite (Gatus) für den Verfügbarkeitsbericht (availability_report)
GATUS_URL = os.environ.get("GATUS_URL", "")
# Sicherheitsschlüssel/Passkeys (WebAuthn): Relying-Party-ID ist die Hauptdomain (gilt auch für Subdomains)
WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", MAIN_DOMAIN.split(":")[0])
WEBAUTHN_RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "mandari")
# Plattform-Administration nur mit Sicherheitsschlüssel – erst einschalten, wenn Schlüssel ausgegeben sind
TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS = os.environ.get(
    "TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS", "false"
).lower() in ("1", "true", "yes")

ROOT_URLCONF = "mandari.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.csp",  # csp_nonce für Inline-Skripte (#172)
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "insight_core.context_processors.navigation_context",
                "insight_core.context_processors.active_body",
                "apps.common.demo.demo_context",  # Hinweis in der Demo-Instanz (Issue #99)
                "apps.work.rahmen.rahmen_kontext",  # neues Erscheinungsbild von Work je Organisation (Issue #852)
            ],
        },
    },
]

WSGI_APPLICATION = "mandari.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mandari")

# Remove +asyncpg suffix if present (from FastAPI config)
DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

# Parse DATABASE_URL
import dj_database_url

# conn_max_age: Unter ASGI/Channels hält jeder Worker-Thread eine persistente
# Verbindung — bei 600s summiert sich das schnell auf Postgres' max_connections
# („FATAL: sorry, too many clients already"). 60s behält den Wiederverwendungs-
# Vorteil, begrenzt aber die Ansammlung. Per Env übersteuerbar.
DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE", "60")),
        conn_health_checks=True,
    )
}

# Verbindungspool (Issue #257)
#
# Unter ASGI bearbeitet je ein Thread eine Anfrage und behält seine Verbindung
# danach noch ``CONN_MAX_AGE`` Sekunden. Bei einer Anfragespitze — ein
# Schwachstellen-Scanner genügt — wächst die Zahl der Verbindungen deshalb
# schneller, als sie zurückgegeben werden, bis ``max_connections`` erreicht ist.
# Dann bekommt *jeder* Dienst an derselben Datenbank "sorry, too many clients
# already", auch einer ganz ohne Last.
#
# Der Pool deckelt die Zahl nach oben, statt sie mit der Last wachsen zu lassen.
# Django verlangt dafür ``CONN_MAX_AGE = 0``: Die Wiederverwendung übernimmt der
# Pool, nicht mehr Django. Nur für PostgreSQL — SQLite kennt weder das Problem
# noch die Option.
#
# Wichtig (Issue #344): Unter ASGI bekommt jede Anfrage ihren *eigenen* Thread,
# asgiref legt je Anfrage einen Executor mit einem Thread an. Die Zahl
# gleichzeitiger Threads ist also nicht begrenzt; eine Anfragespitze stellt
# beliebig viele Threads vor die ``max_size`` Verbindungen. Deshalb begrenzt
# ``max_waiting`` die Warteschlange: Was darüber hinausgeht, bekommt sofort 503,
# statt Threads zu stapeln, deren Clients ohnehin aufgeben. Dass abgebrochene
# Anfragen ihre Verbindung zurückgeben, regelt
# ``apps.common.db_connections.ReleaseDatabaseConnectionsMiddleware``.
DB_POOL_ENABLED = os.environ.get("DB_POOL", "true").lower() in ("true", "1", "yes")

if DB_POOL_ENABLED and DATABASES["default"]["ENGINE"].endswith("postgresql"):
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"].setdefault("OPTIONS", {})["pool"] = {
        # Vorgehalten, damit die erste Anfrage nicht auf den Verbindungsaufbau wartet.
        "min_size": int(os.environ.get("DB_POOL_MIN", "2")),
        # Obergrenze gleichzeitig benutzter Verbindungen je Prozess.
        "max_size": int(os.environ.get("DB_POOL_MAX", "10")),
        # Höchstens so viele Anfragen dürfen auf eine Verbindung warten; jede weitere
        # scheitert sofort mit 503 (TooManyRequests). 0 hieße unbegrenzt.
        "max_waiting": int(os.environ.get("DB_POOL_MAX_WAITING", "20")),
        # So lange wartet eine Anfrage höchstens auf eine Verbindung. Kurz genug, dass
        # ein Besucher eine Fehlerseite sieht statt eines hängenden Browsers.
        "timeout": float(os.environ.get("DB_POOL_TIMEOUT", "5")),
        "max_lifetime": float(os.environ.get("DB_POOL_MAX_LIFETIME", "1800")),
    }

# Ereignistechnik (apps.events, Issue #505): Direktverbindung zu PostgreSQL für den Weckruf per
# LISTEN. Nötig, wenn DATABASE_URL auf PgBouncer im Transaktionsmodus zeigt; dort kommen
# Meldungen nicht an. Leer: die Verbindungsdaten von DATABASE_URL (genügt ohne Pooler). Ohne
# funktionierenden Weckruf fragen Sequenzierer und Zustellung nur regelmäßig ab (höchstens
# wenige Sekunden später).
EVENTS_DB_DIRECT_URL = os.environ.get("EVENTS_DB_DIRECT_URL", "")

# Ereignistechnik (apps.events.publish, Issue #502): Jedes veröffentlichte Ereignis gegen das
# Vertragsregister prüfen (Hülle, Typ und Version, Sichtbarkeit, Nutzlast). Standard wie DEBUG; die
# Tests schalten die Prüfung ein (settings_test.py). Im Betrieb bleibt sie aus: Dort gelten nur die
# Formatprüfungen der Hülle, die Verträge sichern die Tests der Produzenten.
EVENTS_VALIDATE_CONTRACTS = os.environ.get("EVENTS_VALIDATE_CONTRACTS", str(DEBUG)).lower() in ("true", "1", "yes")

# DSGVO (apps.events.datenschutz, Issue #511): Nach publish(..., operation="redact") die Nutzlast
# personenbezogener Journaleinträge zu den Personen leeren, die das Ereignis nennt (Objekt vom Typ User oder
# Personenfeld laut Vertrag; die Kennung bleibt). Der Auftrag entsteht in derselben Transaktion, immer in
# events_task, und läuft im Worker, auch ohne TASKS_BACKEND=journal. Standard aus; von Hand:
# manage.py events_neutralize --person <uuid> [--dry-run].
EVENTS_REDACT_NEUTRALIZE = os.environ.get("EVENTS_REDACT_NEUTRALIZE", "false").strip().lower() in ("1", "true", "yes")

# Befehle (hub.commands, Issue #539): So viele Tage bleibt ein Idempotenzschlüssel samt Quittung
# gespeichert. Bis dahin erhält eine Wiederholung mit demselben Schlüssel dieselbe Quittung, danach
# gilt der Schlüssel als neu. Aufgeräumt wird täglich per Zeitplan (apps/events/schedules.py).
EVENTS_IDEMPOTENCY_RETENTION_DAYS = int(os.environ.get("EVENTS_IDEMPOTENCY_RETENTION_DAYS", "30"))

# Aufbewahrung der Ereignistechnik (apps.events.aufbewahrung, manage.py events_purge, Issue #511): Journal in
# Tagen (Spezifikation: mindestens 90, kleinere Werte lehnt events_purge ab; nie kürzer als
# OPARL_CHANGES_RETENTION_DAYS, nie über den kleinsten Cursor eines Abonnements), beendete Aufträge: erledigte
# und tote bzw. endgültig fehlgeschlagene. Das Journal räumt der Zeitplan befehl:events_purge nur mit
# EVENTS_JOURNAL_PURGE_ENABLED auf (Standard aus); Aufträge räumt der Zeitplan auftraege_aufraeumen immer auf.
EVENTS_JOURNAL_PURGE_ENABLED = os.environ.get("EVENTS_JOURNAL_PURGE_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
EVENTS_JOURNAL_RETENTION_DAYS = int(os.environ.get("EVENTS_JOURNAL_RETENTION_DAYS", "90"))
EVENTS_TASKS_DONE_RETENTION_DAYS = int(os.environ.get("EVENTS_TASKS_DONE_RETENTION_DAYS", "14"))
EVENTS_TASKS_DEAD_RETENTION_DAYS = int(os.environ.get("EVENTS_TASKS_DEAD_RETENTION_DAYS", "90"))

# Worker (manage.py events_worker, Issues #509, #515): Braucht diese Installation einen laufenden
# Worker? Dann melden /health/ und /health/ready/ "degraded" und der Admin einen Hinweis, solange
# keiner die nötigen Rollen bedient (apps.events.presence). "true": alle Rollen; "false": nie (etwa
# eine Vorführinstanz ohne Worker; ihre Zeitpläne laufen dann nicht); leer (Standard): immer tasks
# und scheduler, weil die wiederkehrende Arbeit als Zeitpläne im Worker läuft, dazu sequencer, wenn
# der Ingestor Ereignisse schreibt (INGESTOR_EVENTS_ENABLED).
EVENTS_WORKER_REQUIRED = os.environ.get("EVENTS_WORKER_REQUIRED", "").strip().lower()
if EVENTS_WORKER_REQUIRED not in ("", "auto", "true", "false", "1", "0", "yes", "no"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("EVENTS_WORKER_REQUIRED muss true, false oder leer sein.")
# Schalter des Ingestors (ingestor/src/config.py) für die Ereignisse zum RIS-Bestand. Die Anwendung liest
# ihn, um zu erkennen, dass der Sequenzierer laufen muss (EVENTS_WORKER_REQUIRED), und meldet damit selbst
# Rücknahmen, die mandari Session sofort im Bestand markiert (hub.ris.retraction, Issue #707).
INGESTOR_EVENTS_ENABLED = os.environ.get("INGESTOR_EVENTS_ENABLED", "false").strip().lower() in (
    "true",
    "1",
    "yes",
    "on",
)

# „Worker lebt“ an die Statusseite (Issue #574, apps/events/push.py): externer Endpunkt von Gatus,
# z. B. https://status.example/api/v1/endpoints/betrieb_worker/external; leer = keine Meldung.
# Bleibt die Meldung aus, alarmiert die Statusseite. Gemeldet wird alle WORKER_PUSH_INTERVAL Sekunden.
WORKER_PUSH_URL = os.environ.get("WORKER_PUSH_URL", "").strip()
WORKER_PUSH_TOKEN = os.environ.get("WORKER_PUSH_TOKEN", "")
WORKER_PUSH_INTERVAL = float(os.environ.get("WORKER_PUSH_INTERVAL", "60"))


# Ereignisse aus mandari Session an die Datendrehscheibe (apps.session.hub_events, Issues #533–#535): Sitzungen,
# Tagesordnung, Ladung, Vorlagen, Beratungsfolge, Anlagen, Abstimmungen, Beschlüsse und Niederschriften als
# Ereignisse ris.* im Journal, in derselben Transaktion wie die Änderung.
# "aus" (Standard): nichts. "schatten": Ereignisse werden geschrieben und erreichen Feed und Abonnenten wie
# bei "aktiv"; scheitert das Schreiben, bleibt die Änderung bestehen und der Fehler steht im Protokoll
# (Parallelbetrieb neben den bisherigen Wegen). "aktiv": Änderung und Ereignis sind atomar. Je Mandant
# überschreibbar (SessionTenant.hub_events, Admin). Braucht den Sequenzierer im Worker; nicht "aus" verlangt
# ihn (EVENTS_WORKER_REQUIRED).
SESSION_EVENTS = os.environ.get("SESSION_EVENTS", "aus").strip().lower() or "aus"
if SESSION_EVENTS not in ("aus", "schatten", "aktiv"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SESSION_EVENTS muss aus, schatten oder aktiv sein.")

# Cache - use Redis if available, fallback to local memory
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Check if Redis is configured
if REDIS_URL and not DEBUG:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
    # cached_db statt cache: Redis läuft mit allkeys-lru und darf Session-Keys
    # evicten — reine Cache-Sessions führen dann zu zufälligen Logouts (Issue #5).
    # Die DB bleibt die Quelle der Wahrheit, der Cache beschleunigt nur.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"
else:
    # Use local memory cache for development
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Django Channels — WebSocket layer
# Uses Redis if available, falls back to in-memory for development
# Lese-Timeout bewusst über der blockierenden Wartezeit von channels_redis (#216),
# siehe mandari/redis_config.py.
from mandari.redis_config import build_channel_layers

CHANNEL_LAYERS = build_channel_layers(REDIS_URL)


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

# Passwort-Validierung: siehe Block unter „Session settings“ (eine einzige Definition)


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "de-de"

TIME_ZONE = "Europe/Berlin"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Vite (frontend/ → static/dist/, siehe vite.config.ts).
# Entwicklung: `npm run dev` + DJANGO_VITE_DEV_MODE=1 (HMR), sonst `npm run build`.
DJANGO_VITE = {
    "default": {
        "dev_mode": os.environ.get("DJANGO_VITE_DEV_MODE", "").lower() in ("1", "true", "yes"),
        "dev_server_port": 5173,
        "manifest_path": BASE_DIR / "static" / "dist" / "manifest.json",
        "static_url_prefix": "dist",
    }
}

# WhiteNoise for static files (only in production)
# Django 6.0: Using STORAGES instead of deprecated STATICFILES_STORAGE
if not DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            # Manifest-Storage von WhiteNoise; Vite-Dateien behalten ihren Vite-Namen (mandari/static_files.py)
            "BACKEND": "mandari.static_files.ManifestStaticFilesStorage",
        },
    }

from mandari.static_files import immutable_file_test

# Gehashte Dateien dauerhaft cachen: die des Manifest-Storage und zusätzlich die von Vite unter dist/assets/.
WHITENOISE_IMMUTABLE_FILE_TEST = immutable_file_test


# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Lokaler Dokument-Cache für OParl-Dateien (Issue #87): je Kommune ein Verzeichnis,
# damit sich pro Stadt eine Storage Box mounten lässt. Schutzgrenze verhindert volle Platte.
OPARL_FILES_ROOT = Path(os.environ.get("OPARL_FILES_ROOT", str(MEDIA_ROOT / "oparl_files")))
FILE_CACHE_MAX_MB = int(os.environ.get("FILE_CACHE_MAX_MB", "80"))
FILE_CACHE_MIN_FREE_GB = int(os.environ.get("FILE_CACHE_MIN_FREE_GB", "15"))
# Obergrenze der Gesamtgröße (Issue #961, insight_core/services/file_cache_limit.py): 0 = unbegrenzt. Darüber
# verdrängt der stündliche Lauf die am wenigsten gebrauchten Dokumente, bis FILE_CACHE_EVICT_TARGET_PERCENT der
# Grenze erreicht sind (mit Objektspeicher nur lokal, sonst holt die Vorschau sie bei Bedarf neu).
FILE_CACHE_MAX_TOTAL_GB = float(os.environ.get("FILE_CACHE_MAX_TOTAL_GB", "0") or 0)
FILE_CACHE_EVICT_TARGET_PERCENT = int(os.environ.get("FILE_CACHE_EVICT_TARGET_PERCENT", "90") or 90)
FILE_PROXY_TIMEOUT_SECONDS = int(os.environ.get("FILE_PROXY_TIMEOUT_SECONDS", "15"))
# Lokale Kopien liefert der Webserver aus (X-Accel-Redirect, Range/ETag), Django prüft nur Zugriff und
# Sperre (Issue #785, docs/FILE_CACHE.md). Erst einschalten, wenn der Webserver die Ablage lesen kann
# und den Block aus dem Caddyfile hat – sonst kommen leere Antworten an.
FILE_ACCEL_REDIRECT = os.environ.get("FILE_ACCEL_REDIRECT", "false").lower() in ("1", "true", "yes")
# Drossel je Host über alle Prozesse (insight_core/services/host_pacing.py, gemeinsam mit dem Ingestor):
# Mindestabstand in Sekunden zwischen zwei Anfragen an dasselbe Ratsinformationssystem. Je Quelle
# abweichend über sync_config["request_interval"]; 0 schaltet die Drossel ab. Die Vorschau wartet höchstens
# FILE_PROXY_PACE_MAX_WAIT_SECONDS auf ihren Zeitpunkt und bittet sonst um einen neuen Versuch.
RIS_REQUEST_INTERVAL = float(os.environ.get("RIS_REQUEST_INTERVAL", "1.0"))
FILE_PROXY_PACE_MAX_WAIT_SECONDS = float(os.environ.get("FILE_PROXY_PACE_MAX_WAIT_SECONDS", "5"))
# Löschabgleich (Issue #787): Kopie und Text eines gesperrten Dokuments nach so vielen Tagen löschen
FILE_PURGE_AFTER_DAYS = int(os.environ.get("FILE_PURGE_AFTER_DAYS", "30"))
# Lässt sich die Quelle vor dem Löschen nicht befragen, wartet das Löschen höchstens so viele Tage zusätzlich
FILE_PURGE_CONFIRM_GRACE_DAYS = int(os.environ.get("FILE_PURGE_CONFIRM_GRACE_DAYS", "7"))
# Bremse des Löschabgleichs: Liefern in einem Lauf mehr Dokumente einer Quelle neu 404/410, wird keines
# gesperrt (kaputte Quelle, Umstellung, Wartung) und die Quelle ruht für den Lauf
FILE_RECONCILE_MAX_MISSING = int(os.environ.get("FILE_RECONCILE_MAX_MISSING", "10"))
# Ablage nach SHA-256 mit Referenzzählung (Issue #788): "sha256" (Standard) oder "kommune" (bisheriges
# Layout je Kommune, ohne Deduplizierung und ohne Objektspeicher)
FILE_STORE_LAYOUT = os.environ.get("FILE_STORE_LAYOUT", "sha256")
# Der Ingestor legt Dateien, die er für den Text lädt, selbst in der Ablage ab (OPARL_FILES_ROOT im
# Ingestor). Dann holt der Dokument-Cache Dateien in der Texterkennung nicht ein zweites Mal.
INGESTOR_STORES_FILES = os.environ.get("INGESTOR_STORES_FILES", "false").lower() in ("1", "true", "yes")
# Abruf der RIS-Dateien (Issue #919, hub/ris/abruf.py): Ein Abruf beansprucht seine Datei höchstens so lange
# (danach gibt cache_files sie frei), je Quelle und Lauf höchstens so viele Abrufe, und die Prüfung
# „dokumentabruf“ (/health/worker/) scheitert, wenn fällige Wiederholungen länger als so viele Stunden liegen.
DOCUMENT_FETCH_STALE_MINUTES = int(os.environ.get("DOCUMENT_FETCH_STALE_MINUTES", "30"))
DOCUMENT_FETCH_MAX_QUEUED = int(os.environ.get("DOCUMENT_FETCH_MAX_QUEUED", "200"))
DOCUMENT_FETCH_RETRY_ALERT_HOURS = float(os.environ.get("DOCUMENT_FETCH_RETRY_ALERT_HOURS", "6"))
# S3-kompatibler Objektspeicher für die Ablage (Issue #788), Standard aus. Zugangsdaten nur aus der Umgebung.
# Eingeschaltet ist die lokale Ablage ein Zwischenspeicher mit höchstens OBJ_CACHE_MAX_GB.
OBJ_ENABLED = os.environ.get("OBJ_ENABLED", "false").lower() in ("1", "true", "yes")
OBJ_ENDPOINT = os.environ.get("OBJ_ENDPOINT", "")
OBJ_BUCKET = os.environ.get("OBJ_BUCKET", "")
OBJ_KEY = os.environ.get("OBJ_KEY", "")
OBJ_SECRET = os.environ.get("OBJ_SECRET", "")
OBJ_REGION = os.environ.get("OBJ_REGION", "")
OBJ_ADDRESSING_STYLE = os.environ.get("OBJ_ADDRESSING_STYLE", "auto")
OBJ_TIMEOUT_SECONDS = float(os.environ.get("OBJ_TIMEOUT_SECONDS", "30"))
OBJ_CACHE_MAX_GB = int(os.environ.get("OBJ_CACHE_MAX_GB", "60"))
# Gesamtdauer eines Abrufs aus dem Objektspeicher (danach gilt er als gestört; kein Abruf bei der Quelle, #919)
OBJ_FETCH_TOTAL_SECONDS = float(os.environ.get("OBJ_FETCH_TOTAL_SECONDS", "60"))
# Prüfsummen beim Upload: "when_required" (verträglich mit S3-kompatiblen Anbietern) oder "when_supported"
OBJ_CHECKSUMS = os.environ.get("OBJ_CHECKSUMS", "when_required")
# Quellen-Schonung (Issue #89): ab so vielen Sync-Fehlversuchen in Folge lassen Dokument-Cache
# und Datei-Proxy das Ratsinformationssystem in Ruhe (Ratenlimits, IP-Sperren).
INSIGHT_SOURCE_BACKOFF_FAILURES = int(os.environ.get("INSIGHT_SOURCE_BACKOFF_FAILURES", "3"))
# Dokumentabrufe gehen nur an öffentliche Adressen (insight_core/services/safe_fetch.py);
# für den Selbstbetrieb mit einem Ratsinformationssystem im internen Netz einschalten.
INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS = os.environ.get("INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Sitzungsmappe (Issue #218): Obergrenzen der in das Gesamt-PDF eingebundenen PDF-Anlagen je Mappe.
# Darüber hinaus werden Anlagen als Verweisseite aufgenommen (im ZIP-Paket bleiben sie vollständig) –
# so bleibt der Speicherbedarf der Erzeugung im Rahmen des Containers.
SESSION_PACKAGE_MAX_EMBED_MB = int(os.environ.get("SESSION_PACKAGE_MAX_EMBED_MB", "200"))
SESSION_PACKAGE_MAX_PAGES = int(os.environ.get("SESSION_PACKAGE_MAX_PAGES", "3000"))

# Protokollierung (Issue #221, docs/PROTOKOLLIERUNG.md)
# Archivpakete vor der fristgerechten Löschung: Alias eines Eintrags in STORAGES (z. B. ein
# S3-Speicher) oder – leer – ein Verzeichnis. Liegt es unter MEDIA_ROOT, liefert serve_media es
# nie aus (PROTECTED_MEDIA_PREFIXES). Die Pakete löscht mandari nicht selbst.
AUDIT_ARCHIVE_STORAGE = os.environ.get("AUDIT_ARCHIVE_STORAGE", "")
AUDIT_ARCHIVE_ROOT = Path(os.environ.get("AUDIT_ARCHIVE_ROOT", str(MEDIA_ROOT / "audit_archive")))
# Berichte der Zeitpläne (Issue #516), etwa der monatliche Verfügbarkeitsbericht: im Medien-Volume
# (also in der Sicherung), nie über /media/ abrufbar (mandari/media.py)
REPORTS_ROOT = Path(os.environ.get("REPORTS_ROOT", str(MEDIA_ROOT / "berichte")))
# Obergrenze eines Exports aus der Oberfläche; größere Zeiträume über manage.py export_audit_log
AUDIT_EXPORT_MAX_ROWS = int(os.environ.get("AUDIT_EXPORT_MAX_ROWS", "100000"))
# Aufbewahrung des mandantenübergreifenden Sicherheitsprotokolls in Tagen (purge_security_audit_log)
SECURITY_AUDIT_RETENTION_DAYS = int(os.environ.get("SECURITY_AUDIT_RETENTION_DAYS", "365"))


# Default primary key field type
# https://docs.djangoproject.com/en/6.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# =============================================================================
# Mandari-spezifische Einstellungen
# =============================================================================

# Marketing-Website URL (lokal: http://localhost:8001, Produktion: leer = gleiche Domain)
MARKETING_URL = os.environ.get("MARKETING_URL", "http://localhost:8001" if DEBUG else "")

# Elasticsearch
ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
ELASTICSEARCH_AUTO_INDEX = os.environ.get("ELASTICSEARCH_AUTO_INDEX", "True").lower() in (
    "true",
    "1",
    "yes",
)

# Suchindex als Abonnement der Datendrehscheibe (insight_search.abonnement, Issue #526): "aus" (Standard)
# registriert kein Abonnement. "schatten" pflegt aus den ris.*-Ereignissen einen Schattenindex
# (schatten-papers usw., gleiche Abbildung) neben dem Live-Index; Vergleich mit
# manage.py suchindex_schatten vergleichen. "aktiv" schreibt den Live-Index; erst mit dem Umschalten
# (Issue #527) verwenden, das die bisherigen Wege abschaltet. Der Schattenbetrieb lässt sich auf
# Kommunen (Kennungen, kommagetrennt; leer = alle) und eine ungefähre Obergrenze an Dokumenten
# begrenzen (Speicher von Elasticsearch, 0 = keine Grenze).
SEARCH_INDEX_SUBSCRIPTION = os.environ.get("SEARCH_INDEX_SUBSCRIPTION", "aus").strip().lower() or "aus"
if SEARCH_INDEX_SUBSCRIPTION not in ("aus", "schatten", "aktiv"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SEARCH_INDEX_SUBSCRIPTION muss aus, schatten oder aktiv sein.")
# Benachrichtigungen als Abonnement der Datendrehscheibe (Issue #529, DEPLOYMENT.md „Benachrichtigungen als
# Abonnement“): "aus" (Standard) benachrichtigt wie bisher in der Anfrage. "schatten" schreibt zusätzlich
# Ereignisse der Aufgaben, das Abonnement "benachrichtigung" vergleicht nur. "aktiv" benachrichtigt nur
# noch über das Abonnement (Worker, Rolle dispatch). Rückweg: "aus".
WORK_NOTIFICATION_SUBSCRIPTION = os.environ.get("WORK_NOTIFICATION_SUBSCRIPTION", "aus").strip().lower() or "aus"
if WORK_NOTIFICATION_SUBSCRIPTION not in ("aus", "schatten", "aktiv"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("WORK_NOTIFICATION_SUBSCRIPTION muss aus, schatten oder aktiv sein.")
# Work-Daten folgen Tagesordnungspunkten und Vorlagen über Neuveröffentlichungen des RIS (Issue #547,
# apps/work/ris/verknuepfungen.py): "aus" (Standard) schaltet den Abgleich ab, "probe" pflegt nur die Anker und
# meldet im Protokoll, was geschähe, "aktiv" hängt Notizen, Positionen und Kommentare an den Nachfolger um.
WORK_RIS_RELINK = os.environ.get("WORK_RIS_RELINK", "aus").strip().lower() or "aus"
if WORK_RIS_RELINK not in ("aus", "probe", "aktiv"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("WORK_RIS_RELINK muss aus, probe oder aktiv sein.")
WORK_RIS_RELINK_INTERVAL_MINUTES = int(os.environ.get("WORK_RIS_RELINK_INTERVAL_MINUTES", "15"))
SEARCH_INDEX_SHADOW_BODIES = [
    teil.strip().lower() for teil in os.environ.get("SEARCH_INDEX_SHADOW_BODIES", "").split(",") if teil.strip()
]
for _kommune in SEARCH_INDEX_SHADOW_BODIES:
    try:
        uuid.UUID(_kommune)
    except ValueError:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "SEARCH_INDEX_SHADOW_BODIES: nur Kennungen von Kommunen (UUID), kommagetrennt."
        ) from None
SEARCH_INDEX_SHADOW_MAX_DOCS = int(os.environ.get("SEARCH_INDEX_SHADOW_MAX_DOCS", "100000"))
if SEARCH_INDEX_SHADOW_MAX_DOCS < 0:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SEARCH_INDEX_SHADOW_MAX_DOCS darf nicht negativ sein (0 = keine Grenze).")
# RIS-Projektor für Session-Mandanten (hub.projections.ris_session, Issue #536): "aus" (Standard) registriert
# kein Abonnement. "schatten" schreibt aus den Ereignissen von mandari Session die Schatten-Quelle neben dem
# RIS-Bestand (eigene Tabelle, nach außen unsichtbar); Vergleich mit dem Spiegel:
# manage.py ris_projektor_schatten vergleichen. Den Bestand selbst schreibt der Projektor erst mit dem
# Umschalten (Issue #537). Mandanten: Kennungen der Session-Mandanten (UUID), kommagetrennt; leer = alle.
RIS_SESSION_PROJECTOR = os.environ.get("RIS_SESSION_PROJECTOR", "aus").strip().lower() or "aus"
if RIS_SESSION_PROJECTOR not in ("aus", "schatten"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("RIS_SESSION_PROJECTOR muss aus oder schatten sein (aktiv erst mit Issue #537).")
RIS_SESSION_PROJECTOR_TENANTS = [
    teil.strip().lower() for teil in os.environ.get("RIS_SESSION_PROJECTOR_TENANTS", "").split(",") if teil.strip()
]
for _mandant in RIS_SESSION_PROJECTOR_TENANTS:
    try:
        uuid.UUID(_mandant)
    except ValueError:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "RIS_SESSION_PROJECTOR_TENANTS: nur Kennungen von Session-Mandanten (UUID), kommagetrennt."
        ) from None

# Abfrage der Volltextsuche (Konzept Insight-Suche, P0): "v2" (Standard) sucht alle Wörter (UND), Straßen
# mit optionalem Grundwort, Unschärfe nur als Rückfall, mit begrenztem Aktualitätsbonus und Mindestrelevanz;
# "v1" ist die bisherige Abfrage (ODER, Unschärfe immer) als Rückfall ohne Deploy (Neustart genügt).
SEARCH_RANKING = os.environ.get("SEARCH_RANKING", "v2").strip().lower() or "v2"
if SEARCH_RANKING not in ("v1", "v2"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SEARCH_RANKING muss v1 oder v2 sein.")
# Stärke des Aktualitätsbonus: Ein frisch beratener Treffer zählt höchstens (1 + Wert)-fach (0 = aus)
SEARCH_RECENCY_WEIGHT = float(os.environ.get("SEARCH_RECENCY_WEIGHT", "1.0"))
# Treffer unter diesem Anteil des besten Werts ihres Index entfallen (v2; 0 = alle behalten). 0,05 behält in
# Münster rund 90 % der Treffer mit allen Wörtern (0,1: rund 75 %) und hält Randtreffer aus „Neueste“ heraus
SEARCH_MIN_RELEVANCE = float(os.environ.get("SEARCH_MIN_RELEVANCE", "0.05"))
# Namensteil eines Straßenkompositums („witzleben“ aus „Witzlebenstraße“) zählt als eigene Lesart nur,
# wenn höchstens so viele Vorgänge und Dokumente der Kommune ihn enthalten
SEARCH_NAME_PART_MAX_DOCS = int(os.environ.get("SEARCH_NAME_PART_MAX_DOCS", "400"))

# Ortsband der Insight-Suche (Konzept Insight-Suche, P0.8): erkennt die Suche eine eindeutige Straße aus dem
# eigenen Straßenverzeichnis, zeigt sie die neuesten Vorgänge im Umkreis mit Karte. Nur für Kommunen, deren Vorgänge
# zu mindestens INSIGHT_SEARCH_PLACES_MIN_SHARE verortet sind. Suchanfragen gehen dabei an keinen externen Dienst.
INSIGHT_SEARCH_PLACES = os.environ.get("INSIGHT_SEARCH_PLACES", "true").lower() in ("true", "1", "yes", "an")
INSIGHT_SEARCH_PLACES_MIN_SHARE = float(os.environ.get("INSIGHT_SEARCH_PLACES_MIN_SHARE", "0.5"))
INSIGHT_SEARCH_PLACES_RADIUS = int(os.environ.get("INSIGHT_SEARCH_PLACES_RADIUS", "500"))

# KI-Anbieter (Issue #950): Anbieter, Basis-URL, Modell und Schlüssel stehen im Admin (KI-Einstellungen, für
# Work auch je Organisation). Hier nur die Positivliste erlaubter Hosts als technische Sperre, kommagetrennt;
# leer ist nichts erlaubt (kein Standard, shared/mandari_dokumente/ki_hosts.py). Gilt für jeden KI-Aufruf, auch
# für die Texterkennung über MISTRAL_BASE_URL.
from mandari_dokumente.ki_hosts import erlaubte_hosts_aus_umgebung

KI_ERLAUBTE_HOSTS = list(erlaubte_hosts_aus_umgebung())

# KI-Assistent in Insight (Issue #899): höchstens so viele Runden mit Werkzeugen je Antwort und so viele Sekunden
# je Antwort. Anbieter und Modell kommen aus den KI-Einstellungen (Bürgerportal-Modell, Ausweichmodell).
INSIGHT_CHAT_MAX_TOOL_ROUNDS = int(os.environ.get("INSIGHT_CHAT_MAX_TOOL_ROUNDS", "4"))
INSIGHT_CHAT_TIME_LIMIT_SECONDS = int(os.environ.get("INSIGHT_CHAT_TIME_LIMIT_SECONDS", "90"))
# Kostenbremse: Tagesobergrenze aller Antworten des KI-Assistenten zusammen (Modellaufrufe und Token aus den
# Chat-Nutzungen seit Mitternacht). Ist eine erreicht, fragt der Assistent den Anbieter bis zum nächsten Tag nicht.
INSIGHT_CHAT_DAILY_MAX_CALLS = int(os.environ.get("INSIGHT_CHAT_DAILY_MAX_CALLS", "500"))
INSIGHT_CHAT_DAILY_MAX_TOKENS = int(os.environ.get("INSIGHT_CHAT_DAILY_MAX_TOKENS", "1000000"))
# Öffentlicher, nur lesender MCP-Server unter /insight/mcp (Issue #899, docs/INSIGHT_MCP.md). Standard aus; die
# Freischaltung entscheidet der Betrieb. Grenzen je Adresse und Minute (alle Anfragen), je Adresse und Tag
# (Werkzeugaufrufe) und insgesamt je Minute; 0 schaltet eine Grenze ab. INSIGHT_MCP_ALLOWED_ORIGINS: weitere Hosts,
# deren Browser-Clients (Kopfzeile Origin) zugreifen dürfen, kommagetrennt; ALLOWED_HOSTS gelten immer.
INSIGHT_MCP_ENABLED = os.environ.get("INSIGHT_MCP_ENABLED", "false").lower() in ("true", "1", "yes")
INSIGHT_MCP_PER_IP_MINUTE = int(os.environ.get("INSIGHT_MCP_PER_IP_MINUTE", "30"))
INSIGHT_MCP_PER_IP_DAY = int(os.environ.get("INSIGHT_MCP_PER_IP_DAY", "500"))
INSIGHT_MCP_PER_MINUTE = int(os.environ.get("INSIGHT_MCP_PER_MINUTE", "300"))
INSIGHT_MCP_ALLOWED_ORIGINS = [
    host.strip() for host in os.environ.get("INSIGHT_MCP_ALLOWED_ORIGINS", "").split(",") if host.strip()
]

# Mistral-kompatible Texterkennung (optional): nur mit Schlüssel UND Basis-URL, deren Host in KI_ERLAUBTE_HOSTS
# steht; sonst bleibt die Texterkennung lokal (pypdf, Tesseract). Nur für öffentliche RIS-Dateien.
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_BASE_URL = os.environ.get("MISTRAL_BASE_URL", "").strip()
MISTRAL_OCR_RATE_LIMIT = int(os.environ.get("MISTRAL_OCR_RATE_LIMIT", "60"))  # Requests pro Minute

MISTRAL_OCR_MODEL = os.environ.get("MISTRAL_OCR_MODEL", "pixtral-12b-2409")

# Texterkennung (Issues #817, #530): eine Implementierung in shared/mandari_dokumente, gleiche Variablen wie
# im Ingestor. TEXT_EXTRACTION_RUNNER wählt, wer den Text der RIS-Dateien erkennt: "ingestor" (Standard,
# OCR-Worker des Ingestors) oder "worker" (Aufträge file.extract_text in der Warteschlange ocr, lesen nur aus
# Ablage und Objektspeicher; braucht TASKS_BACKEND=journal, sonst startet die Anwendung nicht, siehe unten bei
# TASKS). In Anwendung und Ingestor gleich setzen, sonst arbeiten beide oder keiner.
TEXT_EXTRACTION_RUNNER = os.environ.get("TEXT_EXTRACTION_RUNNER", "ingestor").strip().lower() or "ingestor"
if TEXT_EXTRACTION_RUNNER not in ("ingestor", "worker"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("TEXT_EXTRACTION_RUNNER muss ingestor oder worker sein.")
# Dateien länger als diese Zeit in "processing" gelten als abgebrochen und werden zurückgestellt; nach
# TEXT_EXTRACTION_MAX_ATTEMPTS Abbrüchen gescheitert („Speichergrenze“). Die Prüfung „texterkennung“
# (/health/worker/) meldet Dateien, die trotzdem länger hängen.
TEXT_EXTRACTION_STALE_MINUTES = int(os.environ.get("TEXT_EXTRACTION_STALE_MINUTES", "60"))
# Statusprüfung „texterkennung“: rot erst ab so vielen aufgegebenen Dateien je 24 h (einzelne Riesenscans sind erwartbar)
TEXT_EXTRACTION_GIVE_UP_ALERT = int(os.environ.get("TEXT_EXTRACTION_GIVE_UP_ALERT", "5"))
TEXT_EXTRACTION_MAX_ATTEMPTS = int(os.environ.get("TEXT_EXTRACTION_MAX_ATTEMPTS", "3"))
TEXT_EXTRACTION_MAX_SIZE_MB = int(os.environ.get("TEXT_EXTRACTION_MAX_SIZE_MB", "50"))
# Je Lauf des Zeitplans höchstens so viele Aufträge einreihen bzw. wartend halten (nur mit Runner "worker");
# dieselbe Grenze für extract_texts ohne --limit
TEXT_EXTRACTION_QUEUE_DEPTH = int(os.environ.get("TEXT_EXTRACTION_QUEUE_DEPTH", "20"))
# Prüfung „dokumenttext“ (/health/worker/, Issue #919, nur mit Runner "worker"): rot, sobald ein abgelegtes Dokument
# länger als so viele Stunden auf seinen Text wartet
TEXT_EXTRACTION_BACKLOG_ALERT_HOURS = float(os.environ.get("TEXT_EXTRACTION_BACKLOG_ALERT_HOURS", "24"))
# Grenzen je Seite und Datei (DEPLOYMENT.md, „OCR-Worker des Ingestors“)
OCR_DPI = int(os.environ.get("OCR_DPI", "200"))
OCR_MAX_MEGAPIXELS = float(os.environ.get("OCR_MAX_MEGAPIXELS", "8"))
OCR_MEMORY_LIMIT_MB = int(os.environ.get("OCR_MEMORY_LIMIT_MB", "1024"))
OCR_PAGE_TIMEOUT = float(os.environ.get("OCR_PAGE_TIMEOUT", "120"))
OCR_FILE_BUDGET_SECONDS = float(os.environ.get("OCR_FILE_BUDGET_SECONDS", "1200"))
OCR_MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "100"))

# Insight Subscriptions (E-Mail-Digest)
# Abos zu Themen und Orten (Seite /insight/benachrichtigungen/, generate_alerts, send_digest).
# Standard aus: Die Befehle sind nirgends eingeplant; der Wiederaufbau über die Datendrehscheibe
# ist geplant. Abmelden bleibt immer möglich. Beschluss-Abos sind davon nicht betroffen.
INSIGHT_SUBSCRIPTIONS_ENABLED = os.environ.get("INSIGHT_SUBSCRIPTIONS_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
# Ratsfragen im Bürgerportal (/insight/fragen/, send_question_reminders, Issue #734). Standard aus
# (pausiert, bis die Funktion neu aufgebaut ist): Bisherige Fragen bleiben lesbar, es lassen sich
# keine neuen stellen, keine Mails, keine Erinnerungen, keine Antwortquoten.
INSIGHT_QUESTIONS_ENABLED = os.environ.get("INSIGHT_QUESTIONS_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
INSIGHT_DIGEST_ENABLED = os.environ.get("INSIGHT_DIGEST_ENABLED", "True").lower() in ("true", "1", "yes")
INSIGHT_DIGEST_MAX_ALERTS_PER_MAIL = int(os.environ.get("INSIGHT_DIGEST_MAX_ALERTS_PER_MAIL", "20"))
INSIGHT_DIGEST_FROM_EMAIL = os.environ.get("INSIGHT_DIGEST_FROM_EMAIL", "")  # Falls leer → DEFAULT_FROM_EMAIL
# Betriebsmonitor: Alarm-Empfänger (kommagetrennt; leer → Moderations-Empfänger bzw. Superuser) und Schwellen
INSIGHT_ALERT_EMAILS = [e.strip() for e in os.environ.get("INSIGHT_ALERT_EMAILS", "").split(",") if e.strip()]
INSIGHT_SOURCE_STALE_WARNING_HOURS = int(os.environ.get("INSIGHT_SOURCE_STALE_WARNING_HOURS", "48"))
INSIGHT_SOURCE_STALE_CRITICAL_DAYS = int(os.environ.get("INSIGHT_SOURCE_STALE_CRITICAL_DAYS", "7"))
# Ratsfragen: Empfänger für Moderations-Hinweise (kommagetrennt); leer → aktive Superuser
INSIGHT_MODERATION_EMAILS = [e.strip() for e in os.environ.get("INSIGHT_MODERATION_EMAILS", "").split(",") if e.strip()]

# Georeferenzierung
GEOREF_ENABLED = os.environ.get("GEOREF_ENABLED", "True").lower() in ("true", "1", "yes")
PHOTON_API_URL = os.environ.get("PHOTON_API_URL", "https://photon.komoot.io/api/")
GEOCODING_RATE_LIMIT = int(os.environ.get("GEOCODING_RATE_LIMIT", "5"))  # Requests pro Sekunde
# Kappung nur für LLM-Pass und Legacy-Photon-Pfad (Gazetteer-Pass nutzt Volltext)
GEOREF_TEXT_MAX_CHARS = int(os.environ.get("GEOREF_TEXT_MAX_CHARS", "8000"))
# Automatischer Georef-Lauf (Regex/Gazetteer-Pass, KEIN LLM): periodisch nach
# Sync-Zyklen bzw. als Zeitplan im Worker (insight_core/schedules.py), begrenzt pro Lauf
GEOREF_AUTO_ENABLED = os.environ.get("GEOREF_AUTO_ENABLED", "True").lower() in ("true", "1", "yes")
GEOREF_AUTO_LIMIT = int(os.environ.get("GEOREF_AUTO_LIMIT", "50"))  # Papers pro Lauf
GEOREF_AUTO_INTERVAL_MINUTES = int(os.environ.get("GEOREF_AUTO_INTERVAL_MINUTES", "15"))
# Nachfolger der Texterkennung als Abonnements der Datendrehscheibe (Issue #919, ADR Dokumentkette, Abschnitt 9,
# DEPLOYMENT.md „Nachfolger der Texterkennung“): Neuer Text (ris.file.text_extracted) stößt die Verortung seines
# Vorgangs an (GEOREF_SUBSCRIPTION, Abonnement insight.verortung) und verwirft dessen KI-Zusammenfassung
# (SUMMARY_SUBSCRIPTION, Abonnement insight.zusammenfassung). "aus" (Standard) registriert kein Abonnement,
# "schatten" zählt nur, was geschähe, "aktiv" wirkt. Der Zeitplan verortung_automatisch bleibt das Sicherheitsnetz.
GEOREF_SUBSCRIPTION = os.environ.get("GEOREF_SUBSCRIPTION", "aus").strip().lower() or "aus"
SUMMARY_SUBSCRIPTION = os.environ.get("SUMMARY_SUBSCRIPTION", "aus").strip().lower() or "aus"
for _name, _modus in (("GEOREF_SUBSCRIPTION", GEOREF_SUBSCRIPTION), ("SUMMARY_SUBSCRIPTION", SUMMARY_SUBSCRIPTION)):
    if _modus not in ("aus", "schatten", "aktiv"):
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(f"{_name} muss aus, schatten oder aktiv sein.")
# Fraktionssitzungen: periodische Erzeugung aus Sitzungsreihen (Issue #61)
FACTION_SCHEDULE_INTERVAL_MINUTES = int(os.environ.get("FACTION_SCHEDULE_INTERVAL_MINUTES", "60"))
FACTION_SCHEDULE_HORIZON_DAYS = int(os.environ.get("FACTION_SCHEDULE_HORIZON_DAYS", "90"))
# Kartenmarker-Cache (map_markers-Endpoint)
MAP_MARKERS_CACHE_SECONDS = int(os.environ.get("MAP_MARKERS_CACHE_SECONDS", "600"))

# Encryption Master Key (für Work-Module Datenverschlüsselung)
# Generate with: python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
ENCRYPTION_MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY", "")
# Nur während eines Schlüsselwechsels: der bisherige Hauptschlüssel (mehrere durch Komma getrennt).
# Wird ausschließlich zum Lesen verwendet; Ablauf in docs/KRYPTOKONZEPT.md, danach wieder leeren.
ENCRYPTION_MASTER_KEY_PREVIOUS = os.environ.get("ENCRYPTION_MASTER_KEY_PREVIOUS", "")

# OParl
OPARL_REQUEST_TIMEOUT = int(os.environ.get("OPARL_REQUEST_TIMEOUT", "300"))
OPARL_MAX_RETRIES = int(os.environ.get("OPARL_MAX_RETRIES", "5"))

# OParl-Aggregations-API (Issue #17): mandari als eigene OParl-1.1-Datenquelle.
# Basis-URL für alle Objekt-IDs — host-unabhängig konfigurierbar, damit die
# API auch unter einer eigenen (Sub-)Domain (z. B. oparl.mandari.de) laufen kann.
OPARL_BASE_URL = os.environ.get("OPARL_BASE_URL", f"{SITE_URL}/oparl").rstrip("/")
OPARL_API_PAGE_SIZE = int(os.environ.get("OPARL_API_PAGE_SIZE", "100"))  # Objekte pro Listen-Seite
OPARL_API_RATE_LIMIT = int(os.environ.get("OPARL_API_RATE_LIMIT", "120"))  # Requests pro Minute je IP (0 = aus)
OPARL_API_CACHE_SECONDS = int(os.environ.get("OPARL_API_CACHE_SECONDS", "60"))  # Cache ungefilterter Listen
# Lizenz-URL am System-Objekt des Aggregators (OParl 1.1 ``license``); leer = keine übergreifende Angabe,
# es gilt die Lizenz der jeweiligen Kommune am Body
OPARL_LICENSE_URL = os.environ.get("OPARL_LICENSE_URL", "").strip()
# Änderungsfeed je Kommune (…/changes, docs/adr/20260929-aenderungsfeed-format.md). Standard aus: Ohne laufende
# Erzeuger der Ereignisse wäre der Feed leer und täuschte Abnehmern vor, es habe sich nichts geändert.
OPARL_CHANGES_ENABLED = os.environ.get("OPARL_CHANGES_ENABLED", "false").lower() in ("1", "true", "yes")
# Gültigkeit eines Cursors in Tagen; zugesagt sind mindestens 30. So lange muss das Journal seine Zeilen behalten.
OPARL_CHANGES_RETENTION_DAYS = int(os.environ.get("OPARL_CHANGES_RETENTION_DAYS", "90"))
# Snapshots (…/snapshot), die gleichzeitig entstehen dürfen; weitere Anfragen erhalten 503 mit Retry-After
OPARL_SNAPSHOT_PARALLEL = int(os.environ.get("OPARL_SNAPSHOT_PARALLEL", "2"))
if OPARL_CHANGES_RETENTION_DAYS < 30:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("OPARL_CHANGES_RETENTION_DAYS muss mindestens 30 sein (Zusage des Änderungsfeeds).")

# IndexNow (Issue #939, insight_core/services/indexnow.py): meldet neue, geänderte und entfernte Seiten des
# Bürgerportals an Bing, Yandex & Co. (Abonnement insight.indexnow, Warteschlange adapter). Ohne Schlüssel aus.
# Schlüssel: 8–128 Zeichen aus A–Z, a–z, 0–9 und -; die Anwendung liefert ihn unter /insight/<schlüssel>.txt aus.
INDEXNOW_KEY = os.environ.get("INDEXNOW_KEY", "").strip()
INDEXNOW_ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "").strip() or "https://api.indexnow.org/indexnow"

# Katalog der offenen Ratsinformationen nach DCAT-AP.de 3.0 (Issue #104, docs/DCAT_KATALOG.md): alle gelisteten
# Kommunen unter /data/dcat/catalog, je Kommune unter /data/dcat/body/<uuid>/catalog (.ttl, .rdf, .jsonld).
# Standard aus: Herausgeber und Kontakt müssen stimmen, bevor ein Datenportal den Katalog einsammelt.
DCAT_ENABLED = os.environ.get("DCAT_ENABLED", "false").lower() in ("1", "true", "yes")
# Herausgeber der Datensätze des Aggregators (der Betreiber, keine Person) und seine Webseite
DCAT_PUBLISHER_NAME = os.environ.get("DCAT_PUBLISHER_NAME", "mandari").strip()
DCAT_PUBLISHER_URL = os.environ.get("DCAT_PUBLISHER_URL", SITE_URL).strip()
# Kontaktadresse im Katalog: ein Funktionspostfach, keine persönliche Adresse; leer = nur die Webseite
DCAT_CONTACT_EMAIL = os.environ.get("DCAT_CONTACT_EMAIL", "").strip()
# Kennung des Betreibers bei GovData (http://dcat-ap.de/def/contributors/…), vergeben bei der Anmeldung dort
DCAT_CONTRIBUTOR_ID = os.environ.get("DCAT_CONTRIBUTOR_ID", "").strip()
# Fertige Kataloge im Cache (Sekunden, 0 = aus); ein neuer Veröffentlichungsstand gilt sofort
DCAT_CACHE_SECONDS = int(os.environ.get("DCAT_CACHE_SECONDS", "300"))

# Sync-Einstellungen (alle 10 Minuten inkrementell, Full-Sync um 3 Uhr)
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "10"))
SYNC_FULL_HOUR = int(os.environ.get("SYNC_FULL_HOUR", "3"))

# Zeitpläne (apps.events.schedule, Issue #516): einzelne abschalten, Namen kommagetrennt, z. B.
# "befehl:build_meeting_packages". Ihre Termine verstreichen ohne Auftrag; ein noch vorhandener
# Cron-Eintrag des Befehls läuft dann wieder (Rückweg je Befehl, DEPLOYMENT.md „Geplante Aufgaben“).
EVENTS_SCHEDULES_DISABLED = [
    name.strip() for name in os.environ.get("EVENTS_SCHEDULES_DISABLED", "").split(",") if name.strip()
]

# Hintergrundaufträge über die Tasks-Schnittstelle von Django
# https://docs.djangoproject.com/en/stable/topics/tasks/ und docs/adr/20260929-auftraege-und-zeitplaene.md
#
# TASKS_BACKEND=immediate (Standard): führt @task-Aufträge sofort in der Anfrage aus.
# TASKS_BACKEND=journal: schreibt sie in die Tabelle events_task; abgearbeitet werden sie vom
# Runner ``manage.py events_tasks`` (eigener Prozess bzw. Container). Ohne laufenden Runner
# bleiben Aufträge liegen – erst den Runner starten, dann umschalten.
# Ein vollständiger Importpfad eines anderen Backends ist ebenfalls erlaubt.
TASK_QUEUES = ["default", "mail", "index", "ocr", "ai", "adapter", "live"]

# Live-Übertragungen von Gremiensitzungen (Issue #915, docs/LIVE_UEBERTRAGUNG.md): Status der Streaming-Anbieter rund
# um die Sitzungen, Einzelbilder per Texterkennung (nur im Speicher). Aus = keine Anfragen nach außen, kein Zeitplan,
# die Warteschlange live ruht (Parallelität 0, kein Worker nötig).
LIVE_UEBERTRAGUNG_AKTIV = os.environ.get("LIVE_UEBERTRAGUNG_AKTIV", "false").lower() in ("1", "true", "yes")
LIVE_TESSERACT_CMD = os.environ.get("LIVE_TESSERACT_CMD", "tesseract")
LIVE_TESSDATA_DIR = os.environ.get("LIVE_TESSDATA_DIR", "")
LIVE_OCR_MEMORY_LIMIT_MB = int(os.environ.get("LIVE_OCR_MEMORY_LIMIT_MB", "256"))
LIVE_OCR_TIMEOUT_SECONDS = int(os.environ.get("LIVE_OCR_TIMEOUT_SECONDS", "20"))
LIVE_PROTOKOLL_TAGE = int(os.environ.get("LIVE_PROTOKOLL_TAGE", "90"))
_TASK_BACKENDS = {
    "immediate": "django.tasks.backends.immediate.ImmediateBackend",
    "journal": "apps.events.tasks_backend.JournalBackend",
}
_tasks_backend = os.environ.get("TASKS_BACKEND", "").strip() or "immediate"
TASKS = {
    "default": {
        "BACKEND": _TASK_BACKENDS.get(_tasks_backend.lower(), _tasks_backend),
        # Beide Backends kennen alle Warteschlangen, damit @task(queue_name="mail") auch sofort läuft
        "QUEUES": TASK_QUEUES,
        # Nur für JournalBackend und den Runner (apps/events/tasks_backend.py); das sofort
        # ausführende Backend ignoriert sie. Parallelität und Zeitgrenzen je Warteschlange haben
        # Standardwerte im Code, Abweichungen je Auftragstyp stehen unter "tasks".
        "OPTIONS": {
            "max_tasks_per_process": int(os.environ.get("TASKS_MAX_TASKS_PER_PROCESS", "1000")),
            "max_memory_mb": int(os.environ.get("TASKS_MAX_MEMORY_MB", "400")),
            # Warteschlange live (Issue #915): ein Leseauftrag und die Statusabfrage zugleich (Dienst worker-live);
            # aus = 0, dann erwartet die Anwesenheitsprüfung keinen Worker für live
            "concurrency": {"live": 2 if LIVE_UEBERTRAGUNG_AKTIV else 0},
            "tasks": {
                # Verwaltungsbefehle als Zeitpläne (Issue #516): eigene Zeitgrenze je Befehl, höchstens
                # 3600 s; der Prozess endet vorher (apps.events.verwaltungsbefehle)
                "apps.events.verwaltungsbefehle.befehl_ausfuehren": {"timeout": 3660, "max_attempts": 1},
                # PDF-Export mit vielen Einträgen braucht länger als die 5 Minuten der Warteschlange
                "apps.work.background_tasks.generate_dsgvo_export_task": {"timeout": 900, "max_attempts": 3},
                # Admin (Issue #515): Ein Sync oder das Löschen einer großen Kommune dauert länger als
                # 5 Minuten. Ein Sync wird nicht wiederholt (Protokoll und nächster Lauf zeigen den Fehler).
                "insight_core.background_tasks.quelle_synchronisieren": {"timeout": 3600, "max_attempts": 1},
                "insight_core.background_tasks.kommune_loeschen": {"timeout": 3600, "max_attempts": 3},
                # Texterkennung einer Datei (Issue #530): Zeitbudget je Datei (OCR_FILE_BUDGET_SECONDS) plus Abruf;
                # Abbrüche zählt die Datei selbst (TEXT_EXTRACTION_MAX_ATTEMPTS)
                "insight_core.background_tasks.file_extract_text": {"timeout": 1800, "max_attempts": 3},
                # Live-Übertragungen (Issue #915): ein Leseauftrag dauert rund 50 s, die Statusabfrage Sekunden; beide
                # nicht wiederholen, der Zeitplan reiht jede Minute neu ein
                "hub.live.auftraege.live_bilder_lesen": {"timeout": 120, "max_attempts": 1},
                "hub.live.schedules.live_status_abfragen": {"timeout": 120, "max_attempts": 1},
            },
        },
    }
}

# Benachrichtigungen als Abonnement (Issue #529) brauchen Aufträge im Worker: Mit dem sofort ausführenden
# Backend liefe der Mailversand in der Transaktion der Zustellung (ein Rollback nähme die Benachrichtigung
# zurück, nicht aber die schon versendete Mail).
if WORK_NOTIFICATION_SUBSCRIPTION != "aus" and TASKS["default"]["BACKEND"] != _TASK_BACKENDS["journal"]:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("WORK_NOTIFICATION_SUBSCRIPTION schatten/aktiv braucht TASKS_BACKEND=journal.")

# Texterkennung im Worker (Issue #919, docs/adr/20261007-dokumentkette.md, Abschnitt 11): Voraussetzung sind
# Aufträge im Journal mit einem Runner für ocr. Mit dem sofort ausführenden Backend liefe jede direkt eingereihte
# Erkennung (file_extract_text.enqueue) in der Webanfrage oder im Zeitplan. Wie WORK_NOTIFICATION_SUBSCRIPTION:
# lieber nicht starten.
if TEXT_EXTRACTION_RUNNER == "worker" and TASKS["default"]["BACKEND"] != _TASK_BACKENDS["journal"]:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("TEXT_EXTRACTION_RUNNER=worker braucht TASKS_BACKEND=journal.")

# =============================================================================
# Email Configuration
# =============================================================================
# Can be overridden via SiteSettings in Admin

# Custom email backend that reads SMTP settings from SiteSettings (Admin)
# Falls back to environment variables if SiteSettings is not configured
# Django ≥ 6.1 verbietet die alten EMAIL_*-Settings neben MAILERS; die Umgebungsvariablen
# heißen weiterhin EMAIL_*, landen aber in MAIL_BACKEND / SMTP_FALLBACK (Issue #80).
MAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "apps.common.email_backend.SiteSettingsEmailBackend",
)

# SMTP-Zugang aus der Umgebung – Fallback, wenn in den SiteSettings (Admin) nichts hinterlegt ist
SMTP_FALLBACK = {
    "host": os.environ.get("EMAIL_HOST", ""),
    "port": int(os.environ.get("EMAIL_PORT", "587")),
    "username": os.environ.get("EMAIL_HOST_USER", ""),
    "password": os.environ.get("EMAIL_HOST_PASSWORD", ""),
    "use_tls": os.environ.get("EMAIL_USE_TLS", "True").lower() in ("true", "1", "yes"),
    "use_ssl": os.environ.get("EMAIL_USE_SSL", "False").lower() in ("true", "1", "yes"),
    "timeout": int(os.environ.get("EMAIL_TIMEOUT", "30")),
}

# Absender, wenn weder die Systemeinstellungen noch DEFAULT_FROM_EMAIL einen nennen. Leer gilt als nicht
# gesetzt (docker-compose.yml reicht die Variable auch leer durch). Message-ID und EHLO richten sich nur nach
# einem ausdrücklich gesetzten Absender, nie nach diesem Rückfall (apps.common.mail_domain, Issue #957).
DEFAULT_FROM_EMAIL_FALLBACK = "noreply@mandari.de"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "").strip() or DEFAULT_FROM_EMAIL_FALLBACK
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
# Domain hinter dem @ der Message-ID und Name im EHLO gegenüber dem Mailserver (Issue #957). Django nähme
# socket.getfqdn(), im Container die Container-ID. Leer = Domain von DEFAULT_FROM_EMAIL, wenn ausdrücklich
# gesetzt, sonst Host aus SITE_URL. Gesetzt beim Start in apps.common.mail_domain (gilt für alle Versandwege).
EMAIL_MESSAGE_ID_DOMAIN = os.environ.get("EMAIL_MESSAGE_ID_DOMAIN", "").strip()

# Mail-Dienst (apps.common.mail, Issue #528): Mailarten, die als Auftrag (Warteschlange "mail") statt in
# der Anfrage versendet werden – kommagetrennte Muster wie "work.*,konto.passwort" oder "*" für alle.
# Wirkt nur mit TASKS_BACKEND=journal und laufendem Worker. Leer (Standard) = wie bisher sofort; das ist
# auch der Rückweg. Mails im Postausgang versendet der Worker unabhängig vom Schalter.
MAIL_QUEUE = [muster.strip() for muster in os.environ.get("MAIL_QUEUE", "").split(",") if muster.strip()]
# Größere Mails (Texte und Anhänge, Bytes) gehen sofort raus, statt im Postausgang zu liegen
MAIL_QUEUE_MAX_BYTES = int(os.environ.get("MAIL_QUEUE_MAX_BYTES", str(20 * 1024 * 1024)))

# Django ≥ 6.1 (Issue #80): Versandwege heißen MAILERS. Der Standardweg ist das
# SiteSettings-Backend (Zugangsdaten aus dem Admin, Fallback auf die EMAIL_*-Werte
# oben). Organisationseigenes SMTP wird zur Laufzeit aufgebaut (apps.common.mail_backends).
MAILERS = {
    "default": {
        "BACKEND": MAIL_BACKEND,
        "OPTIONS": {},
    },
}
# Demo-Instanz: Es verlässt keine einzige Mail das System, auch nicht über SiteSettings oder
# organisationseigenes SMTP (apps.common.mail_backends.build_backend, Issue #99).
if DEMO_INSTANCE:
    MAILERS["default"]["BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"


# =============================================================================
# Authentication
# =============================================================================

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/work/"  # Nach Login zum Work-Portal
LOGOUT_REDIRECT_URL = "/accounts/logged-out/"  # Nach Logout zur Abmeldung-Seite

# Session settings
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # 30 Tage
SESSION_SAVE_EVERY_REQUEST = True  # Session bei jeder Anfrage verlängern

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},  # BSI-Empfehlung für Verwaltungsanwendungen
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Logging
# Strukturierte Logs mit Request-Kennung (apps/common/observability.py, Issue #162):
# Produktion JSON/INFO, Entwicklung Text/DEBUG; LOG_FORMAT und LOG_LEVEL übersteuern.
from apps.common.observability import logging_config

LOGGING = logging_config(debug=DEBUG)


# =============================================================================
# Django 6.0 Content Security Policy (CSP)
# =============================================================================
# Stufe 1 (jetzt): Report-Only. Die Middleware ist eingehängt, der Browser meldet
# Verstöße in der Konsole, blockiert aber nichts. Sobald Inline-Skripte/-Styles
# ausgelagert bzw. mit Nonce versehen sind, wird die Policy nach SECURE_CSP
# verschoben und damit erzwungen (Frontend-Modernisierung, Epic siehe GitHub).
# Der Reverse Proxy setzt derzeit zusätzlich eine permissive Enforce-Policy.

from django.utils.csp import CSP

SECURE_CSP_REPORT_ONLY = {
    "default-src": [CSP.SELF],
    # Alpine wertet Inline-Ausdrücke per eval aus und braucht unsafe-eval, bis der Alpine-CSP-Build kommt (#172).
    # Ohne den Eintrag schickte jeder Seitenaufruf Dutzende gleichlautende Meldungen an /csp-report/
    # (im September 2026 knapp die Hälfte aller Anfragen); so melden die Browser nur echte Abweichungen.
    "script-src": [CSP.SELF, CSP.NONCE, CSP.UNSAFE_EVAL],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],  # Inline-Styles bleiben bis zur Auslagerung erlaubt
    "img-src": [CSP.SELF, "data:", "https:", "blob:"],
    "font-src": [CSP.SELF, "data:"],
    # Nur der eigene Ursprung: Karten laden Rasterkacheln per <img> über den eigenen Kachel-Proxy (img-src),
    # kein Skript verbindet sich mit einem externen Kartendienst.
    "connect-src": [CSP.SELF],
    "worker-src": [CSP.SELF, "blob:"],
    "child-src": ["blob:"],
    # Dokumentvorschau (Insight, Work) lädt Seiten des eigenen Ursprungs im iframe
    "frame-src": [CSP.SELF, "blob:"],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    # Eigene Seiten dürfen die Vorschau einbetten, fremde nicht (wie die erzwungene Policy des Reverse Proxy)
    "frame-ancestors": [CSP.SELF],
    # Verstöße landen im Protokoll (Logger mandari.csp) und im Zähler mandari_csp_violations_total (#172)
    "report-uri": ["/csp-report/"],
}


# =============================================================================
# Transport- und Cookie-Sicherheit (in Django, nicht nur im Reverse Proxy)
# =============================================================================
# TLS terminiert der Reverse Proxy und leitet HTTP selbst auf HTTPS um; Django
# erkennt HTTPS über X-Forwarded-Proto. Interne Healthchecks laufen über HTTP,
# daher kein SECURE_SSL_REDIRECT (Check W008 bewusst stillgelegt).

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "False").lower() in ("true", "1", "yes")
SECURE_REDIRECT_EXEMPT = [r"^health/?$"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

if not DEBUG:
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", str(60 * 60 * 24 * 365)))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False  # Aufnahme in die Preload-Liste ist eine bewusste Entscheidung (W021 stillgelegt)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Sitzungs- und CSRF-Cookie gelten nur für genau den Host, der sie gesetzt hat (kein
# Domain-Attribut). Mit HTTPS tragen sie zusätzlich das Präfix __Host-: Browser nehmen ein
# solches Cookie nur mit Secure, Path=/ und ohne Domain an, eine Subdomain kann es also weder
# setzen noch überschreiben. Ohne HTTPS (lokale Entwicklung) bleiben die Standardnamen.
# Hinweis: Die Umstellung der Namen beendet einmalig alle bestehenden Sitzungen.
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
_host_only_cookies = not DEBUG and SITE_URL.startswith("https://")
SESSION_COOKIE_NAME = "__Host-sessionid" if _host_only_cookies else "sessionid"
CSRF_COOKIE_NAME = "__Host-csrftoken" if _host_only_cookies else "csrftoken"

SILENCED_SYSTEM_CHECKS = [
    "security.W008",  # SSL-Redirect übernimmt der Reverse Proxy (Healthchecks über HTTP)
    "security.W021",  # HSTS-Preload bewusst noch nicht gesetzt
]


# =============================================================================
# Django Unfold Admin Theme
# =============================================================================
# https://unfoldadmin.com/docs/

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

UNFOLD = {
    # Branding
    "SITE_TITLE": "Mandari Admin",
    "SITE_HEADER": "Mandari",
    "SITE_SUBHEADER": "Kommunalpolitische Transparenz",
    "SITE_URL": "/",
    # UI Options
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SHOW_BACK_BUTTON": True,
    # Environment Badge (oben rechts)
    "ENVIRONMENT": "mandari.admin_utils.environment_callback",
    # Dashboard
    "DASHBOARD_CALLBACK": "insight_core.admin_dashboard.dashboard_callback",
    # Farbschema - passend zum Frontend (Indigo)
    "COLORS": {
        "base": {
            "50": "249 250 251",  # gray-50
            "100": "243 244 246",  # gray-100
            "200": "229 231 235",  # gray-200
            "300": "209 213 219",  # gray-300
            "400": "156 163 175",  # gray-400
            "500": "107 114 128",  # gray-500
            "600": "75 85 99",  # gray-600
            "700": "55 65 81",  # gray-700
            "800": "31 41 55",  # gray-800
            "900": "17 24 39",  # gray-900
            "950": "3 7 18",  # gray-950
        },
        "primary": {
            "50": "238 242 255",  # indigo-50
            "100": "224 231 255",  # indigo-100
            "200": "199 210 254",  # indigo-200
            "300": "165 180 252",  # indigo-300
            "400": "129 140 248",  # indigo-400
            "500": "99 102 241",  # indigo-500
            "600": "79 70 229",  # indigo-600
            "700": "67 56 202",  # indigo-700
            "800": "55 48 163",  # indigo-800
            "900": "49 46 129",  # indigo-900
            "950": "30 27 75",  # indigo-950
        },
    },
    # Sidebar Navigation
    # Icons: Material Symbols (https://fonts.google.com/icons)
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("Dashboard"),
                "separator": False,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Ubersicht"),
                        "icon": "home",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": _("OParl Daten"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Kommunen"),
                        "icon": "account_balance",
                        "link": reverse_lazy("admin:insight_core_oparlbody_changelist"),
                    },
                    {
                        "title": _("Gremien"),
                        "icon": "groups",
                        "link": reverse_lazy("admin:insight_core_oparlorganization_changelist"),
                    },
                    {
                        "title": _("Personen"),
                        "icon": "person",
                        "link": reverse_lazy("admin:insight_core_oparlperson_changelist"),
                    },
                    {
                        "title": _("Sitzungen"),
                        "icon": "event",
                        "link": reverse_lazy("admin:insight_core_oparlmeeting_changelist"),
                    },
                    {
                        "title": _("Vorgänge"),
                        "icon": "description",
                        "link": reverse_lazy("admin:insight_core_oparlpaper_changelist"),
                    },
                    {
                        "title": _("Tagesordnung"),
                        "icon": "format_list_numbered",
                        "link": reverse_lazy("admin:insight_core_oparlagendaitem_changelist"),
                    },
                    {
                        "title": _("Dateien"),
                        "icon": "attach_file",
                        "link": reverse_lazy("admin:insight_core_oparlfile_changelist"),
                    },
                    {
                        "title": _("Mitgliedschaften"),
                        "icon": "badge",
                        "link": reverse_lazy("admin:insight_core_oparlmembership_changelist"),
                    },
                    {
                        "title": _("Orte"),
                        "icon": "location_on",
                        "link": reverse_lazy("admin:insight_core_oparllocation_changelist"),
                    },
                    {
                        "title": _("Orts-Koordinaten"),
                        "icon": "map",
                        "link": reverse_lazy("admin:insight_core_locationmapping_changelist"),
                    },
                    {
                        "title": _("Beratungen"),
                        "icon": "forum",
                        "link": reverse_lazy("admin:insight_core_oparlconsultation_changelist"),
                    },
                    {
                        "title": _("Wahlperioden"),
                        "icon": "date_range",
                        "link": reverse_lazy("admin:insight_core_oparllegislativeterm_changelist"),
                    },
                ],
            },
            {
                "title": _("Sync & Quellen"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Betriebsmonitor"),
                        "icon": "monitor_heart",
                        "link": reverse_lazy("admin_monitoring"),
                    },
                    {
                        "title": _("OParl Quellen"),
                        "icon": "database",
                        "link": reverse_lazy("admin:insight_core_oparlsource_changelist"),
                    },
                    {
                        "title": _("Sync-Protokoll"),
                        "icon": "history",
                        "link": reverse_lazy("admin:insight_sync_synclog_changelist"),
                    },
                    {
                        "title": _("Sync-Einstellungen"),
                        "icon": "settings",
                        "link": reverse_lazy("admin:insight_sync_syncconfig_changelist"),
                    },
                ],
            },
            {
                "title": _("Work Module"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Parteigruppen"),
                        "icon": "account_tree",
                        "link": reverse_lazy("admin:tenants_partygroup_changelist"),
                    },
                    {
                        "title": _("Organisationen"),
                        "icon": "corporate_fare",
                        "link": reverse_lazy("admin:tenants_organization_changelist"),
                    },
                    {
                        "title": _("Mitgliedschaften"),
                        "icon": "group_add",
                        "link": reverse_lazy("admin:tenants_membership_changelist"),
                    },
                    {
                        "title": _("Rollen"),
                        "icon": "admin_panel_settings",
                        "link": reverse_lazy("admin:tenants_role_changelist"),
                    },
                    {
                        "title": _("Berechtigungen"),
                        "icon": "verified_user",
                        "link": reverse_lazy("admin:tenants_permission_changelist"),
                    },
                    # Einladungen removed - personal data (managed via Work portal)
                ],
            },
            {
                "title": _("Support"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Support-Tickets"),
                        "icon": "support_agent",
                        "link": reverse_lazy("admin:work_supportticket_changelist"),
                        "badge": "apps.work.admin.support_ticket_badge",
                    },
                ],
            },
            {
                "title": _("Session RIS"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Mandanten"),
                        "icon": "domain",
                        "link": reverse_lazy("admin:session_sessiontenant_changelist"),
                    },
                    {
                        "title": _("Mandant anlegen"),
                        "icon": "domain_add",
                        "link": reverse_lazy("admin:session_sessiontenant_provision"),
                    },
                    {
                        "title": _("Gremien"),
                        "icon": "groups",
                        "link": reverse_lazy("admin:session_sessionorganization_changelist"),
                    },
                    # Personen removed - personal data (managed via Session portal)
                    {
                        "title": _("Sitzungen"),
                        "icon": "event",
                        "link": reverse_lazy("admin:session_sessionmeeting_changelist"),
                    },
                    {
                        "title": _("Vorlagen"),
                        "icon": "description",
                        "link": reverse_lazy("admin:session_sessionpaper_changelist"),
                    },
                    {
                        "title": _("Anträge"),
                        "icon": "how_to_vote",
                        "link": reverse_lazy("admin:session_sessionapplication_changelist"),
                    },
                    {
                        "title": _("Protokolle"),
                        "icon": "article",
                        "link": reverse_lazy("admin:session_sessionprotocol_changelist"),
                    },
                ],
            },
            {
                "title": _("Session Verwaltung"),
                "separator": False,
                "collapsible": True,
                "items": [
                    # Session Benutzer removed - personal data (managed via Session portal)
                    {
                        "title": _("Session Rollen"),
                        "icon": "admin_panel_settings",
                        "link": reverse_lazy("admin:session_sessionrole_changelist"),
                    },
                    {
                        "title": _("API-Tokens"),
                        "icon": "key",
                        "link": reverse_lazy("admin:session_sessionapitoken_changelist"),
                    },
                    {
                        "title": _("Audit-Log"),
                        "icon": "history",
                        "link": reverse_lazy("admin:session_sessionauditlog_changelist"),
                    },
                ],
            },
            {
                "title": _("Benutzer & Sicherheit"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Benutzer"),
                        "icon": "account_circle",
                        "link": reverse_lazy("admin:accounts_user_changelist"),
                    },
                    {
                        "title": _("Login-Versuche"),
                        "icon": "login",
                        "link": reverse_lazy("admin:accounts_loginattempt_changelist"),
                    },
                    {
                        "title": _("Gruppen"),
                        "icon": "shield",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                    },
                ],
                # DSGVO: Folgende wurden entfernt (persönliche Daten):
                # - 2FA-Geräte → Work Portal
                # - Vertrauenswürdige Geräte → Work Portal
                # - Sitzungen → Work Portal
                # - Sicherheitsbenachrichtigungen → Work Portal
            },
            {
                # Ereignistechnik (Issue #510): nur für Administratoren, Eingriffe im Sicherheitsprotokoll
                "title": _("Ereignistechnik"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Abonnements"),
                        "icon": "sync_alt",
                        "link": reverse_lazy("admin:events_subscription_changelist"),
                        "permission": "apps.events.admin.nur_administratoren",
                    },
                    {
                        "title": _("Geparkte Ereignisse"),
                        "icon": "pending_actions",
                        "link": reverse_lazy("admin:events_parkedevent_changelist"),
                        "permission": "apps.events.admin.nur_administratoren",
                    },
                    {
                        "title": _("Aufträge"),
                        "icon": "task",
                        "link": reverse_lazy("admin:events_task_changelist"),
                        "permission": "apps.events.admin.nur_administratoren",
                    },
                    {
                        "title": _("Worker"),
                        "icon": "memory",
                        "link": reverse_lazy("admin:events_workerprocess_changelist"),
                        "permission": "apps.events.admin.nur_administratoren",
                    },
                ],
            },
            {
                "title": _("System"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Einstellungen"),
                        "icon": "settings",
                        "link": reverse_lazy("admin:common_sitesettings_changelist"),
                    },
                    {
                        "title": _("GPU-Rechenknoten"),
                        "icon": "memory",
                        "link": reverse_lazy("admin:minutes_computesettings_changelist"),
                    },
                    {
                        "title": _("GPU-Knoten (Übersicht)"),
                        "icon": "dns",
                        "link": reverse_lazy("admin:minutes_gpunode_changelist"),
                    },
                ],
            },
        ],
    },
    # Site Dropdown (oben rechts, neben User)
    "SITE_DROPDOWN": [
        {
            "icon": "public",
            "title": _("Zur Website"),
            "link": "/",
        },
        {
            "icon": "code",
            "title": _("GitHub"),
            "link": "https://github.com/mandariOSS/mandari",
        },
    ],
}
