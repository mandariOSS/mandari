# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweisband oben auf Start (Issue #857): immer höchstens ein Hinweis zur Zeit.

Heute gibt es eine Quelle, die neueste ungelesene Ankündigung. Weitere Hinweise reihen sich in
``hinweis_fuer_start`` nach Vorrang ein; das Band (``templates/work/partials/hinweisband.html``) bleibt dasselbe.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from apps.tenants.models import Membership
from apps.work.notifications import ankuendigung


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


def hinweis_fuer_start(membership: Membership) -> Hinweis | None:
    """Der eine Hinweis für Start oder ``None``."""
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
