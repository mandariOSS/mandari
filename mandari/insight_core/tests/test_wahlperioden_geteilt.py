# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geteilte Wahlperioden beim Löschen einer Kommune (Issue #1453).

Eine Wahlperiode gehört der Kommune, die sie zuerst gespeichert hat; weitere Kommunen derselben Quelle sind über
``shared_bodies`` verknüpft. Wird die erste gelöscht, geht die Wahlperiode an die als nächste verknüpfte über.
"""

from __future__ import annotations

import pytest

from insight_core.models import OParlBody, OParlLegislativeTerm, OParlSource
from insight_core.services.body_deletion import delete_body_data

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org/oparl"


def _kommune(source: OParlSource, nummer: int) -> OParlBody:
    return OParlBody.objects.create(external_id=f"{RIS}/body/{nummer}", source=source, name=f"Kommune {nummer}")


def test_geteilte_wahlperiode_geht_an_die_naechste_kommune_ueber() -> None:
    source = OParlSource.objects.create(name="Kreis", url=f"{RIS}/system")
    kreis, gemeinde, dorf = (_kommune(source, nummer) for nummer in (1, 2, 3))
    geteilt = OParlLegislativeTerm.objects.create(external_id=f"{RIS}/legislativeterm/1", body=kreis, name="geteilt")
    geteilt.shared_bodies.add(gemeinde)
    geteilt.shared_bodies.add(dorf)
    allein = OParlLegislativeTerm.objects.create(external_id=f"{RIS}/legislativeterm/2", body=kreis, name="allein")

    ergebnis = delete_body_data(str(kreis.pk))

    geteilt.refresh_from_db()
    assert geteilt.body_id == gemeinde.pk, "an die als nächste verknüpfte Kommune"
    assert list(geteilt.shared_bodies.all()) == [dorf]
    assert not OParlLegislativeTerm.objects.filter(pk=allein.pk).exists()
    assert ergebnis["detail"]["legislative_terms_handed_over"] == 1
    assert list(OParlLegislativeTerm.objects.filter(OParlLegislativeTerm.of_body(dorf.pk))) == [geteilt]


def test_geloeschte_mitnutzende_kommune_verliert_nur_die_verknuepfung() -> None:
    source = OParlSource.objects.create(name="Kreis", url=f"{RIS}/system")
    kreis, gemeinde = _kommune(source, 1), _kommune(source, 2)
    geteilt = OParlLegislativeTerm.objects.create(external_id=f"{RIS}/legislativeterm/1", body=kreis, name="geteilt")
    geteilt.shared_bodies.add(gemeinde)

    delete_body_data(str(gemeinde.pk))

    geteilt.refresh_from_db()
    assert geteilt.body_id == kreis.pk and not geteilt.shared_bodies.exists()
