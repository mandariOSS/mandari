# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Empfängerkreis des Sitzungsdienstes für Hinweis-Mails.

Aktive Session-Benutzer mit einer Berechtigung (z. B. ``edit_meetings``). Betrifft ein Hinweis ein
nichtöffentliches Objekt, erhalten ihn nur Personen, die das Objekt selbst sehen dürfen
(``emails(visible=…)`` bzw. :meth:`StaffRecipients.for_meeting`). Genutzt von den Fristen-
Erinnerungen (``reminder_service``) und vom Hinweis „Vertretung gesucht“
(``invitation_response_service``) – eigenes Modul, damit beide dieselbe Regel verwenden, ohne sich
gegenseitig zu importieren.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.session.models import SessionTenant, SessionUser
from apps.session.permissions import role_permissions
from apps.session.visibility import meeting_visible


class StaffRecipients:
    """Empfängerkreis einer Berechtigung: Adressen mit den Rechten der jeweiligen Person."""

    def __init__(self, tenant: SessionTenant, permission: str) -> None:
        self.people: list[tuple[str, set[str]]] = []
        users = (
            SessionUser.objects.filter(tenant=tenant, is_active=True, user__is_active=True)
            .select_related("user")
            .prefetch_related("roles")
        )
        for su in users:
            permissions = role_permissions(su)
            if permission in permissions and su.user.email:
                self.people.append((su.user.email, permissions))

    def __bool__(self) -> bool:
        return bool(self.people)

    def emails(self, visible: Callable[[set[str]], bool] | None = None) -> list[str]:
        """Adressen – bei Nichtöffentlichem nur derer, die das Objekt selbst sehen dürfen."""
        return sorted({email for email, perms in self.people if visible is None or visible(perms)})

    def for_meeting(self, meeting: Any) -> list[str]:
        """Adressen für einen Hinweis zu einer Sitzung (nichtöffentlich: nur mit Sichtrecht)."""
        return self.emails(lambda perms: meeting_visible(perms, meeting))
