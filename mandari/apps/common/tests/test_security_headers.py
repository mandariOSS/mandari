# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erste pytest-django-Tests: Sicherheitskonfiguration, die eine Vergabestelle abfragt.

Diese Tests sichern die Härtung aus settings.py ab (CSP-Middleware, Cookie-Flags,
Passwortlänge) und dienen als Startpunkt für die Migration der Smoke-Skripte.
"""

import os

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csp import ContentSecurityPolicyMiddleware
from django.test import RequestFactory, override_settings
from django.utils.csp import CSP


def test_csp_middleware_directly_after_security_middleware():
    mw = settings.MIDDLEWARE
    sec = mw.index("django.middleware.security.SecurityMiddleware")
    assert mw[sec + 1] == "django.middleware.csp.ContentSecurityPolicyMiddleware"


def test_csp_report_only_policy_is_strict_on_frames_and_objects():
    policy = settings.SECURE_CSP_REPORT_ONLY
    assert policy["frame-ancestors"] == [CSP.NONE]
    assert policy["object-src"] == [CSP.NONE]
    assert CSP.NONCE in policy["script-src"]
    assert CSP.UNSAFE_EVAL not in policy["script-src"]


def test_password_policy_requires_twelve_characters():
    min_length = next(
        v["OPTIONS"]["min_length"]
        for v in settings.AUTH_PASSWORD_VALIDATORS
        if v["NAME"].endswith("MinimumLengthValidator")
    )
    assert min_length >= 12


def test_cookie_and_transport_settings():
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    # pytest-django setzt settings.DEBUG zur Laufzeit auf False; maßgeblich ist der Wert beim Import
    production_like = os.environ.get("DEBUG", "true").lower() in ("false", "0", "no")
    if production_like:
        assert settings.SESSION_COOKIE_SECURE is True
        assert settings.CSRF_COOKIE_SECURE is True
        assert settings.SECURE_HSTS_SECONDS >= 60 * 60 * 24 * 180


def _run_through_csp_middleware() -> HttpResponse:
    # Middleware isoliert prüfen: unabhängig von Datenbank, Redis oder Suche einzelner Views.
    # Django fügt den Nonce nur ein, wenn ihn die Antwort tatsächlich verwendet; Templates greifen über den
    # Context-Processor auf denselben LazyNonce zu (request._csp_nonce).
    middleware = ContentSecurityPolicyMiddleware(lambda request: HttpResponse(f"nonce:{request._csp_nonce}"))
    return middleware(RequestFactory().get("/__csp-probe__/"))


def test_report_only_csp_header_is_emitted_with_nonce():
    response = _run_through_csp_middleware()
    header = response.headers.get("Content-Security-Policy-Report-Only", "")
    assert "frame-ancestors 'none'" in header
    assert "object-src 'none'" in header
    assert "script-src 'self' 'nonce-" in header
    assert "Content-Security-Policy" not in response.headers


@override_settings(SECURE_CSP={"default-src": [CSP.SELF]})
def test_enforced_policy_is_emitted_when_configured():
    response = _run_through_csp_middleware()
    assert response.headers.get("Content-Security-Policy") == "default-src 'self'"
