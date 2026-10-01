# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Basisadresse der dauerhaften Kennungen dieser Installation (Issue #733).

Kanonische Kennungen eigener Objekte bilden sich aus der **festgeschriebenen** Basisadresse
(:class:`apps.common.models.IdentifierBase`), ausgegebene Adressen aus der aktuellen ``SITE_URL``. Solange
beide gleich sind, ist die kanonische URI eines Objekts seine Adresse. Nach einem Domainwechsel tritt die
Basis an die Stelle von ``SITE_URL`` (``mandari_oparl.ids.canonical_uri``), und die Kennungen bleiben.
"""

from __future__ import annotations

from django.conf import settings

from apps.common.models import IdentifierBase


def site_url() -> str:
    """Aktuelle öffentliche Adresse der Installation (``SITE_URL``) ohne abschließenden Schrägstrich."""
    return str(getattr(settings, "SITE_URL", "") or "http://localhost:8000").rstrip("/")


def stored_identifier_base() -> str | None:
    """Festgeschriebene Basisadresse, ohne sie anzulegen (für lesende Prüfungen); ``None``, solange keine besteht."""
    return IdentifierBase.objects.filter(pk=1).values_list("url", flat=True).first()


def identifier_base() -> str:
    """
    Festgeschriebene Basisadresse der Kennungen, ohne abschließenden Schrägstrich.

    Ist noch keine festgelegt (neue Installation), gilt ab jetzt die aktuelle ``SITE_URL`` – einmalig und
    dauerhaft.
    """
    entry, _created = IdentifierBase.objects.get_or_create(pk=1, defaults={"url": site_url()})
    return entry.url
