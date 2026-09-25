# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Manipulationsschutz für Protokolle: Hash-Kette je Bereich (Issue #221).

Jeder Protokolleintrag bekommt beim Schreiben

- ``seq``: seine laufende Nummer innerhalb des Bereichs (Mandant, Fraktion oder das
  plattformweite Sicherheitsprotokoll),
- ``prev_hash``: den Hash des Vorgängers (der erste Eintrag verweist auf :data:`GENESIS_HASH`),
- ``entry_hash``: SHA-256 über die kanonische JSON-Darstellung seiner Felder samt ``seq`` und
  ``prev_hash``.

Wer einen Eintrag nachträglich ändert, bricht dessen Hash; wer einen Eintrag löscht, reißt eine
Lücke in die Nummern und in die Verkettung; wer am Ende löscht, passt nicht mehr zum Kettenkopf.
:func:`verify` findet all das.

Nebenläufigkeit: Neue Einträge sperren den Kettenkopf des Bereichs (``SELECT … FOR UPDATE`` auf
:class:`~apps.common.models.AuditChainHead`) bis zum Ende ihrer Transaktion. Zusätzlich verhindert
ein eindeutiger Index auf (Bereich, ``seq``) eine Verzweigung, selbst wenn die Sperre einmal fehlen
sollte. Kosten je Eintrag: Kopf lesen, Eintrag schreiben, Kopf fortschreiben – drei Abfragen.

Altbestand: Einträge aus der Zeit vor der Kette haben ``seq = NULL``. Solange ein Bereich solche
Einträge hat, bleibt seine Kette „nicht initialisiert“ und neue Einträge kommen ebenfalls ohne
Nummer hinzu. :func:`backfill` (Befehl ``audit_chain_backfill``) verkettet den Bestand in
Zeitreihenfolge, stapelweise und ID-basiert, und schaltet die Kette danach scharf.

Grenze: Die Kette schützt gegen Änderungen an einzelnen Einträgen. Wer Schreibzugriff auf die
Datenbank hat, könnte die gesamte Kette samt Kopf neu berechnen. Dagegen hilft der Abgleich mit
außerhalb aufbewahrten Kettenankern (Exporte, Archivpakete, Ausgabe von ``verify_audit_chain``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils import timezone

#: Vorgänger-Hash des ersten Eintrags einer Kette
GENESIS_HASH = "0" * 64
#: Version der kanonischen Darstellung (fließt in jeden Hash ein)
HASH_VERSION = 1
#: Einträge je Stapel beim Prüfen und Verketten
DEFAULT_BATCH_SIZE = 2000
#: Mehr Befunde werden nicht einzeln aufgeführt
MAX_REPORTED_ERRORS = 50


class ChainError(Exception):
    """Die Kette ist nicht in dem Zustand, den eine Operation voraussetzt."""


@dataclass(frozen=True)
class ChainSpec:
    """Beschreibung einer Protokoll-Kette (Modell, Bereichsfeld, gehashte Felder)."""

    key: str
    model_label: str
    scope_field: str | None
    fields: tuple[str, ...]
    #: Unveränderliche Kopien von Fremdschlüsseln (ref_feld, quell_feld), im Altbestand nachzutragen
    legacy_refs: tuple[tuple[str, str], ...] = ()
    label: str = ""

    @property
    def model(self) -> type[models.Model]:
        return apps.get_model(self.model_label)

    def scope_key(self, scope_id: Any) -> str:
        return self.key if self.scope_field is None else f"{self.key}:{scope_id}"

    def scope_filter(self, scope_id: Any) -> dict[str, Any]:
        return {} if self.scope_field is None else {self.scope_field: scope_id}

    def scope_of(self, entry: models.Model) -> Any:
        return None if self.scope_field is None else getattr(entry, self.scope_field)

    def entries(self, scope_id: Any) -> models.QuerySet[Any]:
        return self.model._default_manager.filter(**self.scope_filter(scope_id))


SESSION = ChainSpec(
    key="session",
    model_label="session.SessionAuditLog",
    scope_field="tenant_id",
    fields=(
        "id",
        "tenant_id",
        "created_at",
        "user_ref",
        "on_behalf_of_ref",
        "ip_address",
        "user_agent",
        "action",
        "model_name",
        "object_id",
        "object_repr",
        "changes",
    ),
    legacy_refs=(("user_ref", "user_id"), ("on_behalf_of_ref", "on_behalf_of_id")),
    label="Session-Protokoll",
)

FACTION = ChainSpec(
    key="faction",
    model_label="work.FactionAuditLog",
    scope_field="organization_id",
    fields=(
        "id",
        "organization_id",
        "created_at",
        "membership_ref",
        "actor_label",
        "ip_address",
        "user_agent",
        "action",
        "model_name",
        "object_id",
        "object_repr",
        "meeting_id_ref",
        "is_internal",
        "changes",
    ),
    legacy_refs=(("membership_ref", "membership_id"),),
    label="Fraktions-Änderungshistorie",
)

SECURITY = ChainSpec(
    key="security",
    model_label="accounts.SecurityAuditLog",
    scope_field=None,
    fields=("id", "created_at", "event", "user_ref", "identifier_hash", "ip_address", "user_agent", "details"),
    label="Sicherheitsprotokoll",
)

SPECS: dict[str, ChainSpec] = {spec.key: spec for spec in (SESSION, FACTION, SECURITY)}


# =============================================================================
# Kanonische Darstellung
# =============================================================================


def normalize_ip(value: Any) -> str | None:
    """IP-Adresse in eine eindeutige Schreibweise bringen; Ungültiges wird ``None``.

    PostgreSQL speichert ``inet`` normalisiert. Damit der Hash beim Schreiben und nach dem Lesen
    übereinstimmt, wird die Adresse auf beiden Wegen gleich aufbereitet (IPv4-mapped IPv6 als IPv4).
    """
    if value in (None, ""):
        return None
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        if address.scope_id:
            return None
    return str(address)


def _canonical(value: Any) -> Any:
    """Wert JSON-kanonisch aufbereiten – identisch vor dem Speichern und nach dem Lesen."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # jsonb speichert 1e20 als 100000000000000000000 und liefert eine Ganzzahl zurück
        return int(value) if value.is_integer() else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        aware = value if timezone.is_aware(value) else timezone.make_aware(value, dt.UTC)
        return aware.astimezone(dt.UTC).isoformat(timespec="microseconds")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return str(value)


def normalize_json(value: Any) -> Any:
    """JSON-Feld so aufbereiten, wie es nach der Rundreise durch die Datenbank zurückkommt."""
    return _canonical(json.loads(json.dumps(value if value is not None else {}, cls=DjangoJSONEncoder)))


def _field_value(entry: models.Model, name: str) -> Any:
    model_field = entry._meta.get_field(name)
    value = getattr(entry, getattr(model_field, "attname", name))
    if isinstance(model_field, models.GenericIPAddressField):
        return normalize_ip(value)
    return _canonical(value)


def payload(spec: ChainSpec, entry: models.Model) -> dict[str, Any]:
    """Die gehashten Daten eines Eintrags (auch Grundlage für Export und Archiv)."""
    data: dict[str, Any] = {name: _field_value(entry, name) for name in spec.fields}
    data["v"] = HASH_VERSION
    data["chain"] = spec.key
    data["seq"] = getattr(entry, "seq", None)
    data["prev_hash"] = getattr(entry, "prev_hash", None)
    return data


def canonical_json(data: Any) -> str:
    """Kanonisches JSON: sortierte Schlüssel, keine Leerzeichen, UTF-8 ohne Escapes."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_hash(spec: ChainSpec, entry: models.Model) -> str:
    """SHA-256 über die kanonische Darstellung (inklusive ``seq`` und ``prev_hash``)."""
    return hashlib.sha256(canonical_json(payload(spec, entry)).encode("utf-8")).hexdigest()


def hash_payload(data: dict[str, Any]) -> str:
    """Hash aus einer bereits kanonischen Darstellung (z. B. aus einem Export) nachrechnen."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


# =============================================================================
# Kettenkopf und Schreiben
# =============================================================================


def _head_model() -> type[Any]:
    return apps.get_model("common", "AuditChainHead")


def get_head(spec: ChainSpec, scope_id: Any) -> Any:
    """Kettenkopf ohne Sperre lesen (oder ``None``, wenn der Bereich noch keine Kette hat)."""
    return _head_model()._default_manager.filter(scope=spec.scope_key(scope_id)).first()


def lock_head(spec: ChainSpec, scope_id: Any) -> Any:
    """Kettenkopf sperren (in einer Transaktion aufrufen); legt ihn beim ersten Eintrag an.

    Ein neuer Kopf gilt als initialisiert, wenn der Bereich noch keine unverketteten Einträge
    hat. Legen zwei Anfragen gleichzeitig den ersten Kopf an, gewinnt eine; die andere wartet an
    der Sperre und schreibt danach hinter sie.
    """
    head_model = _head_model()
    key = spec.scope_key(scope_id)
    head = head_model._default_manager.select_for_update().filter(scope=key).first()
    if head is None:
        pending = spec.entries(scope_id).filter(seq__isnull=True).exists()
        now = timezone.now()
        head_model._default_manager.bulk_create(
            [head_model(scope=key, initialized=not pending, started_at=None if pending else now)],
            ignore_conflicts=True,
        )
        head = head_model._default_manager.select_for_update().get(scope=key)
    return head


def append(spec: ChainSpec, entry: models.Model) -> None:
    """Eintrag an die Kette seines Bereichs hängen und speichern.

    Setzt ``created_at`` unter der Sperre, damit Zeit und laufende Nummer dieselbe Reihenfolge
    haben. Läuft in einer (Sub-)Transaktion: scheitert das Schreiben, bleiben Kopf und Kette
    unverändert und eine umgebende Transaktion bleibt benutzbar.
    """
    record: Any = entry  # Protokollmodelle haben seq/prev_hash/entry_hash/created_at
    scope_id = spec.scope_of(entry)
    for ref_name, source_name in spec.legacy_refs:
        if getattr(record, ref_name, None) is None:
            setattr(record, ref_name, getattr(record, source_name, None))
    for name in spec.fields:
        model_field = entry._meta.get_field(name)
        if isinstance(model_field, models.JSONField):
            setattr(record, name, normalize_json(getattr(record, name)))
        elif isinstance(model_field, models.GenericIPAddressField):
            setattr(record, name, normalize_ip(getattr(record, name)))
        elif isinstance(model_field, models.CharField | models.TextField):
            text = getattr(record, name)
            if isinstance(text, str) and "\x00" in text:
                setattr(record, name, text.replace("\x00", ""))

    with transaction.atomic():
        head = lock_head(spec, scope_id)
        record.created_at = timezone.now()
        if head.initialized:
            record.seq = head.last_seq + 1
            record.prev_hash = head.last_hash or GENESIS_HASH
            record.entry_hash = compute_hash(spec, entry)
        record._chain_insert = True  # Weiche für Model.save() der Protokollmodelle
        try:
            entry.save(force_insert=True)
        finally:
            record._chain_insert = False
        if head.initialized:
            _head_model()._default_manager.filter(pk=head.pk).update(
                last_seq=record.seq, last_hash=record.entry_hash, updated_at=timezone.now()
            )


def is_chain_insert(entry: models.Model) -> bool:
    """True, wenn ``save()`` gerade aus :func:`append` kommt (Weiche in den Modellen)."""
    return bool(getattr(entry, "_chain_insert", False))


def drop_head(spec: ChainSpec, scope_id: Any) -> None:
    """Kettenkopf entfernen (nach dem Löschen des Mandanten bzw. der Organisation)."""
    _head_model()._default_manager.filter(scope=spec.scope_key(scope_id)).delete()


# =============================================================================
# Altbestand verketten
# =============================================================================


def backfill(spec: ChainSpec, scope_id: Any, *, batch_size: int = 1000) -> int:
    """Unverkettete Einträge in Zeitreihenfolge an die Kette hängen und sie scharf schalten.

    Arbeitet stapelweise (je Stapel eine kurze Transaktion mit Sperre des Kettenkopfs), lädt nie
    den ganzen Bestand in den Speicher und ist wiederholbar. Ist die Kette bereits initialisiert,
    passiert nichts – unverkettete Einträge wären dann eine Unregelmäßigkeit, die :func:`verify`
    meldet, und dürfen nicht nachträglich legitimiert werden.

    Returns: Anzahl neu verketteter Einträge.
    """
    total = 0
    ref_fields = [ref for ref, _source in spec.legacy_refs]
    while True:
        with transaction.atomic():
            head = lock_head(spec, scope_id)
            if head.initialized:
                return total
            batch = list(spec.entries(scope_id).filter(seq__isnull=True).order_by("created_at", "id")[:batch_size])
            seq = head.last_seq
            prev = head.last_hash or GENESIS_HASH
            for entry in batch:
                for ref_name, source_name in spec.legacy_refs:
                    if getattr(entry, ref_name) is None:
                        setattr(entry, ref_name, getattr(entry, source_name))
                seq += 1
                entry.seq = seq
                entry.prev_hash = prev
                entry.entry_hash = compute_hash(spec, entry)
                prev = entry.entry_hash
            if batch:
                spec.model._default_manager.bulk_update(
                    batch, ["seq", "prev_hash", "entry_hash", *ref_fields], batch_size=500
                )
            done = len(batch) < batch_size
            updates: dict[str, Any] = {"last_seq": seq, "last_hash": prev if seq else "", "updated_at": timezone.now()}
            if done:
                updates.update(initialized=True, started_at=timezone.now())
            _head_model()._default_manager.filter(pk=head.pk).update(**updates)
        total += len(batch)
        if done:
            return total


# =============================================================================
# Prüfen
# =============================================================================


@dataclass
class VerifyResult:
    """Ergebnis einer Kettenprüfung."""

    spec_key: str
    scope: str
    checked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    initialized: bool = False
    head_seq: int = 0
    head_hash: str = ""
    anchor_seq: int = 0
    anchor_hash: str = ""
    error_count: int = 0

    @property
    def ok(self) -> bool:
        return self.error_count == 0

    def error(self, message: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bereich": self.scope,
            "geprueft": self.checked,
            "intakt": self.ok,
            "befunde": self.error_count,
            "hinweise": list(self.warnings),
            "kopf_nr": self.head_seq,
            "kopf_hash": self.head_hash,
            "anker_nr": self.anchor_seq,
        }


def iter_chain(spec: ChainSpec, scope_id: Any, *, after_seq: int = 0, upto_seq: int | None = None) -> Iterator[Any]:
    """Verkettete Einträge nach laufender Nummer, stapelweise per Schlüssel-Paginierung."""
    cursor = after_seq
    base = spec.entries(scope_id).filter(seq__isnull=False)
    if upto_seq is not None:
        base = base.filter(seq__lte=upto_seq)
    while True:
        batch = list(base.filter(seq__gt=cursor).order_by("seq")[:DEFAULT_BATCH_SIZE])
        if not batch:
            return
        yield from batch
        cursor = batch[-1].seq


def verify(spec: ChainSpec, scope_id: Any, *, on_entry: Callable[[Any], None] | None = None) -> VerifyResult:
    """Kette eines Bereichs vollständig prüfen (Hashes, Nummernfolge, Verkettung, Kopf)."""
    key = spec.scope_key(scope_id)
    result = VerifyResult(spec_key=spec.key, scope=key)
    head = get_head(spec, scope_id)
    entries = spec.entries(scope_id)
    if head is None:
        pending = entries.filter(seq__isnull=True).count()
        if pending:
            result.warnings.append(
                f"Die Kette ist noch nicht angelegt; {pending} Einträge sind unverkettet (audit_chain_backfill)."
            )
        return result

    result.initialized = head.initialized
    result.head_seq = head.last_seq
    result.head_hash = head.last_hash
    result.anchor_seq = head.anchor_seq
    result.anchor_hash = head.anchor_hash

    expected_seq = head.anchor_seq + 1
    expected_prev = head.anchor_hash if head.anchor_seq else GENESIS_HASH
    last_seen = head.anchor_seq
    for entry in iter_chain(spec, scope_id, after_seq=head.anchor_seq):
        if entry.seq != expected_seq:
            if entry.seq > expected_seq:
                result.error(f"Lücke: Einträge Nr. {expected_seq} bis {entry.seq - 1} fehlen.")
            else:
                result.error(f"Nummer {entry.seq} ist doppelt vergeben.")
        if entry.prev_hash != expected_prev:
            result.error(f"Eintrag Nr. {entry.seq}: Verkettung zum Vorgänger ist unterbrochen.")
        if compute_hash(spec, entry) != entry.entry_hash:
            result.error(f"Eintrag Nr. {entry.seq} wurde nachträglich verändert.")
        if on_entry is not None:
            on_entry(entry)
        expected_prev = entry.entry_hash
        expected_seq = entry.seq + 1
        last_seen = entry.seq
        result.checked += 1

    if last_seen != head.last_seq:
        if last_seen < head.last_seq:
            result.error(
                f"Die Kette endet bei Nr. {last_seen}, der Kettenkopf erwartet Nr. {head.last_seq} "
                "(Einträge am Ende gelöscht?)."
            )
        else:
            result.error(f"Einträge nach dem Kettenkopf (Nr. {head.last_seq}) gefunden.")
    elif head.last_seq > head.anchor_seq and expected_prev != head.last_hash:
        result.error("Der Hash des letzten Eintrags passt nicht zum Kettenkopf.")

    stale = entries.filter(seq__isnull=False, seq__lte=head.anchor_seq).count()
    if stale:
        result.warnings.append(f"{stale} bereits archivierte Einträge sind noch vorhanden.")

    pending = entries.filter(seq__isnull=True).count()
    if pending:
        if head.initialized:
            result.error(f"{pending} Einträge stehen außerhalb der Kette (nachträglich eingefügt?).")
        else:
            result.warnings.append(
                f"Altbestand: {pending} Einträge sind noch nicht verkettet – Befehl audit_chain_backfill ausführen."
            )
    elif not head.initialized:
        result.warnings.append("Die Kette ist noch nicht scharf geschaltet – Befehl audit_chain_backfill ausführen.")
    return result


def scope_ids(spec: ChainSpec) -> list[Any]:
    """Alle Bereiche einer Kette, die Einträge oder einen Kettenkopf haben."""
    if spec.scope_field is None:
        return [None]
    ids = set(spec.model._default_manager.values_list(spec.scope_field, flat=True).distinct())
    prefix = f"{spec.key}:"
    for scope in _head_model()._default_manager.filter(scope__startswith=prefix).values_list("scope", flat=True):
        raw = scope[len(prefix) :]
        try:
            ids.add(uuid.UUID(raw))
        except ValueError:
            ids.add(raw)
    return sorted(ids, key=str)
