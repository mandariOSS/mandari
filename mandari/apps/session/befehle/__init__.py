# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle von Session: jede Änderung als Befehl beim Eigentümer (Issue #1588, ``docs/adr/20261128-api-first-ohne-ausnahme.md``).

Ein Befehl entsteht aus drei Teilen; Anleitung mit allen Schritten in ``docs/DATENDREHSCHEIBE.md``, Abschnitt 6:

1. **Vertrag** ``hub/contracts/schemas/session.<objekt>.<befehl>/v1.json`` (JSON Schema, ``x-owner: apps.session``).
2. **Fachfunktion** in ``apps/session/befehle/<bereich>.py`` mit ``@befehl(name, recht=…)``. Sie bekommt einen
   ``Kontext`` mit geprüftem Mandanten, Person und Rechten und liefert die Quittung::

       @befehl("session.textblock.create", recht="manage_settings")
       def anlegen(kontext: Kontext) -> HandlerResult:
           werte = kontext.texte("title", "content")
           baustein = SessionTextBlock.objects.create(tenant=kontext.tenant, **werte)
           kontext.protokoll("create", baustein, titel=baustein.title)
           return kontext.quittung(baustein)

3. **View** liest nur das Formular und ruft ``ausfuehren(request, name, inhalt)``; Quittung oder Fehler kommen als
   ``hub.commands.Ergebnis`` zurück.

``@befehl`` registriert den Handler beim Dispatcher und gibt ihn für die gemeinsame Schnittstelle frei
(``POST /api/mandari/v1/session/<mandant>/befehle/<befehl>/v1``, Scope ``sitzung:schreiben``). Vor der Fachfunktion
prüft es, immer in der Transaktion des Befehls:

- **Mandant** aktiv, sonst 404 (``mandant-unbekannt``).
- **Person**: Auslöser ``user:<id>`` mit aktivem Konto und aktivem Zugang zum Mandanten, sonst 403.
- **Rechte**: alle Rechte aus ``recht`` nach dem Rechtemodell von Session (``SessionPermissionChecker``, mit
  Vertretungen), sonst 403. Anwendungen mit Access-Token brauchen zusätzlich „API-Zugang“ (``access_api``): Der
  Mandant entscheidet, ob seine Leute über Anwendungen arbeiten, wie beim Lesen über die Schnittstelle.

Objektbezogene Prüfungen (Sichtbarkeit von Nichtöffentlichem, Geltungsbereich) gehören in die Fachfunktion; Objekte
holt sie mit ``Kontext.hole`` immer im Mandanten (Mandantentrennung), sonst 404.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.http import HttpRequest

from apps.accounts.oidc import catalog
from hub.commands import (
    Command,
    CommandError,
    Ergebnis,
    Exposure,
    HandlerResult,
    TenantArea,
    command_handler,
    register_tenant_area,
)
from hub.commands import ausfuehren as _ausfuehren
from hub.commands.channel import SCHNITTSTELLE, current_channel

#: Mandantenbereich der Adressen: ``/api/mandari/v1/session/<kurzname>/befehle/…``
BEREICH: Final = "session"
#: Freigabe aller Befehle von Session für die gemeinsame Schnittstelle
FREIGABE: Final = Exposure(area=BEREICH, scopes=(catalog.SITZUNG_SCHREIBEN,))
#: Recht, ohne das Anwendungen (Access-Token) im Mandanten nichts ändern
API_ZUGANG: Final = "access_api"
USER_PREFIX: Final = "user:"

#: Feste Meldungen (nie mit Werten aus dem Inhalt)
MANDANT_UNBEKANNT: Final = "Der Mandant ist nicht bekannt oder deaktiviert."
KEIN_ZUGANG: Final = "Diese Anmeldung hat keinen Zugang zu diesem Mandanten."
KEIN_RECHT: Final = "Für diese Aktion fehlt das Recht in diesem Mandanten."
KEIN_API_ZUGANG: Final = "Für Änderungen über Anwendungen fehlt das Recht „API-Zugang“ in diesem Mandanten."
NUR_LEERZEICHEN: Final = "darf nicht nur aus Leerzeichen bestehen"
LEERE_ANGABE: Final = "Bitte die Angaben prüfen."


@dataclass(frozen=True)
class Kontext:
    """Was eine Fachfunktion über ihren Befehl weiß: geprüfter Mandant, Person und ihre Rechte."""

    command: Command
    tenant: Any
    session_user: Any
    rechte: frozenset[str] = field(default_factory=frozenset)

    @property
    def inhalt(self) -> Mapping[str, Any]:
        """Inhalt des Befehls, schon gegen den Vertrag geprüft."""
        return self.command.body

    def hole(
        self,
        modell: type[models.Model] | models.QuerySet[Any],
        kennung: Any,
        *,
        meldung: str,
        sperren: bool = False,
        **bezug: Any,
    ) -> Any:
        """
        Objekt dieses Mandanten mit der Kennung, sonst 404 mit ``meldung``.

        Ohne ``bezug`` filtert es über das Feld ``tenant`` des Modells; Modelle ohne dieses Feld nennen den Weg zum
        Mandanten selbst (``meeting__tenant=kontext.tenant``). ``sperren`` sperrt die Zeile bis zum Ende des Befehls.
        """
        queryset = modell.all() if isinstance(modell, models.QuerySet) else modell._default_manager.all()
        if not bezug:
            if not any(f.name == "tenant" for f in queryset.model._meta.get_fields()):
                raise ImproperlyConfigured(
                    f"{queryset.model.__name__} hat kein Feld tenant: Bezug zum Mandanten angeben, "
                    "z. B. meeting__tenant=kontext.tenant"
                )
            bezug = {"tenant": self.tenant}
        if sperren:
            queryset = queryset.select_for_update(of=("self",))
        objekt = queryset.filter(pk=kennung, **bezug).first()
        if objekt is None:
            raise CommandError.of(404, "nicht-gefunden", meldung)
        return objekt

    def texte(self, *felder: str) -> dict[str, str]:
        """
        Die genannten Textfelder des Inhalts ohne Leerraum am Rand, soweit vorhanden.

        Ein Feld, das nur aus Leerraum besteht, ergibt 422 mit dem Feld (der Vertrag prüft nur die Länge).
        """
        werte = {name: str(self.inhalt[name]).strip() for name in felder if name in self.inhalt}
        leer = {name: NUR_LEERZEICHEN for name, wert in werte.items() if not wert}
        if leer:
            raise CommandError.invalid(LEERE_ANGABE, leer)
        return werte

    def protokoll(self, aktion: str, objekt: Any, **angaben: Any) -> None:
        """
        Eintrag im Prüfprotokoll des Mandanten (``apps.session.audit``), in der Transaktion des Befehls.

        Nennt die Person, den Weg (Oberfläche oder Schnittstelle, dann mit Anwendung) und ``angaben``, nie Inhalte von
        Vorlagen, Niederschriften oder Anträgen.
        """
        from apps.session import audit

        changes = {**angaben, **current_channel().audit_details()}
        audit.log_event(aktion, objekt, tenant=self.tenant, user=self.session_user, changes=changes)

    def quittung(self, objekt: Any, **daten: Any) -> HandlerResult:
        """Quittung mit der Kennung des Objekts; ``daten`` nur Kennungen und Codes, nie Inhalte."""
        kennung = objekt.pk
        return HandlerResult(
            reference=str(kennung),
            aggregate_id=kennung if isinstance(kennung, uuid.UUID) else None,
            data=daten,
        )


Fachfunktion = Callable[[Kontext], HandlerResult]


def befehl(name: str, *, recht: str | Sequence[str], version: int = 1) -> Callable[[Fachfunktion], Fachfunktion]:
    """
    Registriert eine Fachfunktion als Befehl von Session und gibt ihn für die Schnittstelle frei.

    ``recht``: ein Recht oder mehrere (alle nötig) aus dem Rechtemodell von Session. Ohne Recht gibt es keinen
    Befehl; was darüber hinaus objektbezogen gilt, prüft die Fachfunktion.
    """
    noetig = (recht,) if isinstance(recht, str) else tuple(recht)
    _pruefe_rechte(name, noetig)

    def decorate(fachfunktion: Fachfunktion) -> Fachfunktion:
        @functools.wraps(fachfunktion)
        def handler(command: Command) -> HandlerResult:
            from apps.session import audit

            kontext = kontext_fuer(command, noetig)
            # Auch Einträge aus Model-Signalen nennen die Person, die den Befehl gesendet hat
            with audit.im_befehl(kontext.session_user):
                return fachfunktion(kontext)

        command_handler(name, version, exposure=FREIGABE)(handler)
        return fachfunktion

    return decorate


def _pruefe_rechte(name: str, noetig: tuple[str, ...]) -> None:
    from apps.session.permissions import ALL_PERMISSIONS, AUDIT_PERMISSIONS

    unbekannt = sorted(set(noetig) - ALL_PERMISSIONS - AUDIT_PERMISSIONS)
    if not noetig or unbekannt:
        raise ImproperlyConfigured(f"Befehl {name}: Recht fehlt oder ist unbekannt ({', '.join(unbekannt) or '–'})")


def kontext_fuer(command: Command, noetig: Sequence[str] = ()) -> Kontext:
    """Mandant, Person und Rechte des Befehls prüfen (siehe Moduldokumentation)."""
    from apps.session.models import SessionTenant, SessionUser
    from apps.session.permissions import SessionPermissionChecker
    from apps.session.services.delegation_service import annotate_active

    art, _, kennung = command.tenant_ref.partition(":")
    tenant = SessionTenant.objects.filter(pk=kennung, is_active=True).first() if art == BEREICH else None
    if tenant is None:
        raise CommandError.of(404, "mandant-unbekannt", MANDANT_UNBEKANNT)
    user_id = _user_id(command.actor_ref)
    session_user = (
        annotate_active(SessionUser.objects.select_related("tenant", "user").prefetch_related("roles"))
        .filter(user_id=user_id, user__is_active=True, tenant=tenant, is_active=True)
        .first()
        if user_id is not None
        else None
    )
    if session_user is None:
        raise CommandError.of(403, "keine-berechtigung", KEIN_ZUGANG)
    rechte = frozenset(SessionPermissionChecker(session_user).permissions)
    if not set(noetig) <= rechte:
        raise CommandError.of(403, "keine-berechtigung", KEIN_RECHT)
    if current_channel().kind == SCHNITTSTELLE and API_ZUGANG not in rechte:
        raise CommandError.of(403, "keine-berechtigung", KEIN_API_ZUGANG)
    return Kontext(command=command, tenant=tenant, session_user=session_user, rechte=rechte)


def _user_id(actor_ref: str | None) -> uuid.UUID | None:
    if not actor_ref or not actor_ref.startswith(USER_PREFIX):
        return None
    try:
        return uuid.UUID(actor_ref.removeprefix(USER_PREFIX))
    except ValueError:
        return None


def ausfuehren(
    request: HttpRequest,
    name: str,
    inhalt: Mapping[str, Any],
    *,
    version: int = 1,
    anlagen: Mapping[str, Any] | None = None,
) -> Ergebnis:
    """
    Befehl aus einer Session-View für den Mandanten der Anfrage (``SessionMixin``) und die angemeldete Person.

    ``anlagen``: Dateien je SHA-256 (``hub.commands.types.Command``); sie gehen nur im selben Prozess mit.
    """
    tenant = getattr(request, "session_tenant", None)
    if tenant is None:
        raise ImproperlyConfigured("ausfuehren() braucht request.session_tenant (Session-View mit SessionMixin)")
    return _ausfuehren(request, name, inhalt, mandant=f"{BEREICH}:{tenant.pk}", version=version, anlagen=anlagen)


def _mandant(kurzname: str) -> uuid.UUID | None:
    from apps.session.models import SessionTenant

    kennung = SessionTenant.objects.filter(slug=kurzname, is_active=True).values_list("pk", flat=True).first()
    return kennung if isinstance(kennung, uuid.UUID) else None


MANDANTEN: Final = TenantArea(
    key=BEREICH,
    kind=BEREICH,
    resolve=_mandant,
    description="Kurzname des Session-Mandanten, wie in /session/<kurzname>/",
)


def register() -> None:
    """Mandantenbereich anmelden und die Befehle registrieren (aus ``SessionConfig.ready``)."""
    register_tenant_area(MANDANTEN)
    from . import textbausteine  # noqa: F401 – der Import registriert die Befehle
