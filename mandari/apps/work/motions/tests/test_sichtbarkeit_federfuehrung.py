# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Federführung und Mitarbeit sehen das Dokument, für das sie eingeteilt sind.

Gefunden über smoke_motions_tracking.py, das in keinem CI-Job lief (Issue #249):
Die Sichtbarkeit berücksichtigte nur Autorschaft, Organisationsfreigabe und
ausdrückliche Freigaben. Wer die Federführung zugewiesen bekam, erhielt zwar
eine Benachrichtigung darüber — konnte das Dokument danach aber nicht öffnen.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.work.motions.models import Motion

pytestmark = pytest.mark.django_db


def _motion(organization, autor, **kwargs) -> Motion:
    return Motion.objects.create(
        organization=organization,
        author=autor,
        title="Radweg Hauptstraße",
        visibility="private",
        **kwargs,
    )


def test_federfuehrung_sieht_das_dokument() -> None:
    org = OrganizationFactory()
    autor = MembershipFactory(organization=org)
    zustaendig = MembershipFactory(organization=org)
    motion = _motion(org, autor, responsible=zustaendig)

    sichtbar = Motion.visible_to(zustaendig)
    assert motion in sichtbar, "Wer die Federführung hat, muss das Dokument öffnen können"


def test_mitarbeit_sieht_das_dokument() -> None:
    org = OrganizationFactory()
    autor = MembershipFactory(organization=org)
    mitarbeit = MembershipFactory(organization=org)
    motion = _motion(org, autor)
    motion.contributors.add(mitarbeit)

    assert motion in Motion.visible_to(mitarbeit)


def test_unbeteiligte_sehen_es_weiterhin_nicht() -> None:
    """Die Erweiterung darf nicht mehr öffnen als nötig."""
    org = OrganizationFactory()
    autor = MembershipFactory(organization=org)
    fremd = MembershipFactory(organization=org)
    motion = _motion(org, autor)

    assert motion not in Motion.visible_to(fremd)


def test_andere_organisation_bleibt_aussen_vor() -> None:
    org = OrganizationFactory()
    andere = OrganizationFactory()
    autor = MembershipFactory(organization=org)
    fremd = MembershipFactory(organization=andere)
    motion = _motion(org, autor, responsible=autor)

    assert motion not in Motion.visible_to(fremd), "Mandantengrenze gilt vor jeder Rolle"
