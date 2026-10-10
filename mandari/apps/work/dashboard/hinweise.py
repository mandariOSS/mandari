# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweisband oben auf Start (Issue #857): immer höchstens ein Hinweis zur Zeit.

Quellen nach Vorrang (``hinweis_fuer_start``), das Band (``templates/work/partials/hinweisband.html``) bleibt dasselbe:

1. die neueste ungelesene Ankündigung,
2. die Empfehlung, das Konto mit einem zweiten Faktor zu schützen (nur ohne zweiten Faktor). „Später“ stellt sie
   für ``ZWEI_FAKTOR_SPAETER_TAGE`` Tage zurück; der Zeitpunkt steht in ``User.settings`` (gilt für die Person in
   allen Organisationen).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.urls import reverse
from django.utils import timezone

from apps.accounts.two_factor_policy import two_factor_recommended
from apps.tenants.models import Membership
from apps.work.notifications import ankuendigung

#: Schlüssel in ``User.settings``: bis wann die Empfehlung zum zweiten Faktor zurückgestellt ist (ISO-Zeitpunkt)
ZWEI_FAKTOR_SPAETER_SCHLUESSEL = "zwei_faktor_hinweis_spaeter_bis"
ZWEI_FAKTOR_SPAETER_TAGE = 30


@dataclass(frozen=True)
class Hinweis:
    """Inhalt des Hinweisbands."""

    titel: str
    text: str
    #: POST-Adresse, die den Hinweis als gelesen markiert (Wegklicken und Folgen des Links)
    gelesen_url: str
    link: str = ""
    linktext: str = ""
    #: Zweiter, schlichter Link „Rückmeldung geben“ (leer: keiner)
    rueckmeldung_url: str = ""
    icon: str = "megaphone"
    #: Folgen des Links markiert den Hinweis ebenfalls als gelesen (Ankündigung); sonst ein schlichter Link
    link_markiert_gelesen: bool = True
    #: Beschriftung der Schaltfläche zum Ausblenden (z. B. „Später“); leer: Symbol „Schließen“
    ausblenden_text: str = ""


def hinweis_fuer_start(membership: Membership) -> Hinweis | None:
    """Der eine Hinweis für Start oder ``None``."""
    return _ankuendigung(membership) or _zwei_faktor_empfehlung(membership)


def _ankuendigung(membership: Membership) -> Hinweis | None:
    benachrichtigung = ankuendigung.fuer_band(membership)
    if benachrichtigung is None:
        return None
    org_slug = membership.organization.slug
    metadaten = benachrichtigung.metadata or {}
    rueckmeldung_url = ""
    if metadaten.get("rueckmeldung") and not membership.is_guest:
        rueckmeldung_url = reverse("work:support", kwargs={"org_slug": org_slug})
    return Hinweis(
        titel=benachrichtigung.title,
        text=benachrichtigung.message,
        gelesen_url=reverse(
            "work:notification_mark_read", kwargs={"org_slug": org_slug, "notification_id": benachrichtigung.id}
        ),
        link=benachrichtigung.link,
        linktext=metadaten.get("linktext") or ankuendigung.LINKTEXT_STANDARD,
        rueckmeldung_url=rueckmeldung_url,
    )


def _zwei_faktor_empfehlung(membership: Membership) -> Hinweis | None:
    """Empfehlung für Konten ohne zweiten Faktor, solange sie nicht mit „Später“ zurückgestellt ist."""
    user = membership.user
    # Zurückgestellt zuerst: kostet keine Abfrage
    if membership.is_guest or zwei_faktor_zurueckgestellt(user) or not two_factor_recommended(user):
        return None
    org_slug = membership.organization.slug
    return Hinweis(
        titel="Schützen Sie Ihr Konto mit einem zweiten Faktor",
        text=(
            "Wer nur Ihr Passwort kennt, kommt dann nicht mehr in Ihr Konto: Sie bestätigen die Anmeldung zusätzlich "
            "mit einem Code aus einer Authenticator-App. Die Einrichtung dauert etwa eine Minute."
        ),
        gelesen_url=reverse("work:two_factor_hint_later", kwargs={"org_slug": org_slug}),
        link=reverse("work:security", kwargs={"org_slug": org_slug}),
        linktext="Einrichten",
        icon="shield",
        link_markiert_gelesen=False,
        ausblenden_text="Später",
    )


def zwei_faktor_zurueckgestellt(user: Any, *, jetzt: datetime | None = None) -> bool:
    """True, solange die Empfehlung zum zweiten Faktor mit „Später“ zurückgestellt ist."""
    wert = (getattr(user, "settings", None) or {}).get(ZWEI_FAKTOR_SPAETER_SCHLUESSEL)
    if not isinstance(wert, str):
        return False
    try:
        bis = datetime.fromisoformat(wert)
    except ValueError:
        return False
    if timezone.is_naive(bis):
        bis = timezone.make_aware(bis)
    return (jetzt or timezone.now()) < bis


def zwei_faktor_spaeter(user: Any) -> None:
    """„Später“: Empfehlung zum zweiten Faktor für ``ZWEI_FAKTOR_SPAETER_TAGE`` Tage zurückstellen."""
    user.refresh_from_db(fields=["settings"])
    einstellungen = dict(user.settings or {})
    einstellungen[ZWEI_FAKTOR_SPAETER_SCHLUESSEL] = (
        timezone.now() + timedelta(days=ZWEI_FAKTOR_SPAETER_TAGE)
    ).isoformat()
    user.settings = einstellungen
    user.save(update_fields=["settings"])
