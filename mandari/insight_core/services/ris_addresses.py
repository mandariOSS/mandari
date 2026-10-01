# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adressen einer Session-Quelle im RIS-Bestand umziehen, ohne Kennungen zu ändern (Issue #733).

Nach einem Domainwechsel (``SITE_URL``) gibt die Session-OParl-Schnittstelle ihre Objekte unter neuen
Adressen aus; deren kanonische Kennungen bleiben auf der festgeschriebenen Basis der Installation
(``apps.common.identifiers``). Der Bestand führt die Objekte noch unter den alten Adressen. Der Umzug
schreibt sie um:

- die URL der Quelle und alle Adressen unter ihr: ``external_id``, Verweise (``*_external_id``), Links
  (``*_url``) und die Rohdaten (``raw_json``) der Objekte,
- die bisherige Basis der Kennungen hält er in ``sync_config["id_base"]`` fest, falls sie dort noch fehlt.

Die Kennungen (``id``) ändern sich nicht, Signale und Suchindex bleiben unberührt. Ingestor und Spiegel
bilden die Kennungen danach auf der festgeschriebenen Basis (``mandari_oparl.ids``); der nächste Abgleich
aktualisiert die Objekte, statt sie neu anzulegen. Eine nach dem Domainwechsel automatisch angelegte,
leere Quelle unter der neuen Adresse geht im Umzug auf.

Der Umzug läuft in einer Transaktion und bricht ab, wenn unter der neuen Adresse schon Objekte stehen
(dann hat ein Abgleich sie zwischenzeitlich neu angelegt).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.db.models import Model
from mandari_oparl.ids import SOURCE_ID_BASE_KEY, source_id_base

from insight_core.models import OParlSource
from insight_core.services.ris_ids import ENTITIES

#: Seitengröße beim Umschreiben
BATCH_SIZE = 500


class MoveError(Exception):
    """Der Umzug ist nicht möglich; die Meldung nennt den Grund (ohne Inhalte)."""


@dataclass
class Move:
    """Ergebnis eines Umzugs (bzw. der Vorschau)."""

    source: OParlSource
    old: str
    new: str
    id_base: str
    #: Entität -> Anzahl umgezogener Objekte
    objects: dict[str, int] = field(default_factory=dict)
    #: Kennung der leeren Quelle unter der neuen Adresse, die im Umzug aufgeht
    replaced_source_id: Any = None

    @property
    def total(self) -> int:
        return sum(self.objects.values())


def _prefix(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def address_fields(model: type[Model]) -> list[str]:
    """Felder mit Adressen: Schlüssel, Verweise, Links und Rohdaten."""
    return [
        f.attname
        for f in model._meta.concrete_fields
        if f.name in ("external_id", "raw_json") or f.name.endswith(("_external_id", "_url"))
    ]


def rewrite(value: Any, old: str, new: str) -> Any:
    """Adressen unter ``old`` auf ``new`` umschreiben, auch in Listen und verschachtelten Objekten."""
    if isinstance(value, str):
        return f"{new}{value[len(old) :]}" if value.startswith(old) else value
    if isinstance(value, list):
        return [rewrite(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: rewrite(item, old, new) for key, item in value.items()}
    return value


def _move_objects(model: type[Model], old: str, new: str) -> int:
    """Alle Objekte unter ``old`` seitenweise umschreiben; Kennungen bleiben."""
    fields = address_fields(model)
    manager = model._default_manager
    moved = 0
    last = None
    while True:
        page_qs = manager.filter(external_id__startswith=old).order_by("pk").only("pk", *fields)
        if last is not None:
            page_qs = page_qs.filter(pk__gt=last)
        page = list(page_qs[:BATCH_SIZE])
        if not page:
            return moved
        for obj in page:
            for name in fields:
                setattr(obj, name, rewrite(getattr(obj, name), old, new))
        manager.bulk_update(page, fields)
        moved += len(page)
        last = page[-1].pk


def move_source(source: OParlSource, new_url: str, *, apply: bool = False) -> Move:
    """
    Adressen der Quelle ``source`` von ihrer URL auf ``new_url`` umziehen (``apply``) bzw. nur zählen.

    Raises:
        MoveError: Unter der neuen Adresse steht eine Quelle mit Kommunen oder es stehen dort schon Objekte.
    """
    old, new = _prefix(source.url), _prefix(new_url)
    id_base = source_id_base(source.sync_config) or source.url
    move = Move(source=source, old=old, new=new, id_base=id_base)
    if old == new:
        return move

    other = OParlSource.objects.exclude(pk=source.pk).filter(url__in={new_url, new}).first()
    if other is not None:
        if other.bodies.exists():
            raise MoveError(f"Unter {new} ist schon eine Quelle mit Kommunen registriert ({other.pk}).")
        move.replaced_source_id = other.pk
    for entity in ENTITIES:
        if entity.model._default_manager.filter(external_id__startswith=new).exists():
            raise MoveError(f"Unter {new} stehen schon Objekte ({entity.name}); erst prüfen, dann umziehen.")
        move.objects[entity.name] = entity.model._default_manager.filter(external_id__startswith=old).count()
    if not apply:
        return move

    with transaction.atomic():
        if move.replaced_source_id is not None:
            OParlSource.objects.filter(pk=move.replaced_source_id).delete()
        for entity in ENTITIES:
            move.objects[entity.name] = _move_objects(entity.model, old, new)
        config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
        # Bisherige Basis der Kennungen; hatte die Quelle schon eine, bleibt sie
        config[SOURCE_ID_BASE_KEY] = id_base
        source.url = new_url
        source.sync_config = config
        source.save(update_fields=["url", "sync_config", "updated_at"])
    return move
