# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geo-Abdeckung der Kommunen: Welche Kommunen sind noch nicht an OSM angebunden? (Issue #54)

Für die Georeferenzierung braucht eine Kommune ``osm_relation_id`` (Straßen- und
Adressimport), Zentrum/Bounding-Box (Karte, Plausibilitätsprüfung) und idealerweise den
AGS. Dieser Service listet Lücken; die Datenpflege selbst bleibt beim Betreiber
(Admin → Kommune → „Geografische Daten“, dann ``fetch_osm_geodata`` und ``import_streets``).

Gebiete oberhalb der Gemeinde (Regierungsbezirk, Kreis; AGS kürzer als acht Stellen, siehe
``OParlBody.is_regional_level``) brauchen nur OSM-Relation und Bounding-Box.

Körperschaften ohne eigenes Gebiet (``is_non_territorial``: Zweckverband, GmbH, Waldgemarkung …)
sind keine Lücke. Verbandsgemeinden, Ämter und Samtgemeinden haben keinen Gemeindeschlüssel;
bei ihnen genügt der Regionalschlüssel (``rgs``). Die Zuordnung übernimmt seit Issue #351
``resolve_body_geodata``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import Count, Q, QuerySet

if TYPE_CHECKING:
    from insight_core.models import OParlBody


@dataclass(frozen=True)
class BodyGeoStatus:
    body: OParlBody
    has_osm_relation: bool
    has_ags: bool
    has_bbox: bool
    street_count: int
    address_count: int
    regional: bool = False
    non_territorial: bool = False

    @property
    def missing(self) -> list[str]:
        if self.non_territorial:
            return []  # kein eigenes Gebiet, keine Grenze – keine Lücke
        gaps: list[str] = []
        if not self.has_osm_relation:
            gaps.append("osm_relation_id")
        if not self.has_ags:
            gaps.append("AGS")
        if not self.has_bbox:
            gaps.append("Bounding-Box")
        if self.regional:
            return gaps
        if not self.street_count:
            gaps.append("Straßenverzeichnis")
        if not self.address_count:
            gaps.append("Adressen")
        return gaps

    @property
    def complete(self) -> bool:
        return not self.missing


def _counts_per_body(queryset: QuerySet[Any]) -> dict[Any, int]:
    return dict(queryset.order_by().values("body_id").annotate(n=Count("id")).values_list("body_id", "n"))


def geo_status_for_bodies(only_gaps: bool = True) -> list[BodyGeoStatus]:
    """Geo-Status aller (nicht gelöschten) Kommunen, optional nur die mit Lücken.

    Straßen und Adressen werden getrennt gezählt. Beide in einer Abfrage zu annotieren, verbindet
    sie je Kommune zum Kreuzprodukt – für Köln 26.000 Straßen mal 167.000 Adressen. Das füllte am
    24.09.2026 mit temporären Dateien die Platte des Datenbankservers.
    """
    from insight_core.models import Address, OParlBody, Street

    streets = _counts_per_body(Street.objects.all())
    addresses = _counts_per_body(Address.objects.all())
    statuses: list[BodyGeoStatus] = []
    for body in OParlBody.objects.filter(deleted=False).order_by("name"):
        status = BodyGeoStatus(
            body=body,
            has_osm_relation=bool(body.osm_relation_id),
            has_ags=bool((body.ags or "").strip() or (body.rgs or "").strip()),
            has_bbox=bool(body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west),
            street_count=streets.get(body.id, 0),
            address_count=addresses.get(body.id, 0),
            regional=body.is_regional_level,
            non_territorial=body.is_non_territorial,
        )
        if only_gaps and status.complete:
            continue
        statuses.append(status)
    return statuses


def bodies_without_osm_filter() -> Q:
    """Q-Objekt „ohne OSM-Zuordnung“ (keine Relation-ID oder weder AGS noch RGS) für Admin-Filter.

    Körperschaften ohne eigenes Gebiet zählen nicht dazu.
    """
    without_key = (Q(ags__isnull=True) | Q(ags="")) & (Q(rgs__isnull=True) | Q(rgs=""))
    return (Q(osm_relation_id__isnull=True) | without_key) & Q(is_non_territorial=False)
