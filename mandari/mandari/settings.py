"""
Django settings for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

import os
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
SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-change-me-in-production-with-a-real-secret-key")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

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
# Django uses leading dot for subdomain matching
_wildcard_domain = f".{_site_domain.replace('www.', '')}"
if _wildcard_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_wildcard_domain)

# CSRF trusted origins from environment (filter empty strings)
_csrf_env = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(",") if o.strip()]

# Ensure SITE_URL is always in CSRF trusted origins
if SITE_URL and SITE_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)
# Add wildcard subdomain support for CSRF (Django 4.0+)
_wildcard_csrf_origin = f"https://*.{_site_domain.replace('www.', '')}"
if _wildcard_csrf_origin not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_wildcard_csrf_origin)

# Subdomain redirect settings (e.g., volt.mandari.de -> /work/volt/)
# Extract main domain from SITE_URL (e.g., 'mandari.de')
MAIN_DOMAIN = os.environ.get("MAIN_DOMAIN", _site_domain.replace("www.", ""))
SUBDOMAIN_REDIRECT_ENABLED = os.environ.get("SUBDOMAIN_REDIRECT_ENABLED", "true").lower() == "true"


# Application definition

INSTALLED_APPS = [
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
    # Mandari Insight apps (OSS)
    "insight_core",
    "insight_content",
    "insight_sync",
    "insight_search",
    "insight_ai",
    # Mandari Work apps (OSS - AGPL 3.0)
    "apps.common",
    "apps.accounts",
    "apps.tenants",
    "apps.work",
    # Mandari Session RIS (OSS - AGPL 3.0)
    "apps.session",
]

# Custom User Model
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Database error handler - shows maintenance page on DB connection issues
    "apps.common.middleware.DatabaseErrorMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Subdomain redirect for organization shortcuts (e.g., volt.mandari.de -> /work/volt/)
    "apps.tenants.middleware.SubdomainRedirectMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    # Work module organization context
    "apps.tenants.middleware.OrganizationMiddleware",
]

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
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "insight_core.context_processors.navigation_context",
                "insight_core.context_processors.active_body",
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

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    )
}


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
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    SESSION_CACHE_ALIAS = "default"
else:
    # Use local memory cache for development
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


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

# WhiteNoise for static files (only in production)
# Django 6.0: Using STORAGES instead of deprecated STATICFILES_STORAGE
if not DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }


# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# Default primary key field type
# https://docs.djangoproject.com/en/6.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# =============================================================================
# Mandari-spezifische Einstellungen
# =============================================================================

# Meilisearch
MEILISEARCH_URL = os.environ.get("MEILISEARCH_URL", "http://localhost:7700")
MEILISEARCH_KEY = os.environ.get("MEILISEARCH_KEY", "masterKey")
MEILISEARCH_AUTO_INDEX = os.environ.get("MEILISEARCH_AUTO_INDEX", "True").lower() in (
    "true",
    "1",
    "yes",
)

# Groq API (für KI-Features)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Mistral API (für OCR)
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_OCR_RATE_LIMIT = int(os.environ.get("MISTRAL_OCR_RATE_LIMIT", "60"))  # Requests pro Minute

# Text Extraction
TEXT_EXTRACTION_ENABLED = os.environ.get("TEXT_EXTRACTION_ENABLED", "True").lower() in (
    "true",
    "1",
    "yes",
)
TEXT_EXTRACTION_ASYNC = os.environ.get("TEXT_EXTRACTION_ASYNC", "True").lower() in (
    "true",
    "1",
    "yes",
)
TEXT_EXTRACTION_MAX_SIZE_MB = int(os.environ.get("TEXT_EXTRACTION_MAX_SIZE_MB", "50"))

# Encryption Master Key (für Work-Module Datenverschlüsselung)
# Generate with: python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
ENCRYPTION_MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY", "")

# OParl
OPARL_REQUEST_TIMEOUT = int(os.environ.get("OPARL_REQUEST_TIMEOUT", "300"))
OPARL_MAX_RETRIES = int(os.environ.get("OPARL_MAX_RETRIES", "5"))

# Sync-Einstellungen (alle 10 Minuten inkrementell, Full-Sync um 3 Uhr)
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "10"))
SYNC_FULL_HOUR = int(os.environ.get("SYNC_FULL_HOUR", "3"))

# Django 6.0 Background Tasks
# https://docs.djangoproject.com/en/6.0/topics/tasks/
TASKS = {
    "default": {
        # Development: Immediate execution (synchronous)
        # Production: Use "django.tasks.backends.database.DatabaseBackend"
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend"
        if DEBUG
        else "django.tasks.backends.database.DatabaseBackend",
    }
}

# =============================================================================
# Email Configuration
# =============================================================================
# Can be overridden via SiteSettings in Admin

# Custom email backend that reads SMTP settings from SiteSettings (Admin)
# Falls back to environment variables if SiteSettings is not configured
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "apps.common.email_backend.SiteSettingsEmailBackend",
)

EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() in ("true", "1", "yes")
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False").lower() in ("true", "1", "yes")

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@mandari.de")
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)

# Email timeout
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "30"))


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
        "OPTIONS": {"min_length": 8},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "insight_core": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "insight_sync": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "apps.work": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps.common": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}


# =============================================================================
# Django 6.0 Content Security Policy (CSP)
# =============================================================================
# Built-in CSP support for protection against XSS attacks

if not DEBUG:
    from django.utils.csp import CSP

    SECURE_CSP = {
        "default-src": [CSP.SELF],
        "script-src": [CSP.SELF, CSP.NONCE],  # Alle Scripts lokal
        "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],  # TailwindCSS, MapLibre CSS
        "img-src": [CSP.SELF, "data:", "https:", "blob:"],  # blob: für MapLibre Tiles
        "font-src": [CSP.SELF],
        "connect-src": [CSP.SELF, "https://tiles.versatiles.org"],  # HTMX, VersaTiles
        "worker-src": [CSP.SELF, "blob:"],  # MapLibre WebWorker
        "child-src": ["blob:"],  # MapLibre
        "frame-ancestors": [CSP.NONE],
    }

    # Add CSP middleware (after SecurityMiddleware)
    # Note: Add 'django.middleware.csp.ContentSecurityPolicyMiddleware' to MIDDLEWARE
    # after 'django.middleware.security.SecurityMiddleware' when ready


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
                        "title": _("Vorgange"),
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
                        "title": _("OParl Quellen"),
                        "icon": "database",
                        "link": reverse_lazy("admin:insight_core_oparlsource_changelist"),
                    },
                ],
            },
            {
                "title": _("Content"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Blog"),
                        "icon": "edit_note",
                        "link": reverse_lazy("admin:insight_content_blogpost_changelist"),
                    },
                    {
                        "title": _("Releases"),
                        "icon": "new_releases",
                        "link": reverse_lazy("admin:insight_content_release_changelist"),
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
                    {
                        "title": _("Knowledge Base"),
                        "icon": "menu_book",
                        "link": reverse_lazy("admin:work_knowledgebasearticle_changelist"),
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
                "title": _("System"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Einstellungen"),
                        "icon": "settings",
                        "link": reverse_lazy("admin:common_sitesettings_changelist"),
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
