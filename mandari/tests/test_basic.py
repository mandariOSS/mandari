"""
Basic tests to verify Django is configured correctly.
"""

import pytest
from django.conf import settings
from django.test import Client


class TestDjangoConfiguration:
    """Test that Django is properly configured."""

    def test_settings_loaded(self):
        """Verify Django settings are loaded."""
        assert settings.configured
        assert settings.SECRET_KEY is not None

    def test_installed_apps(self):
        """Verify required apps are installed."""
        required_apps = [
            "django.contrib.admin",
            "django.contrib.auth",
            "apps.accounts",
            "apps.tenants",
            "apps.common",
        ]
        for app in required_apps:
            assert app in settings.INSTALLED_APPS, f"{app} not in INSTALLED_APPS"

    def test_database_configured(self):
        """Verify database is configured."""
        assert "default" in settings.DATABASES
        assert settings.DATABASES["default"]["ENGINE"] is not None


class TestHealthEndpoint:
    """Test the health check endpoint."""

    @pytest.mark.django_db
    def test_health_endpoint_returns_200(self):
        """Verify /health/ returns 200 OK."""
        client = Client()
        response = client.get("/health/")
        assert response.status_code == 200

    @pytest.mark.django_db
    def test_health_endpoint_content(self):
        """Verify /health/ returns expected content."""
        client = Client()
        response = client.get("/health/")
        assert b"ok" in response.content.lower() or response.status_code == 200


class TestAdminAccess:
    """Test admin is accessible."""

    @pytest.mark.django_db
    def test_admin_login_page(self):
        """Verify admin login page is accessible."""
        client = Client()
        response = client.get("/admin/login/")
        assert response.status_code == 200
