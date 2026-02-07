"""
Django settings for Mandari Marketing Website (Wagtail).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from project root (shared with mandari)
env_path = BASE_DIR.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

SECRET_KEY = os.environ.get("WEBSITE_SECRET_KEY", os.environ.get("SECRET_KEY", "django-insecure-change-me"))
DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

SITE_URL = os.environ.get("SITE_URL", "http://localhost:8001")
MANDARI_API_URL = os.environ.get("MANDARI_API_URL", "http://mandari:8000/api")

from urllib.parse import urlparse

_site_domain = urlparse(SITE_URL).netloc

_allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
if "localhost" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("localhost")
if _site_domain and _site_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_site_domain)

_csrf_env = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(",") if o.strip()]
if SITE_URL and SITE_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)

INSTALLED_APPS = [
    # Wagtail
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",
    # Wagtail dependencies
    "modelcluster",
    "taggit",
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    # Project apps
    "marketing",
    "blog",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
]

ROOT_URLCONF = "website.urls"

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
                "marketing.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "website.wsgi.application"

# Database
DATABASE_URL = os.environ.get(
    "WEBSITE_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mandari_website"),
)
DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

import dj_database_url

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Wagtail settings
WAGTAIL_SITE_NAME = "Mandari"
WAGTAILADMIN_BASE_URL = SITE_URL
WAGTAIL_ADMIN_URL = "cms-admin/"

# Search
WAGTAILSEARCH_BACKENDS = {
    "default": {
        "BACKEND": "wagtail.search.backends.database",
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
