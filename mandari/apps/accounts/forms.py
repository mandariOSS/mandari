# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Authentication forms for Mandari.

Provides forms for:
- Login
- Password reset
- Password change
"""

from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import (
    PasswordResetForm as DjangoPasswordResetForm,
)
from django.contrib.auth.forms import (
    SetPasswordForm as DjangoSetPasswordForm,
)
from django.core.exceptions import ValidationError

User = get_user_model()


class LoginForm(forms.Form):
    """Login form with email and password."""

    email = forms.EmailField(
        max_length=255,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "deine@email.de",
                "autofocus": True,
                "autocomplete": "email",
            }
        ),
        label="E-Mail-Adresse",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Dein Passwort",
                "autocomplete": "current-password",
            }
        ),
        label="Passwort",
    )
    remember_me = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Angemeldet bleiben",
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user_cache = None

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get("email")
        password = cleaned_data.get("password")

        if email and password:
            # Authenticate using email as username
            self.user_cache = authenticate(
                self.request,
                username=email,
                password=password,
            )

            if self.user_cache is None:
                raise ValidationError(
                    "E-Mail-Adresse oder Passwort ist falsch.",
                    code="invalid_login",
                )

            if not self.user_cache.is_active:
                raise ValidationError(
                    "Dieses Konto ist deaktiviert. Bitte kontaktiere den Administrator.",
                    code="inactive",
                )

        return cleaned_data

    def get_user(self):
        """Return the authenticated user."""
        return self.user_cache


class PasswordResetForm(DjangoPasswordResetForm):
    """Custom password reset form with German labels."""

    email = forms.EmailField(
        max_length=255,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "deine@email.de",
                "autofocus": True,
                "autocomplete": "email",
            }
        ),
        label="E-Mail-Adresse",
    )


class SetPasswordForm(DjangoSetPasswordForm):
    """Custom set password form with German labels."""

    new_password1 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Neues Passwort",
                "autocomplete": "new-password",
            }
        ),
        label="Neues Passwort",
        help_text="Mindestens 8 Zeichen.",
    )
    new_password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Passwort bestätigen",
                "autocomplete": "new-password",
            }
        ),
        label="Passwort bestätigen",
    )


class RegistrationForm(forms.Form):
    """Registration form for invited users."""

    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Max",
                "autofocus": True,
                "autocomplete": "given-name",
            }
        ),
        label="Vorname",
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Mustermann",
                "autocomplete": "family-name",
            }
        ),
        label="Nachname",
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Mindestens 8 Zeichen",
                "autocomplete": "new-password",
            }
        ),
        label="Passwort",
        help_text="Mindestens 8 Zeichen.",
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Passwort wiederholen",
                "autocomplete": "new-password",
            }
        ),
        label="Passwort bestätigen",
    )

    def __init__(self, *args, email=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.email = email

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            raise ValidationError("Die Passwörter stimmen nicht überein.")

        # Check password strength
        if password1 and len(password1) < 8:
            raise ValidationError("Das Passwort muss mindestens 8 Zeichen lang sein.")

        return password2

    def save(self):
        """Create the user account."""
        user = User.objects.create_user(
            email=self.email,
            password=self.cleaned_data["password1"],
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
        )
        return user
