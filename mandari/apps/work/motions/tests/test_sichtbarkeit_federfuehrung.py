# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Federführung und Mitarbeit sehen das Dokument, für das sie eingeteilt sind.

Gefunden über smoke_motions_tracking.py, das in keinem CI-Job lief (Issue #249):
Die Sichtbarkeit berücksichtigte nur Autorschaft, Organisationsfreigabe und
ausdrückliche Freigaben. Wer die Federführung zugewiesen bekam, erhielt zwar
eine Benachrichtigung darüber — konnte das Dokument danach aber nicht öffnen.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.tenants.models import Membership, Organization
from apps.work.motions.models import Motion

pytestmark = pytest.mark.django_db

# Die Factories sind untypisiert; cast(Any, ...) ist das im Repo uebliche Muster
# (siehe django-stubs-Hinweis in den Deploy-Notizen), damit neuer Testcode die
# mypy-Allowlist nicht wachsen laesst.


def _org() -> Organization:
    return cast(Organization, cast(Any, OrganizationFactory)())


def _mitglied(organization: Organization) -> Membership:
    return cast(Membership, cast(Any, MembershipFactory)(organization=organization))


def _sichtbar_fuer(membership: Membership) -> Any:
    return cast(Any, Motion).visible_to(membership)


def _motion(organization: Organization, autor: Membership, **kwargs: Any) -> Motion:
    return Motion.objects.create(
        organization=organization,
        author=autor,
        title="Radweg Hauptstraße",
        visibility="private",
        **kwargs,
    )


def test_federfuehrung_sieht_das_dokument() -> None:
    org = _org()
    autor = _mitglied(org)
    zustaendig = _mitglied(org)
    motion = _motion(org, autor, responsible=zustaendig)

    sichtbar = _sichtbar_fuer(zustaendig)
    assert motion in sichtbar, "Wer die Federführung hat, muss das Dokument öffnen können"


def test_mitarbeit_sieht_das_dokument() -> None:
    org = _org()
    autor = _mitglied(org)
    mitarbeit = _mitglied(org)
    motion = _motion(org, autor)
    motion.contributors.add(mitarbeit)

    assert motion in _sichtbar_fuer(mitarbeit)


def test_unbeteiligte_sehen_es_weiterhin_nicht() -> None:
    """Die Erweiterung darf nicht mehr öffnen als nötig."""
    org = _org()
    autor = _mitglied(org)
    fremd = _mitglied(org)
    motion = _motion(org, autor)

    assert motion not in _sichtbar_fuer(fremd)


def test_andere_organisation_bleibt_aussen_vor() -> None:
    org = _org()
    andere = _org()
    autor = _mitglied(org)
    fremd = _mitglied(andere)
    motion = _motion(org, autor, responsible=autor)

    assert motion not in _sichtbar_fuer(fremd), "Mandantengrenze gilt vor jeder Rolle"
