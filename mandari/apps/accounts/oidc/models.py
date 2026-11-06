# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Modelle für die austauschbaren Modelle von django-oauth-toolkit (Issue #1206).

Die Bibliothek erlaubt, Anwendung, Tokens und Codes durch eigene Modelle zu ersetzen
(``OAUTH2_PROVIDER_*_MODEL``). Das geht nur, solange es keine Daten gibt; deshalb legt die Plattform alle
sechs Modelle von Anfang an selbst fest, auch die Gerätefreigabe (RFC 8628), die mandari noch nicht anbietet.
Die Tabellen der Bibliothek (``oauth2_provider_*``) entstehen dadurch nicht.

Was mandari ergänzt:

- :class:`OAuthApplication` trägt die erlaubten Scopes je Client (``allowed_scopes``) und einen Schalter
  ``is_active``. Erlaubt sind nur der Code-Ablauf (im Namen einer Person, PKCE S256) und Client Credentials
  (Dienste), Redirect-URIs nur mit https (http nur für localhost bei ``DEBUG``), Signatur nur RS256, das
  Client-Secret immer gehasht, bei vertraulichen Anwendungen nie leer (#1215).
- Access- und Refresh-Token stehen nicht im Klartext in der Datenbank: Mit
  ``COMPLIANT_BCP_RFC9700_TOKEN_STORAGE`` bleibt ``token`` leer, nachgeschlagen wird über die SHA-256-Prüfsumme
  ``token_checksum`` (``apps/common/tokens.py``, Verzeichnis der Token-Felder).
- Code und Refresh-Token tragen den Zeitpunkt der Anmeldung in der Sitzung, aus der sie stammen (``auth_time``).
  Daraus entsteht ``auth_time`` im ID-Token, auch nach einem Refresh; ``User.last_login`` gilt dafür nicht, weil
  jede Anmeldung auf irgendeinem Gerät ihn neu setzt.
- Profil Nichtöffentliches (#565): Ein Dienst kann an genau einen Bereich gebunden sein (``binding``, eine fachfreie
  Kennung, die die Fachschicht auswertet, :mod:`.bindings`), und eine vertrauliche Anwendung kann sich statt mit
  Client-Secret mit ``private_key_jwt`` (RFC 7523) ausweisen; ihre öffentlichen Schlüssel stehen in ``jwks``. Gebundene
  Dienste weisen sich nur so aus. Angenommene Client-Assertions sperren ihre Kennung ``jti`` bis zum Ablauf
  (:class:`OAuthClientAssertion`).

Die Modelle werden in ``apps/accounts/models.py`` eingebunden und gehören damit zur App ``accounts``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.hashers import check_password, identify_hasher
from django.core.exceptions import ValidationError
from django.db import models
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import (
    AbstractAccessToken,
    AbstractApplication,
    AbstractDeviceGrant,
    AbstractGrant,
    AbstractIDToken,
    AbstractRefreshToken,
    ApplicationManager,
)

from . import bindings, catalog

#: Abläufe, die eine Anwendung haben darf
ALLOWED_GRANT_TYPES = (AbstractApplication.GRANT_AUTHORIZATION_CODE, AbstractApplication.GRANT_CLIENT_CREDENTIALS)
#: Hosts, für die bei ``DEBUG`` auch http-Redirects gelten (Entwicklung auf dem eigenen Rechner)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
#: Mindestlänge eines neu eingetragenen Client-Secrets (erzeugt werden 128 Zeichen)
MIN_SECRET_LENGTH = 32


def client_secret_problem(secret: str) -> str:
    """
    Grund, aus dem ein Client-Secret einer vertraulichen Anwendung nicht taugt, sonst ``""``.

    Ein leeres Secret wird gehasht gespeichert und gilt danach für jede Anfrage ohne Secret: Die Bibliothek prüft
    eine fehlende Angabe als leeren Text gegen den Hash. Eine vertrauliche Anwendung wäre damit ohne Secret nutzbar.
    """
    if not secret:
        return "Vertrauliche Anwendungen brauchen ein Client-Secret."
    try:
        identify_hasher(secret)
    except ValueError:
        # Klartext, also ein neu eingetragenes Secret
        if len(secret) < MIN_SECRET_LENGTH:
            return f"Das Client-Secret braucht mindestens {MIN_SECRET_LENGTH} Zeichen."
        return ""
    if check_password("", secret):
        return "Das gespeicherte Client-Secret ist leer. Bitte ein neues eintragen."
    return ""


def redirect_uri_problem(uri: str, *, debug: bool) -> str:
    """Grund, aus dem eine Redirect-URI nicht taugt, sonst ``""``. Verglichen wird später immer exakt."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return f"{uri}: keine gültige Adresse."
    if parts.fragment:
        return f"{uri}: Redirect-URIs dürfen kein Fragment (#…) enthalten."
    if parts.scheme == "https" and parts.hostname:
        return ""
    if parts.scheme == "http" and debug and parts.hostname in LOOPBACK_HOSTS:
        return ""
    if parts.scheme == "http":
        return f"{uri}: http ist nur bei DEBUG und nur für localhost erlaubt, sonst https."
    return f"{uri}: Nur https-Adressen sind erlaubt."


class OAuthApplication(AbstractApplication):  # type: ignore[misc]  # Bibliothek ohne Typen
    """
    Eine Anwendung (Client), die sich bei mandari Tokens holt.

    Angelegt wird sie im Admin oder mit ``manage.py oidc_anwendung``, eine dynamische Registrierung gibt es nicht.
    Vertrauenswürdige eigene Anwendungen überspringen die Zustimmungsseite (``skip_authorization``).
    """

    allowed_scopes = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Erlaubte Scopes",
        help_text="Mehr als diese Scopes kann die Anwendung nicht anfordern.",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Aktiv",
        help_text="Ausgeschaltet bekommt die Anwendung keine Tokens mehr, und ihre Tokens gelten nicht mehr.",
    )
    binding = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_default="",
        verbose_name="Bindung",
        help_text=(
            "Nur für Dienste: genau ein Bereich, an den die Anwendung gebunden ist, etwa ein Session-Mandant "
            "(„session-mandant:<Kennung>“). Leer: keine Bindung."
        ),
    )
    jwks = models.JSONField(
        null=True,
        blank=True,
        default=None,
        verbose_name="Öffentliche Schlüssel (JWKS)",
        help_text=(
            "Für die Anmeldung mit private_key_jwt (RFC 7523): öffentliche Schlüssel der Anwendung als JWK Set, "
            "nur RS256 (RSA ab 2048 Bit) und ES256 (P-256). Dann gilt kein Client-Secret."
        ),
    )

    objects = ApplicationManager()

    #: Feld der Bibliothek (``ClientSecretField``, ohne Typangaben); die Angabe nennt mypy nur den Typ
    client_secret: str

    class Meta(AbstractApplication.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-Anwendung"
        verbose_name_plural = "OIDC-Anwendungen"

    def natural_key(self) -> tuple[str]:
        return (self.client_id,)

    @property
    def acts_for_person(self) -> bool:
        """Code-Ablauf: Die Anwendung handelt im Namen einer angemeldeten Person."""
        return bool(self.authorization_grant_type == AbstractApplication.GRANT_AUTHORIZATION_CODE)

    @property
    def scope_list(self) -> list[str]:
        """Erlaubte Scopes, die es im Katalog gibt und die zum Ablauf und zur Bindung der Anwendung passen."""
        names = self.allowed_scopes if isinstance(self.allowed_scopes, list) else []
        return catalog.allowed_for([str(name) for name in names], person=self.acts_for_person, bound=bool(self.binding))

    @property
    def uses_private_key_jwt(self) -> bool:
        """Weist sich die Anwendung mit ``private_key_jwt`` aus (öffentliche Schlüssel in ``jwks``)?"""
        return bool(self.jwks)

    def get_allowed_schemes(self) -> list[str]:
        return ["https", "http"] if settings.DEBUG else ["https"]

    def is_usable(self, request: Any) -> bool:
        return bool(self.is_active) and self.authorization_grant_type in ALLOWED_GRANT_TYPES

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        try:
            super().clean()
        except ValidationError as exc:
            for field, messages in exc.message_dict.items():
                errors.setdefault(field, []).extend(messages)

        if self.authorization_grant_type not in ALLOWED_GRANT_TYPES:
            errors.setdefault("authorization_grant_type", []).append(
                "Erlaubt sind nur der Code-Ablauf (Authorization Code mit PKCE) und Client Credentials."
            )
        for uri in self.redirect_uris.split():
            problem = redirect_uri_problem(uri, debug=bool(settings.DEBUG))
            if problem:
                errors.setdefault("redirect_uris", []).append(problem)

        names = self.allowed_scopes if isinstance(self.allowed_scopes, list) else None
        if names is None:
            errors.setdefault("allowed_scopes", []).append("Erwartet wird eine Liste von Scopes.")
        else:
            unknown = sorted(str(name) for name in names if catalog.get(str(name)) is None)
            if unknown:
                errors.setdefault("allowed_scopes", []).append(f"Unbekannte Scopes: {', '.join(unknown)}.")
            unbound = sorted(str(name) for name in names if self._needs_binding(str(name)))
            if unbound:
                errors.setdefault("allowed_scopes", []).append(
                    f"Nur für Dienste mit Bindung an einen Bereich: {', '.join(unbound)}."
                )
            unfit = sorted(
                str(name)
                for name in names
                if catalog.get(str(name)) and str(name) not in self.scope_list and str(name) not in unbound
            )
            if unfit:
                errors.setdefault("allowed_scopes", []).append(
                    f"Passen nicht zum Ablauf der Anwendung: {', '.join(unfit)}."
                )
        self._clean_binding_and_keys(errors)

        if self.authorization_grant_type == AbstractApplication.GRANT_CLIENT_CREDENTIALS:
            if self.client_type != AbstractApplication.CLIENT_CONFIDENTIAL:
                errors.setdefault("client_type", []).append("Dienste (Client Credentials) brauchen ein Client-Secret.")
            if self.algorithm != AbstractApplication.NO_ALGORITHM:
                errors.setdefault("algorithm", []).append("Dienste bekommen kein ID-Token; Signatur bitte leer lassen.")
        elif self.authorization_grant_type == AbstractApplication.GRANT_AUTHORIZATION_CODE:
            if self.algorithm != AbstractApplication.RS256_ALGORITHM:
                errors.setdefault("algorithm", []).append("ID-Tokens signiert mandari nur mit RS256.")
        if self.client_type == AbstractApplication.CLIENT_CONFIDENTIAL and not self.uses_private_key_jwt:
            problem = client_secret_problem(str(self.client_secret or ""))
            if problem:
                errors.setdefault("client_secret", []).append(problem)
        if errors:
            raise ValidationError(errors)

    def _needs_binding(self, name: str) -> bool:
        """Passt der Scope zum Ablauf, verlangt aber eine Bindung, die die Anwendung nicht hat?"""
        scope = catalog.get(name)
        if scope is None or not scope.bound or self.binding:
            return False
        return bool(scope.person if self.acts_for_person else scope.service)

    def _clean_binding_and_keys(self, errors: dict[str, list[str]]) -> None:
        """Bindung nur für Dienste mit ``private_key_jwt``; Schlüssel nur öffentlich, RS256 oder ES256 (#565)."""
        from .client_assertion import jwks_problems

        if self.jwks not in (None, {}, ""):
            problems = list(jwks_problems(self.jwks))
            if self.client_type != AbstractApplication.CLIENT_CONFIDENTIAL:
                problems.insert(0, "Öffentliche Schlüssel gibt es nur für vertrauliche Anwendungen.")
            if problems:
                errors.setdefault("jwks", []).extend(problems)
        if not self.binding:
            return
        problem = bindings.problem(str(self.binding))
        if problem:
            errors.setdefault("binding", []).append(problem)
        if self.authorization_grant_type != AbstractApplication.GRANT_CLIENT_CREDENTIALS:
            errors.setdefault("binding", []).append("Gebunden werden nur Dienste (Client Credentials).")
        if not self.uses_private_key_jwt:
            errors.setdefault("jwks", []).append(
                "Gebundene Dienste weisen sich mit private_key_jwt aus; bitte öffentliche Schlüssel (JWKS) eintragen."
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Unabhängig vom Formular: Secret immer gehasht, Herkunft immer „von Hand angelegt“
        self.hash_client_secret = True
        self.registration_source = AbstractApplication.RegistrationSource.MANUAL
        if not self.client_secret:
            # Öffentliche Anwendungen nutzen kein Secret; ein zufälliges, nie angezeigtes statt eines leeren, das
            # sonst jede Anfrage ohne Secret als Client-Authentifizierung durchgehen ließe
            self.client_secret = str(generate_client_secret())
        super().save(*args, **kwargs)


class OAuthClientAssertion(models.Model):
    """
    Angenommene Client-Assertion (``private_key_jwt``, RFC 7523, Abschnitt 3): ihre Kennung ``jti`` ist bis zum Ablauf
    gesperrt, eine zweite Nutzung wird abgelehnt.

    Ohne Fremdschlüssel (nur die Client-ID): Ein älteres Image, das die Tabelle nicht kennt, kann Anwendungen weiter
    löschen. Abgelaufene Einträge räumt das Annehmen neuer Assertions weg.
    """

    client_id = models.CharField(max_length=100, verbose_name="Client-ID")
    jti = models.CharField(max_length=255, verbose_name="Kennung (jti)")
    expires = models.DateTimeField(verbose_name="Gesperrt bis")
    created = models.DateTimeField(auto_now_add=True, verbose_name="Angenommen am")

    class Meta:
        verbose_name = "OIDC-Client-Assertion"
        verbose_name_plural = "OIDC-Client-Assertions"
        constraints = [models.UniqueConstraint(fields=["client_id", "jti"], name="oidc_assertion_jti_einmal")]
        indexes = [models.Index(fields=["expires"], name="oidc_assertion_ablauf")]

    def __str__(self) -> str:
        return f"{self.client_id}: {self.jti}"


class OAuthAccessToken(AbstractAccessToken):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Access-Token; in der Datenbank steht nur die SHA-256-Prüfsumme (``token`` bleibt leer)."""

    class Meta(AbstractAccessToken.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-Access-Token"
        verbose_name_plural = "OIDC-Access-Tokens"


class OAuthRefreshToken(AbstractRefreshToken):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Refresh-Token mit Rotation; in der Datenbank steht nur die SHA-256-Prüfsumme (``token`` bleibt leer)."""

    auth_time = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Anmeldezeitpunkt",
        help_text="Anmeldung der Person in der Sitzung, aus der die Kette stammt (auth_time im ID-Token).",
    )

    class Meta(AbstractRefreshToken.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-Refresh-Token"
        verbose_name_plural = "OIDC-Refresh-Tokens"


class OAuthIDToken(AbstractIDToken):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Ausgestelltes ID-Token; gespeichert sind nur Kennung (``jti``), Ablauf und Scopes, nicht das Token."""

    class Meta(AbstractIDToken.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-ID-Token"
        verbose_name_plural = "OIDC-ID-Tokens"


class OAuthGrant(AbstractGrant):  # type: ignore[misc]  # Bibliothek ohne Typen
    """
    Autorisierungscode des Code-Ablaufs.

    Bleibt im Klartext: Er gilt 60 Sekunden, nur einmal, und ist an PKCE (S256) gebunden. Ohne den
    ``code_verifier``, der nie gespeichert wird, ist er wertlos.
    """

    auth_time = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Anmeldezeitpunkt",
        help_text="Anmeldung der Person in der Sitzung, in der der Code entstand (auth_time im ID-Token).",
    )

    class Meta(AbstractGrant.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-Autorisierungscode"
        verbose_name_plural = "OIDC-Autorisierungscodes"


class OAuthDeviceGrant(AbstractDeviceGrant):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Gerätefreigabe (RFC 8628). mandari bietet den Ablauf nicht an; das Modell ist nur vorab festgelegt."""

    class Meta(AbstractDeviceGrant.Meta):  # type: ignore[misc]
        verbose_name = "OIDC-Gerätefreigabe"
        verbose_name_plural = "OIDC-Gerätefreigaben"
