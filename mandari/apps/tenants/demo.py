# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Organisationen mit Laufzeit (Issue #1406).

Eine Organisation mit ``demo_laufzeit_tage`` ist eine Demo. Ihre Laufzeit beginnt, wenn die erste Einladung
angenommen wird (:func:`laufzeit_starten`); daraus entsteht ``demo_endet_am``. Danach sperrt der stündliche
Zeitplan ``demo_ablauf`` die Organisation (:func:`abgelaufene_sperren`, nur ``is_active``; alle Daten bleiben).
Konten, die nur noch über eine abgelaufene Demo Zugang hätten, können sich nicht mehr anmelden und sehen den
Hinweis „Demozeit abgelaufen“ (:func:`abgelaufene_demo_fuer`).

Organisationen der Bereitstellung (``provisioning_ref``) sind nie Demos: Übernimmt die Provisioning-API eine Demo
bei einem Abschluss, entfallen die Demo-Angaben, und ihr Status gilt allein.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Membership, Organization, UserInvitation

logger = logging.getLogger(__name__)

#: Hinweis bei der Anmeldung und auf der Hinweisseite
HINWEIS = "Die Demozeit ist abgelaufen."

#: Zugänge außerhalb von Work (z. B. Session), mit denen ein Konto trotz abgelaufener Demo angemeldet bleiben darf.
#: Die Plattform kennt keine Fachmodule; diese melden ihre Prüfung in ``AppConfig.ready`` an.
_weitere_zugaenge: list[Callable[[Any], bool]] = []


def weiteren_zugang_melden(pruefung: Callable[[Any], bool]) -> None:
    """Prüfung „Konto hat hier Zugang“ eines Fachmoduls anmelden (einmal je Prozess, doppelte zählen einmal)."""
    if pruefung not in _weitere_zugaenge:
        _weitere_zugaenge.append(pruefung)


def abgelaufen_q(jetzt: datetime | None = None, *, prefix: str = "") -> Q:
    """Bedingung „Demo abgelaufen“ für Organisationen (``prefix`` z. B. ``"organization__"``)."""
    jetzt = jetzt or timezone.now()
    return Q(**{f"{prefix}demo_endet_am__lte": jetzt, f"{prefix}provisioning_ref__isnull": True})


def ist_abgelaufen(organization: Organization, jetzt: datetime | None = None) -> bool:
    """True, wenn die Demo-Zeit der Organisation vorbei ist."""
    ende = organization.demo_endet_am
    return organization.provisioning_ref is None and ende is not None and ende <= (jetzt or timezone.now())


def laufzeit_starten(organization: Organization, jetzt: datetime | None = None) -> bool:
    """
    Laufzeit einer Demo beginnen (bei der ersten angenommenen Einladung). True, wenn ``demo_endet_am`` dabei
    gesetzt wurde; eine laufende oder beendete Demo und Organisationen ohne Laufzeit bleiben unverändert.
    """
    tage = organization.demo_laufzeit_tage
    if not tage or organization.demo_endet_am is not None or organization.provisioning_ref is not None:
        return False
    organization.demo_endet_am = (jetzt or timezone.now()) + timedelta(days=tage)
    Organization.objects.filter(pk=organization.pk).update(demo_endet_am=organization.demo_endet_am)
    logger.info("Demo %s läuft bis %s", organization.slug, organization.demo_endet_am.isoformat())
    return True


def abgelaufene_demo_fuer(user: Any, jetzt: datetime | None = None) -> Organization | None:
    """
    Die abgelaufene Demo, wenn das Konto außer ihr keinen Zugang mehr hat, sonst None.

    Zugang behalten: Plattform-Administration, aktive Mitgliedschaften in einer aktiven Organisation, die keine
    abgelaufene Demo ist, und Zugänge anderer Module (:func:`weiteren_zugang_melden`, z. B. Session).
    """
    if not getattr(user, "is_authenticated", False) or user.is_staff or user.is_superuser:
        return None
    jetzt = jetzt or timezone.now()
    mitgliedschaften = Membership.objects.filter(user=user, is_active=True)
    demo = (
        mitgliedschaften.filter(abgelaufen_q(jetzt, prefix="organization__"))
        .select_related("organization")
        .order_by("-organization__demo_endet_am")
        .first()
    )
    if demo is None:
        return None
    andere = mitgliedschaften.filter(organization__is_active=True).exclude(abgelaufen_q(jetzt, prefix="organization__"))
    if andere.exists() or any(pruefung(user) for pruefung in _weitere_zugaenge):
        return None
    # Eine offene Einladung in eine andere Organisation braucht die Anmeldung, um sie anzunehmen
    einladungen = UserInvitation.objects.filter(
        email__iexact=user.email, accepted_at__isnull=True, expires_at__gt=jetzt, organization__is_active=True
    ).exclude(abgelaufen_q(jetzt, prefix="organization__"))
    if einladungen.exists():
        return None
    return demo.organization


def kontakt() -> str:
    """Adresse für Rückfragen: der Absender der Plattform (leer, wenn keiner eingerichtet ist)."""
    from email.utils import parseaddr

    from apps.common.email import get_from_email

    try:
        return parseaddr(get_from_email())[1]
    except Exception:  # Hinweis ohne Adresse statt Fehler bei der Anmeldung
        logger.warning("Demo-Hinweis: Absender der Plattform nicht lesbar")
        return ""


def hinweis(organization: Organization) -> str:
    """Text für Anmeldung und Hinweisseite: Name, Enddatum, Daten bleiben, Kontakt."""
    text = HINWEIS
    if organization.demo_endet_am is not None:
        ende = timezone.localtime(organization.demo_endet_am).strftime("%d.%m.%Y")
        text = f"Die Demozeit für „{organization.name}“ ist am {ende} abgelaufen."
    text += " Eure Daten bleiben erhalten."
    adresse = kontakt()
    if adresse:
        text += f" Wenn ihr weitermachen möchtet, schreibt uns an {adresse}."
    return text


def abgelaufene_sperren(jetzt: datetime | None = None) -> list[Organization]:
    """Aktive Demos nach ihrem Ende sperren (nur ``is_active``); liefert die gesperrten Organisationen."""
    jetzt = jetzt or timezone.now()
    # Bedingung unter Zeilensperre prüfen: Eine gleichzeitige Verlängerung im Admin oder eine Übernahme durch die
    # Bereitstellung wartet und wird danach nicht mehr gesperrt
    with transaction.atomic():
        faellig = list(
            Organization.objects.select_for_update().filter(abgelaufen_q(jetzt), is_active=True).order_by("slug")
        )
        if faellig:
            Organization.objects.filter(pk__in=[org.pk for org in faellig]).update(is_active=False, updated_at=jetzt)
    for org in faellig:
        org.is_active = False
        logger.info("Demo %s abgelaufen (Ende %s), Organisation gesperrt", org.slug, org.demo_endet_am)
    return faellig
