# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wann darf ein bestehendes Konto allein anhand seiner E-Mail-Adresse übernommen werden?

Einladungen in Session-Mandanten, das Anlegen eines Mandanten mit erstem Administrator und
Gast-Einladungen hängen ein vorhandenes Konto an, wenn es zur eingegebenen Adresse passt.
Das ist nur vertretbar, wenn das Konto nachweislich zur Inhaberin des Postfachs gehört:

- die Adresse ist bestätigt (Bestätigungs-, Einladungs- oder Passwort-Link eingelöst), oder
- das Konto hat bereits Zugang zu einer Organisation oder einem Session-Mandanten – dorthin
  gelangt ein Konto nur über eine bestätigte Registrierung, eine Einladung oder die Verwaltung.

Ein Konto aus einer nie bestätigten Selbstregistrierung erfüllt beides nicht. Solche Konten
erhalten stattdessen eine Einladung bzw. einen Link an das Postfach.
"""

from __future__ import annotations

from typing import Any


def can_adopt_by_email(user: Any) -> bool:
    """True, wenn ``user`` per Adresse in Mitgliedschaften oder Einladungen übernommen werden darf."""
    if getattr(user, "email_verified", False):
        return True
    from apps.session.models import SessionUser
    from apps.tenants.models import Membership

    return Membership.objects.filter(user=user).exists() or SessionUser.objects.filter(user=user).exists()
