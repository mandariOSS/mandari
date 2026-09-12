# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for accounts app (authentication).
"""

from django.urls import path

from .views import (
    LoggedOutView,
    LoginTwoFactorView,
    LoginView,
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
    RegisterView,
    SelfRegisterView,
    TwoFactorEnrollView,
)
from .views_webauthn import (
    LoginOptionsView,
    LoginVerifyView,
    RegistrationOptionsView,
    RegistrationVerifyView,
    SecurityKeysView,
)

app_name = "accounts"

urlpatterns = [
    # Login / Logout
    path("login/", LoginView.as_view(), name="login"),
    path("login/zwei-faktor/", LoginTwoFactorView.as_view(), name="login_2fa"),
    path("zwei-faktor/einrichten/", TwoFactorEnrollView.as_view(), name="two_factor_enroll"),
    # Sicherheitsschlüssel und Passkeys (WebAuthn)
    path("sicherheitsschluessel/", SecurityKeysView.as_view(), name="security_keys"),
    path(
        "sicherheitsschluessel/registrieren/optionen/",
        RegistrationOptionsView.as_view(),
        name="webauthn_register_options",
    ),
    path("sicherheitsschluessel/registrieren/", RegistrationVerifyView.as_view(), name="webauthn_register"),
    path("login/sicherheitsschluessel/optionen/", LoginOptionsView.as_view(), name="webauthn_login_options"),
    path("login/sicherheitsschluessel/", LoginVerifyView.as_view(), name="webauthn_login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logged-out/", LoggedOutView.as_view(), name="logged_out"),
    # Registration (for invited users)
    path("register/", RegisterView.as_view(), name="register"),
    # Self-registration (for organizations with open registration)
    path("register/<slug:org_slug>/", SelfRegisterView.as_view(), name="self_register"),
    # Password Reset
    path("password-reset/", PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password-reset/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
]
