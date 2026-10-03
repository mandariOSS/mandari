# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechteauflösung eines Session-Kontos (Issue #772, ADR „Rechte mit Geltungsbereich“).

- :func:`bisherige_rechte` – die bisherigen Rechtenamen (``approve_papers`` …) für ``SessionPermissionChecker`` und
  ``visible_to()``. Grundlage sind nur die Spiegelzuweisungen aus ``SessionUser.roles`` (mandantenweit, unbefristet),
  ausgewertet über den Rechtekatalog: Ein Häkchen gilt, wenn sein Leitrecht mandantenweit gilt. Keine Abfrage, wenn
  die Rollen vorgeladen sind – die Abfragezahl der Seiten bleibt unverändert.
- :func:`zugriffskontext` – der vollständige Kontext für die zentrale Prüfung (#773): zusätzlich befristete
  Zuweisungen und Zuweisungen mit Geltungsbereich, aber nur bei eingeschaltetem Schalter des Mandanten.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from . import katalog, kern
from .bereiche import bereich, bereichsbaum


def spiegel_zuweisungen(session_user: Any) -> list[kern.Zuweisung]:
    """Die Rollen aus ``SessionUser.roles`` als mandantenweite, unbefristete Zuweisungen (nutzt vorgeladene Rollen)."""
    if not session_user:
        return []
    return [kern.Zuweisung(rechte=katalog.rechte_der_rolle(rolle)) for rolle in session_user.roles.all()]


def bisherige_rechte(session_user: Any) -> set[str]:
    """Bisherige Rechtenamen aus den eigenen Rollen – ohne Vertretungen, ohne befristete und begrenzte Zuweisungen."""
    rechte = kern.mandantenweite_rechte(spiegel_zuweisungen(session_user), timezone.localdate())
    return katalog.haekchen_aus_rechten(rechte)


def zusaetzliche_zuweisungen(session_user: Any) -> list[kern.Zuweisung]:
    """
    Aktive Zuweisungen neben dem Spiegel (befristet oder mit Geltungsbereich), nur bei eingeschaltetem Schalter.

    Rollen anderer Mandanten und Administrator-Rollen wirken hier nie: Die Administrator-Vollmacht gibt es vorerst
    nur mandantenweit und unbefristet über ``SessionUser.roles``.
    """
    from apps.session.models import SessionRoleAssignment

    if not session_user or not session_user.tenant.scoped_permissions_enabled:
        return []
    zeilen = (
        SessionRoleAssignment.objects.filter(user=session_user, revoked_at__isnull=True)
        .exclude(scope_type=SessionRoleAssignment.SCOPE_TENANT, valid_from__isnull=True, valid_until__isnull=True)
        .select_related("role")
    )
    ergebnis = []
    for zeile in zeilen:
        rolle = zeile.role
        if rolle.tenant_id != session_user.tenant_id or rolle.is_admin:
            continue
        begrenzt = zeile.scope_type != SessionRoleAssignment.SCOPE_TENANT and zeile.scope_id is not None
        ergebnis.append(
            kern.Zuweisung(
                rechte=katalog.rechte_der_rolle(rolle),
                bereich=bereich(zeile.scope_type, zeile.scope_id) if begrenzt else None,
                gueltig_von=zeile.valid_from,
                gueltig_bis=zeile.valid_until,
            )
        )
    return ergebnis


def zugriffskontext(
    session_user: Any, *, tag: date | None = None, baum: kern.Baum | None = None
) -> kern.Zugriffskontext:
    """
    Zugriffskontext eines Kontos: Spiegel plus – bei eingeschaltetem Schalter – die zusätzlichen Zuweisungen.

    Ohne Schalter genau die bisherigen Rollen, mandantenweit. Einmal je Anfrage berechnen; der Bereichsbaum kostet
    zwei Abfragen und wird nur geladen, wenn eine Zuweisung mit Geltungsbereich wirkt.
    """
    if not session_user:
        return kern.Zugriffskontext()
    tag = tag or timezone.localdate()
    zuweisungen = spiegel_zuweisungen(session_user) + zusaetzliche_zuweisungen(session_user)
    if baum is None:
        if any(z.bereich is not None and z.wirkt_am(tag) for z in zuweisungen):
            baum = bereichsbaum(session_user.tenant)
        else:
            baum = kern.Baum({})
    return kern.zugriffskontext(zuweisungen, tag, baum)
