# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwendungen, die eine Kommune (Session) oder Fraktion (Work) selbst registriert (Issue #1591).

Die Fachschicht ruft diese Funktionen mit der fachfreien Kennung ihres Bereichs (``owner``, etwa
``bindings.compose(bindings.TENANT, str(tenant.pk))``) und prüft vorher selbst das Recht der Person. Die Plattform
prüft die Regeln der Anwendung (``OAuthApplication.clean``): nur Code-Ablauf im Namen von Personen, immer mit
Zustimmung, nur die Scopes, die die Fachschicht für ihre Art angemeldet hat (``bindings.Kind.scopes``), Redirect-URIs
nur mit https. Bindungen, öffentliche Schlüssel, Dienste und Grenzen bleiben beim Betreiber.

Jede Änderung steht im Sicherheitsprotokoll (``oidc_anwendung`` mit ``bereich``), in einer Transaktion mit der
Änderung. Das Client-Secret einer vertraulichen Anwendung gibt es nur in der Rückgabe von :func:`register` und
:func:`new_secret`; gespeichert ist nur sein Hash. Sperren und Löschen widerrufen alle Tokens der Anwendung.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.settings import oauth2_settings

from . import audit, bindings, catalog, services
from .models import OAuthApplication

#: Wer handelt (``durch`` im Sicherheitsprotokoll)
BY_OWNER = "Bereich"


class RegistrationError(Exception):
    """Die Anwendung verletzt eine Regel; ``messages`` nennt jede in einem Satz."""

    def __init__(self, messages: Sequence[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = list(messages)


@dataclass(frozen=True)
class Registration:
    """Ergebnis von :func:`register` und :func:`new_secret`."""

    application: OAuthApplication
    #: Client-Secret im Klartext, nur jetzt; leer bei öffentlichen Anwendungen
    secret: str


def scope_choices(owner: str) -> list[tuple[str, str]]:
    """Scopes, die der Bereich vergeben darf, mit der Beschreibung der Zustimmungsseite, in der Reihenfolge des Katalogs."""
    registrable = set(bindings.registrable_scopes(owner))
    return [(scope.name, scope.description) for scope in catalog.CATALOG if scope.name in registrable]


def owned(owner: str) -> list[OAuthApplication]:
    """Anwendungen des Bereichs, nach Namen."""
    if not owner:
        return []
    return list(OAuthApplication.objects.filter(owner=owner).order_by("name", "pk"))


def get_owned(owner: str, application_id: Any) -> OAuthApplication | None:
    """Anwendung des Bereichs; eine fremde gibt es für ihn nicht."""
    if not owner or not str(application_id).isdigit():
        return None
    application: OAuthApplication | None = OAuthApplication.objects.filter(
        owner=owner, pk=int(str(application_id))
    ).first()
    return application


def _messages(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [message for messages in exc.message_dict.values() for message in messages]
    return list(exc.messages)


def _record(request: Any, user: Any, owner: str, application: OAuthApplication, aktion: str, **details: Any) -> None:
    audit.write(
        audit.EVENT_APPLICATION,
        request,
        user_ref=getattr(user, "pk", None),
        aktion=aktion,
        anwendung=application.client_id,
        name=application.name,
        durch=BY_OWNER,
        bereich=owner,
        **details,
    )


def register(
    request: Any,
    owner: str,
    *,
    user: Any,
    name: str,
    confidential: bool,
    redirect_uris: Sequence[str],
    scopes: Sequence[str],
) -> Registration:
    """
    Anwendung für den Bereich anlegen: Code-Ablauf mit PKCE, RS256, Zustimmung jedes Mal, Grenzen nach Standard.

    Raises: :class:`RegistrationError` mit allen verletzten Regeln
    """
    secret = str(generate_client_secret()) if confidential else ""
    application = OAuthApplication(
        owner=owner,
        name=name.strip(),
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL if confidential else OAuthApplication.CLIENT_PUBLIC,
        authorization_grant_type=OAuthApplication.GRANT_AUTHORIZATION_CODE,
        redirect_uris=" ".join(uri.strip() for uri in redirect_uris if uri.strip()),
        algorithm=OAuthApplication.RS256_ALGORITHM,
        allowed_scopes=list(dict.fromkeys(str(scope) for scope in scopes)),
        skip_authorization=False,
        client_secret=secret,
        is_active=True,
    )
    problems: list[str] = []
    if not oauth2_settings.OIDC_RSA_PRIVATE_KEY:
        # Der Code-Ablauf signiert ID-Tokens mit RS256; ohne Schlüssel lehnt das Modell die Anwendung ab
        raise RegistrationError(
            ["Die Anmeldung über mandari ist in dieser Installation nicht vollständig eingerichtet."]
        )
    if not application.name:
        problems.append("Bitte einen Namen angeben.")
    if not application.redirect_uris:
        problems.append("Mindestens eine Redirect-URI angeben (https://…).")
    if not application.allowed_scopes:
        problems.append("Mindestens einen Scope auswählen.")
    try:
        application.full_clean(exclude=["client_id", "client_secret", "user"])
    except ValidationError as exc:
        problems.extend(_messages(exc))
    if problems:
        raise RegistrationError(list(dict.fromkeys(problems)))
    with transaction.atomic():
        application.save()
        _record(
            request,
            user,
            owner,
            application,
            "angelegt",
            scopes=" ".join(application.scope_list),
            typ="vertraulich" if confidential else "öffentlich",
        )
    return Registration(application=application, secret=secret)


def set_active(request: Any, owner: str, application_id: Any, *, user: Any, active: bool) -> OAuthApplication | None:
    """Anwendung des Bereichs sperren (mit Widerruf aller Tokens) oder wieder einschalten; ``None``: keine solche."""
    application = get_owned(owner, application_id)
    if application is None or bool(application.is_active) == active:
        return application
    with transaction.atomic():
        application.is_active = active
        application.save(update_fields=["is_active", "updated"])
        _record(request, user, owner, application, "entsperrt" if active else "gesperrt")
        if not active:
            # Wie im Admin: beim Wiedereinschalten lebt kein alter Zugang auf
            services.revoke_application(request, application, by=BY_OWNER, bereich=owner)
    return application


def new_secret(request: Any, owner: str, application_id: Any, *, user: Any) -> Registration | None:
    """Neues Client-Secret für eine vertrauliche Anwendung des Bereichs; das bisherige gilt sofort nicht mehr."""
    application = get_owned(owner, application_id)
    if application is None or application.client_type != OAuthApplication.CLIENT_CONFIDENTIAL:
        return None
    secret = str(generate_client_secret())
    with transaction.atomic():
        application.client_secret = secret
        application.save()
        _record(request, user, owner, application, "neues Client-Secret")
    return Registration(application=application, secret=secret)


def delete(request: Any, owner: str, application_id: Any, *, user: Any) -> str | None:
    """Anwendung des Bereichs löschen, mit allen Tokens; Ergebnis: ihr Name, ``None``: keine solche."""
    application = get_owned(owner, application_id)
    if application is None:
        return None
    name = str(application.name)
    with transaction.atomic():
        services.revoke_application(request, application, by=BY_OWNER, bereich=owner)
        _record(request, user, owner, application, "gelöscht")
        application.delete()
    return name
