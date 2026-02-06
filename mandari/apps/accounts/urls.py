# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for accounts app (authentication).
"""

from django.urls import path

from .views import (
    LoggedOutView,
    LoginView,
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
    RegisterView,
)

app_name = "accounts"

urlpatterns = [
    # Login / Logout
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logged-out/", LoggedOutView.as_view(), name="logged_out"),
    # Registration (for invited users)
    path("register/", RegisterView.as_view(), name="register"),
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
